#!/usr/bin/env python3
"""Grounding checks for judge_signature_v1.0 smoke runs."""
from __future__ import annotations

import argparse
import json
import pathlib
import re


ALLOWED_ROLES = {"审判长", "审判员", "代理审判员", "助理审判员", "人民陪审员"}
FORBIDDEN_ROLE_PARTS = ("书记员", "代理书记员", "法官助理")


def compact(value) -> str:
    return re.sub(r"[\s　]+", "", "" if value is None else str(value))


def is_missing(value) -> bool:
    return value is None or value == ""


def source_text(case: dict) -> str:
    parts = [case.get("signature_snippet") or "", case.get("tail_fallback") or ""]
    for item in case.get("regex_candidates") or []:
        if isinstance(item, dict):
            parts.append(item.get("evidence_text") or "")
    return "\n".join(parts)


def read_jsonl(path: pathlib.Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def read_cases(cases_dir: pathlib.Path) -> dict:
    cases = {}
    for path in sorted(cases_dir.glob("**/batch_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for item in data:
            raw_id = str(item.get("raw_match_id"))
            cases[raw_id] = item
    return cases


def read_results(results_dir: pathlib.Path) -> list:
    rows = []
    for path in sorted(results_dir.glob("**/*_result.jsonl")):
        rows.extend(read_jsonl(path))
    return rows


def role_forbidden(role) -> bool:
    role_s = "" if role is None else str(role)
    return any(part in role_s for part in FORBIDDEN_ROLE_PARTS) or role_s not in ALLOWED_ROLES


def evidence_ok(row: dict, case: dict) -> bool:
    evidence = row.get("evidence_text")
    if is_missing(evidence):
        return False
    evidence_c = compact(evidence)
    source_c = compact(source_text(case))
    return (
        evidence_c in source_c
        and compact(row.get("judge_name")) in evidence_c
        and compact(row.get("judge_role")) in evidence_c
    )


def positive_shape_ok(row: dict) -> bool:
    if row.get("has_judge_signature") is not True:
        return False
    if is_missing(row.get("judge_name")):
        return False
    if role_forbidden(row.get("judge_role")):
        return False
    if row.get("failure_reason") is not None:
        return False
    if is_missing(row.get("evidence_text")):
        return False
    candidates = row.get("all_judge_candidates")
    if not isinstance(candidates, list) or not candidates:
        return False
    for candidate in candidates:
        if not isinstance(candidate, dict):
            return False
        if role_forbidden(candidate.get("role")):
            return False
        if is_missing(candidate.get("name")) or is_missing(candidate.get("evidence_text")):
            return False
    return True


def negative_shape_ok(row: dict) -> bool:
    if row.get("has_judge_signature") is not False:
        return False
    return (
        row.get("judge_name") is None
        and row.get("judge_role") is None
        and row.get("evidence_text") is None
        and row.get("all_judge_candidates") == []
        and not is_missing(row.get("failure_reason"))
    )


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

    cases_dir = pathlib.Path(args.cases_dir)
    results_dir = pathlib.Path(args.results_dir)
    cases = read_cases(cases_dir)
    rows = read_results(results_dir)

    id_preserved = 0
    raw_preserved = 0
    clerk_as_judge = 0
    positive_rows = 0
    evidence_grounded = 0
    positive_shape = 0
    negative_shape = 0
    result_raw_ids = []
    missing_input = 0

    for row in rows:
        raw_id = str(row.get("raw_match_id"))
        result_raw_ids.append(raw_id)
        case = cases.get(raw_id)
        if not case:
            missing_input += 1
            continue
        if str(row.get("id")) == str(case.get("id")):
            id_preserved += 1
        if raw_id == str(case.get("raw_match_id")):
            raw_preserved += 1

        role = row.get("judge_role")
        if role is not None and role_forbidden(role):
            clerk_as_judge += 1
        for candidate in row.get("all_judge_candidates") or []:
            if isinstance(candidate, dict) and role_forbidden(candidate.get("role")):
                clerk_as_judge += 1

        if row.get("has_judge_signature") is True:
            positive_rows += 1
            if evidence_ok(row, case):
                evidence_grounded += 1
            if positive_shape_ok(row):
                positive_shape += 1
        elif row.get("has_judge_signature") is False and negative_shape_ok(row):
            negative_shape += 1

    duplicate_raw_ids = len(result_raw_ids) - len(set(result_raw_ids))
    compared = len(rows)
    out = {
        "task": "judge_signature_v1.0",
        "cases_total": len(cases),
        "result_rows": compared,
        "missing_input_for_result": missing_input,
        "duplicate_raw_match_id_results": duplicate_raw_ids,
        "id_preserved_pct": pct(id_preserved, compared),
        "raw_match_id_preserved_pct": pct(raw_preserved, compared),
        "clerk_as_judge_count": clerk_as_judge,
        "positive_rows": positive_rows,
        "negative_rows_with_valid_shape": negative_shape,
        "evidence_grounded_pct": pct(evidence_grounded, positive_rows),
        "positive_shape_pct": pct(positive_shape, positive_rows),
    }
    pathlib.Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
