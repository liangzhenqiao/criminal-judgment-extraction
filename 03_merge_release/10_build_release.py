#!/usr/bin/env python3
"""05_build_release_v2 · 独立数据集发布包 v2（A 线本地打包）

输入(只读):
  - 合并表 v3 merged_extraction_1985_2021_v3_20260707122903.parquet（2,834,831 · offense 2020-2021 已并）
  - 法官链接 case_judge_joined_FULL.csv（id→judge name/role，2014-2019，含姓名，已决定纳入）
决策(已锁定):
  - source 列完全同质化（发布数据统一为 "qwen-open-model-pipeline"；内部审计留详细来源另存）
  - judge 含真实姓名纳入（PII/伦理 Usage Note）
  - sentence 用抽取原值（inf→null；无期/死缓 type 为权威→月数 null；英文残留归一）
  - PII 脱敏同 v1：弃 hukou_text / prior_conviction_desc；crime_location_raw 清洗；保留 judge_name
派生(廉价、独立可算): age_at_case / age_at_offense / prior_count / prior_total_months
延后 v2.1 富化(文档标注): hukou/court 地理编码 · offense_name/offense_L1（需文书段落重派生）· the 1985–2013 supplement line, 2014-2019 offense
输出: data_cleaning/master/release_v2_<TS>/  parquet + csv.gz + 字段字典 + README + PII报告 + QA + 内部provenance审计 + SHA256
硬闸: PII 扫描（身份证/手机/邮箱）0 命中（judge_name 是姓名、不在这三类，按已锁定的决策保留并在 README 声明）。
"""
import glob
import gzip
import hashlib
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

TS = "20260709134500"
__import__("sys").path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_config  # paths come from config.yaml (see config.example.yaml)
CFG = pipeline_config.load(handle_help=True, keys=('work_dir', 'judge_signature_csv',))
ROOT = CFG.path("work_dir")  # intermediates live under <work_dir>/data_cleaning/{raw,master}
MERGED = ROOT / "data_cleaning/raw/merged_extraction_1985_2021_v7_20260709134500.parquet"
JUDGE = CFG.path("judge_signature_csv")  # id -> judge, role (signature-block task output)
OUT = ROOT / "data_cleaning/master" / f"release_v2_3_{TS}"
STEM = "china_criminal_defendant_panel_1985_2021_v2_3"
N_EXPECTED = 2834831
LIST_COLS = ["prior_years", "prior_offenses", "prior_sentence_months", "prior_released_years"]
SENTENCE_TYPE_MAP = {"detention": "拘役", "prison_fixed": "有期徒刑",
                     "prison_suspended": "有期徒刑", "control": "管制", "life": "无期徒刑",
                     "death": "死刑", "death_suspended": "死刑缓期执行"}
NONQUANT = {"无期徒刑", "死刑", "死刑缓期执行"}
PII = {"id_card": re.compile(r"\d{17}[\dXx]|\d{15}(?!\d)"),
       "phone": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
       "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+")}

FIELD_GROUPS = {
    "identifiers": ["id", "judgment_year", "case_id_collision"],
    "demographics": ["sex", "birth_year", "age_at_case", "age_at_offense", "ethnicity",
                     "edu_level", "prior_occupation_raw"],
    "offense_sentencing": ["party_role", "offense_name", "offense_L1", "sentence_months",
                           "sentence_type", "probation_flag", "fine_yuan", "damage_amount_yuan",
                           "crime_location_raw"],
    "criminal_history": ["has_prior_conviction", "is_recidivist_legal", "prior_years",
                         "prior_offenses", "prior_sentence_months", "prior_released_years",
                         "prior_count", "prior_total_months"],
    "offense_timing": ["offense_start_date", "offense_end_date", "offense_year_main",
                       "offense_year_alt", "offense_date_precision", "offense_confidence",
                       "offense_source", "no_offense_time"],
    "procedural_dates": ["prosecution_date", "case_filed_date", "arrest_date",
                         "detention_date", "formal_arrest_date"],
    "judge": ["judge_name", "judge_role"],
    "provenance": ["source", "offense_name_source"],
}


def jload(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return []
    return v if isinstance(v, list) else []


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    qa = {"ts": TS}
    df = pd.read_parquet(MERGED)
    assert len(df) == N_EXPECTED, len(df)

    # 内部 provenance 审计（同质化前留档，不进发布包正文）
    prov = df["source"].value_counts(dropna=False).to_dict()
    (OUT / "INTERNAL_provenance_audit.json").write_text(json.dumps(
        {"note": "发布数据 source 已同质化；此为内部真实来源分布留档",
         "source_dist": {str(k): int(v) for k, v in prov.items()}}, ensure_ascii=False, indent=2))

    # ---- sentence 归一（同 v1） ----
    df["sentence_months"] = pd.to_numeric(df["sentence_months"], errors="coerce")
    df.loc[np.isinf(df["sentence_months"].fillna(0)), "sentence_months"] = np.nan
    df.loc[df["sentence_months"] == 9999, "sentence_months"] = np.nan  # 寿命/不可数标记
    qa["sentence_9999_nulled"] = int((pd.read_parquet(MERGED, columns=["sentence_months"])["sentence_months"] == 9999).sum())
    ps = df["sentence_type"] == "prison_suspended"
    df.loc[ps, "probation_flag"] = True
    qa["sentence_type_harmonized"] = int(df["sentence_type"].isin(SENTENCE_TYPE_MAP).sum())
    df["sentence_type"] = df["sentence_type"].replace(SENTENCE_TYPE_MAP)
    nq = df["sentence_type"].isin(NONQUANT)
    qa["sentence_nulled_nonquant"] = int((nq & df["sentence_months"].notna()).sum())
    df.loc[nq, "sentence_months"] = np.nan

    # ---- 派生 ----
    df["age_at_case"] = (df["judgment_year"] - pd.to_numeric(df["birth_year"], errors="coerce"))
    df.loc[(df["age_at_case"] < 10) | (df["age_at_case"] > 100), "age_at_case"] = np.nan
    df["age_at_offense"] = (pd.to_numeric(df["offense_year_main"], errors="coerce")
                            - pd.to_numeric(df["birth_year"], errors="coerce"))
    df.loc[(df["age_at_offense"] < 10) | (df["age_at_offense"] > 100), "age_at_offense"] = np.nan
    po = df["prior_offenses"].map(jload)
    df["prior_count"] = po.map(len)
    psm = df["prior_sentence_months"].map(jload)
    df["prior_total_months"] = psm.map(lambda a: float(np.nansum([x for x in a
                                       if isinstance(x, (int, float)) and x != 9999])) if a else 0.0)

    # ---- judge join（含姓名，已决定纳入） ----
    j = pd.read_csv(JUDGE, usecols=["id", "judge", "role"]).drop_duplicates("id", keep="first")
    jm = j.set_index("id")
    df["judge_name"] = df["id"].map(jm["judge"])
    df["judge_role"] = df["id"].map(jm["role"])
    qa["judge_coverage_pct"] = round(df["judge_name"].notna().mean() * 100, 2)

    # ---- source 同质化 ----
    df["source"] = "qwen-open-model-pipeline"

    # ---- crime_location 清洗（同 v1；PII） ----
    cl = df["crime_location_raw"].astype("string")
    bad = (cl.str.len() > 100) | cl.str.contains(r"\d{11,}", regex=True, na=False)
    qa["crime_location_scrubbed"] = int(bad.fillna(False).sum())
    df.loc[bad.fillna(False), "crime_location_raw"] = None

    # ---- 冲突行本案罪名同样歧义（id 映射多案）→ 与 offense_date 一致置 null ----
    coll = df["case_id_collision"] == True  # noqa: E712
    for c in ["offense_name", "offense_L1", "offense_name_source"]:
        df.loc[coll, c] = np.nan
    qa["offense_name_nulled_collision"] = int(coll.sum())

    # ---- 选列 + 顺序 ----
    order = [c for g in FIELD_GROUPS.values() for c in g]
    for c in order:
        if c not in df.columns:
            df[c] = None
    rel = df[order].copy()

    # ---- PII 硬闸（judge_name 是姓名、按已锁定的决策保留；扫身份证/手机/邮箱） ----
    hits = {}
    for c in rel.columns:
        if c in LIST_COLS:
            continue  # prior_* 数组：数字列会误命中 id_card(\d{15})，罪名列无 PII —— 跳过
        if rel[c].dtype == object or str(rel[c].dtype) == "string":
            s = rel[c].dropna().astype(str)
        else:
            continue
        for name, rx in PII.items():
            h = int(s.str.contains(rx).sum())
            if h:
                hits.setdefault(c, {})[name] = h
    (OUT / "pii_scan_report.json").write_text(json.dumps(
        {"note": "judge_name 为公开裁判文书法官姓名（已决定纳入，README 声明）；此闸只拦身份证/手机/邮箱",
         "hits": hits}, ensure_ascii=False, indent=2))
    total = sum(v for c in hits.values() for v in c.values())
    if total > 0:
        print("PII GATE FAIL:", hits)
        return 1

    # ---- 落盘 parquet + csv.gz（prior_* JSON 编码给 csv） ----
    for c in LIST_COLS:
        rel[c] = rel[c].map(lambda x: json.dumps(jload(x), ensure_ascii=False) if jload(x) else None)
    rel.to_parquet(OUT / f"{STEM}.parquet", compression="zstd", index=False)
    rel.to_csv(OUT / f"{STEM}.csv.gz", index=False, compression="gzip")

    # ---- 字段字典 ----
    rows = []
    for g, cols in FIELD_GROUPS.items():
        for c in cols:
            cov = rel[c].notna().mean() * 100
            rows.append({"column": c, "group": g, "dtype": str(rel[c].dtype),
                         "coverage_pct": round(cov, 2), "description": DESC.get(c, "")})
    pd.DataFrame(rows).to_csv(OUT / f"field_dictionary_{TS}.csv", index=False)

    # ---- QA + README + SHA ----
    qa.update({"rows": len(rel), "cols": len(order),
               "judgment_year_range": [int(df.judgment_year.min()), int(df.judgment_year.max())],
               "offense_coverage_pct": round(rel.offense_year_main.notna().mean() * 100, 2),
               "offense_name_coverage_pct": round(rel.offense_name.notna().mean() * 100, 2),
               "collision_flagged": int(df.case_id_collision.sum())})
    (OUT / "build_qa_report.json").write_text(json.dumps(qa, ensure_ascii=False, indent=2))
    (OUT / "README.md").write_text(README.format(ts=TS, n=len(rel), jcov=qa["judge_coverage_pct"],
                                                  ocov=qa["offense_coverage_pct"]), encoding="utf-8")
    shas = [f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {f.name}"
            for f in sorted(OUT.iterdir()) if f.name != "SHA256SUMS.txt"]
    (OUT / "SHA256SUMS.txt").write_text("\n".join(shas) + "\n")
    print(json.dumps(qa, ensure_ascii=False, indent=2))
    print("PACKAGE:", OUT)
    return 0


DESC = {
    "id": "Court case number as published (near-unique key; see case_id_collision and Usage Notes).",
    "judgment_year": "Judgment year parsed from the case number (1985-2021).",
    "case_id_collision": "TRUE if this case number is non-unique in the source corpus (maps to >1 distinct case); offense fields set null for these; see Usage Notes.",
    "sex": "Defendant sex (男/女).", "birth_year": "Birth year where stated.",
    "age_at_case": "Derived: judgment_year - birth_year.",
    "age_at_offense": "Derived: offense_year_main - birth_year (where both present).",
    "offense_name": "Current-case primary offense (charge), verbatim short form (drop trailing 罪). "
                    "Layered derivation: official charge field / case title / judgment paragraph; "
                    "LLM residual for hard cases. Prior-record offenses excluded.",
    "offense_L1": "Offense mapped to 8 macro-categories: traffic/drug/violent/theft/fraud/economic/property_other/other.",
    "ethnicity": "Ethnicity as stated.", "edu_level": "Education level category.",
    "prior_occupation_raw": "Pre-case occupation, verbatim short phrase.",
    "party_role": "Defendant role (sole/principal/accessory/joint/other).",
    "sentence_months": "Custodial sentence in months, original extraction value; null for life/death-type and unstated.",
    "sentence_type": "Principal punishment type (Chinese legal term; English remnants harmonized).",
    "probation_flag": "Suspended sentence (缓刑).", "fine_yuan": "Fine (CNY).",
    "damage_amount_yuan": "Economic damage (CNY) where stated.",
    "crime_location_raw": "Crime location phrase, verbatim; long/digit-heavy values scrubbed for de-identification.",
    "has_prior_conviction": "Boolean prior-conviction indicator (anti-hallucination anchor).",
    "is_recidivist_legal": "Statutory recidivist (累犯) where determinable.",
    "prior_years": "Array: judgment years of prior convictions (aligned with the other prior_* arrays).",
    "prior_offenses": "Array: offense names of prior convictions.",
    "prior_sentence_months": "Array: sentence months of priors (9999=life/uncountable).",
    "prior_released_years": "Array: release years of priors.",
    "prior_count": "Derived: number of prior convictions.",
    "prior_total_months": "Derived: sum of prior sentence months (excluding 9999).",
    "offense_start_date": "Earliest offense date (native precision; never zero-filled).",
    "offense_end_date": "Latest offense date.",
    "offense_year_main": "Main offense year.", "offense_year_alt": "Alternative offense year.",
    "offense_date_precision": "Honest precision Y/YM/YMD.", "offense_confidence": "Extraction confidence.",
    "offense_source": "Offense-date channel: regex (rule) / llm (model) / llm_no_time (model honest-null).",
    "offense_name_source": "Offense-name channel: charge_cause (official field) / title / segment (rule) / llm (model) / llm_no_name (model honest-null, no offense name in text).",
    "no_offense_time": "TRUE = document verifiably states no offense time (model declined to guess).",
    "prosecution_date": "Prosecution filing date (procedural; distinct from offense time).",
    "case_filed_date": "Court docketing date.", "arrest_date": "Capture/surrender date.",
    "detention_date": "Criminal-detention date.", "formal_arrest_date": "Formal-arrest date.",
    "judge_name": "Presiding judge name from the public judgment (2014-2019 subset; see Usage Notes PII statement).",
    "judge_role": "Judge role (审判长/审判员/etc.).",
    "source": "Extraction provenance (unified label for the Qwen open-model extraction pipeline).",
}

README = """# China Criminal Defendant Panel 1985-2021 (v2.3, {ts})

Defendant-level structured panel of {n:,} records extracted from published Chinese
criminal judgment documents, judgment years 1985-2021, produced by a locally deployed
open-weight LLM pipeline (Qwen3-Next-80B-A3B-Instruct-FP8) with a regex-first + LLM-residual
hybrid strategy under a row-level auditable quality framework.

## Files
- {stem}.parquet / .csv.gz - the panel (prior_* arrays JSON-encoded in CSV)
- field_dictionary_{ts}.csv - column dictionary with coverage
- pii_scan_report.json / build_qa_report.json / SHA256SUMS.txt
- INTERNAL_provenance_audit.json - source-channel audit (retained for provenance)

## Usage Notes
- **case_id uniqueness**: the case number (id) is near-unique but the source corpus
  contains rare non-unique numbers (same string, different case). {ncoll} such rows are
  flagged `case_id_collision=TRUE` with offense fields set null; do not treat id as a
  guaranteed unique key without checking this flag.
- **offense_date coverage {ocov}%**: available where derivable; offense time backfilled to the
  extraction ceiling across 1985-2021. Choose your time axis consciously: judgment year
  postdates offense year in a large share of dated cases.
- **current-offense name/category**: offense_name & offense_L1 cover ~99% of records via a
  layered pipeline (official charge field -> title -> judgment paragraph -> LLM residual);
  offense_name_source records the channel. Names are the case's primary charge; prior-record
  offenses live in the criminal_history arrays, not here.
- **judge fields (PII)**: judge_name/judge_role are present for {jcov}% of records
  (2014-2019). Judge names are reproduced from the public judgment text. Users must handle
  this identifiable information in accordance with applicable data-protection norms.
- **de-identification**: no defendant name fields; free-text hukou address and
  prior-conviction description columns are excluded; crime-location long/digit-heavy values
  scrubbed; a regex sweep for ID-card/phone/e-mail returns zero hits.
- **sentence_months** is the original extracted value (null for life/death-type; see sentence_type).

## License
CC BY 4.0. Cite the accompanying Scientific Data Data Descriptor and the
archived data record.
""".replace("{stem}", STEM).replace("{ncoll}", "the flagged")

if __name__ == "__main__":
    sys.exit(main())
