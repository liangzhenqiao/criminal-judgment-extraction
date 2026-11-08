#!/usr/bin/env python3
"""Grounding / quality checks for offense_date_v1.0 runs.

读 batch_*.json（cases，按 id 索引）+ *_result.jsonl（results），复核：
  - id 保真、重复 id
  - grounded_pct：成功行的 all_offense_dates_raw 每元素是 facts_segment 逐字子串
    （compact 比对，与 judge_signature 同族；另报 exact 子串率供透明）
  - precision_dishonest_count：日期格式与 date_precision 不符（如 YM 却带 -DD）
  - failure_reason 非 null 占比（模型声明"无作案时间"的比例）
  - 精度 / 置信度分布
输出扁平 JSON，字段被 acceptance.yaml 的 grounding.metrics 路径引用。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re


RE_YMD = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RE_YM = re.compile(r"^\d{4}-\d{2}$")
RE_Y = re.compile(r"^\d{4}$")


def compact(value) -> str:
    return re.sub(r"[\s　]+", "", "" if value is None else str(value))


RANK = {"Y": 0, "YM": 1, "YMD": 2}


def date_fmt(value):
    if not isinstance(value, str):
        return None
    if RE_YMD.match(value):
        return "YMD"
    if RE_YM.match(value):
        return "YM"
    if RE_Y.match(value):
        return "Y"
    return None


def precision_honest(start, end, prec) -> bool:
    """新语义：每端各按自身精度，date_precision == 两端较粗者。"""
    fs, fe = date_fmt(start), date_fmt(end)
    if fs is None or fe is None or prec not in RANK:
        return False
    coarse = fs if RANK[fs] <= RANK[fe] else fe
    return prec == coarse


def read_jsonl(path: pathlib.Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def read_cases(cases_dir: pathlib.Path) -> dict:
    cases = {}
    for path in sorted(cases_dir.glob("**/batch_*.json")):
        for item in json.loads(path.read_text(encoding="utf-8")):
            cases[str(item.get("id"))] = item
    return cases


def read_results(results_dir: pathlib.Path) -> list:
    rows = []
    for path in sorted(results_dir.glob("**/*_result.jsonl")):
        rows.extend(read_jsonl(path))
    return rows


def pct(num: int, den: int) -> float:
    if den == 0:
        return 100.0
    return round(num / den * 100.0, 3)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases-dir", required=True)
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--schema")
    args = parser.parse_args()

    cases = read_cases(pathlib.Path(args.cases_dir))
    rows = read_results(pathlib.Path(args.results_dir))

    result_ids = []
    id_preserved = 0
    missing_input = 0

    success_rows = 0          # failure_reason is None
    failure_rows = 0          # failure_reason not None
    rows_with_raw = 0         # success rows checked for grounding
    grounded_compact = 0      # all raw elements compact-substring of facts
    grounded_exact = 0        # all raw elements exact-substring of facts
    grounding_violations = []
    precision_dishonest = 0
    precision_dist = {"YMD": 0, "YM": 0, "Y": 0, "null": 0}
    confidence_dist = {"high": 0, "medium": 0, "low": 0, "other": 0}

    for row in rows:
        rid = str(row.get("id"))
        result_ids.append(rid)
        case = cases.get(rid)
        if case is None:
            missing_input += 1
        else:
            id_preserved += 1

        prec = row.get("date_precision")
        precision_dist[prec if prec in ("YMD", "YM", "Y") else "null"] += 1
        conf = row.get("confidence")
        confidence_dist[conf if conf in ("high", "medium", "low") else "other"] += 1

        fr = row.get("failure_reason")
        if fr is not None:
            failure_rows += 1
            continue
        success_rows += 1

        # 精度诚实：每端各按自身精度，date_precision == 两端较粗者
        start, end = row.get("offense_start_date"), row.get("offense_end_date")
        if not precision_honest(start, end, prec):
            precision_dishonest += 1

        # grounding：成功行的 raw 元素必须是 facts_segment 逐字子串
        raw = row.get("all_offense_dates_raw") or []
        if not isinstance(raw, list) or not raw:
            continue
        rows_with_raw += 1
        src = case.get("facts_segment", "") if case else ""
        src_c = compact(src)
        ok_compact = all(compact(e) and compact(e) in src_c for e in raw)
        ok_exact = all(isinstance(e, str) and e and e in src for e in raw)
        if ok_compact:
            grounded_compact += 1
        else:
            if len(grounding_violations) < 20:
                bad = [e for e in raw if not (compact(e) and compact(e) in src_c)]
                grounding_violations.append({"id": rid, "bad_raw": bad[:5]})
        if ok_exact:
            grounded_exact += 1

    duplicate_ids = len(result_ids) - len(set(result_ids))
    out = {
        "task": "offense_date_v1.0",
        "cases_total": len(cases),
        "result_rows": len(rows),
        "missing_input_for_result": missing_input,
        "duplicate_id_results": duplicate_ids,
        "id_preserved_pct": pct(id_preserved, len(rows)),
        "success_rows": success_rows,
        "failure_rows": failure_rows,
        "failure_reason_nonnull_pct": pct(failure_rows, len(rows)),
        "rows_with_raw_checked": rows_with_raw,
        "grounded_pct": pct(grounded_compact, rows_with_raw),
        "grounded_exact_pct": pct(grounded_exact, rows_with_raw),
        "grounding_violation_count": rows_with_raw - grounded_compact,
        "grounding_violation_samples": grounding_violations,
        "precision_dishonest_count": precision_dishonest,
        "precision_dist": precision_dist,
        "confidence_dist": confidence_dist,
    }
    pathlib.Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "grounding_violation_samples"},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
