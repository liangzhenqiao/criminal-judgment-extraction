#!/usr/bin/env python3
"""08_extract_recoverable_facts · 为最后一轮 offense 补抽取原文 facts

可恢复集 = v4 里 offense 仍 null、offense_source 空(从未产出)、非冲突、且在 case_events 有非空 facts。
从 case_events_segmented(2014-2021, 只读) 抽取这些 id 的**所有** facts 版本(含近似重复/潜在冲突)，
键用**本数据集的 id 写法**(便于 GPU 输出直接 join 回来)，写成与 offense_facts_pack 同格式的
facts_only_part_*.jsonl.gz，供 GPU 走同套(相似度冲突清洗 → regex 前置 → LLM 残差)流程。
输出: data_cleaning/raw/recoverable_facts_pack_<TS>/ (facts_only_part_*.jsonl.gz + MANIFEST.json + SHA256SUMS.txt)
"""
import gzip
import hashlib
import json
import re
import sys
from pathlib import Path

import pandas as pd
import pyarrow.dataset as ds
import pyarrow.compute as pc

TS = "20260707172535"
__import__("sys").path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_config  # paths come from config.yaml (see config.example.yaml)
CFG = pipeline_config.load(handle_help=True, keys=('work_dir', 'segmented_2014_2021',))
ROOT = CFG.path("work_dir")  # intermediates live under <work_dir>/data_cleaning/{raw,master}
V4 = ROOT / "data_cleaning/raw/merged_extraction_1985_2021_v4_20260707170002.parquet"
SEG = str(CFG.path("segmented_2014_2021"))
OUT = ROOT / "data_cleaning/raw" / f"recoverable_facts_pack_{TS}"
PART_ROWS = 100_000


def norm(i):
    return (str(i).replace("（", "(").replace("）", ")")
            .translate(str.maketrans("０１２３４５６７８９", "0123456789")))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    df = pd.read_parquet(V4, columns=["id", "offense_year_main", "offense_source", "case_id_collision"])
    rec = df[df["offense_year_main"].isna() & df["offense_source"].isna()
             & ~df["case_id_collision"].fillna(False)]
    # norm -> 数据集原始 id 写法
    norm2id = {}
    for i in rec["id"].dropna():
        norm2id.setdefault(norm(i), str(i))
    recset = set(norm2id)
    print(f"可恢复候选(never-produced 非冲突)={len(recset):,}")

    dset = ds.dataset(SEG, format="parquet", partitioning="hive")
    part = 0
    buf = []
    written = 0
    matched_ids = set()

    def flush():
        nonlocal part, buf
        if not buf:
            return
        with gzip.open(OUT / f"facts_only_part_{part:03d}.jsonl.gz", "wt", encoding="utf-8") as f:
            f.write("\n".join(buf) + "\n")
        part += 1
        buf = []

    for y in range(2014, 2022):
        t = dset.to_table(columns=["case_id", "facts_segment"], filter=(pc.field("judge_year") == y))
        for cid, fs in zip(t.column("case_id").to_pylist(), t.column("facts_segment").to_pylist()):
            if not cid or not fs or not fs.strip():
                continue
            nc = norm(cid)
            if nc in recset:
                buf.append(json.dumps({"id": norm2id[nc], "facts_segment": fs}, ensure_ascii=False))
                written += 1
                matched_ids.add(nc)
                if len(buf) >= PART_ROWS:
                    flush()
    flush()

    manifest = {
        "ts": TS, "source": "case_events_segmented 2014-2021 (read-only)",
        "recoverable_candidates": len(recset),
        "unique_ids_with_facts": len(matched_ids),
        "total_fact_rows_written (含多版本)": written,
        "parts": part,
        "note": "id 写法=本数据集原始写法；含每 id 的所有 facts 版本(供 GPU 相似度冲突清洗)；"
                "GPU 侧走同套 offense_date_v1.0 流程(过滤已隐含=只有这些 id;冲突清洗→regex→LLM)",
    }
    (OUT / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    shas = [f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {f.name}"
            for f in sorted(OUT.iterdir()) if f.name != "SHA256SUMS.txt"]
    (OUT / "SHA256SUMS.txt").write_text("\n".join(shas) + "\n")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print("PACK:", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
