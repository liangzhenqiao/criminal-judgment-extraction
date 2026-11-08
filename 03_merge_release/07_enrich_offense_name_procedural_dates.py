#!/usr/bin/env python3
"""12_enrich_v6 · 并入本案罪名三层派生 + 程序日期补密 → merged v6

1. offense_name/offense_L1/offense_name_source（11 号脚本派生, 97.93%; 残差待 GPU 后补为 'llm'）
2. 程序日期补密（免费）: 四轮 offense regex parquet 对"全部行"都算过 prosecution/立案/到案/拘留/逮捕,
   此前集成只取了 regex_status=='ok' 行——现把非 ok 行的程序日期也补进当前为 null 的位置。
"""
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

TS = "20260709115253"
__import__("sys").path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_config  # paths come from config.yaml (see config.example.yaml)
CFG = pipeline_config.load(handle_help=True, keys=('work_dir', 'supplement_1985_2013_results', 'offense_backfill_2020_2021', 'offense_backfill_2014_2019', 'offense_backfill_final',))
ROOT = CFG.path("work_dir")  # intermediates live under <work_dir>/data_cleaning/{raw,master}
V5 = ROOT / "data_cleaning/raw/merged_extraction_1985_2021_v5_20260709092811.parquet"
DERIVED = ROOT / f"data_cleaning/raw/offense_name_derived_{TS}.parquet"
REGEX_PARQUETS = [
    CFG.path("supplement_1985_2013_results") / "offense_pack/offense_date_regex_20260703.parquet",
    CFG.path("offense_backfill_2020_2021") / "offense_regex_2020_2021.parquet",
    CFG.path("offense_backfill_2014_2019") / "offense_regex_2014_2019.parquet",
    CFG.path("offense_backfill_final") / "offense_regex_final.parquet",
]
PROC = ["prosecution_date", "case_filed_date", "arrest_date", "detention_date", "formal_arrest_date"]
DN = re.compile(r"_d\d+$")
FW = str.maketrans("０１２３４５６７８９", "0123456789")


def norm(i):
    return str(i).replace("（", "(").replace("）", ")").translate(FW)


def main():
    df = pd.read_parquet(V5)
    n = len(df)
    df["_nb"] = df["id"].map(lambda x: norm(DN.sub("", str(x))) if isinstance(x, str) else None)

    # ---- 1) offense_name 派生并入 ----
    der = pd.read_parquet(DERIVED).set_index("nb")
    df["offense_name"] = df["_nb"].map(der["offense_name"])
    df["offense_L1"] = df["_nb"].map(der["offense_L1"])
    df["offense_name_source"] = df["_nb"].map(der["offense_name_source"])
    cov = df["offense_name"].notna().mean() * 100

    # ---- 2) 程序日期补密 ----
    proc_map = {}
    for p in REGEX_PARQUETS:
        t = pq.read_table(p, columns=["id"] + PROC).to_pandas()
        for c in PROC:
            col = t[c]
            for i, v in zip(t["id"], col):
                if v is None or (isinstance(v, float) and np.isnan(v)):
                    continue
                key = norm(str(i))
                d = proc_map.setdefault(key, {})
                if c not in d:
                    d[c] = v
    filled = {c: 0 for c in PROC}
    nb = df["_nb"]
    for c in PROC:
        cur_null = df[c].isna()
        vals = nb.map(lambda k: proc_map.get(k, {}).get(c) if isinstance(k, str) else None)
        m = cur_null & vals.notna()
        df.loc[m, c] = vals[m]
        filled[c] = int(m.sum())

    df.drop(columns=["_nb"], inplace=True)
    out = ROOT / f"data_cleaning/raw/merged_extraction_1985_2021_v6_{TS}.parquet"
    df.to_parquet(out, compression="zstd", index=False)

    stats = {"ts": TS, "rows": int(n),
             "offense_name_coverage_pct": round(cov, 2),
             "offense_name_source_dist": df["offense_name_source"].value_counts(dropna=False).to_dict(),
             "offense_L1_dist": df["offense_L1"].value_counts(dropna=False).to_dict(),
             "proc_dates_filled": filled,
             "proc_coverage_after": {c: round(df[c].notna().mean() * 100, 2) for c in PROC}}
    (ROOT / f"data_cleaning/raw/v6_enrich_stats_{TS}.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2, default=str))
    print(json.dumps(stats, ensure_ascii=False, indent=1, default=str))
    print("saved:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
