#!/usr/bin/env python3
"""14_integrate_offense_name_llm · GPU offense_name 残差并入 → merged v7

源(只读, SHA 已验的内置盘交付副本):
  results/**/*_result.jsonl        LLM 合法行(29,587 抽到 + 27,554 诚实null)
  failures_for_*/**/*_failures.jsonl  170 隔离;其中 success_bad_name(_charset) 为
    "抽对但被 24 字上限误挡的真·长罪名"→ the merge stage relaxes validation and recovers them(重跑 grounding + name-in-raw,
     仅放宽长度/字符集, grounding 硬标准不变)
并入规则: 只填 merged v6 中 offense_name 仍为 null 的 base-id(即当初 57,311 残差); source 标 'llm';
  诚实null 标 source='llm_no_name'(已 attempted, 区别于未抽)。offense_L1 同步 classify。
"""
import glob
import json
import re
import sys
from pathlib import Path

import pandas as pd

TS = "20260709134500"
__import__("sys").path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_config  # paths come from config.yaml (see config.example.yaml)
CFG = pipeline_config.load(handle_help=True, keys=('work_dir', 'offense_name_llm_results',))
ROOT = CFG.path("work_dir")  # intermediates live under <work_dir>/data_cleaning/{raw,master}
V6 = ROOT / "data_cleaning/raw/merged_extraction_1985_2021_v6_20260709115253.parquet"
DELIVERY = CFG.path("offense_name_llm_results")
OUT_DIR = ROOT / "data_cleaning/raw"
DN = re.compile(r"_d\d+$")
FW = str.maketrans("０１２３４５６７８９", "0123456789")


def norm(i):
    return str(i).replace("（", "(").replace("）", ")").translate(FW)


def compact(v):
    return re.sub(r"[\s　]+", "", "" if v is None else str(v))


# L1 映射（与 06/07 同源，移植自上游 preprocessing_scripts/07）
L1_RULES = [
    (re.compile(r"危险驾驶|交通肇事|肇事逃逸|醉酒驾驶"), "traffic"),
    (re.compile(r"贩卖毒品|运输毒品|制造毒品|走私毒品|非法持有毒品|窝藏毒品|容留他人吸毒|"
                r"引诱.{0,4}吸毒|教唆.{0,4}吸毒|非法种植毒品原植物|走私.{0,6}制毒物品|"
                r"非法买卖.{0,6}制毒物品|麻醉药品|精神药品"), "drug"),
    (re.compile(r"故意杀人|故意伤害|过失致人死亡|过失致人重伤|抢劫|强奸|强制猥亵|猥亵儿童|"
                r"奸淫幼女|绑架|拐卖妇女|拐卖儿童|拐骗儿童|非法拘禁|聚众斗殴|寻衅滋事|"
                r"暴力危及|劫持"), "violent"),
    (re.compile(r"盗窃|扒窃|入户盗窃|盗掘"), "theft"),
    (re.compile(r"诈骗|合同诈骗|信用卡诈骗|集资诈骗|金融凭证诈骗|票据诈骗|保险诈骗|"
                r"贷款诈骗|有价证券诈骗"), "fraud"),
    (re.compile(r"职务侵占|挪用资金|挪用公款|贪污|受贿|行贿|介绍贿赂|非法吸收公众存款|"
                r"非法集资|集资|非法经营|虚开|增值税|偷税|逃税|抗税|骗取出口退税|"
                r"假冒注册商标|销售假冒注册商标|侵犯著作权|侵犯商业秘密|生产、?销售伪劣|"
                r"生产、?销售假药|生产、?销售有毒|生产、?销售不符合|组织.{0,4}领导传销|"
                r"传销活动|内幕交易|操纵证券|洗钱|妨害信用卡管理|妨害对公司|信用卡管理|"
                r"非法采矿|非法占用农用地|滥伐林木|盗伐林木|非法捕捞|非法狩猎|污染环境|"
                r"珍贵、?濒危野生动物|计算机信息系统"), "economic"),
    (re.compile(r"抢夺|敲诈勒索|侵占|掩饰、?隐瞒犯罪所得|掩饰、?隐瞒|"
                r"窝藏、?转移、?收购、?销售赃物|故意毁坏财物|破坏生产经营"), "property_other"),
]


def classify_L1(name):
    if not name:
        return None
    for pat, label in L1_RULES:
        if pat.search(name):
            return label
    return "other"


def recover_long(fc, d):
    """success_bad_name(_charset): 放宽长度/字符集(<=60, 允许中文顿号/括号), grounding 与 name-in-raw 不放宽。"""
    if fc not in ("success_bad_name", "success_bad_name_charset"):
        return None
    obj = d.get("parsed_output")
    if not isinstance(obj, dict):
        return None
    name = obj.get("offense_name")
    raw = obj.get("all_offense_names_raw")
    if not isinstance(name, str) or not isinstance(raw, list) or not raw:
        return None
    if not (2 <= len(name) <= 60):
        return None
    if re.search(r"[0-9A-Za-z]", name):   # 字符集仍禁英数（真罪名不含）
        return None
    src = compact(str(d["input_case"].get("case_title") or "")
                  + str(d["input_case"].get("judgment_segment") or "")
                  + str(d["input_case"].get("facts_segment") or ""))
    for e in raw:
        if not compact(e) or compact(e) not in src:
            return None
    nc = compact(name)
    if not any(v in compact(e) for e in raw for v in (nc, nc + "罪")):
        return None
    return name


def main():
    # ---- 收集 LLM 结果 ----
    got, honest_null = {}, set()
    for f in glob.glob(str(DELIVERY / "results/**/*_result.jsonl"), recursive=True):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            nb = norm(str(d["id"]))
            if d.get("failure_reason") is None and d.get("offense_name"):
                got[nb] = d["offense_name"]
            else:
                honest_null.add(nb)
    llm_success = len(got)
    llm_null = len(honest_null)

    # ---- 回收长罪名 ----
    recovered = {}
    fc_dist = {}
    for f in glob.glob(str(DELIVERY / "failures_for_*/**/*_failures.jsonl"), recursive=True):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            fc = d.get("failure_code", "?")
            fc_dist[fc] = fc_dist.get(fc, 0) + 1
            nm = recover_long(fc, d)
            if nm:
                recovered[norm(str(d["id"]))] = nm
    got.update(recovered)   # 回收并入抽到集
    unrecovered_fail = sum(v for k, v in fc_dist.items()) - len(recovered)

    # ---- 并入 v6 → v7 ----
    df = pd.read_parquet(V6)
    df["_nb"] = df["id"].map(lambda x: norm(DN.sub("", str(x))) if isinstance(x, str) else None)
    before_cov = df["offense_name"].notna().sum()

    fill_name = df["offense_name"].isna() & df["_nb"].isin(set(got))
    idx = df.index[fill_name]
    names = df.loc[idx, "_nb"].map(got)
    df.loc[idx, "offense_name"] = names.values
    df.loc[idx, "offense_L1"] = names.map(classify_L1).values
    df.loc[idx, "offense_name_source"] = "llm"

    # 诚实null: 标 attempted
    mark_null = df["offense_name"].isna() & df["_nb"].isin(honest_null) & df["offense_name_source"].isna()
    df.loc[mark_null, "offense_name_source"] = "llm_no_name"

    df.drop(columns=["_nb"], inplace=True)
    after_cov = df["offense_name"].notna().sum()
    n = len(df)
    out = OUT_DIR / f"merged_extraction_1985_2021_v7_{TS}.parquet"
    df.to_parquet(out, compression="zstd", index=False)

    stats = {
        "ts": TS, "rows": int(n),
        "llm_success_rows": llm_success, "llm_honest_null": llm_null,
        "long_name_recovered": len(recovered),
        "fail_code_dist": fc_dist, "unrecovered_fail": int(unrecovered_fail),
        "offense_name_filled_this_step": int(fill_name.sum()),
        "offense_name_marked_no_name": int(mark_null.sum()),
        "offense_name_cov_before": int(before_cov),
        "offense_name_cov_after": int(after_cov),
        "offense_name_cov_after_pct": round(after_cov / n * 100, 2),
        "offense_L1_dist": df["offense_L1"].value_counts(dropna=False).to_dict(),
        "offense_name_source_dist": df["offense_name_source"].value_counts(dropna=False).to_dict(),
    }
    (OUT_DIR / f"v7_offense_name_stats_{TS}.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2, default=str))
    print(json.dumps({k: v for k, v in stats.items()
                      if k not in ("offense_L1_dist", "offense_name_source_dist")},
                     ensure_ascii=False, indent=1, default=str))
    print("L1:", stats["offense_L1_dist"])
    print("src:", stats["offense_name_source_dist"])
    print("saved:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
