#!/usr/bin/env python3
"""06_integrate_offense_2014_2019 · 把 B 线 2014-2019 缺口 offense 并入合并表 → v4

输入(只读):
  - v3 合并表 merged_extraction_1985_2021_v3_20260707122903.parquet
  - B 线补抽包(已 SHA 验签 + 独立复核):
      offense_backfill_2014_2019…/offense_regex_2014_2019.parquet (regex-ok 399,227)
      offense_backfill_2014_2019…/results/**/batch_*_result.jsonl (LLM success 105,272 + null)
      offense_backfill_2014_2019…/collision_ids_excluded.txt (35,227 真冲突)
处理:
  - 只填当前 offense_source 为 null 的 2014-2019 gap 案例(148 万面板已有的不动)
  - regex-ok + LLM-success → offense 字段;LLM honest-null → no_offense_time=True
  - 35,227 真冲突 id → case_id_collision=True + offense 全 null(与 2020-2021 冲突口径一致)
输出: data_cleaning/raw/merged_extraction_1985_2021_v4_<TS>.parquet + offense_integration_v4_stats_<TS>.json
"""
import glob
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

TS = "20260707170002"
__import__("sys").path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_config  # paths come from config.yaml (see config.example.yaml)
CFG = pipeline_config.load(handle_help=True, keys=('work_dir', 'offense_backfill_2014_2019',))
ROOT = CFG.path("work_dir")  # intermediates live under <work_dir>/data_cleaning/{raw,master}
MERGED = ROOT / "data_cleaning/raw/merged_extraction_1985_2021_v3_20260707122903.parquet"
BF = CFG.path("offense_backfill_2014_2019")
OUT_DIR = ROOT / "data_cleaning/raw"
OFF_COLS = ["offense_start_date", "offense_end_date", "offense_year_main", "offense_year_alt",
            "offense_date_precision", "offense_confidence", "offense_source", "no_offense_time",
            "prosecution_date", "case_filed_date", "arrest_date", "detention_date", "formal_arrest_date"]


def yr(s):
    return int(s[:4]) if isinstance(s, str) and len(s) >= 4 and s[:4].isdigit() else None


def build_backfill_offense():
    reg = pq.read_table(BF / "offense_regex_2014_2019.parquet").to_pandas()
    reg = reg[reg["regex_status"] == "ok"].copy()
    d1 = pd.DataFrame({
        "id": reg["id"].astype(str),
        "offense_start_date": reg.get("offense_start_date"), "offense_end_date": reg.get("offense_end_date"),
        "offense_year_main": reg.get("offense_year_main"), "offense_year_alt": reg.get("offense_year_alt"),
        "offense_date_precision": reg.get("date_precision"), "offense_confidence": reg.get("confidence"),
        "offense_source": "regex", "no_offense_time": False,
        "prosecution_date": reg.get("prosecution_date"), "case_filed_date": reg.get("case_filed_date"),
        "arrest_date": reg.get("arrest_date"), "detention_date": reg.get("detention_date"),
        "formal_arrest_date": reg.get("formal_arrest_date"),
    })
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
        "offense_start_date": llm.get("offense_start_date"), "offense_end_date": llm.get("offense_end_date"),
        "offense_year_main": llm["offense_start_date"].apply(yr) if "offense_start_date" in llm else None,
        "offense_year_alt": llm["offense_end_date"].apply(yr) if "offense_end_date" in llm else None,
        "offense_date_precision": llm.get("date_precision"), "offense_confidence": llm.get("confidence"),
        "offense_source": np.where(llm["failure_reason"].notna(), "llm_no_time", "llm"),
        "no_offense_time": llm["failure_reason"].eq("no_offense_time_in_text"),
        "prosecution_date": None, "case_filed_date": None, "arrest_date": None,
        "detention_date": None, "formal_arrest_date": None,
    })
    return pd.concat([d1, d2], ignore_index=True).drop_duplicates("id", keep="first")


def main():
    df = pd.read_parquet(MERGED)
    before = int(df["offense_year_main"].notna().sum())
    bf = build_backfill_offense().set_index("id")
    coll = set(l.strip() for l in open(BF / "collision_ids_excluded.txt", encoding="utf-8") if l.strip())

    in1419 = (df["judgment_year"] >= 2014) & (df["judgment_year"] <= 2019)
    fillable = in1419 & df["offense_source"].isna() & df["id"].isin(set(bf.index))
    idx = df.index[fillable]
    ids = df.loc[idx, "id"].astype(str).values
    for c in OFF_COLS:
        if c in bf.columns:
            df.loc[idx, c] = bf.reindex(ids)[c].values
    filled = int(fillable.sum())

    # 新增 2014-2019 真冲突 flag（并到既有 2020-2021 冲突 flag 上）
    new_coll = df["id"].isin(coll)
    df["case_id_collision"] = df["case_id_collision"].fillna(False) | new_coll
    n_new_coll = int((new_coll & ~df["case_id_collision"].isna()).sum())
    df.loc[df["case_id_collision"], OFF_COLS] = np.nan
    df.loc[df["case_id_collision"], "no_offense_time"] = np.nan

    def tobool(v):
        if isinstance(v, bool): return v
        if v is None or (isinstance(v, float) and np.isnan(v)): return None
        s = str(v).strip().lower()
        return True if s in ("true", "1") else (False if s in ("false", "0") else None)
    df["no_offense_time"] = df["no_offense_time"].map(tobool).astype("object")
    for c in ["offense_year_main", "offense_year_alt"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    after = int(df["offense_year_main"].notna().sum())
    out = OUT_DIR / f"merged_extraction_1985_2021_v4_{TS}.parquet"
    df.to_parquet(out, compression="zstd", index=False)

    era = np.where(df["judgment_year"] <= 2013, "1985-2013",
                   np.where(df["judgment_year"] <= 2019, "2014-2019", "2020-2021"))
    cov = {e: {"n": int((era == e).sum()),
               "offense_cov_pct": round(df.loc[era == e, "offense_year_main"].notna().mean() * 100, 1)}
           for e in ["1985-2013", "2014-2019", "2020-2021"]}
    stats = {"ts": TS, "rows": len(df),
             "offense_before": before, "offense_after": after, "gap_filled_2014_2019": filled,
             "offense_cov_before_pct": round(before / len(df) * 100, 2),
             "offense_cov_after_pct": round(after / len(df) * 100, 2),
             "collision_total_flagged": int(df["case_id_collision"].sum()),
             "collision_new_2014_2019": int(new_coll.sum()),
             "per_era_offense_coverage": cov}
    (OUT_DIR / f"offense_integration_v4_stats_{TS}.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2))
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print("saved:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
