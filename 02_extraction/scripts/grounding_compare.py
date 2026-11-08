#!/usr/bin/env python3
"""Generate grounding metrics for Gate 7 from smoke inputs and runner outputs.

The output schema is compatible with
scripts/smoke_gate.py: metrics are written under the "v1" key by default.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


DEFAULT_ARRAY_FIELDS = [
    "prior_years",
    "prior_offenses",
    "prior_sentence_months",
    "prior_released_years",
]


def iter_strings(obj: Any):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from iter_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_strings(v)


def full_text(case: dict) -> str:
    preferred = [
        case.get("defendant_segment"),
        case.get("facts_segment"),
        case.get("judgment_segment"),
    ]
    parts = [p for p in preferred if isinstance(p, str) and p.strip()]
    if parts:
        return "\n".join(parts)
    return "\n".join(s for s in iter_strings(case) if s.strip())


def load_cases(root: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in sorted(root.rglob("*.json")):
        try:
            raw = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        if isinstance(raw, list):
            arr = raw
        elif isinstance(raw, dict) and isinstance(raw.get("data"), list):
            arr = raw["data"]
        elif isinstance(raw, dict):
            arr = [raw]
        else:
            continue
        for rec in arr:
            if isinstance(rec, dict) and rec.get("id") is not None:
                out[str(rec["id"])] = rec
    return out


def load_jsonl_results(root: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in sorted(root.rglob("*.jsonl")):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                rec = json.loads(line)
                if isinstance(rec, dict) and rec.get("id") is not None:
                    out[str(rec["id"])] = rec
    return out


def load_schema(path: str | None) -> dict:
    if not path:
        return {"array_fields": DEFAULT_ARRAY_FIELDS}
    data = json.load(open(path, encoding="utf-8"))
    data.setdefault("array_fields", DEFAULT_ARRAY_FIELDS)
    return data


def pct(num: int, den: int):
    if den == 0:
        return None
    return round(num / den * 100, 2)


def is_truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in {"true", "1", "yes", "y", "有", "是"}
    return bool(v)


def grounded_offense(value: str, text: str) -> tuple[bool, bool]:
    if value in text or value.replace("罪", "") in text:
        return True, True
    parts = [p.strip() for p in re.split(r"[、,，/]", value) if p.strip()]
    split_ok = bool(parts) and all(p in text or p.replace("罪", "") in text for p in parts)
    return False, split_ok


def compute_metrics(cases: dict[str, dict], records: dict[str, dict], array_fields: list[str]) -> dict:
    common = sorted(set(cases) & set(records))
    pos_total = pos_grounded = pos_grounded_split = 0
    arr_aligned_pass = arr_aligned_total = 0
    hpc_with_arr = hpc_with_none_arr = hpc_with_empty_arr = 0
    no_hpc_arr_filled = 0
    problematic = []

    for cid in common:
        rec = records[cid]
        text = full_text(cases[cid])
        arrays = {k: rec.get(k) if isinstance(rec.get(k), list) else [] for k in array_fields}
        lens = [len(v) for v in arrays.values()]
        if lens and max(lens) > 0:
            arr_aligned_total += 1
            if len(set(lens)) == 1:
                arr_aligned_pass += 1

        prior_offenses = arrays.get("prior_offenses", [])
        has_meaningful_prior = any(isinstance(o, str) and o.strip() for o in prior_offenses)
        has_any_prior = len(prior_offenses) > 0
        hpc = is_truthy(rec.get("has_prior_conviction"))
        if hpc:
            if has_meaningful_prior:
                hpc_with_arr += 1
            elif has_any_prior:
                hpc_with_none_arr += 1
            else:
                hpc_with_empty_arr += 1
        elif has_meaningful_prior:
            no_hpc_arr_filled += 1

        for offense in prior_offenses:
            if not isinstance(offense, str) or not offense.strip():
                continue
            pos_total += 1
            direct, split = grounded_offense(offense, text)
            if direct:
                pos_grounded += 1
            if direct or split:
                pos_grounded_split += 1
            elif len(problematic) < 10:
                problematic.append({
                    "id": cid,
                    "value": offense,
                    "prior_conviction_desc": str(rec.get("prior_conviction_desc") or "")[:160],
                })

    return {
        "cases_compared": len(common),
        "result_records": len(records),
        "input_cases": len(cases),
        "hpc_with_real_arr": hpc_with_arr,
        "hpc_with_none_arr": hpc_with_none_arr,
        "hpc_with_empty_arr_violation": hpc_with_empty_arr,
        "no_hpc_arr_filled_violation": no_hpc_arr_filled,
        "arrays_aligned": f"{arr_aligned_pass}/{arr_aligned_total}",
        "arrays_aligned_pct": pct(arr_aligned_pass, arr_aligned_total),
        "prior_offenses": {
            "total": pos_total,
            "grounded": pos_grounded,
            "grounded_pct": pct(pos_grounded, pos_total),
            "grounded_with_split": pos_grounded_split,
            "grounded_with_split_pct": pct(pos_grounded_split, pos_total),
        },
        "problematic_samples": problematic,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases-dir", required=True, help="input batch directory containing *.json cases")
    ap.add_argument("--results-dir", required=True, help="runner results directory containing *.jsonl")
    ap.add_argument("--schema", help="task schema JSON; uses array_fields when present")
    ap.add_argument("--label", default="v1")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    cases_dir = Path(a.cases_dir)
    results_dir = Path(a.results_dir)
    if not cases_dir.is_dir():
        sys.stderr.write(f"cases-dir not found: {cases_dir}\n")
        return 2
    if not results_dir.is_dir():
        sys.stderr.write(f"results-dir not found: {results_dir}\n")
        return 2

    schema = load_schema(a.schema)
    cases = load_cases(cases_dir)
    records = load_jsonl_results(results_dir)
    if not cases:
        sys.stderr.write(f"no input cases found under {cases_dir}\n")
        return 2
    if not records:
        sys.stderr.write(f"no result jsonl records found under {results_dir}\n")
        return 2

    metrics = compute_metrics(cases, records, list(schema.get("array_fields") or DEFAULT_ARRAY_FIELDS))
    if metrics["cases_compared"] == 0:
        sys.stderr.write("no overlapping ids between input cases and result records\n")
        return 2

    out = {
        "meta": {
            "cases_dir": str(cases_dir),
            "results_dir": str(results_dir),
            "schema": a.schema,
        },
        a.label: metrics,
    }
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "grounding": a.label,
        "cases_compared": metrics["cases_compared"],
        "arrays_aligned_pct": metrics["arrays_aligned_pct"],
        "prior_offenses_grounding_pct": metrics["prior_offenses"]["grounded_with_split_pct"],
        "no_hpc_arr_filled_violation": metrics["no_hpc_arr_filled_violation"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
