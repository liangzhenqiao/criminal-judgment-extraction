#!/usr/bin/env python3
"""11_derive_offense_name · 本案罪名三层瀑布派生（本地 99% + LLM 残差收集）

层1 案由列(charge_cause 结构化字段, 零抽取) → 层2 标题词典锚定 → 层3 判决/事实段句式框+词典锚定
词典 = 语料自建（charge_cause 高频值, 即官方案由词汇表——刑法法定罪名的实际使用形态）
残差 → 收集 {id, case_title, judgment_segment, facts_segment} 供 GPU offense_name_v1.0 LLM 任务

源(只读): case_events_segmented(2014-2021) + local_seg_1985_2013(本地重建)
输出: data_cleaning/raw/offense_name_derived_<TS>.parquet (id, offense_name, offense_L1, offense_name_source)
      data_cleaning/raw/offense_name_residual_<TS>/ (LLM 输入池 + 统计)
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

import pandas as pd
import pyarrow.dataset as ds
import pyarrow.compute as pc

TS = "20260709115253"
__import__("sys").path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_config  # paths come from config.yaml (see config.example.yaml)
CFG = pipeline_config.load(handle_help=True, keys=('work_dir', 'segmented_2014_2021', 'segmented_1985_2013',))
ROOT = CFG.path("work_dir")  # intermediates live under <work_dir>/data_cleaning/{raw,master}
V5 = ROOT / "data_cleaning/raw/merged_extraction_1985_2021_v5_20260709092811.parquet"
SEG_MAIN = str(CFG.path("segmented_2014_2021"))
SEG_EARLY = str(CFG.path("segmented_1985_2013"))
OUT_DIR = ROOT / "data_cleaning/raw"
RES_DIR = OUT_DIR / f"offense_name_residual_{TS}"

DN = re.compile(r"_d\d+$")
FW = str.maketrans("０１２３４５６７８９", "0123456789")


def norm(i):
    return str(i).replace("（", "(").replace("）", ")").translate(FW)


# ---- 8 类 L1 映射（移植自上游 preprocessing_scripts/07） ----
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
                r"非法采矿|非法占用农用地|滥伐林木|盗伐林木|非法捕捞|非法狩猎|污染环境"), "economic"),
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


def clean_cc(v):
    """案由值清洗：去空白/尾缀'罪'/明显非罪名。"""
    if not v:
        return None
    s = str(v).strip().strip("、;；,，")
    s = re.sub(r"罪$", "", s)
    if not (2 <= len(s) <= 24):
        return None
    if re.search(r"[0-9A-Za-z]|判决|裁定|纠纷|合同$|上诉|申请", s):
        return None
    return s


def iter_sources(columns):
    for src in (SEG_MAIN, SEG_EARLY):
        d = ds.dataset(src, format="parquet", partitioning="hive")
        for frag in d.get_fragments():
            t = frag.to_table(columns=columns)
            yield [t.column(c).to_pylist() for c in columns]


def main():
    RES_DIR.mkdir(parents=True, exist_ok=True)
    # ---- 数据集 base-id 全集 ----
    dfm = pd.read_parquet(V5, columns=["id", "case_id_collision"])
    dfm = dfm[dfm["id"].notna()].copy()
    dfm["base"] = dfm["id"].map(lambda x: DN.sub("", str(x)) if isinstance(x, str) else None)
    dfm = dfm[dfm["base"].notna()].copy()
    dfm["nb"] = dfm["base"].map(norm)
    dataset_ids = set(dfm["nb"])
    collision_nb = set(dfm.loc[dfm["case_id_collision"] == True, "nb"])  # noqa: E712
    print(f"dataset base-ids={len(dataset_ids):,}  collision={len(collision_nb):,}", flush=True)

    # ---- Pass A: 案由词典 + 每 id 的 charge_cause / title ----
    dict_counter = Counter()
    cc_map, title_map = {}, {}
    scanned = 0
    for cids, ccs, titles in iter_sources(["case_id", "charge_cause", "case_title"]):
        for cid, cc, ti in zip(cids, ccs, titles):
            scanned += 1
            c = clean_cc(cc)
            if c:
                dict_counter[c] += 1
            if not cid:
                continue
            nb = norm(cid)
            if nb in dataset_ids:
                if c and nb not in cc_map:
                    cc_map[nb] = c
                if ti and str(ti).strip() and nb not in title_map:
                    title_map[nb] = str(ti)
    print(f"PassA scanned={scanned:,}  cc_map={len(cc_map):,}  title_map={len(title_map):,}", flush=True)

    # 词典：高频案由（官方词汇表实际形态），长优先
    vocab = [w for w, c in dict_counter.items() if c >= 5]
    vocab.sort(key=len, reverse=True)
    print(f"vocab={len(vocab):,} (freq>=5)", flush=True)
    VOCAB_RE = re.compile("(" + "|".join(re.escape(w) for w in vocab) + ")")

    # ---- 层1+层2 ----
    result = {}     # nb -> (name, source)
    for nb in dataset_ids:
        c = cc_map.get(nb)
        if c:
            result[nb] = (c, "charge_cause")
    unresolved = dataset_ids - set(result)
    print(f"after L1: resolved={len(result):,}  unresolved={len(unresolved):,}", flush=True)

    for nb in list(unresolved):
        ti = title_map.get(nb)
        if not ti:
            continue
        m = VOCAB_RE.search(ti)
        if m:
            result[nb] = (m.group(1), "title")
            unresolved.discard(nb)
    print(f"after L2: resolved={len(result):,}  unresolved={len(unresolved):,}", flush=True)

    # ---- 层3: 只为 unresolved 收集段落并匹配 ----
    seg_map = {}
    for cids, jsegs, fsegs in iter_sources(["case_id", "judgment_segment", "facts_segment"]):
        for cid, jg, fs in zip(cids, jsegs, fsegs):
            if not cid:
                continue
            nb = norm(cid)
            if nb in unresolved and nb not in seg_map:
                seg_map[nb] = ((jg or ""), (fs or ""))
    FRAME = re.compile(r"(?:犯|以|系)(" + "|".join(re.escape(w) for w in vocab[:2000]) + r")罪")
    for nb in list(unresolved):
        jg, fs = seg_map.get(nb, ("", ""))
        text = jg + " " + fs
        if not text.strip():
            continue
        m = FRAME.search(text) or VOCAB_RE.search(jg) or VOCAB_RE.search(fs[:600])
        if m:
            result[nb] = (m.group(1), "segment")
            unresolved.discard(nb)
    print(f"after L3: resolved={len(result):,}  unresolved={len(unresolved):,}", flush=True)

    # ---- 落盘派生表 ----
    rows = []
    for nb in dataset_ids:
        nm, src = result.get(nb, (None, None))
        rows.append({"nb": nb, "offense_name": nm, "offense_L1": classify_L1(nm),
                     "offense_name_source": src})
    der = pd.DataFrame(rows)
    out_parquet = OUT_DIR / f"offense_name_derived_{TS}.parquet"
    der.to_parquet(out_parquet, compression="zstd", index=False)

    # ---- 残差池（LLM 输入）：非冲突、有文本可读 ----
    resid = []
    no_text = 0
    for nb in unresolved:
        if nb in collision_nb:
            continue
        ti = title_map.get(nb, "")
        jg, fs = seg_map.get(nb, ("", ""))
        if not (ti.strip() or jg.strip() or fs.strip()):
            no_text += 1
            continue
        resid.append({"id": nb, "case_title": ti[:200],
                      "judgment_segment": jg[:1000], "facts_segment": fs[:800]})
    with open(RES_DIR / "residual_pool.jsonl", "w", encoding="utf-8") as f:
        for r in resid:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    stats = {
        "ts": TS, "dataset_base_ids": len(dataset_ids),
        "L1_charge_cause": int((der.offense_name_source == "charge_cause").sum()),
        "L2_title": int((der.offense_name_source == "title").sum()),
        "L3_segment": int((der.offense_name_source == "segment").sum()),
        "resolved_total_pct": round(der.offense_name.notna().mean() * 100, 2),
        "unresolved": len(unresolved),
        "residual_llm_pool": len(resid),
        "residual_no_text": no_text,
        "residual_collision_skipped": len(unresolved & collision_nb),
        "vocab_size": len(vocab),
        "L1_dist": der.offense_L1.value_counts(dropna=False).to_dict(),
    }
    (RES_DIR / "derive_stats.json").write_text(json.dumps(
        {k: (str(v) if isinstance(v, dict) else v) if k == "L1_dist" else v
         for k, v in stats.items()}, ensure_ascii=False, indent=2, default=str))
    print(json.dumps({k: v for k, v in stats.items() if k != "L1_dist"}, ensure_ascii=False, indent=1))
    print("L1_dist:", stats["L1_dist"])
    print("saved:", out_parquet, RES_DIR, sep="\n  ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
