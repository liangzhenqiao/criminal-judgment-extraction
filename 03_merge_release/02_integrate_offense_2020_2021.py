#!/usr/bin/env python3
"""04_integrate_offense_2020_2021 · 把 GPU 补抽的 2020-2021 offense 并入合并表

输入(只读):
  - 当前合并表 data_cleaning/raw/merged_extraction_1985_2021_v2_20260707080211.parquet
  - GPU 补抽包(已验签/独立复算 grounding 100%):
      offense_backfill…/offense_regex_2020_2021.parquet  (regex-ok 289,456)
      offense_backfill…/results/**/batch_*_result.jsonl  (LLM success 79,935 + null)
      offense_backfill…/collision_ids.txt                (3,667 冲突 id → offense=null + flag)
处理:
  - 仅对当前 offense 为 null 的 id 填充(2020-2021 段);绝不改动既有 2014-2019 / 1985-2013 offense
  - regex-ok + LLM-success 合并为 offense 字段;LLM honest-null → no_offense_time=True
  - collision id → offense 全部保持 null + case_id_collision=True(语料层已知案号冲突,论文 Usage Notes 交代)
输出: data_cleaning/raw/merged_extraction_1985_2021_v3_<TS>.parquet + offense_integration_stats_<TS>.json
"""
import glob
import json
import sys
from pathlib import Path

import pandas as pd
import numpy as np
import pyarrow.parquet as pq

TS = "20260707122903"
__import__("sys").path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_config  # paths come from config.yaml (see config.example.yaml)
CFG = pipeline_config.load(handle_help=True, keys=('work_dir', 'offense_backfill_2020_2021',))
ROOT = CFG.path("work_dir")  # intermediates live under <work_dir>/data_cleaning/{raw,master}
MERGED = ROOT / "data_cleaning/raw/merged_extraction_1985_2021_v2_20260707080211.parquet"
BF = CFG.path("offense_backfill_2020_2021")
OUT_DIR = ROOT / "data_cleaning/raw"

OFF_COLS = ["offense_start_date", "offense_end_date", "offense_year_main", "offense_year_alt",
            "offense_date_precision", "offense_confidence", "offense_source", "no_offense_time",
            "prosecution_date", "case_filed_date", "arrest_date", "detention_date", "formal_arrest_date"]


def yr(s):
    if isinstance(s, str) and len(s) >= 4 and s[:4].isdigit():
        return int(s[:4])
    return None


def build_backfill_offense():
    # regex-ok
    reg = pq.read_table(BF / "offense_regex_2020_2021.parquet").to_pandas()
    reg = reg[reg["regex_status"] == "ok"].copy()
    d1 = pd.DataFrame({
        "id": reg["id"].astype(str),
        "offense_start_date": reg.get("offense_start_date"),
        "offense_end_date": reg.get("offense_end_date"),
        "offense_year_main": reg.get("offense_year_main"),
        "offense_year_alt": reg.get("offense_year_alt"),
        "offense_date_precision": reg.get("date_precision"),
        "offense_confidence": reg.get("confidence"),
        "offense_source": "regex",
        "no_offense_time": False,
        "prosecution_date": reg.get("prosecution_date"),
        "case_filed_date": reg.get("case_filed_date"),
        "arrest_date": reg.get("arrest_date"),
        "detention_date": reg.get("detention_date"),
        "formal_arrest_date": reg.get("formal_arrest_date"),
    })
    # LLM residual (success + honest null)
    rows = []
    for f in glob.glob(str(BF / "results/**/batch_*_result.jsonl"), recursive=True):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    llm = pd.DataFrame(rows)
    d2 = pd.DataFrame({
        "id": llm["id"].astype(str),
        "offense_start_date": llm.get("offense_start_date"),
        "offense_end_date": llm.get("offense_end_date"),
        "offense_year_main": llm["offense_start_date"].apply(yr) if "offense_start_date" in llm else None,
        "offense_year_alt": llm["offense_end_date"].apply(yr) if "offense_end_date" in llm else None,
        "offense_date_precision": llm.get("date_precision"),
        "offense_confidence": llm.get("confidence"),
        "offense_source": np.where(llm["failure_reason"].notna(), "llm_no_time", "llm"),
        "no_offense_time": llm["failure_reason"].eq("no_offense_time_in_text"),
        "prosecution_date": None, "case_filed_date": None, "arrest_date": None,
        "detention_date": None, "formal_arrest_date": None,
    })
    d = pd.concat([d1, d2], ignore_index=True).drop_duplicates("id", keep="first")
    return d


def main():
    df = pd.read_parquet(MERGED)
    n = len(df)
    before_off = df["offense_year_main"].notna().sum()

    bf = build_backfill_offense()
    bf_map = bf.set_index("id")
    coll = set(l.strip() for l in open(BF / "collision_ids.txt") if l.strip())

    # 只填当前 offense 为 null 的 id（= 2020-2021 段）
    cur_null = df["offense_year_main"].isna()
    fillable = df["id"].isin(set(bf["id"])) & cur_null
    idx = df.index[fillable]
    ids = df.loc[idx, "id"].astype(str).values
    filled = 0
    for col in OFF_COLS:
        if col in bf_map.columns:
            vals = bf_map.reindex(ids)[col].values
            df.loc[idx, col] = vals
    filled = int(fillable.sum())

    # collision flag（数据集里存在的冲突 id → 标记 + offense 保持 null）
    df["case_id_collision"] = df["id"].isin(coll)
    n_coll_in_ds = int(df["case_id_collision"].sum())
    # 保险：collision id 的 offense 一律清空（不应被填，但双保险）
    df.loc[df["case_id_collision"], OFF_COLS] = np.nan
    df.loc[df["case_id_collision"], "no_offense_time"] = np.nan

    after_off = df["offense_year_main"].notna().sum()

    # 归一 no_offense_time 为 bool/None
    def tobool(v):
        if isinstance(v, bool): return v
        if v is None or (isinstance(v, float) and np.isnan(v)): return None
        s = str(v).strip().lower()
        return True if s in ("true", "1") else (False if s in ("false", "0") else None)
    df["no_offense_time"] = df["no_offense_time"].map(tobool).astype("object")
    for c in ["offense_year_main", "offense_year_alt"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    out = OUT_DIR / f"merged_extraction_1985_2021_v3_{TS}.parquet"
    df.to_parquet(out, compression="zstd", index=False)

    # per-era offense coverage
    era = np.where(df["judgment_year"] <= 2013, "1985-2013",
                   np.where(df["judgment_year"] <= 2019, "2014-2019", "2020-2021"))
    cov = {}
    for e in ["1985-2013", "2014-2019", "2020-2021"]:
        m = era == e
        cov[e] = {"n": int(m.sum()),
                  "offense_cov_pct": round(df.loc[m, "offense_year_main"].notna().mean() * 100, 1)}
    stats = {
        "ts": TS, "rows": int(n),
        "offense_nonnull_before": int(before_off),
        "offense_nonnull_after": int(after_off),
        "offense_filled_2020_2021": filled,
        "offense_cov_before_pct": round(before_off / n * 100, 2),
        "offense_cov_after_pct": round(after_off / n * 100, 2),
        "collision_ids_flagged_in_dataset": n_coll_in_ds,
        "per_era_offense_coverage": cov,
    }
    (OUT_DIR / f"offense_integration_stats_{TS}.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2))
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print("saved:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
