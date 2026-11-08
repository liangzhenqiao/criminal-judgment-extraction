# Synthetic examples

Every file at this level is **synthetic**; `reproduction_sample/` holds real
published judgments and has its own README. The texts were written for this
repository and do not come from any judgment. Person names use the masked form
"某"/"某某", dates are given to the year only, and the case numbers use the
fictitious court code "示" (e.g. `(2016)示0101刑初1号`) that does not exist.

| Input | Expected model output | Task profile |
|---|---|---|
| `criminal_defendant_input.json` | `criminal_defendant_output.json` | `02_extraction/profiles/tasks/criminal_defendant_v1.3` (21 keys) |
| `offense_date_input.json` | `offense_date_output.json` | `02_extraction/profiles/tasks/offense_date_v1.0` (7 keys) |
| `offense_name_input.json` | `offense_name_output.json` | `02_extraction/profiles/tasks/offense_name_v1.0` (5 keys) |

Outputs use the batch wire format `{"data": [...]}`, one record per input in
input order. `04_validation/tests/test_offline_extraction.py` serves the
21-key output from a local mock endpoint to run Smoke Stage A without a GPU and
checks all three outputs against their schemas and the verbatim-evidence rule.
The `sample_case.json` of each task profile is the first input of the matching
file here.
