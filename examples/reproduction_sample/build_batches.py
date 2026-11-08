#!/usr/bin/env python3
"""Build extraction batches for the sample documents.

Runs paragraph segmentation and regex pre-fill on each text, the same functions
the full pipeline uses, and writes one batch JSON per year in the runner's
five-key item form. Output is a batches root the runner accepts.

Usage:
  python examples/reproduction_sample/build_batches.py --out work/repro/batches
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()

    seg = load("seg", REPO / "01_preprocess/02_segment_paragraphs.py")
    pre = load("pre", REPO / "01_preprocess/03_regex_prefill.py")
    sys.argv = [sys.argv[0], "--pack", str(HERE)]
    bat = load("bat", REPO / "01_preprocess/05_build_llm_batches_1985_2013.py")

    pools: dict[str, list] = {}
    for r in csv.DictReader(open(HERE / "manifest.csv", encoding="utf-8")):
        sid, year = r["sample_id"], r["judgment_year"]
        text = (HERE / "documents" / f"{sid}.txt").read_text(encoding="utf-8")
        d, f, j, ju = seg.segment_one(text)
        hy = int(year) if year else None
        hints = pre.extract_one(dict(defendant_segment=d, judgment_segment=j,
                                     judges_segment=ju, judge_year=hy))
        rec = dict(hints, case_id=r["case_no"], judge_year=hy,
                   defendant_segment=d, facts_segment=f, judgment_segment=j)
        pools.setdefault(year or "0000", []).append(bat.build_item(rec))

    for year, items in pools.items():
        d = a.out / f"y{year}_b200"
        d.mkdir(parents=True, exist_ok=True)
        (d / "batch_000000.json").write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    print(f"{sum(len(v) for v in pools.values())} items in {len(pools)} batch(es) under {a.out}")


if __name__ == "__main__":
    main()
