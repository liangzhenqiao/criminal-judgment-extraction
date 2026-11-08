#!/usr/bin/env python3
"""Score a sample run against the recorded values and apply the six gates.

Inputs, all under --work:
  results/**/*_result.jsonl   accepted records written by the runner
  state/batch_state.json      per-batch counts; summed, so a resumed run scores whole
Reference: expected/records.jsonl (released values) and expected/*.jsonl (deterministic stages).

Agreement is computed the way the 94.0% reference was (Technical Validation,
Supplementary): exact match after normalisation, over the 11 scalar fields
below, counting only items where both the run and the release hold a value.
Items where either side is null are reported, not scored.

Writes <work>/gates.json and prints the gate table. Exit status 0 only if all
six gates pass.

Usage:
  python examples/reproduction_sample/compare_output.py --work work/repro
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

FIELDS = ["sex", "birth_year", "ethnicity", "edu_level", "has_prior_conviction",
          "is_recidivist_legal", "party_role", "sentence_months", "sentence_type",
          "probation_flag", "fine_yuan"]

# gate -> (threshold, direction). Sources:
#   documents     98% of the 1,000, i.e. 1,000 x (1 - fail_rate_max), so the two
#                 gates cannot disagree
#   agreement     production model and prompt, 2,486 / 2,645 = 0.940 on the same 11 fields
#   id_preserved  02_extraction/profiles/tasks/judge_signature_v1.0/acceptance.yaml id_preserved_pct_min
#   fail_rate     same file, fail_rate_max
#   throughput    floor that catches a stalled or misrouted endpoint; production
#                 baseline is 6.7 items/s at concurrency 48, so 0.80 is far below a
#                 healthy run and is printed beside it
#   deterministic check_sample.py
GATES = {"documents": (980, "min"), "agreement": (0.940, "min"),
         "id_preserved": (1.0, "min"), "fail_rate": (0.02, "max"),
         "throughput": (0.80, "min"), "deterministic_diff": (0, "max")}
PRODUCTION_THROUGHPUT = 6.7  # items/s, production baseline at concurrency 48


def norm(v):
    """Same normalisation as the 94.0% reference computation."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 6)
    s = str(v).strip()
    if s == "" or s.lower() in {"null", "none", "nan"}:
        return None
    if s.lower() in {"true", "false"}:
        return s.lower() == "true"
    try:
        return round(float(s), 6)
    except ValueError:
        return s


def base_id(rid: str, expected: dict) -> tuple[str | None, int]:
    """Map an output id to its document id; multi-defendant outputs carry _dN."""
    if rid in expected:
        return rid, 1
    m = re.match(r"^(.*)_d(\d+)$", rid)
    if m and m.group(1) in expected:
        return m.group(1), int(m.group(2))
    return None, 0


def deterministic_diff() -> int:
    p = subprocess.run([sys.executable, str(HERE / "check_sample.py")],
                       capture_output=True, text=True)
    m = re.search(r"(\d+) mismatched record", p.stdout)
    return int(m.group(1)) if m else -1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", type=Path, required=True)
    a = ap.parse_args()

    expected = {}
    for line in (HERE / "expected" / "records.jsonl").read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        expected[r["id"]] = r

    # first defendant per document, as in the release
    produced: dict[str, tuple[int, dict]] = {}
    for p in sorted((a.work / "results").rglob("*_result.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(r, dict):
                continue
            doc, k = base_id(str(r.get("id", "")), expected)
            if doc and (doc not in produced or k < produced[doc][0]):
                produced[doc] = (k, r)

    state_p = a.work / "state" / "batch_state.json"
    if not state_p.exists():
        print(f"FAIL  no {state_p}; the runner has not run under this --work")
        return 1
    state = json.loads(state_p.read_text(encoding="utf-8"))
    not_done = sorted(k for k, v in state.items() if v.get("status") != "done")
    ok_n = fail_n = 0
    wall = 0.0
    codes: dict[str, int] = {}
    for v in state.values():
        if v.get("status") == "done":
            ok_n += v.get("ok_n", 0)
            fail_n += v.get("fail_n", 0)
            wall += v.get("wall_sec", 0.0)
            for c, n in (v.get("fail_codes") or {}).items():
                codes[c] = codes.get(c, 0) + n
    records = max(ok_n + fail_n, 1)
    id_fail = sum(n for c, n in codes.items() if c.startswith("id_"))

    per_field = {f: {"match": 0, "differ": 0, "run_null": 0, "release_null": 0, "both_null": 0}
                 for f in FIELDS}
    for rid, exp in expected.items():
        got = produced.get(rid, (0, {}))[1]
        for f in FIELDS:
            mv, gv = norm(got.get(f)), norm(exp.get(f))
            key = ("both_null" if mv is None and gv is None else
                   "run_null" if mv is None else
                   "release_null" if gv is None else
                   "match" if mv == gv else "differ")
            per_field[f][key] += 1
    match = sum(c["match"] for c in per_field.values())
    scored = match + sum(c["differ"] for c in per_field.values())

    got = {
        "documents": len(produced),
        "agreement": match / scored if scored else 0.0,
        "id_preserved": 1 - id_fail / records,
        "fail_rate": fail_n / records,
        "throughput": ok_n / wall if wall else 0.0,
        "deterministic_diff": deterministic_diff(),
    }

    print(f"{'field':22s} {'match':>6s} {'differ':>6s} {'run_null':>8s} {'rel_null':>8s} {'both_null':>9s}")
    for f, c in per_field.items():
        print(f"{f:22s} {c['match']:6d} {c['differ']:6d} {c['run_null']:8d} "
              f"{c['release_null']:8d} {c['both_null']:9d}")
    print()

    ok = not not_done
    verdicts = {}
    for k, (bar, direction) in GATES.items():
        v = got[k]
        good = (v >= bar if direction == "min" else 0 <= v <= bar)
        ok &= good
        verdicts[k] = {"value": v, "threshold": bar, "direction": direction, "pass": good}
        shown = f"{v:.4f}" if isinstance(v, float) else str(v)
        print(f"{'PASS' if good else 'FAIL'}  {k:18s} {shown:>10s}  (threshold {bar})")
    print(f"throughput against the production baseline ({PRODUCTION_THROUGHPUT}/s): "
          f"{got['throughput']:.4f}")
    print(f"\n{len(produced)}/{len(expected)} documents produced; {match}/{scored} scored items "
          f"agree; {ok_n} accepted and {fail_n} failed records in {wall:.0f} s of batch time.")
    if codes:
        print("failure codes: " + ", ".join(f"{c}={n}" for c, n in sorted(codes.items())))
    if not_done:
        print(f"FAIL  {len(not_done)} batch(es) not done: {', '.join(not_done[:5])}")

    out = {"all_pass": ok, "gates": verdicts, "batches_not_done": not_done,
           "accepted_records": ok_n, "failed_records": fail_n, "batch_wall_sec": wall,
           "failure_codes": codes, "fields": per_field}
    (a.work / "gates.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"gates.json -> {a.work / 'gates.json'}")
    if not ok:
        print(f"RESULT: FAIL. Attach {a.work / 'failures_for_*'}/ and the console output.")
    else:
        print("RESULT: PASS")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
