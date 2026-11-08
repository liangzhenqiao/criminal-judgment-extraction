"""One pass over each yearly zip of the corpus, accumulating three outputs.

用法（项目根运行）：
  uv run --no-project --with polars --with openpyxl python -I 04_validation/02_corpus_single_pass_scan.py \
      --years 1990 --out <输出目录> [--ckpt <断点目录>] [--resume]

- Per-id match counts: every id of the release → number of hits, the source link of the selected article and the year zip it sits in.
- Defendant counts per selected article, from roster() in 03_build_pack.py (validation bundle) (the same function imported, not a second regex).
- Per-year counts from each zip: all documents / criminal / criminal first instance / criminal first instance titled "判决书" / kept by the production filter.
- 多篇命中的选择规则与 02_draw_hard_cases.py 的取文规则相同：
  排序键 (标题不含"判决书", 审理程序 != "一审")，键相同取扫描顺序中的第一篇（年份升序、月份文件升序、行序）。
- 生产过滤（01_preprocess/01_stream_zip_to_parquet.py）：
  案件类型编码 数值 == 4，且 审理程序 ∈ PROCEDURE_KEEP，且 裁判日期 可解析。
- 只读语料目录；只流式读取（zipfile 逐条目 + csv.reader）；不整体读入任何大文件。
"""
import argparse, csv, importlib.util, io, json, pickle, re, sys, time, zipfile
from datetime import date
from pathlib import Path

csv.field_size_limit(sys.maxsize)

__import__("sys").path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_config  # paths come from config.yaml (see config.example.yaml)
CFG = pipeline_config.load(handle_help=False, keys=('corpus_zip_dir', 'release_dir',))

PARQUET_NAMES = ("china_criminal_judgment_records_1985_2021.parquet", "china_criminal_defendant_panel_1985_2021.parquet")  # current release name, then the former one


def _pick(d: Path) -> Path:
    for name in PARQUET_NAMES:
        if (d / name).exists():
            return d / name
    return d / PARQUET_NAMES[0]
# 03_build_pack.py ships in the validation bundle, not in this repository (place it beside this script); a missing file is not an error
BUILD_PACK = Path(__file__).resolve().parent / "03_build_pack.py"
PROCEDURE_KEEP = {"一审", "刑事一审", "二审", "刑事二审", "再审", "刑事再审"}
FIRST_INSTANCE = {"一审", "刑事一审"}
DATE_RE = re.compile(r"^\s*(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})")


def load_roster():
    if not BUILD_PACK.exists():
        print(f"[warn] {BUILD_PACK.name} not found; n_defendants is reported as 0", flush=True)
        return lambda text: 0
    spec = importlib.util.spec_from_file_location("build_pack", BUILD_PACK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.roster


def release_ids():
    import polars as pl
    p = CFG.path("release_dir")
    f = _pick(p) if p.is_dir() else p
    df = pl.read_parquet(f, columns=["id", "judgment_year"])
    return f, df


def date_ok(s):
    m = DATE_RE.match(s or "")
    if not m:
        return False
    try:
        date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return True
    except ValueError:
        return False


def scan_year(y, want, strip_map, state, roster, log):
    zp = CORPUS / f"{y}年裁判文书数据.zip"
    c = dict(year=y, docs=0, crim_code4=0, crim_type_label=0, crim_first=0, crim_first_judgment=0,
             prod_kept=0, rows_bad_width=0, rows_fffd_in_caseno=0, csv_files=0, matched_rows=0)
    t0 = time.time()
    with zipfile.ZipFile(zp, metadata_encoding="gbk") as z:
        infos = sorted((i for i in z.infolist() if i.filename.endswith(".csv")), key=lambda i: i.filename)
        for info in infos:
            c["csv_files"] += 1
            with z.open(info) as fb:
                rd = csv.reader(io.TextIOWrapper(fb, encoding="utf-8-sig", errors="replace", newline=""))
                h = next(rd, None)
                if h is None:
                    continue
                ix = {k: i for i, k in enumerate(h)}
                miss = [k for k in ("原始链接", "案号", "案件名称", "案件类型", "案件类型编码", "审理程序", "裁判日期", "全文") if k not in ix]
                if miss:
                    log(f"  [WARN] {info.filename} 缺列 {miss}")
                g = lambda row, k: row[ix[k]] if k in ix and ix[k] < len(row) else ""
                width = len(h)
                for row in rd:
                    c["docs"] += 1
                    if len(row) != width:
                        c["rows_bad_width"] += 1
                    code = g(row, "案件类型编码").strip()
                    proc = g(row, "审理程序").strip()
                    title = g(row, "案件名称")
                    try:
                        is4 = float(code) == 4.0
                    except ValueError:
                        is4 = False
                    if is4:
                        c["crim_code4"] += 1
                        if proc in FIRST_INSTANCE:
                            c["crim_first"] += 1
                            if "判决书" in title:
                                c["crim_first_judgment"] += 1
                        if proc in PROCEDURE_KEEP and date_ok(g(row, "裁判日期")):
                            c["prod_kept"] += 1
                    if g(row, "案件类型").strip() == "刑事案件":
                        c["crim_type_label"] += 1
                    raw = g(row, "案号")
                    if "�" in raw:
                        c["rows_fffd_in_caseno"] += 1
                    rid = raw if raw in want else strip_map.get(raw.strip())
                    if rid is None:
                        continue
                    c["matched_rows"] += 1
                    st = state.get(rid)
                    key = ("判决书" not in title, proc != "一审")
                    if st is None:
                        state[rid] = [1, key, g(row, "原始链接").strip(), y, len(roster(g(row, "全文")))]
                    else:
                        st[0] += 1
                        if key < st[1]:
                            st[1:] = [key, g(row, "原始链接").strip(), y, len(roster(g(row, "全文")))]
            log(f"  {y} {Path(info.filename).name} 累计文书 {c['docs']:,} 命中行 {c['matched_rows']:,} 用时 {time.time()-t0:.0f}s")
    c["seconds"] = round(time.time() - t0, 1)
    return c


def main():
    global CORPUS
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="1985-2021", help="如 1990 或 1985-2021")
    ap.add_argument("--out", required=True)
    ap.add_argument("--ckpt", default=None)
    ap.add_argument("--resume", action="store_true")
    a = ap.parse_args()
    CORPUS = CFG.path("corpus_zip_dir")
    if "-" in a.years:
        lo, hi = map(int, a.years.split("-")); years = list(range(lo, hi + 1))
    else:
        years = [int(x) for x in a.years.split(",")]
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    assert not str(out.resolve()).startswith(str(CORPUS.resolve())), "不得向语料目录写文件"
    ck = Path(a.ckpt) if a.ckpt else out / "_ckpt"
    ck.mkdir(parents=True, exist_ok=True)
    log = lambda s: print(time.strftime("%H:%M:%S"), s, flush=True)

    relf, rel = release_ids()
    ids = [i for i in rel["id"].to_list() if i is not None]
    want = set(ids)
    strip_map = {}
    for i in ids:
        strip_map.setdefault(i.strip(), i)
    log(f"释出文件 {relf}；行 {rel.height:,}，非空 id {len(want):,}；strip 后键 {len(strip_map):,}")
    roster = load_roster()

    state, flows, failed, done = {}, [], [], []
    ckf = ck / "state.pkl"
    if a.resume and ckf.exists():
        state, flows, failed, done = pickle.load(open(ckf, "rb"))
        log(f"从断点恢复：已完成年份 {done}")
    for y in years:
        if y in done:
            continue
        log(f"开始 {y}")
        try:
            c = scan_year(y, want, strip_map, state, roster, log)
            flows.append(c); done.append(y)
            log(f"完成 {y}：文书 {c['docs']:,} 刑事 {c['crim_code4']:,} 刑事一审 {c['crim_first']:,} "
                f"刑事一审判决书 {c['crim_first_judgment']:,} 生产过滤保留 {c['prod_kept']:,}；"
                f"已匹配 id 累计 {len(state):,}；用时 {c['seconds']}s")
        except Exception as e:  # 记录后继续
            failed.append({"year": y, "error": repr(e)}); log(f"[FAIL] {y}: {e!r}")
        tmp = ck / "state.pkl.tmp"
        pickle.dump((state, flows, failed, done), open(tmp, "wb")); tmp.replace(ckf)

    # ---------- 输出 ----------
    with open(out / "id_to_cjo_url.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f); w.writerow(["id", "cjo_url", "n_hits", "source_year"])
        for i in ids:
            st = state.get(i)
            w.writerow([i, st[2], st[0], st[3]] if st else [i, "", 0, ""])
    with open(out / "defendants_per_judgment.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f); w.writerow(["id", "n_defendants"])
        for i in ids:
            if i in state:
                w.writerow([i, state[i][4]])
    cols = ["year", "docs", "crim_code4", "crim_type_label", "crim_first", "crim_first_judgment", "prod_kept",
            "rows_bad_width", "rows_fffd_in_caseno", "matched_rows", "csv_files", "seconds"]
    with open(out / "flow_counts_by_year.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore"); w.writeheader()
        for c in sorted(flows, key=lambda c: c["year"]):
            w.writerow(c)
    meta = {"release_file": str(relf), "release_rows": rel.height, "release_nonnull_ids": len(want),
            "years_done": sorted(done), "years_failed": failed,
            "matched_ids": len(state), "multi_hit_ids": sum(1 for s in state.values() if s[0] > 1)}
    json.dump(meta, open(out / "scan_meta.json", "w"), ensure_ascii=False, indent=1)
    log("全部完成 " + json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
