#!/usr/bin/env python3
"""Final release step: v2.4 package -> deposited release (v2.9, 43 columns).

This script reproduces the last data change before deposit. It was executed
on 2026-08-08 as a recorded sequence of inline commands; this file transcribes
those commands into one script. Releases v2.5 to v2.8 only repackaged files
(file names, README, removal of internal report files — the PII report, the QA
table, the provenance audit and the field-dictionary draft); their panel parquet is
byte-identical to v2.4 (sha256 4d279caa...c364).

Steps (same order as the recorded run):
  1. drop the internal ``source`` column (44 -> 43 columns) from the parquet
     and from the gzipped CSV mirror; row count must stay 2,834,831;
  2. map residual English ``sentence_type`` tokens to the Chinese labels used
     everywhere else (42 rows in the v2.4 panel);
  3. drop the ``source`` row from the field dictionary and declare the four
     boolean columns as ``bool`` instead of ``object``;
  4. copy the production system prompt and output schema of the 21-key task;
  5. write SHA256SUMS.txt.
The deposit README was written by hand and is not generated here.

Input : <work_dir>/data_cleaning/master/release_v2_4_20260731090238 (11_fold_offense_source.py)
Output: <work_dir>/data_cleaning/master/release_v2_9_<STAMP>/  (must not exist)

Usage:
  python 03_merge_release/12_finalize_release.py [--config config.yaml] [--stamp YYYYMMDDHHMMSS]
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import os
import shutil
import sys
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_config  # noqa: E402

N_ROWS = 2_834_831
STEM_IN = "china_criminal_defendant_panel_1985_2021_v2_4"
STEM_OUT = "china_criminal_judgment_records_1985_2021"
SENTENCE_TYPE_MAP = {
    "single_fine": "单处罚金", "other": "其他", "exemption": "免于处罚", "fine_only": "单处罚金",
    "life_imprisonment": "无期徒刑", "prison": "有期徒刑", "single_penalty": "单处罚金",
}
BOOL_COLS = ("probation_flag", "has_prior_conviction", "is_recidivist_legal", "no_offense_time")
PROFILE = Path(__file__).resolve().parents[1] / "02_extraction/profiles/tasks/criminal_defendant_v1.3"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None, help="pipeline config YAML (default: config.yaml in repo root)")
    ap.add_argument("--stamp", default=time.strftime("%Y%m%d%H%M%S"))
    args = ap.parse_args()
    cfg = pipeline_config.load()
    master = cfg.path("work_dir") / "data_cleaning" / "master"
    src = master / "release_v2_4_20260731090238"
    out = master / f"release_v2_9_{args.stamp}"
    if out.exists():
        sys.exit(f"refuse: {out} exists (versions are never overwritten)")
    out.mkdir(parents=True)

    # 1a. parquet without `source`
    pf = pq.ParquetFile(src / f"{STEM_IN}.parquet")
    cols = [f.name for f in pf.schema_arrow]
    assert "source" in cols and len(cols) == 44, cols
    keep = [c for c in cols if c != "source"]
    schema = pa.schema([f for f in pf.schema_arrow if f.name != "source"])
    tmp = out / "_step1.parquet"
    with pq.ParquetWriter(tmp, schema) as w:
        for b in pf.iter_batches(columns=keep, batch_size=65536):
            w.write_batch(b)
    # 2. sentence_type English residuals -> Chinese labels
    pf1 = pq.ParquetFile(tmp)
    fixed = 0
    final = out / f"{STEM_OUT}.parquet"
    with pq.ParquetWriter(final, pf1.schema_arrow) as w:
        for b in pf1.iter_batches(batch_size=65536):
            col = b.column("sentence_type").to_pylist()
            fixed += sum(1 for v in col if v in SENTENCE_TYPE_MAP)
            new = pa.array([SENTENCE_TYPE_MAP.get(v, v) if v is not None else None for v in col], pa.large_string())
            arrs = [new if n == "sentence_type" else b.column(n) for n in b.schema.names]
            w.write_batch(pa.RecordBatch.from_arrays(arrs, schema=pf1.schema_arrow))
    os.remove(tmp)
    pf2 = pq.ParquetFile(final)
    assert len(pf2.schema_arrow) == 43 and pf2.metadata.num_rows == N_ROWS
    print(f"step 1-2: 44 -> 43 columns, {N_ROWS:,} rows, sentence_type mapped: {fixed}")

    # 1b. CSV mirror without `source` (field-wise, no re-serialisation of other columns)
    with gzip.open(src / f"{STEM_IN}.csv.gz", "rt", newline="") as f, \
            gzip.open(out / f"{STEM_OUT}.csv.gz", "wt", newline="") as o:
        rd, wr = csv.reader(f), csv.writer(o)
        head = next(rd)
        si = head.index("source")
        wr.writerow([x for i, x in enumerate(head) if i != si])
        n = 0
        for r in rd:
            wr.writerow([x for i, x in enumerate(r) if i != si])
            n += 1
    assert n == N_ROWS, n
    print(f"step 1b: csv.gz {n:,} rows")

    # 3. field dictionary
    dict_in = next(src.glob("field_dictionary_*.csv"))
    lines = dict_in.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [lines[0]]
    for line in lines[1:]:
        name = line.split(",")[0]
        if name == "source":
            continue
        if name in BOOL_COLS:
            line = line.replace(",object,", ",bool,")
        kept.append(line)
    assert len(kept) == 44, len(kept)
    (out / "field_dictionary.csv").write_text("".join(kept), encoding="utf-8")

    # 4. instruction set of the 21-key task (copies deposited with the data).
    # The prompt is the task's pattern.md with its internal snapshot header removed.
    prompt = (PROFILE / "pattern.md").read_text(encoding="utf-8")
    if prompt.startswith("<!--"):
        prompt = prompt[prompt.index("-->") + 3:].lstrip("\n")
    (out / "system_prompt_criminal_defendant.md").write_text(prompt, encoding="utf-8")
    shutil.copy(PROFILE / "release_output_schema.json", out / "extraction_schema_criminal_defendant.json")

    # 5. checksums
    sums = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}"
            for p in sorted(out.iterdir()) if p.is_file()]
    (out / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")
    print("release:", out)


if __name__ == "__main__":
    main()
