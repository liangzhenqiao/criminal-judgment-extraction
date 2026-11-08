# 02_extraction — contract-bound model extraction

The extraction engine serves Qwen3-Next-80B-A3B-Instruct-FP8 with vLLM
(tensor parallelism 2, FP8, prefix caching) behind a LiteLLM model alias and
runs batched, schema-checked extraction against a task profile.

## Task profiles (`profiles/tasks/`)

| Profile | Keys | Input fields | Notes |
|---|---|---|---|
| `criminal_defendant_v1.3` | 21 | `defendant_segment`, `facts_segment`, `judgment_segment`, `regex_extracted` | main task. `pattern.md` is the production system prompt and is sent unchanged, snapshot header included. The copy deposited with the data drops that header |
| `offense_date_v1.0` | 7 | `facts_segment` | offense time; evidence strings must be verbatim substrings |
| `offense_name_v1.0` | 5 | `case_title`, `judgment_segment`, `facts_segment` | residual of the rule-first offense-name waterfall |

Each profile holds the system prompt (`pattern.md`), the output schema
(`schema.json`), acceptance thresholds (`acceptance.yaml`), the grounding check
(`grounding.py`), the verdict specification and a synthetic `sample_case.json`.

The deposited prompt is `pattern.md` with its snapshot header removed; no other
prompt file is maintained. The deposited schema (`release_output_schema.json`)
differs from the production `schema.json` in one way only: it omits
`prior_count` and `prior_total_months` from `forbidden_keys`, which the runner
checks but the release does not carry.

## Order of operations on the GPU host

```bash
uv pip install -r requirements-serving.txt
RT=profiles/runtime/blackwell_2x96gb.yaml        # edit paths first
MD=profiles/models/qwen3-next-80b-fp8.yaml
TASK=profiles/tasks/criminal_defendant_v1.3
bash scripts/doctor.sh --runtime $RT --model $MD          # Gate 0
bash scripts/verify_artifact.sh --runtime $RT --model $MD # Gate 1
bash scripts/launch_vllm.sh --runtime $RT --model $MD     # Gate 2
bash scripts/launch_litellm.sh --runtime $RT --model $MD  # Gate 3
bash scripts/run_smoke.sh --runtime $RT --model $MD --task $TASK   # Gates 4-7
python runner/run_full_extraction.py --pattern $TASK/pattern.md \
    --batches <batch dir> --bundle 4 --concurrency 16 --work-root <work dir>
```

The reproduction run uses concurrency 16 and measured 3.0 records/s. The
production baseline of 6.7 records/s was taken at concurrency 48.

Python scripts print their options with `--help`; shell scripts document their
options in the header comment. Failed
responses are written to `<work-root>/failures_for_*/` (the directory
name is fixed by the runner) together with the full input case, so they can be retried
or excluded without returning to the batch files.

## Offline tests

`04_validation/tests/test_offline_extraction.py` runs the smoke-gate self-test, Smoke Stage A
against a local mock endpoint that answers with `examples/`, and schema and
grounding checks on the synthetic examples. No GPU is needed.
