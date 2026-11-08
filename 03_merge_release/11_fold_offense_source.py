#!/usr/bin/env python3
"""Build release v2.4 from the locked v2.3 by folding ``offense_source``.

A project decision of 2026-07-31 locks the mapping.  The released provenance column
carried thirteen internal engineering tokens while the shipped dictionary
declared three, so every user who ran ``value_counts()`` met values the
documentation could not explain.  v2.4 collapses that column to the public
domain -- four states plus null -- and changes nothing else.

Versioning rule: this writes a NEW directory.  ``release_v2_3_*`` is read-only here
and is archived by the caller after ``config.yaml`` is repointed.

Fidelity discipline
-------------------
The parquet is rewritten through pyarrow with a single column replaced, so the
other 43 columns are the same arrays that were read -- equality is asserted
column by column, not assumed.  The CSV mirror is rewritten field-wise from the
original text rather than re-serialised from the parquet, because re-deriving
the JSON encoding of the ``prior_*`` columns would risk silent reformatting of
columns this release is not supposed to touch.  A round-trip check proves the
parse/emit cycle is byte-identical before the real pass runs.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

__import__("sys").path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_config  # paths come from config.yaml (see config.example.yaml)
CFG = pipeline_config.load(handle_help=True, keys=('work_dir',))
ROOT = CFG.path("work_dir")  # intermediates live under <work_dir>/data_cleaning/{raw,master}
SRC_DIR = ROOT / "data_cleaning" / "master" / "release_v2_3_20260709134500"
STAMP = "20260731090238"
OUT_DIR = ROOT / "data_cleaning" / "master" / f"release_v2_4_{STAMP}"

SRC_PARQUET = SRC_DIR / "china_criminal_defendant_panel_1985_2021_v2_3.parquet"
SRC_CSV = SRC_DIR / "china_criminal_defendant_panel_1985_2021_v2_3.csv.gz"
OUT_PARQUET = OUT_DIR / "china_criminal_defendant_panel_1985_2021_v2_4.parquet"
OUT_CSV = OUT_DIR / "china_criminal_defendant_panel_1985_2021_v2_4.csv.gz"
OUT_DICT = OUT_DIR / f"field_dictionary_{STAMP}.csv"

# Decision B -- the only permitted mapping.  A token absent from this table
# is a build failure, not something to classify on the fly.
MAPPING = {
    "regex": "regex",
    "regex_gpu": "regex",
    "regex_bailian": "regex",   # 早期重跑批次令牌；公开列只留折叠后的值
    "regex_fulltext": "regex",
    "llm": "llm",
    "llm_supplement": "llm",
    "llm_wave2": "llm",
    "llm_retry": "llm",
    "llm_no_time": "llm_no_time",
    "llm_failed": "failed",
    "llm_empty": "failed",
    "llm_garbled": "failed",
    "no_source": "failed",
}
PUBLIC_DOMAIN = {"regex", "llm", "llm_no_time", "failed"}

EXPECTED_ROWS = 2_834_831
EXPECTED_FAILED = 4_411
EXPECTED_NULL = 55_800
# Substrings that must not survive anywhere in the released column.
FORBIDDEN = ("bailian", "gpu", "wave", "supplement", "retry", "empty",
             "garbled", "no_source", "fulltext")

DICT_DESCRIPTION = (
    "Offense-date derivation state: regex (rule) / llm (model) / llm_no_time "
    "(model honest-null, no offense time stated) / failed (extraction did not "
    "produce a usable value). Null where the field was not processed."
)


def fold(value):
    if value is None:
        return None
    if value not in MAPPING:
        raise AssertionError(f"unmapped offense_source token: {value!r}")
    return MAPPING[value]


def build_parquet():
    table = pq.read_table(SRC_PARQUET)
    assert table.num_rows == EXPECTED_ROWS, table.num_rows
    idx = table.schema.get_field_index("offense_source")
    field = table.schema.field(idx)

    original = table.column(idx).to_pylist()
    folded = [fold(v) for v in original]
    new_col = pa.chunked_array([pa.array(folded, type=field.type)])
    out = table.set_column(idx, field, new_col)

    # Ruling F: every other column must be byte-equal to the source.
    for i in range(table.num_columns):
        if i == idx:
            continue
        assert out.column(i).equals(table.column(i)), \
            f"column drifted: {table.schema.field(i).name}"
        assert out.schema.field(i) == table.schema.field(i)
    assert out.schema.field(idx) == field, "offense_source type changed"

    pq.write_table(out, OUT_PARQUET, compression="zstd")
    return folded


def roundtrip_is_lossless(header, sample=200_000):
    """Prove parse+emit reproduces the source bytes before rewriting 2.8M rows."""
    with gzip.open(SRC_CSV, "rt", encoding="utf-8", newline="") as fh:
        fh.readline()  # header, re-emitted separately
        buf, reader = [], None
        raw = []
        for n, line in enumerate(fh):
            if n >= sample:
                break
            raw.append(line)
    parsed = list(csv.reader(raw))
    import io
    sio = io.StringIO(newline="")
    csv.writer(sio, lineterminator="\n").writerows(parsed)
    return sio.getvalue() == "".join(raw)


def build_csv():
    with gzip.open(SRC_CSV, "rt", encoding="utf-8", newline="") as fh:
        header = fh.readline().rstrip("\n").split(",")
    col = header.index("offense_source")

    if not roundtrip_is_lossless(header):
        raise AssertionError(
            "csv parse/emit round-trip is not byte-identical; refusing to "
            "rewrite the mirror because non-target columns could drift"
        )

    counts = {}
    with gzip.open(SRC_CSV, "rt", encoding="utf-8", newline="") as src, \
         gzip.open(OUT_CSV, "wt", encoding="utf-8", newline="") as dst:
        reader = csv.reader(src)
        writer = csv.writer(dst, lineterminator="\n")
        writer.writerow(next(reader))
        for row in reader:
            token = row[col]
            new = "" if token == "" else MAPPING[token]
            row[col] = new
            counts[new] = counts.get(new, 0) + 1
            writer.writerow(row)
    return counts


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main():
    if OUT_DIR.exists():
        sys.exit(f"refusing to overwrite existing {OUT_DIR}")
    OUT_DIR.mkdir(parents=True)

    print("parquet ...", flush=True)
    folded = build_parquet()

    vc = {}
    for v in folded:
        vc[v] = vc.get(v, 0) + 1
    nulls = vc.pop(None, 0)

    assert set(vc) <= PUBLIC_DOMAIN, sorted(vc)
    assert vc.get("failed") == EXPECTED_FAILED, vc.get("failed")
    assert nulls == EXPECTED_NULL, nulls
    assert sum(vc.values()) + nulls == EXPECTED_ROWS
    blob = " ".join(vc)
    for bad in FORBIDDEN:
        assert bad not in blob, bad

    print("csv mirror ...", flush=True)
    csv_counts = build_csv()
    csv_nulls = csv_counts.pop("", 0)
    assert csv_nulls == EXPECTED_NULL, csv_nulls
    assert csv_counts == vc, (csv_counts, vc)

    # source column untouched and already homogeneous (Ruling A).
    src_vals = pq.read_table(OUT_PARQUET, columns=["source"]).column(0).to_pylist()
    assert set(src_vals) == {"qwen-open-model-pipeline"}, set(src_vals)

    # dictionary: only the offense_source description changes.
    rows = list(csv.reader((SRC_DIR / "field_dictionary_20260709134500.csv")
                           .read_text(encoding="utf-8").splitlines()))
    hit = 0
    for r in rows[1:]:
        if r[0] == "offense_source":
            r[4] = DICT_DESCRIPTION
            hit += 1
    assert hit == 1, hit
    with open(OUT_DICT, "w", encoding="utf-8", newline="") as fh:
        csv.writer(fh, lineterminator="\n").writerows(rows)

    # carried through unchanged
    for name in ("pii_scan_report.json", "INTERNAL_provenance_audit.json"):
        shutil.copy2(SRC_DIR / name, OUT_DIR / name)

    qa = json.loads((SRC_DIR / "build_qa_report.json").read_text())
    qa["ts"] = STAMP
    qa["offense_source_states"] = {k: vc[k] for k in sorted(vc)}
    qa["offense_source_null"] = nulls
    (OUT_DIR / "build_qa_report.json").write_text(
        json.dumps(qa, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    readme = (SRC_DIR / "README.md").read_text(encoding="utf-8")
    readme = readme.replace("v2.3, 20260709134500", f"v2.4, {STAMP}")
    readme = readme.replace("_v2_3.parquet", "_v2_4.parquet")
    readme = readme.replace("field_dictionary_20260709134500.csv",
                            f"field_dictionary_{STAMP}.csv")
    readme = readme.replace(
        "- **offense_date coverage 90.56%**",
        "- **offense_source**: the offense-date derivation state, one of `regex`, "
        "`llm`, `llm_no_time` (the document states no offense time), or `failed` "
        "(extraction produced no usable value); null where the field was not\n"
        "  processed. Treat `llm_no_time` as attested-absent and `failed` as "
        "unknown -- they are not interchangeable.\n"
        "- **offense_date coverage 90.56%**")
    readme = readme.replace(
        "offense_name & offense_L1 cover ~99% of records",
        "offense_name & offense_L1 cover 97.55% of records")
    (OUT_DIR / "README.md").write_text(readme, encoding="utf-8")

    names = sorted(p.name for p in OUT_DIR.iterdir() if p.name != "SHA256SUMS.txt")
    (OUT_DIR / "SHA256SUMS.txt").write_text(
        "".join(f"{sha256(OUT_DIR / n)}  {n}\n" for n in names), encoding="utf-8")

    print("\n=== release v2.4 offense_source ===")
    for k in sorted(vc):
        print(f"  {k:<12} {vc[k]:>10,}  {vc[k]/EXPECTED_ROWS*100:6.3f}%")
    print(f"  {'null':<12} {nulls:>10,}  {nulls/EXPECTED_ROWS*100:6.3f}%")
    print(f"  {'total':<12} {EXPECTED_ROWS:>10,}")
    print(f"\nwrote {OUT_DIR}")


if __name__ == "__main__":
    main()
