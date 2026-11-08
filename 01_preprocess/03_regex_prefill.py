"""
02b_regex_extract.py · NER Layer 2 正则高密度抽取

输入: 03_masterdata/case_events_segmented/  (含 defendant/judgment/judges 三段)
输出:
- 03_masterdata/case_events_regex_extracted/  (新增字段 + extraction_confidence)
- 03_masterdata/regex_extraction_quality.csv (字段命中率统计)

抽取字段（高度模板化字段 → 正则；语义字段交给 Layer 3 LLM）:
- sex (男/女)
- birth_year, age_at_case
- edu_level (文盲/小学/初中/高中/中专/大专/本科/硕士/博士)
- prior_occupation_raw (原始职业字符串, 待 Layer 3 标准化)
- hukou_text (户籍原始文本, 待 Layer 3 标准化省份)
- sentence_type (有期徒刑/无期徒刑/死刑/缓刑/拘役/管制 + 罚金标志)
- sentence_months (有期徒刑月数)
- fine_yuan (罚金)
- has_recidivism_history (累犯/前科 标记)
- has_self_surrender (自首)
- has_truthful_confession (坦白)
- has_plea_leniency (认罪认罚)
- judge_chief (审判长姓名, B2 IV 用)
- judge_members (审判员/陪审员姓名, list)
"""
from __future__ import annotations

import argparse
import re
import time
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

# === 中文数字 → 阿拉伯数字 ===
CN_DIGITS = {"零":0,"一":1,"二":2,"两":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10}

def cn_to_int(s: str) -> int | None:
    """简单中文数字解析（支持 一/二/.../十/十一/.../九十九）。"""
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if "十" in s:
        parts = s.split("十")
        head = CN_DIGITS.get(parts[0], 1) if parts[0] else 1
        tail = CN_DIGITS.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
        return head * 10 + tail
    return CN_DIGITS.get(s)


def parse_sentence_months(text: str) -> tuple[str | None, float | None]:
    """从判决段抽取量刑类型 + 月数."""
    if not text:
        return (None, None)

    # 死刑
    if re.search(r"判处死刑(?!缓期)", text):
        return ("death", float("inf"))
    if re.search(r"死刑.{0,5}缓期", text):
        return ("death_suspended", float("inf"))
    if re.search(r"判处无期徒刑", text):
        return ("life", float("inf"))

    # 拘役/管制
    m = re.search(r"判处(拘役|管制)([零一二三四五六七八九十\d]+)个?月", text)
    if m:
        kind = "detention" if m.group(1) == "拘役" else "control"
        months = cn_to_int(m.group(2))
        return (kind, float(months) if months else None)

    # 有期徒刑：N年[M月] / N个月
    # case 1: "有期徒刑 X 年 Y 月"
    m = re.search(r"判处有期徒刑([零一二三四五六七八九十\d]+)年([零一二三四五六七八九十\d]+)个?月", text)
    if m:
        y = cn_to_int(m.group(1)) or 0
        mm = cn_to_int(m.group(2)) or 0
        # 缓刑标识
        suspend = "suspended" if re.search(r"缓刑", text) else "fixed"
        return (f"prison_{suspend}", float(y * 12 + mm))
    # case 2: "有期徒刑 X 年"
    m = re.search(r"判处有期徒刑([零一二三四五六七八九十\d]+)年(?!.{0,3}月)", text)
    if m:
        y = cn_to_int(m.group(1))
        suspend = "suspended" if re.search(r"缓刑", text) else "fixed"
        return (f"prison_{suspend}", float(y * 12) if y else None)
    # case 3: "有期徒刑 X 个月"
    m = re.search(r"判处有期徒刑([零一二三四五六七八九十\d]+)个?月", text)
    if m:
        mm = cn_to_int(m.group(1))
        suspend = "suspended" if re.search(r"缓刑", text) else "fixed"
        return (f"prison_{suspend}", float(mm) if mm else None)

    return (None, None)


def parse_fine(text: str) -> int | None:
    """罚金（元）."""
    if not text:
        return None
    # "并处罚金人民币五千元" / "罚金 10000 元" / "罚金人民币壹万元"
    m = re.search(r"罚金人民币([零一二三四五六七八九十百千万\d]+)元", text)
    if m:
        s = m.group(1)
        if s.isdigit():
            return int(s)
        # 简单万-千-百-十 解析
        result = 0
        chunk = 0
        for c in s:
            v = CN_DIGITS.get(c)
            if v is not None:
                chunk = v
            elif c == "十":
                chunk = (chunk or 1) * 10
            elif c == "百":
                chunk = (chunk or 1) * 100
            elif c == "千":
                chunk = (chunk or 1) * 1000
            elif c == "万":
                result += (chunk or 1) * 10000
                chunk = 0
        result += chunk
        return result if result > 0 else None
    m = re.search(r"罚金\s*(?:人民币)?([\d,]+)\s*元", text)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def extract_defendant_fields(seg: str) -> dict[str, Any]:
    """从被告段抽取性别/出生/学历/职业/户籍."""
    out = {
        "sex": None, "birth_year": None, "age_at_case": None,
        "edu_level": None, "prior_occupation_raw": None, "hukou_text": None,
    }
    if not seg or not isinstance(seg, str):
        return out

    # 性别（取第一个被告人段的"，男，"或"，女，"）
    m = re.search(r"被告人[^，。]{1,15}[，,]\s*(男|女)", seg)
    if m:
        out["sex"] = m.group(1)

    # 出生年（"YYYY年M月D日出生"或"YYYY年生"）
    m = re.search(r"(\d{4})年\s*\d{1,2}月\s*\d{1,2}日出生", seg)
    if not m:
        m = re.search(r"(\d{4})年\s*\d{1,2}月生", seg)
    if not m:
        m = re.search(r"(\d{4})年生", seg)
    if m:
        out["birth_year"] = int(m.group(1))
    # "现年 X 岁"
    m = re.search(r"现年([零一二三四五六七八九十\d]+)岁", seg)
    if m:
        v = cn_to_int(m.group(1))
        if v: out["age_at_case"] = v

    # 学历（按枚举优先级匹配）
    for lab in ["博士研究生","硕士研究生","研究生","博士","硕士","本科","大学","大专","专科","中专","职专","高中","技校","职高","初中","小学","文盲","半文盲","未受教育"]:
        if lab in seg:
            out["edu_level"] = lab
            break
    # 兜底正则
    if out["edu_level"] is None:
        m = re.search(r"([初高小]中|大?专|本科|研究生|文盲)\s*[文化程度毕业]?", seg)
        if m:
            out["edu_level"] = m.group(1)

    # 职业（"，X文化，<职业>。"或 "系<职业>"）
    m = re.search(r"(?:文化|程度)[，,]\s*([^，,。;；]{1,20}?)[。，,；;]", seg)
    if m:
        out["prior_occupation_raw"] = m.group(1).strip()

    # 户籍（"户籍所在地" 或 "住" 或 "户籍地"）
    m = re.search(r"(?:户籍(?:所在地|地)|户口所在地)[:：]?\s*([^，,。;；]{2,40})", seg)
    if not m:
        m = re.search(r"住址?\s*[:：]?\s*([^，,。;；]{2,40})", seg)
    if m:
        out["hukou_text"] = m.group(1).strip()

    return out


def extract_judges(seg: str) -> dict[str, Any]:
    """审判员段：审判长 + 其他成员."""
    out = {"judge_chief": None, "judge_members": None}
    if not seg or not isinstance(seg, str):
        return out

    # 审判长
    m = re.search(r"审判长[\s\u3000]*([\u4e00-\u9fa5·]{2,5})", seg)
    if m:
        out["judge_chief"] = m.group(1)

    # 审判员 / 代理审判员 / 人民陪审员（多个）
    members = []
    for pat in [r"审判员[\s\u3000]*([\u4e00-\u9fa5·]{2,5})",
                r"代理审判员[\s\u3000]*([\u4e00-\u9fa5·]{2,5})",
                r"人民陪审员[\s\u3000]*([\u4e00-\u9fa5·]{2,5})"]:
        members.extend(re.findall(pat, seg))
    if members:
        out["judge_members"] = ",".join(members)

    return out


KEYWORD_FLAGS = [
    ("has_recidivism_history", r"系累犯|属累犯|有前科|曾因.{0,8}罪.{0,8}判处"),
    ("has_self_surrender",     r"自动投案|系自首|具有自首情节"),
    ("has_truthful_confession", r"如实供述|具有坦白情节"),
    ("has_plea_leniency",       r"认罪认罚|签署认罪认罚"),
]


def extract_one(row: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    out.update(extract_defendant_fields(row.get("defendant_segment", "")))
    out.update(extract_judges(row.get("judges_segment", "")))

    sentence_type, sentence_months = parse_sentence_months(row.get("judgment_segment", ""))
    out["sentence_type"] = sentence_type
    out["sentence_months"] = sentence_months
    out["fine_yuan"] = parse_fine(row.get("judgment_segment", ""))

    # 关键词标志（覆盖全文，因为可能在认罪认罚段而不在判决段）
    full = (row.get("defendant_segment", "") or "") + " " + (row.get("judgment_segment", "") or "")
    for k, pat in KEYWORD_FLAGS:
        out[k] = bool(re.search(pat, full))

    # 派生 age_at_case：若有 birth_year + judge_year
    if out.get("birth_year") and row.get("judge_year"):
        try:
            out["age_at_case"] = int(row["judge_year"]) - int(out["birth_year"])
        except Exception:
            pass

    return out


def process_dataset(input_dataset: Path, output_dataset: Path,
                    sample_n: int | None = None) -> dict:
    d = ds.dataset(input_dataset, format="parquet", partitioning="hive")
    output_dataset.mkdir(parents=True, exist_ok=True)
    fragments = list(d.get_fragments())
    if sample_n:
        fragments = fragments[:sample_n]
    total = len(fragments)
    print(f"[info] {total} fragments", flush=True)

    stats = {"rows": 0, "field_hit": {}}
    NEW_COLS = ["sex","birth_year","age_at_case","edu_level","prior_occupation_raw","hukou_text",
                "sentence_type","sentence_months","fine_yuan",
                "has_recidivism_history","has_self_surrender","has_truthful_confession","has_plea_leniency",
                "judge_chief","judge_members"]
    for c in NEW_COLS:
        stats["field_hit"][c] = 0

    for i, frag in enumerate(fragments):
        df = frag.to_table().to_pandas()
        records = df[["defendant_segment","judgment_segment","judges_segment","judge_year"]].to_dict("records")
        ext = pd.DataFrame([extract_one(r) for r in records])
        for c in NEW_COLS:
            df[c] = ext[c].values

        stats["rows"] += len(df)
        for c in NEW_COLS:
            if df[c].dtype == bool:
                stats["field_hit"][c] += int(df[c].sum())
            else:
                stats["field_hit"][c] += int(df[c].notna().sum())

        rel = Path(frag.path).relative_to(input_dataset)
        out_path = output_dataset / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pandas(df, preserve_index=False)
        new_fields = [f.with_type(pa.string()) if f.type == pa.large_string() else f
                      for f in table.schema]
        table = table.cast(pa.schema(new_fields))
        pq.write_table(table, out_path, compression="snappy")

        if (i + 1) % 100 == 0 or i == total - 1:
            print(f"  {i+1}/{total} | rows={stats['rows']:,}", flush=True)
    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path,
                        default=Path("03_masterdata/case_events_segmented"))
    parser.add_argument("--output", type=Path,
                        default=Path("03_masterdata/case_events_regex_extracted"))
    parser.add_argument("--sample-fragments", type=int, default=None)
    args = parser.parse_args()

    t0 = time.time()
    stats = process_dataset(args.input, args.output, args.sample_fragments)
    elapsed = time.time() - t0

    print(f"\n=== STATS ===")
    print(f"  total rows: {stats['rows']:,}")
    n = max(stats['rows'], 1)
    print(f"  字段命中率:")
    for c, hit in stats["field_hit"].items():
        print(f"    {c:30s}  {hit:>10,}  ({hit/n*100:>5.1f}%)")
    print(f"  elapsed: {elapsed:.1f}s")


if __name__ == "__main__":
    raise SystemExit(main())
