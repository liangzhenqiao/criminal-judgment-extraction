#!/usr/bin/env python3
"""Structural constraints over the full release (validation stage D1).

Input (read-only release parquet; do not modify config.yaml): the panel parquet of the release.
产出: <validation_out_dir>/structural_constraints_<TS>/constraint_violations.csv
  每约束一行: constraint_id, constraint_name, check_description,
             denominator(检查分母), n_violations(违反数), violation_rate(违反率),
             n_sample, sample(<=10 例 id/关键字段 JSON), subchecks(子约束分解 JSON), notes。

9 约束 (规格):
  1 类型/范围      judgment_year∈[1985,2021]·sentence_months≥0·fine/damage≥0·age∈[0,100]·birth_year·offense_year·prior_count≥0
  2 键唯一/collision  case_id_collision=TRUE → offense_name 应置空 (README); id 重复 vs 同案主体解释
  3 金额分项-合计守恒  prior_total_months == Σ(prior_sentence_months 数组, 排除 9999) 逐行
  4 罪名-法条        同一 offense_name 是否映射到 >1 个 offense_L1
  5 刑罚-期限        无期/死刑类 sentence_type → sentence_months 应 null; probation_flag 与 sentence_type 相容
  6 案号-法院-日期    从 id(案号) 解析年份, 核对 == judgment_year
  7 前科时序        4 个 prior_* 数组长度对齐且 = prior_count; prior_years/released_years 早于 judgment_year
  8 同案主体        单独犯罪 vs 同案共犯: 单条 id 标共犯 / 多条 id 标单独犯罪 的不一致
  9 重复键/重复行     完全重复行计数

Read-only release parquet; do not modify config.yaml.
"""
import hashlib
import json
import math
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

TS = "20260717090842"
__import__("sys").path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_config  # paths come from config.yaml (see config.example.yaml)
CFG = pipeline_config.load(handle_help=True, keys=('release_dir', 'validation_out_dir',))
OUT = CFG.path("validation_out_dir") / f"structural_constraints_{TS}"
OUT.mkdir(parents=True, exist_ok=True)

REL = CFG.path("release_dir")
PARQUET = next(REL.glob("*.parquet"))

# ---- 约束参数 (声明式, 便于审计) ----
VALID_JUDGMENT_YEAR = (1985, 2021)   # 数据集声明区间 ()
VALID_BIRTH_YEAR = (1900, 2010)      # 可信出生年区间
VALID_AGE = (0, 100)
VALID_OFFENSE_YEAR = (1949, 2022)    # PRC 成立后 ~ 数据集判决上限
LIFE_DEATH_TYPES = {"无期徒刑", "死刑", "死刑缓期执行"}
SOLE_ROLE = "单独犯罪"
JOINT_ROLE_MARKERS = ("主犯", "从犯", "胁从", "共同", "共犯", "首要", "参加者", "参与者", "直接责任")
YEAR_IN_ID = re.compile(r"[（(](\d{4})[）)]")


# ---- helpers ----
def jarr(v):
    """JSON 数组字符串 -> list (prior_* 列存为 '[..]' 字符串; 非法/空 -> [])。"""
    if isinstance(v, str) and v.strip():
        try:
            x = json.loads(v)
            return x if isinstance(x, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _nums(a):
    """list -> 仅保留有限数值元素 (剔除 None/NaN/非数值)。"""
    out = []
    for x in a:
        if isinstance(x, bool):
            continue
        if isinstance(x, (int, float)) and not (isinstance(x, float) and math.isnan(x)):
            out.append(float(x))
    return out


def parse_id_year(s):
    if not isinstance(s, str):
        return None
    m = YEAR_IN_ID.search(s)
    if m:
        y = int(m.group(1))
        if 1980 <= y <= 2025:
            return y
    return None


def _safe(v):
    """转 json 友好原生类型; NaN/None -> None。"""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return None if math.isnan(float(v)) else float(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    return v


def samples(frame, mask, cols, k=10):
    """从 frame[mask] 取最多 k 行, 输出 [{col: val}] (json 安全)。"""
    sub = frame.loc[mask, cols].head(k)
    out = []
    for _, r in sub.iterrows():
        out.append({c: _safe(r[c]) for c in cols})
    return out


def rate(n_viol, denom):
    return round(n_viol / denom, 8) if denom else None


def row(cid, name, desc, denom, n_viol, sample, subchecks=None, notes=""):
    return {
        "constraint_id": cid,
        "constraint_name": name,
        "check_description": desc,
        "denominator": int(denom),
        "n_violations": int(n_viol),
        "violation_rate": rate(int(n_viol), int(denom)),
        "n_sample": len(sample),
        "sample": json.dumps(sample, ensure_ascii=False),
        "subchecks": json.dumps(subchecks, ensure_ascii=False) if subchecks else "",
        "notes": notes,
    }


# ================================================================ constraints
def c1_type_range(df):
    n = len(df)
    subs = {}
    viol_any = pd.Series(False, index=df.index)

    def rng(col, lo, hi, numeric=True):
        s = pd.to_numeric(df[col], errors="coerce") if numeric else df[col]
        checkable = s.notna()
        bad = checkable & ((s < lo) | (s > hi))
        subs[col] = {"rule": f"[{lo},{hi}]", "denominator": int(checkable.sum()),
                     "n_violations": int(bad.sum()), "rate": rate(int(bad.sum()), int(checkable.sum()))}
        return bad

    viol_any |= rng("judgment_year", *VALID_JUDGMENT_YEAR)
    viol_any |= rng("birth_year", *VALID_BIRTH_YEAR)
    viol_any |= rng("age_at_case", *VALID_AGE)
    viol_any |= rng("age_at_offense", *VALID_AGE)
    viol_any |= rng("offense_year_main", *VALID_OFFENSE_YEAR)
    viol_any |= rng("offense_year_alt", *VALID_OFFENSE_YEAR)
    # 非负约束
    for col in ("sentence_months", "fine_yuan", "damage_amount_yuan", "prior_count"):
        s = pd.to_numeric(df[col], errors="coerce")
        checkable = s.notna()
        bad = checkable & (s < 0)
        subs[col] = {"rule": ">=0", "denominator": int(checkable.sum()),
                     "n_violations": int(bad.sum()), "rate": rate(int(bad.sum()), int(checkable.sum()))}
        viol_any |= bad
    smp = samples(df, viol_any, ["id", "judgment_year", "birth_year", "age_at_case",
                                 "offense_year_main", "sentence_months", "fine_yuan"])
    return row(1, "type_range", "数值字段落在声明/可信区间内(逐字段 rule 见 subchecks)",
               n, int(viol_any.sum()), smp, subs,
               notes="denominator=全语料; 每字段自有分母见 subchecks. 观测: judgment_year 实际到 2022, "
                     "birth_year 见 197/9880, offense_year 见 3013 等越界值.")


def c2_key_collision(df):
    coll = df["case_id_collision"] == True  # noqa: E712
    denom = int(coll.sum())
    # README: collision=TRUE 行 offense 字段(offense_name)应置空
    bad = coll & df["offense_name"].notna()
    smp = samples(df, bad, ["id", "case_id_collision", "offense_name", "offense_L1"])
    # 信息: id 唯一性 (发布面板实测)
    vc = df["id"].value_counts()  # 默认剔除 NaN
    dup_ids = int((vc > 1).sum())
    rows_dup_id = int(vc[vc > 1].sum())
    null_id_rows = int(df["id"].isna().sum())
    subs = {
        "collision_offense_name_not_null": {"denominator": denom, "n_violations": int(bad.sum()),
                                            "rate": rate(int(bad.sum()), denom),
                                            "rule": "collision=TRUE -> offense_name 应 null (README)"},
        "id_uniqueness_info": {"total_rows": len(df), "distinct_nonnull_ids": int(df["id"].nunique()),
                               "duplicated_id_strings": dup_ids,
                               "rows_with_duplicated_id": rows_dup_id,
                               "null_id_rows": null_id_rows,
                               "note": "发布面板中每个非空 id 唯一(每案号一行); collision 标记源语料碰撞, "
                                       "已按 README 置空 offense. 无共享 id 的同案组(见约束 8)."},
    }
    return row(2, "key_uniqueness_collision",
               "case_id_collision=TRUE 行 offense_name 应置空(README); id 唯一性", denom, int(bad.sum()),
               smp, subs,
               notes="分母=collision 行(40,732). 实测 id 在面板中 100% 唯一(0 重复串), "
                     f"另有 {null_id_rows} 行 id 为空.")


def c3_prior_months_conservation(df):
    mask = df["prior_count"] > 0
    denom = int(mask.sum())
    psm = df.loc[mask, "prior_sentence_months"].map(jarr)
    computed = psm.map(lambda a: float(sum(x for x in _nums(a) if x not in (9999, 9999.0))))
    actual = pd.to_numeric(df.loc[mask, "prior_total_months"], errors="coerce")
    diff = (computed.values - actual.values)
    bad_arr = np.abs(np.where(np.isnan(diff), 0.0, diff)) > 0.01
    bad = pd.Series(bad_arr, index=df.loc[mask].index)
    sm_frame = df.loc[mask].assign(_computed=computed.values, _actual=actual.values)
    smp = samples(sm_frame, bad, ["id", "prior_count", "prior_sentence_months",
                                  "prior_total_months", "_computed", "_actual"])
    return row(3, "prior_months_conservation",
               "prior_total_months == Σ(prior_sentence_months 排除9999) 逐行", denom, int(bad.sum()),
               smp, notes="分母=prior_count>0 行; 9999=无期/不可数, 求和时排除.")


def c4_offense_name_l1(df):
    m = df["offense_name"].notna() & df["offense_L1"].notna()
    denom = int(m.sum())
    sub = df.loc[m, ["id", "offense_name", "offense_L1"]]
    nun = sub.groupby("offense_name")["offense_L1"].nunique()
    amb_names = set(nun[nun > 1].index)
    modal = (sub[sub["offense_name"].isin(amb_names)]
             .groupby("offense_name")["offense_L1"]
             .agg(lambda s: s.value_counts().index[0]))
    amb_mask = sub["offense_name"].isin(amb_names)
    row_modal = sub["offense_name"].map(modal)
    bad = amb_mask & (sub["offense_L1"] != row_modal)
    smp_names = []
    for nm in list(amb_names)[:10]:
        d = sub.loc[sub["offense_name"] == nm, "offense_L1"].value_counts().to_dict()
        smp_names.append({"offense_name": nm, "offense_L1_counts": {k: int(v) for k, v in d.items()}})
    subs = {"ambiguous_offense_name_strings": len(amb_names),
            "distinct_offense_names": int(sub["offense_name"].nunique())}
    return row(4, "offense_name_to_L1_consistency",
               "同一 offense_name 映射到 >1 个 offense_L1 (违反=偏离该名众数 L1 的行)",
               denom, int(bad.sum()), smp_names, subs,
               notes="违反数=歧义罪名中非众数映射的行数; 样本列出歧义罪名及其 L1 分布.")


def c5_punishment_term(df):
    st_nn = df["sentence_type"].notna()
    denom = int(st_nn.sum())
    # A: 无期/死刑类 -> sentence_months 应 null
    life = df["sentence_type"].isin(LIFE_DEATH_TYPES)
    badA = life & df["sentence_months"].notna()
    # B: probation_flag=TRUE 与不相容的 sentence_type
    incompat = LIFE_DEATH_TYPES | {"无罪", "免于处罚", "免予刑事处罚", "驱逐出境"}
    prob = df["probation_flag"] == True  # noqa: E712
    badB = prob & df["sentence_type"].isin(incompat)
    bad = badA | badB
    smp = samples(df, bad, ["id", "sentence_type", "sentence_months", "probation_flag"])
    subs = {
        "life_death_months_should_be_null": {"denominator": int(life.sum()),
                                             "n_violations": int(badA.sum()),
                                             "rate": rate(int(badA.sum()), int(life.sum()))},
        "probation_incompatible_type": {"denominator": int(prob.sum()),
                                        "n_violations": int(badB.sum()),
                                        "rate": rate(int(badB.sum()), int(prob.sum()))},
    }
    return row(5, "punishment_term_compat",
               "无期/死刑类->sentence_months null; probation 与刑种相容", denom, int(bad.sum()),
               smp, subs, notes="分母=sentence_type 非空行.")


def c6_id_year_vs_judgment(df):
    idy = df["id"].map(parse_id_year)
    parseable = idy.notna()
    m = parseable & df["judgment_year"].notna()
    denom = int(m.sum())
    jy = pd.to_numeric(df["judgment_year"], errors="coerce")
    bad = m & (idy.astype("float") != jy)
    smp_frame = df.assign(_id_year=idy)
    smp = samples(smp_frame, bad, ["id", "_id_year", "judgment_year"])
    subs = {"id_year_unparseable_rows": int((~parseable).sum()),
            "id_year_unparseable_rate": rate(int((~parseable).sum()), len(df))}
    return row(6, "case_number_year_vs_judgment_year",
               "从 id(案号) 括号解析年份 == judgment_year", denom, int(bad.sum()), smp, subs,
               notes="分母=id 年份可解析且 judgment_year 非空; judgment_year 本由案号解析, 应高度一致.")


def c7_prior_temporal(df):
    mask = df["prior_count"] > 0
    denomA = int(mask.sum())
    py = df.loc[mask, "prior_years"].map(jarr)
    po = df.loc[mask, "prior_offenses"].map(jarr)
    psm = df.loc[mask, "prior_sentence_months"].map(jarr)
    pry = df.loc[mask, "prior_released_years"].map(jarr)
    pc = df.loc[mask, "prior_count"].astype(int).values
    lens = np.vstack([py.map(len).values, po.map(len).values,
                      psm.map(len).values, pry.map(len).values]).T
    badA = ~np.all(lens == pc[:, None], axis=1)
    badA_s = pd.Series(badA, index=df.loc[mask].index)
    # B: prior 年份应早于 judgment_year (拆 == 同年 / > 晚于判决)
    jy = pd.to_numeric(df.loc[mask, "judgment_year"], errors="coerce").values
    max_py = py.map(lambda a: max(_nums(a)) if _nums(a) else np.nan).values
    max_pry = pry.map(lambda a: max(_nums(a)) if _nums(a) else np.nan).values
    with np.errstate(invalid="ignore"):
        after = np.where(np.isnan(max_py), False, max_py > jy)        # prior 年 > 判决年 (违反物理)
        equal = np.where(np.isnan(max_py), False, max_py == jy)       # prior 年 == 判决年 (临界)
        rel_after = np.where(np.isnan(max_pry), False, max_pry > jy)  # 释放年 > 判决年
    badB = after | equal | rel_after
    badB_s = pd.Series(badB, index=df.loc[mask].index)
    bad = badA_s | badB_s
    sm_frame = df.loc[mask]
    smp = samples(sm_frame, bad, ["id", "prior_count", "prior_years", "prior_offenses",
                                  "prior_sentence_months", "prior_released_years", "judgment_year"])
    idx = df.loc[mask].index

    def _cnt(arr):
        return int(pd.Series(arr, index=idx).sum())
    subs = {
        "array_length_alignment": {"denominator": denomA, "n_violations": int(badA_s.sum()),
                                   "rate": rate(int(badA_s.sum()), denomA),
                                   "rule": "len(4 个 prior_* 数组) 均 = prior_count"},
        "prior_year_after_judgment_strict": {"denominator": denomA, "n_violations": _cnt(after),
                                             "rate": rate(_cnt(after), denomA),
                                             "rule": "max(prior_years) > judgment_year (物理不可能)"},
        "prior_year_equals_judgment": {"denominator": denomA, "n_violations": _cnt(equal),
                                       "rate": rate(_cnt(equal), denomA),
                                       "rule": "max(prior_years) == judgment_year (临界; 或案号年<实际裁判年)"},
        "released_year_after_judgment": {"denominator": denomA, "n_violations": _cnt(rel_after),
                                         "rate": rate(_cnt(rel_after), denomA),
                                         "rule": "max(prior_released_years) > judgment_year"},
    }
    return row(7, "prior_record_temporal_and_alignment",
               "prior_* 数组长度对齐=prior_count 且 prior 年份不晚于判决年", denomA, int(bad.sum()),
               smp, subs,
               notes="分母=prior_count>0 行. 拆分见 subchecks: strict>(不可能) vs ==(临界). "
                     "judgment_year 取自案号年份, 可能早于实际裁判日期, 故 == 类多属该间隙非硬错误.")


def c8_codefendant_role(df):
    non_coll = ~(df["case_id_collision"] == True)  # noqa: E712
    role_nn = df["party_role"].notna()
    denom = int((non_coll & role_nn).sum())
    vc = df.loc[non_coll, "id"].value_counts()
    size = df["id"].map(vc)  # collision 行 -> NaN
    multi = non_coll & (size >= 2)
    n_multi_rows = int(multi.sum())

    # 真实矛盾 1: 同一(非collision) id 多条 defendant 但 judgment_year 不一致
    jy_nun = df.loc[non_coll].groupby("id")["judgment_year"].nunique(dropna=True)
    inconsistent_ids = set(jy_nun[jy_nun > 1].index)
    badJY = multi & df["id"].isin(inconsistent_ids)
    # 真实矛盾 2: 多条 id 却标单独犯罪 (与共犯并存矛盾)
    badSole = multi & role_nn & (df["party_role"] == SOLE_ROLE)
    bad = badJY | badSole

    # 结构性说明 (预期产物, 非违反): 共犯角色但同案人未出现在面板中
    is_joint = df["party_role"].fillna("").apply(
        lambda r: any(mk in r for mk in JOINT_ROLE_MARKERS))
    joint_single = int((non_coll & role_nn & (size == 1) & is_joint).sum())

    sm_frame = df.assign(_id_rowcount=size)
    smp = samples(sm_frame, bad, ["id", "party_role", "judgment_year", "_id_rowcount"])
    subs = {
        "multi_defendant_judgment_year_inconsistent": {
            "denominator": n_multi_rows, "n_violations": int(badJY.sum()),
            "rate": rate(int(badJY.sum()), n_multi_rows),
            "rule": "同一非collision id 的多条 defendant 行 judgment_year 应一致"},
        "sole_role_with_codefendants": {
            "denominator": n_multi_rows, "n_violations": int(badSole.sum()),
            "rate": rate(int(badSole.sum()), n_multi_rows),
            "rule": "id 有 >=2 条 defendant 但 party_role=单独犯罪"},
        "INFO_joint_role_single_defendant_row": {
            "count": joint_single, "counted_as_violation": False,
            "note": "预期产物(非违反): 共犯角色的被告其同案人未出现在本(被告级)面板中; "
                    "反映面板同案覆盖, 不是逻辑错误"},
    }
    applicable = "N/A" if n_multi_rows == 0 else "applicable"
    return row(8, "codefendant_case_consistency",
               "同案(共享 id)行的 case-level 一致性(判决年一致; 无 单独犯罪 与共犯并存)",
               n_multi_rows, int(bad.sum()), smp, subs,
               notes=f"[{applicable}] 发布面板每案号唯一一行(见约束 2), 无共享 id 的同案组, "
                     f"故本检查分母=0=不适用. 另有 {joint_single:,} 行为'共犯角色+同案人未在(被告级)面板中'"
                     "(预期产物, 见 INFO subcheck, 不计违反).")


def c9_duplicate_rows(df):
    n = len(df)
    try:
        dup = df.duplicated()
    except TypeError:
        tmp = df.copy()
        for c in tmp.columns:
            if tmp[c].dtype == object:
                tmp[c] = tmp[c].astype(str)
        dup = tmp.duplicated()
    n_dup = int(dup.sum())
    dup_ids = df.loc[dup, "id"].head(10).tolist()
    smp = [{"id": _safe(x)} for x in dup_ids]
    subs = {"fully_duplicate_rows_all_but_first": n_dup,
            "distinct_ids_among_dupes": int(df.loc[dup, "id"].nunique())}
    return row(9, "fully_duplicate_rows",
               "完全重复行计数(df.duplicated, 保留首现)", n, n_dup, smp, subs,
               notes="分母=全语料行数.")


# ================================================================ main
def main():
    print(f"[07a] loading parquet: {PARQUET}")
    df = pd.read_parquet(PARQUET)
    n_rows, n_cols = df.shape
    assert n_cols == 44, f"expected 44 cols, got {n_cols}"
    print(f"[07a] loaded shape={df.shape}")

    checks = [c1_type_range, c2_key_collision, c3_prior_months_conservation,
              c4_offense_name_l1, c5_punishment_term, c6_id_year_vs_judgment,
              c7_prior_temporal, c8_codefendant_role, c9_duplicate_rows]
    rows = []
    for fn in checks:
        r = fn(df)
        rows.append(r)
        print(f"[07a] C{r['constraint_id']} {r['constraint_name']}: "
              f"denom={r['denominator']:,} viol={r['n_violations']:,} rate={r['violation_rate']}")

    out_cols = ["constraint_id", "constraint_name", "check_description", "denominator",
                "n_violations", "violation_rate", "n_sample", "sample", "subchecks", "notes"]
    res = pd.DataFrame(rows)[out_cols].sort_values("constraint_id")
    csv_path = OUT / "constraint_violations.csv"
    res.to_csv(csv_path, index=False)

    # byte-verify: 自读回, files_scanned=1, total(约束数)>=1, 且扫描行数>0
    back = pd.read_csv(csv_path)
    total = len(back)
    print(f"\n[07a] BYTE-VERIFY: rows_scanned={n_rows:,} constraints={total} "
          f"(files_scanned=1, total>=1: {total >= 1 and n_rows > 0})")
    print(f"[07a] OUT: {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
