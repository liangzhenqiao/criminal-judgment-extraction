# Validation scripts

Structural, corpus-wide and release-side checks. They run on the corpus archive or
on a release directory and report counts; none of them needs a model or a GPU.

The annotation and scoring scripts are not here. They ship in the validation
bundle, not in this repository, with the tables and the per-item material they read.

## Scripts

| Script | What it checks | Reads |
|---|---|---|
| `01_structural_constraints.py` | field-level structural rules over the release: ranges, sentinels, array lengths, null meanings | release parquet |
| `02_corpus_single_pass_scan.py` | one pass per archive year, three accumulations: release `id` -> hit count and selected article, per-year document counts, multi-hit selection rule | corpus, release ids |
| `03_corpus_scan_summarize.py` | summarises the scan output into the year table | scan output |
| `04_regex_privacy_scan.py` | free-text privacy screening: house numbers, ID numbers, phones, names, per column | release parquet |
| `05_build_judge_id.py` | builds the `judge_id` pseudonym from the printed signature label | release parquet |
| `check_public_release.py` | leak gate over the whole repository; exits 1 on any finding | repository tree |

Paths come from `config.yaml` in the repository root, through
`pipeline_config.py`; copy `config.example.yaml` and edit it, or pass
`--config PATH`. Relative values resolve against the config file's folder.

## Run

```sh
python 04_validation/01_structural_constraints.py
python 04_validation/04_regex_privacy_scan.py
python 04_validation/check_public_release.py
```

The two corpus scans read the whole archive and take hours; `02` accepts
`--years` to narrow the span.

## The gate

`check_public_release.py` is the six-part acceptance gate used before every
release. The repository must read as a standalone template, not as the run log
of a coding assistant, so it fails on: local machine paths and hosting detail;
e-mail addresses; internal project vocabulary; national-ID and mobile numbers;
defendant-name and birth-date forms; files over 1 MB, data files, and named
internal files. Its regular expressions are assembled from fragments, so the
script's own text does not match them; running it is the only way to see what
it catches.

The 9 findings on this snapshot are the standing baseline:

- `02_extraction/runs/20261105_full1000/failure_y2014.jsonl`: data_file
- `02_extraction/runs/20261105_full1000/failure_y2017.jsonl`: data_file
- `02_extraction/runs/20261105_full1000/failure_y2017.jsonl:1`: birth_date
- `02_extraction/runs/20261105_full1000/failure_y2017.jsonl:1`: defendant_name
- `README.md:59`: birth_date
- `README.md:129`: tool_name
- `README.md:130`: tool_name
- `README.zh-CN.md:48`: birth_date
- `README.zh-CN.md:100`: tool_name

`04_validation/tests/test_gate_samples.py` pins that set in
`baseline_findings.txt`, so a new finding fails the tests.

Three exemptions are written into the script and documented there: the
reproduction sample's published text keeps its personal detail (counted, not
failing), `CITATION.cff` may carry an address, and the reuse example's 5,000-row
subsample may be shipped as a data file. `dispatch` and work directories are
skipped.
