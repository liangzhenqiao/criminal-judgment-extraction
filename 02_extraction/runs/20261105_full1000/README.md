# Verdict of the 5 November 2026 full run

Full 1,000-document reproduction check, run end to end on one GPU host on
5 November 2026. It is the run quoted in the paper's Code availability
statement.

| gate | value | threshold |
|---|---|---|
| documents | 998 | ≥ 980 |
| agreement | 0.9723 | ≥ 0.94 |
| id_preserved | 1.0000 | = 1.0 |
| fail_rate | 0.0020 | ≤ 0.02 |
| throughput (records/s) | 3.10 | ≥ 0.8 |
| deterministic_diff | 0 | = 0 |

All six gates pass. 999 records were accepted and 2 failed, both with the
code `consistency_violation:prior_arrays_not_aligned`; the two ledger entries
are `failure_y2014.jsonl` and `failure_y2017.jsonl`. Batch wall time was 322 s.

- `gates.json` — the gate values, thresholds and per-field match table written
  by `02_extraction/scripts/smoke_gate.py`-equivalent scoring.
- `run_summary.json` — the run's own summary: configuration, call count,
  wall time, failure codes.

The 3 November 2026 run over 250 candidate records (13 of 14 gates, throughput
trip at 0.86–0.93 records/s) was read from the console and has no retained
verdict file.
