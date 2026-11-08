"""
02_segment_paragraphs.py · Layer 1 段落定位

把 case_events.full_text 切出 3 个关键段落，缩小后续抽取的包围圈 15-20×：
- defendant_segment: 被告基本信息段（"被告人XX，男/女，YYYY年..."）
- judgment_segment: 判决主文段（"判决如下..." 至 "审判长" 前）
- judges_segment: 审判员段（"审判长" 至文末）

输入: 03_masterdata/case_events/ (Parquet dataset)
输出: 03_masterdata/case_events_segmented/ (新增 3 列)

性能目标: 535 万行 < 30 分钟（CPU bound regex）
"""
from __future__ import annotations

import argparse
import re
import time
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq


DEFENDANT_START = r"(?:被告人|罪犯)[^，。；,;]{1,15}[，,]"
DEFENDANT_STOP = r"(?=被告人|公诉机关|起诉书|辩护人|经审理|本院|本案|根据|检察院\w*指控|附带民事)"
DEFENDANT_RE = re.compile(rf"({DEFENDANT_START}.*?){DEFENDANT_STOP}", re.DOTALL)

JUDGMENT_START = r"(?:判决如下|裁定如下)[：:。]?\s*"
JUDGMENT_STOP = r"(?=审判长|审判员|代理审判员|执行员|如不服本判决|上诉于|本判决为终审)"
JUDGMENT_RE = re.compile(rf"({JUDGMENT_START}.*?){JUDGMENT_STOP}", re.DOTALL)

JUDGES_RE = re.compile(
    # 独任案件常用"审判员"开头；合议庭用"审判长"
    # 截止：附相关法条 / 来源 / "本件与原本"
    r"((?:审判长|代理审判员|审判员|执行员|审判员助理)[\s\u3000].{0,200}?)"
    r"(?=附相关法条|附\s*相关法条|相关法条|来源[：:]|本件与原本核对|$)",
    re.DOTALL,
)

# 事实认定段（含涉案金额 / 犯罪地点 / 共犯角色描述）
# 起始: "经审理查明" / "本院查明" / "公诉机关指控" / "起诉书指控" / "指控"
# 结束: "本院认为" / "判决如下" 前
FACTS_START = r"(?:经审理查明|本院查明|公诉机关指控|起诉书指控|起诉指控|检察院\w*?指控|经审判查明)[：:，,。]?\s*"
FACTS_STOP = r"(?=本院认为|上述事实|以上事实|经审理认为|判决如下|裁定如下|根据.{0,10}《中华人民共和国刑法》|依照.{0,10}《中华人民共和国刑法》)"
FACTS_RE = re.compile(rf"({FACTS_START}.*?){FACTS_STOP}", re.DOTALL)

FALLBACK_HEAD = 800
FACTS_MAX_LEN = 1500  # 事实段上限（与 judgment 相同量级）


def segment_one(text: str) -> tuple[str, str, str, str]:
    """返回 4 段：defendant / facts / judgment / judges."""
    if not isinstance(text, str) or len(text) < 50:
        return ("", "", "", "")

    matches = DEFENDANT_RE.findall(text)
    if matches:
        defendant_seg = " ||| ".join(m.strip()[:500] for m in matches[:5])
    else:
        idx = text.find("被告人")
        defendant_seg = text[idx:idx + FALLBACK_HEAD] if idx >= 0 else ""

    # 事实段
    m = FACTS_RE.search(text)
    facts_seg = m.group(1).strip()[:FACTS_MAX_LEN] if m else ""

    m = JUDGMENT_RE.search(text)
    judgment_seg = m.group(1).strip()[:1500] if m else ""
    if not judgment_seg:
        judgment_seg = text[-FALLBACK_HEAD:] if len(text) > FALLBACK_HEAD else text

    m = JUDGES_RE.search(text)
    judges_seg = m.group(1).strip()[:300] if m else ""

    return (defendant_seg, facts_seg, judgment_seg, judges_seg)


def process_dataset(input_dataset: Path, output_dataset: Path,
                    sample_n: int | None = None) -> dict:
    d = ds.dataset(input_dataset, format="parquet", partitioning="hive")
    output_dataset.mkdir(parents=True, exist_ok=True)

    stats = {"fragments_processed": 0, "rows_total": 0,
             "rows_with_defendant_seg": 0, "rows_with_facts_seg": 0,
             "rows_with_judgment_seg": 0, "rows_with_judges_seg": 0}

    fragments = list(d.get_fragments())
    if sample_n:
        fragments = fragments[:sample_n]
    total_frag = len(fragments)
    print(f"[info] 共 {total_frag} fragments 待处理", flush=True)

    for i, frag in enumerate(fragments):
        df = frag.to_table().to_pandas()
        segs = df["full_text"].apply(segment_one)
        df["defendant_segment"] = segs.apply(lambda x: x[0])
        df["facts_segment"] = segs.apply(lambda x: x[1])
        df["judgment_segment"] = segs.apply(lambda x: x[2])
        df["judges_segment"] = segs.apply(lambda x: x[3])

        stats["rows_total"] += len(df)
        stats["rows_with_defendant_seg"] += (df["defendant_segment"].str.len() > 0).sum()
        stats["rows_with_facts_seg"] += (df["facts_segment"].str.len() > 0).sum()
        stats["rows_with_judgment_seg"] += (df["judgment_segment"].str.len() > 0).sum()
        stats["rows_with_judges_seg"] += (df["judges_segment"].str.len() > 0).sum()

        rel = Path(frag.path).relative_to(input_dataset)
        out_path = output_dataset / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pandas(df, preserve_index=False)
        new_fields = [f.with_type(pa.string()) if f.type == pa.large_string() else f
                      for f in table.schema]
        table = table.cast(pa.schema(new_fields))
        pq.write_table(table, out_path, compression="snappy")
        stats["fragments_processed"] += 1

        if (i + 1) % 100 == 0 or i == total_frag - 1:
            print(f"  {i+1}/{total_frag} | rows={stats['rows_total']:,}", flush=True)

    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=Path("03_masterdata/case_events"))
    parser.add_argument("--output", type=Path, default=Path("03_masterdata/case_events_segmented"))
    parser.add_argument("--sample-fragments", type=int, default=None)
    args = parser.parse_args()

    t0 = time.time()
    print(f"[start] {args.input} -> {args.output}")
    stats = process_dataset(args.input, args.output, args.sample_fragments)
    elapsed = time.time() - t0
    print(f"\n=== STATS ===")
    print(f"  fragments_processed: {stats['fragments_processed']}")
    print(f"  rows_total: {stats['rows_total']:,}")
    n = max(stats['rows_total'], 1)
    print(f"  defendant_seg 命中: {stats['rows_with_defendant_seg']:,} ({stats['rows_with_defendant_seg']/n*100:.1f}%)")
    print(f"  facts_seg 命中: {stats['rows_with_facts_seg']:,} ({stats['rows_with_facts_seg']/n*100:.1f}%)")
    print(f"  judgment_seg 命中: {stats['rows_with_judgment_seg']:,} ({stats['rows_with_judgment_seg']/n*100:.1f}%)")
    print(f"  judges_seg 命中: {stats['rows_with_judges_seg']:,} ({stats['rows_with_judges_seg']/n*100:.1f}%)")
    print(f"  elapsed: {elapsed:.1f}s")


if __name__ == "__main__":
    raise SystemExit(main())
