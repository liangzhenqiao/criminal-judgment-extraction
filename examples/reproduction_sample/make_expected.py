#!/usr/bin/env python3
"""Regenerate the three stage files in expected/ from documents/ and manifest.csv.

Run this only when the regex stages change on purpose. The outputs are the
golden record for check_sample.py; a diff after a change to a regex is the
review material for that change.

Usage:  python examples/reproduction_sample/make_expected.py
"""
from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PRE = HERE.parents[1] / "01_preprocess"


def load(name: str, fname: str):
    spec = importlib.util.spec_from_file_location(name, PRE / fname)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    seg = load("seg", "02_segment_paragraphs.py")
    pre = load("pre", "03_regex_prefill.py")
    sys.argv = [sys.argv[0], "--pack", str(HERE)]
    dat = load("dat", "06_extract_offense_dates_regex.py")

    segs, hints, dates = [], [], []
    for r in csv.DictReader(open(HERE / "manifest.csv", encoding="utf-8")):
        sid = r["sample_id"]
        text = (HERE / "documents" / f"{sid}.txt").read_text(encoding="utf-8")
        d, f, j, ju = seg.segment_one(text)
        segs.append(dict(sample_id=sid, defendant=d, facts=f, judgment=j, judges=ju))
        hy = int(r["judgment_year"]) if r["judgment_year"] else None
        hints.append(dict(sample_id=sid, judge_year=hy, hints=pre.extract_one(
            dict(defendant_segment=d, judgment_segment=j, judges_segment=ju, judge_year=hy))))
        dates.append(dict(sample_id=sid, dates=dat.process_row(sid, f)))

    for name, data in (("segments", segs), ("hints", hints), ("dates", dates)):
        (HERE / "expected" / f"{name}.jsonl").write_text(
            "\n".join(json.dumps(x, ensure_ascii=False, sort_keys=True) for x in data) + "\n",
            encoding="utf-8")
    print(f"{len(segs)} records written to expected/")


if __name__ == "__main__":
    main()
