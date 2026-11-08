# Reproduction sample: 1,000 judgments

One thousand published judgments, drawn from the release and stratified by
judgment year: 987 dated 1995–2021, one dated 2022 and 12 undated
(`judgment_year` 0). The deterministic stages need no corpus, GPU or model; the
extraction stage needs a served endpoint and is scored against recorded values.

At the measured rate of 3.0 records/s (concurrency 16) extraction takes about
6 minutes on two RTX PRO 6000 cards. The production baseline, 6.7 records/s at
concurrency 48, is a production run and not reproduced here. The agreement gate
is scored on about 7,600 items (11 fields where the release holds a value), a
95% interval of about ±0.5 percentage points at 94%.

- `documents/` — the documents as retrieved through a compilation mirror of China
  Judgments Online, minus the trailing footer line that mirror appended to each
  text, named by `sample_id`.
- `manifest.csv` — `sample_id`, `case_no`, `cjo_url`, `pool`, `judgment_year`;
  `pool` is the draw pool label (`A` = the release pool drawn for this check).
  `cjo_url` is the platform record address, not the address the text was
  fetched from; every row carries it.
- `expected/segments.jsonl`, `expected/hints.jsonl`, `expected/dates.jsonl` —
  recorded output of the three deterministic stages on these inputs.
- `expected/records.jsonl` — the released field values for the same documents,
  the reference the extraction run is scored against.
- `check_sample.py` — re-runs paragraph location, regex hint fields and
  offense-date extraction, diffs against `expected/`, exits non-zero on any difference.
- `build_batches.py` — runs the same stages and writes extraction batches, one per year.
- `compare_output.py` — scores a run, prints the six gates of `AGENT_TASK.md`,
  writes `<work>/gates.json`, exits non-zero if any gate fails.
- `make_expected.py` — rewrites the three stage files under `expected/`. Run it
  only when a regex change is intended; the diff is the review material.

The draw is seeded (`20261105`) over release rows that carry a platform URL:
each year gets a share proportional to the square root of its URL-bearing
count, with a floor of twelve where the year holds that many. The documents are
judgments published by the Supreme People's Court on China Judgments Online,
reproduced for verification under CC BY 4.0; they remain the
Court's publication.

`expected/records.jsonl` holds 19 of the 21 schema keys (`hukou_text` and
`prior_conviction_desc` are not in the release). Agreement uses the 11 scalar
fields of the 94.0% reference: `sex`, `birth_year`, `ethnicity`, `edu_level`,
`has_prior_conviction`, `is_recidivist_legal`, `party_role`, `sentence_months`,
`sentence_type`, `probation_flag`, `fine_yuan`. Items where either side is null
are counted per field in the output and not scored.

## Running the parts that need no GPU

The three deterministic stages — paragraph location, the regex hint fields and
offense-date extraction — rerun over the 1,000 documents in `documents/` one by
one and diff every record against `expected/`. That needs no corpus, GPU or
model. The extraction stage does need a served endpoint, and
`compare_output.py` scores a run against the six gates.

```bash
python examples/reproduction_sample/check_sample.py
python examples/reproduction_sample/compare_output.py --work work/repro
```

The first command passes with `0 mismatched record(s)`. The second expects a
run under `work/repro` and prints the gate table.
