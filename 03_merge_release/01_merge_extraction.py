#!/usr/bin/env python3
"""03_merge_extraction_1985_2021 · 合并 1985–2021 抽取字段表（release v2 中间产物）

独立 paper 定位：本数据集从**已跑好的抽取结果 jsonl / parquet** 合并而来，不重跑 GPU、
不依赖分析专用 v111 面板（judgment_year 由 case-id 自解析，sentence 用抽取原值）。
上游源一律只读，读入后加工落本项目。

源（只读）:
  A 2014-2021 21-key 合并抽取 : masterdata/llm_extracted/master_extracted.parquet
                               (2,678,131; 本地首轮 92% + 本地重跑 8%, 带 source 列)
  B 1985-2013 21-key 补抽     : 补抽包 task21/results/**/*.jsonl (+overflow_retry)
  C 2014-2019 offense_date    : data_cleaning/raw/offense_date_merged_v2.parquet (1,480,819)
  D 1985-2013 offense         : 补抽包 offense_pack/offense_date_regex_*.parquet(regex ok)
                               + offense/results/**/*.jsonl (LLM 残差 success)
输出:
  data_cleaning/raw/merged_extraction_1985_2021_v2_<TS>.parquet  (21-key + source + offense + judgment_year)
  data_cleaning/raw/merge_stats_<TS>.json
provenance: source 列如实保留（gpu-* / 重跑批次令牌 / gpu-...-supplement）；论文叙事按
「统一 Qwen 抽取管线」框定，但字段级不抹掉来源。
"""
import glob
import gzip
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

TS = "20260707080211"
__import__("sys").path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_config  # paths come from config.yaml (see config.example.yaml)
CFG = pipeline_config.load(handle_help=True, keys=('work_dir', 'main_extraction_2014_2021', 'offense_date_2014_2019', 'supplement_1985_2013_results',))
ROOT = CFG.path("work_dir")  # intermediates live under <work_dir>/data_cleaning/{raw,master}
PKG = CFG.path("supplement_1985_2013_results")
OUT_DIR = ROOT / "data_cleaning/raw"

A_PATH = CFG.path("main_extraction_2014_2021")
C_PATH = CFG.path("offense_date_2014_2019")

KEYS21 = ["id", "sex", "birth_year", "ethnicity", "edu_level", "prior_occupation_raw",
          "hukou_text", "has_prior_conviction", "prior_conviction_desc", "is_recidivist_legal",
          "party_role", "sentence_months", "sentence_type", "probation_flag", "fine_yuan",
          "damage_amount_yuan", "crime_location_raw", "prior_years", "prior_offenses",
          "prior_sentence_months", "prior_released_years"]
OFFENSE_COLS = ["offense_start_date", "offense_end_date", "offense_year_main", "offense_year_alt",
                "offense_date_precision", "offense_confidence", "offense_source", "no_offense_time",
                "prosecution_date", "case_filed_date", "arrest_date", "detention_date",
                "formal_arrest_date"]
YEAR_RE = re.compile(r"[（(](\d{4})[）)]")
YEAR_FALLBACK = re.compile(r"(\d{4})")


def parse_year(idstr):
    if not isinstance(idstr, str):
        return None
    m = YEAR_RE.search(idstr)
    if not m:
        m = YEAR_FALLBACK.search(idstr)
    if not m:
        return None
    y = int(m.group(1))
    return y if 1980 <= y <= 2022 else None


def load_A():
    df = pq.read_table(A_PATH, columns=KEYS21 + ["source"]).to_pandas()
    df["source_batch"] = "main_2014_2021"
    return df


def load_B():
    rows = []
    files = glob.glob(str(PKG / "task21/results/**/batch_*_result.jsonl"), recursive=True)
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    df = pd.DataFrame(rows)
    for k in KEYS21:
        if k not in df.columns:
            df[k] = None
    df = df[KEYS21].copy()
    df["source"] = "gpu-qwen3-80b-supplement"
    df["source_batch"] = "supplement_1985_2013"
    return df


def load_C():
    df = pq.read_table(C_PATH).to_pandas()
    keep = ["id"] + [c for c in OFFENSE_COLS if c in df.columns]
    return df[keep].copy()


def load_D():
    # D1: regex-ok 部分
    reg_path = next((PKG / "offense_pack").glob("offense_date_regex_*.parquet"))
    reg = pq.read_table(reg_path).to_pandas()
    reg = reg[reg["regex_status"] == "ok"].copy()
    d1 = pd.DataFrame({
        "id": reg["id"],
        "offense_start_date": reg.get("offense_start_date"),
        "offense_end_date": reg.get("offense_end_date"),
        "offense_year_main": reg.get("offense_year_main"),
        "offense_year_alt": reg.get("offense_year_alt"),
        "offense_date_precision": reg.get("date_precision"),
        "offense_confidence": reg.get("confidence"),
        "no_offense_time": False,
        "prosecution_date": reg.get("prosecution_date"),
        "case_filed_date": reg.get("case_filed_date"),
        "arrest_date": reg.get("arrest_date"),
        "detention_date": reg.get("detention_date"),
        "formal_arrest_date": reg.get("formal_arrest_date"),
    })
    d1["offense_source"] = "regex"
    # D2: LLM 残差 success（有日期或明确 null）
    rows = []
    for f in glob.glob(str(PKG / "offense/results/**/batch_*_result.jsonl"), recursive=True):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    llm = pd.DataFrame(rows)

    def year_of(s):
        if isinstance(s, str) and len(s) >= 4 and s[:4].isdigit():
            return int(s[:4])
        return None
    d2 = pd.DataFrame({
        "id": llm["id"],
        "offense_start_date": llm.get("offense_start_date"),
        "offense_end_date": llm.get("offense_end_date"),
        "offense_year_main": llm["offense_start_date"].apply(year_of) if "offense_start_date" in llm else None,
        "offense_year_alt": llm["offense_end_date"].apply(year_of) if "offense_end_date" in llm else None,
        "offense_date_precision": llm.get("date_precision"),
        "offense_confidence": llm.get("confidence"),
        "no_offense_time": llm["failure_reason"].eq("no_offense_time_in_text") if "failure_reason" in llm else None,
        "prosecution_date": None, "case_filed_date": None, "arrest_date": None,
        "detention_date": None, "formal_arrest_date": None,
    })
    d2["offense_source"] = "llm_supplement"
    d = pd.concat([d1, d2], ignore_index=True)
    d = d.drop_duplicates("id", keep="first")  # regex-ok 与 LLM 残差按构造不重叠
    for c in OFFENSE_COLS:
        if c not in d.columns:
            d[c] = None
    return d[["id"] + OFFENSE_COLS].copy()


def main():
    print("[A] load master_extracted 2014-2021 ...", flush=True)
    A = load_A()
    print(f"    A rows={len(A):,}")
    print("[B] load supplement 1985-2013 21-key ...", flush=True)
    B = load_B()
    print(f"    B rows={len(B):,}")

    twenty1 = pd.concat([A, B], ignore_index=True)
    before = len(twenty1)
    twenty1 = twenty1.drop_duplicates("id", keep="first")
    dup21 = before - len(twenty1)
    twenty1["judgment_year"] = twenty1["id"].astype(str).map(parse_year)

    print("[C] load offense 2014-2019 ...", flush=True)
    C = load_C()
    print(f"    C rows={len(C):,}")
    print("[D] load+merge supplement offense (regex ok + LLM residual) ...", flush=True)
    D = load_D()
    print(f"    D rows={len(D):,}")
    offense = pd.concat([C, D], ignore_index=True).drop_duplicates("id", keep="first")

    merged = twenty1.merge(offense, on="id", how="left")

    # ---- stats ----
    n = len(merged)
    yv = merged["judgment_year"]
    stats = {
        "ts": TS,
        "total_defendant_records": int(n),
        "dedup_dropped_across_A_B": int(dup21),
        "source_dist": merged["source"].value_counts(dropna=False).head(20).to_dict(),
        "source_batch_dist": merged["source_batch"].value_counts(dropna=False).to_dict(),
        "judgment_year_parse_coverage_pct": round(yv.notna().mean() * 100, 3),
        "per_year_counts": {int(k): int(v) for k, v in yv.dropna().astype(int).value_counts().sort_index().items()},
        "offense_join_coverage": {
            "any_offense_field": round(merged["offense_start_date"].notna().mean() * 100
                                       if "offense_start_date" in merged else 0, 2),
            "offense_year_main_nonnull_pct": round(merged["offense_year_main"].notna().mean() * 100, 2),
            "no_offense_time_true": int(merged["no_offense_time"].fillna(False).sum())
            if "no_offense_time" in merged else 0,
        },
        "supplement_records": int((merged["source_batch"] == "supplement_1985_2013").sum()),
        "main_records": int((merged["source_batch"] == "main_2014_2021").sum()),
    }
    # 首轮之外的补跑份额（同一模型、同一指令集，串行重跑）
    main = merged["source_batch"] == "main_2014_2021"
    retry = main & ~merged["source"].astype(str).str.startswith("gpu")
    stats["retry_batch_records"] = int(retry.sum())
    stats["retry_batch_pct"] = round(int(retry.sum()) / max(int(main.sum()), 1) * 100, 3)

    # ---- dtype 归一（A=arrow-backed / B,D=json object 混排 → parquet-safe） ----
    def tobool(v):
        if isinstance(v, bool):
            return v
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return None
        s = str(v).strip().lower()
        return True if s in ("true", "1") else (False if s in ("false", "0") else None)

    def tostr(v):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return None
        s = str(v)
        return None if s in ("null", "None", "nan", "") else s

    def tojson(v):
        # 4 个 prior_* 数组存 JSON 字符串（跨 arrow+json 源最稳；最终包按显式 schema 解回）
        if not isinstance(v, (list, tuple, np.ndarray)):
            return None
        out = []
        for x in v:
            if isinstance(x, (list, tuple, np.ndarray)):
                out.append(list(x))
            elif isinstance(x, float) and np.isnan(x):
                out.append(None)
            elif isinstance(x, (np.integer, np.floating)):
                out.append(x.item())
            else:
                out.append(x)
        return json.dumps(out, ensure_ascii=False)

    for c in ["birth_year", "sentence_months", "fine_yuan", "damage_amount_yuan",
              "offense_year_main", "offense_year_alt", "judgment_year"]:
        if c in merged:
            merged[c] = pd.to_numeric(merged[c], errors="coerce")
    for c in ["has_prior_conviction", "is_recidivist_legal", "probation_flag", "no_offense_time"]:
        if c in merged:
            merged[c] = merged[c].map(tobool).astype("object")
    for c in ["id", "sex", "ethnicity", "edu_level", "prior_occupation_raw", "hukou_text",
              "prior_conviction_desc", "party_role", "sentence_type", "crime_location_raw",
              "source", "source_batch", "offense_start_date", "offense_end_date",
              "offense_date_precision", "offense_confidence", "offense_source",
              "prosecution_date", "case_filed_date", "arrest_date", "detention_date",
              "formal_arrest_date"]:
        if c in merged:
            merged[c] = merged[c].map(tostr)
    for c in ["prior_years", "prior_offenses", "prior_sentence_months", "prior_released_years"]:
        if c in merged:
            merged[c] = merged[c].map(tojson)  # JSON-string encoded in intermediate

    out = OUT_DIR / f"merged_extraction_1985_2021_v2_{TS}.parquet"
    merged.to_parquet(out, compression="zstd", index=False)
    (OUT_DIR / f"merge_stats_{TS}.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2))
    print("\n=== MERGE STATS ===")
    print(json.dumps(stats, ensure_ascii=False, indent=1)[:2600])
    print("\nsaved:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
