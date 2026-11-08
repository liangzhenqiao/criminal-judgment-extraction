"""Regex privacy scan of released free-text fields (read-only); writes candidate tables and a summary, no data change.

Default input is the data_cleaning directory from config.yaml. Full judgment texts are not read.
候选值可能含个人信息，CSV 默认写到 <validation_out_dir>/regex_privacy_scan/；候选值不得公开或提交到版本库。

用法（项目根）：
  .venv/bin/python -I 04_validation/04_regex_privacy_scan.py
  .venv/bin/python -I 04_validation/04_regex_privacy_scan.py --self-test
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import resource
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow.parquet as pq

__import__("sys").path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_config  # paths come from config.yaml (see config.example.yaml)
CFG = pipeline_config.load(handle_help=False, keys=('release_dir', 'validation_out_dir',))

PARQUET_NAMES = ("china_criminal_judgment_records_1985_2021.parquet", "china_criminal_defendant_panel_1985_2021.parquet")  # current release name, then the former one


def _pick(d: Path) -> Path:
    for name in PARQUET_NAMES:
        if (d / name).exists():
            return d / name
    return d / PARQUET_NAMES[0]
FIELDS = (
    "judge_name",
    "prior_occupation_raw",
    "crime_location_raw",
    "prior_offenses",
)
PERSON_FIELDS = frozenset(FIELDS) - {"judge_name"}

_ANON_EXACT = frozenset({
    "x", "xx", "xxx", "未知", "不详", "无名", "无名氏", "匿名", "无", "空",
    "null", "none", "n/a", "na",
})
_ANON_MARK = re.compile(r"某|×|✕|＊")
_XMASK = re.compile(
    r"^[一-鿿]{0,2}[Xx]{1,3}$|^[Xx]{1,3}[一-鿿]{0,2}$"
    r"|^[一-鿿][Xx]{1,2}[一-鿿]$"
)
_TOKEN = re.compile(r"^\{\{[A-Za-z]+_[A-Za-z0-9]+\}\}$")

_ID18 = re.compile(
    r"(?<!\d)([1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?!\d)"
)
_ID15 = re.compile(
    r"(?<!\d)([1-9]\d{5}\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3})(?!\d)"
)
_MOBILE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_MOBILE_SPACED = re.compile(r"(?<!\d)(1[3-9]\d[\s\-]\d{4}[\s\-]\d{4})(?!\d)")
_LANDLINE = re.compile(r"(?<!\d)(0\d{2,3}-?\d{7,8})(?!\d)")
_DOOR = re.compile(r"(?<!\d)(\d{1,6})\s*(号楼|号|弄|栋|幢|室|单元)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_URL = re.compile(r"(?:https?://|www\.)[^\s，。；、<>\"']+", re.I)
_PERSON = re.compile(
    r"(?:叫做|名叫|名为|姓名为|姓名是|姓名[:：]"
    r"|被告人|被害人|受害人|证人|审判长|审判员|人民陪审员|法官|原告人"
    r"|犯罪嫌疑人|上诉人|被上诉人|原审被告人)"
    r"\s*([一-鿿]{2,4})"
    r"(?=犯|的|某|，|。|、|；|：|（|\(|\s|$|于|在|因|与|和|及|到|被|以|对|将|等|现|年|月|日)"
)
_PERSON_JIAO = re.compile(
    r"(?<![呼喊称])叫(?!做|停|号)\s*([一-鿿]{2,4})"
    r"(?=犯|的|某|，|。|、|；|：|（|\(|\s|$|于|在|因|与|和|及|到|被|以|对|将|等|现|年|月|日)"
)
_STOP = frozenset("""
盗窃 诈骗 抢劫 故意 过失 伤害 杀人 交通 肇事 危险 驾驶 贩卖 毒品 走私 受贿 贪污
挪用 公款 职务 侵占 敲诈 勒索 聚众 斗殴 寻衅 滋事 强奸 猥亵 非法 持有 枪支 弹药
放火 投毒 绑架 拐卖 虐待 遗弃 妨害 公务 伪造 变造 信用 判决 裁定 起诉 公诉 自诉
附带 民事 刑事 诉讼 代理 辩护 有期 徒刑 拘役 管制 缓刑 罚金 没收 剥夺 政治 权利
无罪 有罪 证据 事实 查明 如下 上述 其中 以及 同时 分别 共同 单独 主犯 从犯 胁从
教唆 单位 犯罪 罪行 罪名 案由 案件 本案 该案 一案 审理 审结 公开 开庭 宣判 送达
上诉 抗诉 申诉 再审 发回 改判 维持 撤销 中止 终结 执行 监狱 看守 拘留 逮捕 取保
候审 监视 居住 社区 矫正 假释 减刑 农民 无业 工人 学生 个体 职工 居民 村民 司机
员工 本院 认为 被告人 被害人 法院 检察 有限 公司 股份 集团 责任
""".split())
_PERSON_CTX = ("叫", "名为", "名叫", "被告人", "被害人", "受害人", "证人", "法官",
               "审判长", "审判员", "原告", "嫌疑人", "上诉人", "姓名")
_DOOR_MARK = ("号", "弄", "栋", "幢", "室", "单元")


def release_parquet(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    return _pick(CFG.path("release_dir"))


def nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s)


def has_anon_label(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    if t.casefold() in _ANON_EXACT:
        return True
    if _ANON_MARK.search(t):
        return True
    if _XMASK.fullmatch(t):
        return True
    if _TOKEN.fullmatch(t):
        return True
    return False


def _overlaps(span, spans) -> bool:
    a, b = span
    return any(not (b <= c or a >= d) for c, d in spans)


def _door_excluded(text: str, m: re.Match) -> bool:
    suffix = m.group(2)
    start, end = m.span()
    before = text[max(0, start - 16):start]
    after = text[end:end + 6]
    if suffix == "号":
        if before and before[-1] in "年月日第":
            return True
        if after.startswith(("牌", "文", "文件")):
            return True
        if re.search(r"(刑初|刑终|刑核|刑更|民初|行初|字第|字第)$", before):
            return True
        if re.search(r"[（(]\d{4}[）)]", text[max(0, start - 40):end]):
            return True
        if re.search(r"(有期徒刑|拘役|管制|缓刑|死刑|无期徒刑|个月|判处)$", before):
            return True
        if re.search(r"\d{4}年", before) and not re.search(r"[路街道巷弄村组区镇乡]", before):
            return True
    return False


def _person_ok(name: str) -> bool:
    if name in _STOP or len(name) < 2 or len(set(name)) == 1:
        return False
    if "罪" in name or any(w in name for w in _STOP):
        return False
    if _ANON_MARK.search(name) or "X" in name or "x" in name:
        return False
    return True


def scan_text(original: str, allow_person: bool) -> tuple[list[str], str | None, bool]:
    """返回 (原因列表, 命中片段, 是否含匿名标签)。原因可空。"""
    text = nfkc(original)
    anon = has_anon_label(text)
    reasons: list[str] = []
    example = None

    def add(reason: str, span: tuple[int, int]) -> None:
        nonlocal example
        reasons.append(reason)
        if example is None:
            a = max(0, span[0] - 24)
            b = min(len(text), span[1] + 24)
            example = text[a:b].replace("\n", " ").replace("\r", " ")

    if any(ch.isdigit() for ch in text):
        id_spans = []
        for m in _ID18.finditer(text):
            add("id_card_18", m.span())
            id_spans.append(m.span())
        for m in _ID15.finditer(text):
            if not _overlaps(m.span(), id_spans):
                add("id_card_15", m.span())
                id_spans.append(m.span())
        for m in _MOBILE.finditer(text):
            if not _overlaps(m.span(), id_spans):
                add("phone_mobile", m.span())
        for m in _MOBILE_SPACED.finditer(text):
            add("phone_mobile", m.span())
        for m in _LANDLINE.finditer(text):
            if not _overlaps(m.span(), id_spans):
                add("phone_landline", m.span())
    if any(mark in text for mark in _DOOR_MARK):
        for m in _DOOR.finditer(text):
            if _door_excluded(text, m):
                continue
            suffix = m.group(2)
            add("doorplate_hao" if suffix == "号" else "doorplate_building", m.span())
    if "@" in text:
        for m in _EMAIL.finditer(text):
            add("email", m.span())
    if "http" in text.lower() or "www." in text.lower():
        for m in _URL.finditer(text):
            add("url", m.span())
    if allow_person and any(k in text for k in _PERSON_CTX):
        for cre in (_PERSON, _PERSON_JIAO):
            for m in cre.finditer(text):
                if _person_ok(m.group(1)):
                    add("person_name", m.span(1))
    # 去重且保序
    seen = []
    for r in reasons:
        if r not in seen:
            seen.append(r)
    return seen, example, anon


def parse_elements(raw: str | None, as_array: bool) -> tuple[list[str], bool]:
    """返回 (非空元素, 是否解析失败)。"""
    if raw is None:
        return [], False
    s = raw.strip()
    if not s:
        return [], False
    if not as_array:
        return [raw], False
    if not s.startswith("["):
        return [raw], True
    try:
        arr = json.loads(s)
    except json.JSONDecodeError:
        return [raw], True
    if not isinstance(arr, list):
        return [raw], True
    out = []
    for el in arr:
        if el is None:
            continue
        text = str(el).strip()
        if text:
            out.append(str(el))
    return out, False


def self_test() -> None:
    id18 = "110101199" "003071234"
    id15 = "1101019" "00307123"
    checks = [
        (id18, False, ["id_card_18"], False),
        (id15, False, ["id_card_15"], False),
        ("联系13800" "138000", False, ["phone_mobile"], False),
        ("电话138 0013 8000", False, ["phone_mobile"], False),
        ("座机010-12345678", False, ["phone_landline"], False),
        ("朝阳路12号", False, ["doorplate_hao"], False),
        ("3栋2单元5室", False, ["doorplate_building"], False),
        ("12号楼", False, ["doorplate_building"], False),
        ("（2019）京0105刑初123号", False, [], False),
        ("2019年3月5号", False, [], False),
        ("有期徒刑12个月", False, [], False),
        ("第12号", False, [], False),
        ("京A12345号牌", False, [], False),
        ("被告人张三犯盗窃罪", True, ["person_name"], False),
        ("被告人盗窃罪", True, [], False),
        ("名叫李四。", True, ["person_name"], False),
        ("农民", True, [], False),
        ("被告人张某犯盗窃罪", True, [], True),
        ("张某", False, [], True),
        ("a@b" ".cn", False, ["email"], False),
        ("见 https://example.com/a", False, ["url"], False),
        ("审判长王五", False, [], False),  # judge_name 列不做人名规则
    ]
    failed = 0
    for text, person, expect, anon in checks:
        reasons, _ex, got_anon = scan_text(text, person)
        if reasons != expect or got_anon != anon:
            failed += 1
            print(f"SELFTEST FAIL {text!r} got {reasons} anon={got_anon} expect {expect} anon={anon}", file=sys.stderr)
    # 18 位证号内部不应再报手机号
    reasons, _, _ = scan_text(id18, False)
    if "phone_mobile" in reasons or "phone_landline" in reasons:
        failed += 1
        print("SELFTEST FAIL id overlapped phone", file=sys.stderr)
    if failed:
        raise SystemExit(f"self-test failed: {failed}")
    print("self-test ok", len(checks))


def rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return rss / (1024 * 1024)
    return rss / 1024


def scan(parquet: Path, out_dir: Path) -> dict:
    pf = pq.ParquetFile(parquet)
    n_rows_meta = pf.metadata.num_rows
    t0 = time.perf_counter()
    uniques = {f: set() for f in FIELDS}
    # (field, value) -> [n_rows, reasons(list), example]
    cands: dict[tuple[str, str], list] = {}
    nonnull = Counter()
    cand_rows = Counter()
    anon_rows = Counter()
    parse_fail = Counter()
    reason_rows = Counter()
    n_seen = 0
    for batch in pf.iter_batches(batch_size=65536, columns=list(FIELDS)):
        data = batch.to_pydict()
        n = batch.num_rows
        for i in range(n):
            for field in FIELDS:
                elements, bad = parse_elements(data[field][i], field == "prior_offenses")
                if bad:
                    parse_fail[field] += 1
                if not elements:
                    continue
                nonnull[field] += 1
                row_reasons = set()
                row_anon = False
                seen_el = set()
                for el in elements:
                    if el in seen_el:
                        continue
                    seen_el.add(el)
                    uniques[field].add(el)
                    reasons, example, anon = scan_text(el, field in PERSON_FIELDS)
                    if anon:
                        row_anon = True
                    if not reasons:
                        continue
                    row_reasons.update(reasons)
                    key = (field, el)
                    rec = cands.get(key)
                    if rec is None:
                        cands[key] = [1, reasons, example or ""]
                    else:
                        rec[0] += 1
                        for r in reasons:
                            if r not in rec[1]:
                                rec[1].append(r)
                        if not rec[2] and example:
                            rec[2] = example
                if row_anon:
                    anon_rows[field] += 1
                if row_reasons:
                    cand_rows[field] += 1
                    for r in row_reasons:
                        reason_rows[(field, r)] += 1
        n_seen += n
        if n_seen % 655360 == 0 or n_seen == n_rows_meta:
            print(f"scanned {n_seen} rss_mb={rss_mb():.0f} candidates={len(cands)}", file=sys.stderr)
    if n_seen != n_rows_meta:
        raise SystemExit(f"行数不一致: scanned {n_seen} metadata {n_rows_meta}")
    elapsed = time.perf_counter() - t0
    peak = rss_mb()

    out_dir.mkdir(parents=True, exist_ok=True)
    cand_path = out_dir / "g3_regex_candidates.csv"
    sum_path = out_dir / "g3_regex_summary.csv"
    report_path = out_dir / "g3_regex_report.md"

    reason_values = Counter()
    with cand_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["field", "value", "reason", "n_rows", "example_context"])
        rows_out = sorted(cands.items(), key=lambda kv: (-kv[1][0], kv[0][0], kv[0][1]))
        for (field, value), (nrow, reasons, example) in rows_out:
            reason = "|".join(reasons)
            w.writerow([field, value, reason, nrow, example])
            for r in reasons:
                reason_values[(field, r)] += 1

    summary_rows = []
    with sum_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "field", "n_nonnull_rows", "n_unique_values", "n_candidate_values",
            "n_candidate_rows", "candidate_row_pct", "anonymised_label_rows",
        ])
        for field in FIELDS:
            n_cv = sum(1 for (ff, _v) in cands if ff == field)
            nn = nonnull[field]
            cr = cand_rows[field]
            pct = (100.0 * cr / nn) if nn else 0.0
            rec = {
                "field": field,
                "n_nonnull_rows": nn,
                "n_unique_values": len(uniques[field]),
                "n_candidate_values": n_cv,
                "n_candidate_rows": cr,
                "candidate_row_pct": round(pct, 6),
                "anonymised_label_rows": anon_rows[field],
            }
            summary_rows.append(rec)
            w.writerow([
                field, nn, len(uniques[field]), n_cv, cr, f"{pct:.6f}", anon_rows[field],
            ])

    write_report(
        report_path, parquet, n_rows_meta, elapsed, peak, summary_rows,
        reason_rows, reason_values, parse_fail, len(cands),
    )
    return {
        "n_rows": n_rows_meta,
        "elapsed_sec": round(elapsed, 1),
        "peak_rss_mb": round(peak, 1),
        "n_candidate_values_total": len(cands),
        "summary": summary_rows,
        "parse_fail": dict(parse_fail),
        "candidates_csv": str(cand_path),
        "summary_csv": str(sum_path),
        "report": str(report_path),
    }


def write_report(path, parquet, n_rows, elapsed, peak, summary_rows, reason_rows, reason_values, parse_fail, n_cand_values):
    lines = []
    a = lines.append
    a("# Regex privacy scan of released free-text fields")
    a("")
    a(f"- 输入：`{parquet}`")
    a(f"- 行数：{n_rows:,}")
    a(f"- 耗时：{elapsed:.1f} 秒")
    a(f"- 峰值 RSS（近似）：{peak:.0f} MB")
    a("- 只读 parquet 四列，按批扫描；不读文书全文；不改数据、不改 config.yaml。")
    a("- 本文件的数字是正则命中，不是「无残留」结论。未命中只表示这些规则没有命中。")
    a("")
    a("## 规则")
    a("")
    a("- 扫描前对副本做 Unicode NFKC。候选表里的 `value` 仍是释出原值。")
    a("- 身份证：18 位（年份 19/20，月日合法，末位可 X）与 15 位旧式（月日合法）。两侧不得再连数字。18 位命中跨度内不再报 15 位或电话。")
    a("- 电话：大陆手机 `1[3-9]` 共 11 位，或 `1xx-xxxx-xxxx` 空格/短横分隔；固定电话为 `0` 开头区号加 7–8 位本地号，可带一个短横。")
    a("- 门牌：数字后接 号 / 号楼 / 弄 / 栋 / 幢 / 室 / 单元。`号` 若落在日期（年月日、第）、案号（（年份）或刑初/字第一类）、刑期（有期徒刑、拘役、管制、缓刑、个月）、号牌、文号上下文，则排除。号楼、弄、栋、幢、室、单元不按这个排除。")
    a("- 人名：只在 `prior_occupation_raw`、`crime_location_raw`、`prior_offenses`。必须紧跟在 叫 / 名为 / 名叫 / 姓名 / 被告人 / 被害人 / 受害人 / 证人 / 法官 / 审判长 / 审判员 / 原告人 / 嫌疑人 / 上诉人 之后，2–4 个汉字，且后面是分隔符或「犯/的/于/在」一类边界。不用「任意 2–4 个汉字」。停用词（罪名、程序词、职业词）剔除。`judge_name` 本身是姓名列，不按人名规则报候选。")
    a("- 邮箱、URL（http/https/www）。")
    a("- 匿名标签单独计数，不算候选：含「某 / × / ＊」、整值是 X 掩码（张X、XX）、整值是 未知/不详/匿名/无，或整值是 `{{FIELD_token}}` 形式。标签与真实命中可出现在同一行：行既计入 anonymised_label_rows，也计入候选行。")
    a("- `prior_offenses` 按 JSON 数组逐元素扫。元素级唯一值。一行里重复元素只计一次。`[null, null]` 或空串不算非空行。")
    a("- `candidate_row_pct` = 候选行 / 该列非空行 × 100。")
    a("")
    a("## 结果")
    a("")
    a("| field | n_nonnull_rows | n_unique_values | n_candidate_values | n_candidate_rows | candidate_row_pct | anonymised_label_rows |")
    a("|---|---:|---:|---:|---:|---:|---:|")
    for r in summary_rows:
        a("| {field} | {n_nonnull_rows:,} | {n_unique_values:,} | {n_candidate_values:,} | {n_candidate_rows:,} | {candidate_row_pct:.4f} | {anonymised_label_rows:,} |".format(**r))
    a("")
    a(f"候选值总数（field × value）：{n_cand_values:,}")
    a("")
    a("| field | reason | n_rows | n_values |")
    a("|---|---|---:|---:|")
    for (field, reason) in sorted(reason_rows):
        a(f"| {field} | {reason} | {reason_rows[(field, reason)]:,} | {reason_values[(field, reason)]:,} |")
    if not reason_rows:
        a("| — | — | 0 | 0 |")
    a("")
    if parse_fail:
        a("prior_offenses JSON 解析失败的行（整格当作一个元素扫）：")
        for k, v in parse_fail.items():
            a(f"- {k}: {v:,}")
        a("")
    a("## 假阳性与漏报")
    a("")
    a("- `doorplate_hao` 在犯罪地点里大量会是真实门牌，也会混进未被排除词覆盖的编号。这一类不能当成已确认泄露，也不能当成已排除。")
    a("- `doorplate_building`（室、弄、栋、幢、单元、号楼）比单独的「号」具体，仍可能是楼栋描述而非住户门牌。")
    a("- 15 位身份证与其它 15 位编号靠月日形态区分，仍可能误中。18 位同样不校验校验位。")
    a("- 手机号、邮箱、URL 形态较窄，假阳性应低于门牌，但仍是候选，不是确认件。")
    a("- 人名规则要求上下文词和右边界，短人名、无这些词的人名不会命中。这是漏报来源，不是清洁证明。")
    a("- 停用词会放过「被告人」后直接跟罪名的片段，也可能误删恰好与停用词同形的姓名。")
    a("- 匿名标签计数依赖「某 / × / X / 未知」等表面形式。没有这些标记的真名不会进入该计数。")
    a("")
    a("## 交给 GPU 模型二次判断的候选")
    a("")
    a("全部候选值都要二判。建议顺序：")
    a("")
    a("1. `id_card_18`、`id_card_15`、`phone_mobile`、`phone_landline`、`email`、`url`、`person_name`：优先。")
    a("2. `doorplate_building`：其次。")
    a("3. `doorplate_hao`：数量可能最大，假阳性风险最高，放在最后。")
    a("")
    a("二判前不要在正文或回应信里写自由文本「无个人信息残留」。`judge_name` → `judge_id` 尚未执行，姓名列本身不在人名候选规则里。")
    a("")
    a("## 文件")
    a("")
    a("- `g3_regex_candidates.csv`：field, value, reason, n_rows, example_context。含原值，本地留存，不进公开仓库（同目录 `.gitignore` 已排除该文件名）。")
    a("- `g3_regex_summary.csv`：只有计数。")
    a("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=None)
    ap.add_argument("--out-dir", default=None, help="默认 <validation_out_dir>/regex_privacy_scan")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    self_test()
    if args.self_test:
        return
    parquet = release_parquet(args.input)
    if not parquet.is_file():
        raise SystemExit(f"找不到 parquet: {parquet}")
    out_dir = Path(args.out_dir) if args.out_dir else CFG.path("validation_out_dir") / "regex_privacy_scan"
    result = scan(parquet, out_dir)
    # 标准输出只打计数，不打候选值
    brief = {
        "n_rows": result["n_rows"],
        "elapsed_sec": result["elapsed_sec"],
        "peak_rss_mb": result["peak_rss_mb"],
        "n_candidate_values_total": result["n_candidate_values_total"],
        "parse_fail": result["parse_fail"],
        "summary": result["summary"],
    }
    print(json.dumps(brief, ensure_ascii=False))


if __name__ == "__main__":
    main()
