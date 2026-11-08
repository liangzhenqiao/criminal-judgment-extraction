#!/usr/bin/env python3
"""Re-run the deterministic stages on the sample and diff against expected/.

The sample is 1,000 real judgments, one per row of manifest.csv. This script runs
the parts of the pipeline that need no GPU or model:

  segment_one()   paragraph location          (01_preprocess/02_segment_paragraphs.py)
  extract_one()   regex hint fields           (01_preprocess/03_regex_prefill.py)
  process_row()   offense-date regex          (01_preprocess/06_extract_offense_dates_regex.py)

and compares each record to expected/segments.jsonl, expected/hints.jsonl and
expected/dates.jsonl. Exit status is 1 on any mismatch. Nothing here calls a
model; the extraction stage itself still needs a served endpoint.

Usage:  python examples/reproduction_sample/check_sample.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PRE = REPO / "01_preprocess"


def load(name: str, fname: str):
    spec = importlib.util.spec_from_file_location(name, PRE / fname)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_jsonl(p: Path):
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]


def main() -> int:
    seg_mod = load("seg", "02_segment_paragraphs.py")
    pre_mod = load("pre", "03_regex_prefill.py")
    # import time of the date module (06) reads the --pack path off sys.argv at
    # module level, so give it a placeholder before loading.
    sys.argv = [sys.argv[0], "--pack", str(HERE)]
    dat_mod = load("dat", "06_extract_offense_dates_regex.py")

    expected_seg = {r["sample_id"]: r for r in read_jsonl(HERE / "expected" / "segments.jsonl")}
    expected_hint = {r["sample_id"]: r for r in read_jsonl(HERE / "expected" / "hints.jsonl")}
    expected_date = {r["sample_id"]: r for r in read_jsonl(HERE / "expected" / "dates.jsonl")}

    bad = 0
    for sid in sorted(expected_seg):
        text = (HERE / "documents" / f"{sid}.txt").read_text(encoding="utf-8")
        d, f, j, ju = seg_mod.segment_one(text)
        got_seg = {"sample_id": sid, "defendant": d, "facts": f, "judgment": j, "judges": ju}
        if got_seg != expected_seg[sid]:
            bad += 1
            print(f"[segments] {sid}: differs from expected")

        row = {"defendant_segment": d, "judgment_segment": j, "judges_segment": ju,
               "judge_year": expected_hint[sid].get("judge_year")}
        got_hint = pre_mod.extract_one(row)
        if got_hint != expected_hint[sid]["hints"]:
            bad += 1
            print(f"[hints] {sid}: differs from expected")

        got_date = dat_mod.process_row(sid, f)
        if got_date != expected_date[sid]["dates"]:
            bad += 1
            print(f"[dates] {sid}: differs from expected")

    print(f"\n{len(expected_seg)} documents checked, {bad} mismatched record(s).")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
