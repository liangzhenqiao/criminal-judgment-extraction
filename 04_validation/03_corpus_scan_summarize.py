"""Summarise the single-pass scan output: per-id match rate, defendant-count distribution, yearly count reconciliation, cross-checks against the hard-case batch.

用法（项目根）：
  uv run --no-project --with polars python -I 04_validation/03_corpus_scan_summarize.py --dir <scan 输出目录> --out <md 路径>
"""
import argparse, csv, json
from pathlib import Path

import polars as pl

__import__("sys").path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_config  # paths come from config.yaml (see config.example.yaml)
CFG = pipeline_config.load(handle_help=False, keys=('release_dir', 'corpus_doc_counts_csv', 'annotation_dir',))

PARQUET_NAMES = ("china_criminal_judgment_records_1985_2021.parquet", "china_criminal_defendant_panel_1985_2021.parquet")  # current release name, then the former one


def _pick(d: Path) -> Path:
    for name in PARQUET_NAMES:
        if (d / name).exists():
            return d / name
    return d / PARQUET_NAMES[0]


def release_path():
    return _pick(CFG.path("release_dir"))


def pct(a, b):
    return f"{100 * a / b:.2f}%" if b else "—"


def md_table(df):
    cols = df.columns
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    def fmt(c, v):
        if v is None:
            return ""
        if c in ("year", "judgment_year"):
            return str(v)
        if isinstance(v, int):
            return f"{v:,}"
        return f"{v:.3f}" if isinstance(v, float) else str(v)
    for r in df.iter_rows():
        lines.append("| " + " | ".join(fmt(c, v) for c, v in zip(cols, r)) + " |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    d = Path(a.dir)
    meta = json.load(open(d / "scan_meta.json"))
    rel = pl.read_parquet(release_path(), columns=["id", "judgment_year"])
    u = pl.read_csv(d / "id_to_cjo_url.csv", schema_overrides={"id": pl.Utf8, "cjo_url": pl.Utf8, "source_year": pl.Int64})
    nd = pl.read_csv(d / "defendants_per_judgment.csv", schema_overrides={"id": pl.Utf8})
    fl = pl.read_csv(d / "flow_counts_by_year.csv")
    out = []
    P = out.append

    # ---------------- V4 ----------------
    n_rel = rel.height
    n_id = u.height
    m = u.filter(pl.col("n_hits") > 0)
    multi = u.filter(pl.col("n_hits") > 1)
    P("# V2/V3/V4 单遍扫描汇总\n")
    P(f"扫描年份：{meta['years_done'][0]}–{meta['years_done'][-1]}（完成 {len(meta['years_done'])} 个）；失败年份：{meta['years_failed'] or '无'}\n")
    P("## V4 案号 → 原始链接\n")
    P(f"- 释出行数 {n_rel:,}；其中 id 为空 {n_rel - n_id:,} 行，不参与匹配。")
    P(f"- 匹配到 ≥1 篇：{m.height:,}（{pct(m.height, n_rel)}，分母为释出 {n_rel:,}）")
    P(f"- 其中多篇命中（n_hits>1）：{multi.height:,}（{pct(multi.height, n_rel)}）")
    P(f"- 未匹配：{n_rel - m.height:,}（含空 id {n_rel - n_id}）（{pct(n_rel - m.height, n_rel)}）")
    url_ok = u.filter(pl.col("cjo_url").is_not_null() & (pl.col("cjo_url").str.strip_chars() != ""))
    gap = m.height - url_ok.height
    gm = m.filter(pl.col("cjo_url").is_null() | (pl.col("cjo_url").str.strip_chars() == ""))
    P(f"- **cjo_url 非空：{url_ok.height:,}（{pct(url_ok.height, n_rel)}）**；cjo_url 为空：{n_id - url_ok.height:,} = 无命中 {n_id - m.height:,} + 有命中但选中篇 `原始链接` 字段在原始语料中为空 {gap:,}（其中 n_hits=1 的 {gm.filter(pl.col('n_hits') == 1).height:,}、n_hits>1 的 {gm.filter(pl.col('n_hits') > 1).height:,}）。")
    P(f"- 对账：释出 {n_rel:,} = 空 id {n_rel - n_id} + 无命中 {n_id - m.height:,} + 有命中无链接 {gap:,} + 有链接 {url_ok.height:,}。CSV 共 {n_id:,} 行（不含空 id），id 无重复（唯一值 {u['id'].n_unique():,}）。")
    P("- 注意：n_hits>1 的无链接 id，选择规则只看标题和审理程序、不看链接是否为空；同案号的其他篇是否有链接，需重扫才能确认，本次未做。")
    P("- 多篇命中时 `cjo_url` 取选中篇：排序键（标题不含“判决书”，审理程序≠一审），键相同取扫描顺序第一篇。`n_hits` 计入全部案件类型的同案号文书。\n")
    hd = m.group_by("n_hits").len().sort("n_hits")
    hd = hd.with_columns(pl.when(pl.col("n_hits") >= 5).then(pl.lit("5+")).otherwise(pl.col("n_hits").cast(pl.Utf8)).alias("命中篇数")).group_by("命中篇数").agg(pl.col("len").sum().alias("id 数")).sort("命中篇数")
    P(md_table(hd) + "\n")
    j = rel.join(u, on="id", how="left").with_columns(pl.col("n_hits").fill_null(0))
    by = j.group_by("judgment_year").agg(pl.len().alias("释出"), (pl.col("n_hits") > 0).sum().alias("匹配"), (pl.col("n_hits") > 1).sum().alias("多命中"),
                                         (pl.col("source_year") == pl.col("judgment_year").cast(pl.Int64)).sum().alias("选中篇所在zip=判决年")).sort("judgment_year")
    by = by.with_columns((pl.col("匹配") / pl.col("释出")).alias("匹配率"))
    P("### 按释出 judgment_year\n")
    P(md_table(by.with_columns(pl.col("judgment_year").cast(pl.Int64))) + "\n")

    # ---------------- V3 ----------------
    P("## V3 每篇文书点名的被告人数\n")
    P("口径：选中篇全文，调用 `03_build_pack.py` (validation bundle) 的 `roster()`（同一函数导入，正则未改写）；只计“起诉书指控/提起公诉/经审理查明/公诉机关指控/检察院指控”之前前 2,500 字中的去重被告名。0 表示正则没认出名单。\n")
    x = nd.join(rel, on="id", how="left")
    s = x["n_defendants"]
    n = x.height
    k0, k1, k2, k3 = [(s == v).sum() for v in (0, 1, 2)] + [(s >= 3).sum()]
    P(f"- 覆盖 id：{n:,}（= V4 匹配数）")
    P(f"- 均值 {s.mean():.3f}；中位数 {s.median():.0f}；含 0 的均值。排除 0 后均值 {s.filter(s > 0).mean():.3f}、中位数 {s.filter(s > 0).median():.0f}")
    P(f"- 0 人（未识别）{k0:,}（{pct(k0, n)}）· 1 人 {k1:,}（{pct(k1, n)}）· 2 人 {k2:,}（{pct(k2, n)}）· 3+ 人 {k3:,}（{pct(k3, n)}）")
    P(f"- 排除 0 后：1 人 {pct(k1, n - k0)} · 2 人 {pct(k2, n - k0)} · 3+ 人 {pct(k3, n - k0)}\n")
    by3 = x.group_by("judgment_year").agg(pl.len().alias("n"), pl.col("n_defendants").mean().alias("均值"), pl.col("n_defendants").median().alias("中位数"),
                                          (pl.col("n_defendants") == 0).mean().alias("0人"), (pl.col("n_defendants") == 1).mean().alias("1人"),
                                          (pl.col("n_defendants") == 2).mean().alias("2人"), (pl.col("n_defendants") >= 3).mean().alias("3+人")).sort("judgment_year")
    P("### 按释出 judgment_year（占比为含 0 的分母）\n")
    P(md_table(by3.with_columns(pl.col("judgment_year").cast(pl.Int64))) + "\n")

    # ---------------- hard-case batch cross-check ----------------
    AW = CFG.path("annotation_dir")
    vlog = {r["id"]: r for r in csv.DictReader(open(AW / "vps_fetch/fetch_log.csv", encoding="utf-8"))}
    man = {r["id"]: r for r in csv.DictReader(open(AW / "manifest.csv", encoding="utf-8-sig")) if r["group"] == "V"}
    um = {r[0]: r for r in u.filter(pl.col("id").is_in(list(vlog))).iter_rows()}
    ndm = dict(nd.filter(pl.col("id").is_in(list(vlog))).iter_rows())
    same_url = sum(1 for i, r in vlog.items() if r["原始链接"] and i in um and um[i][1] == r["原始链接"].strip())
    has_url = sum(1 for r in vlog.values() if r["原始链接"])
    same_nd = sum(1 for i, r in man.items() if i in ndm and str(ndm[i]) == r["n_def_auto"])
    nf_now = [r["sample_no"] for i, r in vlog.items() if r["status"] == "not_found" and um.get(i, (0, 0, 0))[2]]
    P("## 交叉核对：the hard-case batch 240 条难例\n")
    P(f"- fetch_log 有链接 {has_url} 条；本次扫描选中同一链接 {same_url} 条。")
    P(f"- the hard-case batch manifest 237 条中，本次 n_defendants 与 manifest `n_def_auto` 相同 {same_nd} 条。")
    P(f"- an earlier check not_found 的 3 条，本次全库扫描有命中的：{nf_now or '无'}\n")

    # ---------------- V2 ----------------
    P("## V2 流量计数（按年度 zip）\n")
    cc = pl.read_csv(CFG.path("corpus_doc_counts_csv")).rename({"doc_count": "corpus_doc_counts"})
    v2 = fl.join(cc, on="year", how="full", coalesce=True).sort("year").with_columns((pl.col("docs") - pl.col("corpus_doc_counts")).alias("差异"))
    P("列：`docs` 全部文书；`crim_code4` 案件类型编码=4；`crim_type_label` 案件类型=“刑事案件”（核对用）；`crim_first` 刑事且审理程序∈{一审, 刑事一审}；"
      "`crim_first_judgment` 再要求案件名称含“判决书”；`prod_kept` 按 `01_preprocess/01_stream_zip_to_parquet.py` 规则（编码=4、审理程序∈{一审,刑事一审,二审,刑事二审,再审,刑事再审}、裁判日期可解析）。\n")
    show = v2.select("year", "docs", "corpus_doc_counts", "差异", "crim_code4", "crim_type_label", "crim_first", "crim_first_judgment", "prod_kept", "rows_bad_width", "rows_fffd_in_caseno")
    tot = show.select([pl.lit("合计").alias("year")] + [pl.col(c).sum() for c in show.columns[1:]])
    P(md_table(pl.concat([show.with_columns(pl.col("year").cast(pl.Utf8)), tot])) + "\n")
    nz = v2.filter(pl.col("差异").fill_null(1) != 0)
    P(f"- 逐年对账：{v2.height} 年中差异不为 0 的年份 {nz.height} 个" + ("：" + "、".join((f"{r['year']}（{r['差异']:+,}）" if r['差异'] is not None else f"{r['year']}（未扫描或无对照）") for r in nz.iter_rows(named=True)) if nz.height else "。") )
    P("- 生产过滤之后的步骤（段落切分 02、正则抽取 02b、LLM 残差抽取 02c、去重/释出筛选到 2,834,831 行）在现存日志中没有逐年计数，本表标为“未分解”，不作估算。")
    P("- 年份口径：本表按年度 zip（= 原始库按裁判日期分包）计；释出数据的 judgment_year 由解析得到，二者可不同。")
    Path(a.out).write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out))


if __name__ == "__main__":
    main()
