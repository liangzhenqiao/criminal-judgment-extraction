# Agent task

This file is the start-up prompt for a coding agent. It is not the extraction
prompt: the extraction prompt is `pattern/criminal_defendant_v1.3.md`,
which step 6 sends to the model unchanged.

Work on your own from step 0 to step 8. Do not ask a person to confirm
anything along the way. Report only at the end (step 8), or at the first step
whose check fails.

You are a coding agent on a GPU host. Carry out steps 0 to 8 below, in order:
install, download the model, serve it, pass the smoke test, extract the 1,000
sample documents and score them. Every step names a command and a one-line pass
check. A pass check exits 0 when the step passed. If a check fails, stop at that
step and report it; do not continue past it. End with the report in step 8.

After step 1, all commands run from the repository root, held in `$REPO`. Every
block starts with `cd "$REPO"`. If the shell was reset and `$REPO` is empty or
`python` is not the venv one, run `source ~/.repro_env` (written in step 1).

Some steps run long: step 1 up to an hour with an empty package cache, step 3
one to two hours, step 6 several minutes. Many agent shells stop a command
after a few minutes. Step 3 therefore runs in `tmux` and you poll its log. If
your shell has such a limit, run the long command of step 1 or step 6 the same
way: `tmux new -d -s <name> 'bash -lc "<command> > <log> 2>&1"'`, then read the
log until it finishes. Do not start a second copy while one is running.

## 0. Preconditions

On a host in mainland China, turn on the provider's academic route first (AutoDL
provides one). Without it, port 443 to GitHub and to `astral.sh` times out on
these hosts. Elsewhere the line is a no-op.

```bash
source /etc/network_turbo 2>/dev/null || true
```

Install `uv` first if it is missing, then put its directory on `PATH`. The
installer script fetches a second file with no time limit of its own, and that
fetch has hung for minutes on a mainland China host, so `timeout` bounds the
whole installer and `pip` takes over when it fails. The last form is for images
whose system Python refuses `pip install --user`.

```bash
command -v uv || timeout -k 10 300 sh -c 'curl -m 60 -LsSf https://astral.sh/uv/install.sh | sh' \
  || python3 -m pip install --user uv || python3 -m pip install --user --break-system-packages uv
export PATH="$HOME/.local/bin:$PATH"
```

Some images ship no `tmux`, which steps 4 and 6 need. Install it
if the check below reports it missing:

```bash
command -v tmux || { apt-get update -qq && apt-get install -y -qq tmux; }
```

A public repository clones over plain HTTPS with no credentials. A private one
needs a read-only token in `$GITHUB_TOKEN`; the token also raises the rate
limits. `$GH` holds the host name. `GIT_TERMINAL_PROMPT=0` makes `git` fail at
once instead of waiting for a password nobody will type. The route from above
stays on through the access check below, through the clone and through the
Python download in step 1, which fetches an interpreter from GitHub. Step 1
then turns it off, because it slows PyPI mirrors and model downloads.

```bash
export GIT_TERMINAL_PROMPT=0
GH=github.com
if [ -n "${GITHUB_TOKEN:-}" ]; then
  REPO_URL="https://x-access-token:$GITHUB_TOKEN@$GH/liangzhenqiao/criminal-judgment-extraction.git"
else
  REPO_URL="https://$GH/liangzhenqiao/criminal-judgment-extraction.git"
fi
```

If neither URL works, stop and ask for a readable tarball or a read-only link.
Unpack it into an empty directory and start step 1 at the `cd` line. If a
local copy is handed over instead, `rsync` it into an empty directory and do
the same.

Pass checks. Run each line. Each must exit 0.

```bash
test "$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | awk '$1>=90000' | wc -l)" -ge 2   # GPUs: 2 cards, each >= 90000 MiB
test "$(df -BG --output=avail . | tail -1 | tr -dc 0-9)" -ge 165   # disk: weights 82 GB, environment ~15 GB, the doctor keeps 50 GiB (54 GB) free, plus 11 GB for the rewrite of one shard. AutoDL: pass expand_system_disk_by_gb 150 at creation, the default 80 G system disk is too small
test "$(command -v git curl tmux uv nvidia-smi | wc -l)" -eq 5   # tools: all 5 present
git ls-remote "$REPO_URL" HEAD   # repository access
```

On a reused host, an old `.venv` shares its files with the `uv` cache
(`~/.cache/uv`, about 19 GB), so deleting the `.venv` alone frees nothing. If
the disk check fails on such a host, delete the old checkout and run
`uv cache clean`, then check again. Step 1 then downloads every package again.

With a different card layout (one larger card, or more cards), set
`vllm.tensor_parallel_size` in the model profile copy (step 3) and
`env.CUDA_VISIBLE_DEVICES` in the runtime profile copy to match. Throughput
changes with the layout. The GPU check then reads: total memory across cards
≥ 180000 MiB.

## 1. Install

```bash
git clone "$REPO_URL" criminal-judgment-extraction
cd criminal-judgment-extraction && REPO=$PWD
uv venv .venv --python 3.11 && source .venv/bin/activate   # may fetch Python from GitHub, so before the unset
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY        # turbo route off from here on
if [ -f /etc/network_turbo ]; then export UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple; fi   # direct PyPI is too slow on mainland China hosts
uv pip install -r requirements.txt -r 02_extraction/requirements-serving.txt
mkdir -p work/repro work/models
printf 'export PATH="$HOME/.local/bin:$PATH"\nexport REPO=%s\ncd "$REPO" && source "$REPO/.venv/bin/activate"\n' "$REPO" > ~/.repro_env
if [ -f /etc/network_turbo ]; then echo 'export UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple' >> ~/.repro_env; fi   # keep the mirror after a shell reset
```

The `uv pip install` line downloads about 10 GB of packages (`torch`, `vllm`,
CUDA libraries). With an empty `uv` cache it took 53 minutes from the Aliyun
mirror on 5 November 2026; with a warm cache it takes a few minutes. Long
silences between `Downloaded` lines are normal.

Other mainland China hosts have no `/etc/network_turbo`: run
`export UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple` yourself before
this block, and add it to `~/.repro_env` the same way. If the Aliyun mirror
fails, use `https://pypi.tuna.tsinghua.edu.cn/simple`.

Pass check: `cd "$REPO" && python -c "import vllm, litellm, pandas"`

## 2. Check the deterministic stages (no GPU)

```bash
cd "$REPO"
python examples/reproduction_sample/check_sample.py | tee work/repro/check_sample.log
```

Pass check: `cd "$REPO" && grep -qx '1000 documents checked, 0 mismatched record(s).' work/repro/check_sample.log`

Any mismatch means the checkout or the Python packages differ from the
released ones; reinstall from step 1 before going on.

## 3. Write the profiles, then download the model

Write the two profiles this run uses:

```bash
cd "$REPO"
sed -e "s#<work-base>/conda_envs/vllm#$REPO/.venv#" \
    -e "s#<work-base>/modelscope_models#$REPO/work/models#" \
    -e "s#<work-base>#$REPO#" \
    02_extraction/profiles/runtime/blackwell_2x96gb.yaml > work/repro/runtime.yaml
cp 02_extraction/profiles/models/qwen3-next-80b-fp8.yaml work/repro/model.yaml
```

Pass check: `cd "$REPO" && test "$(grep -c '<work-base>' work/repro/runtime.yaml)" = 0`

Download the model. The weights are 82 GB in 8 shards, and step 0 checked the
disk for them. `02_extraction/scripts/download_model.sh` picks one source and
downloads from it:

- ModelScope on a mainland China host (one with `/etc/network_turbo`, or where
  `huggingface.co` does not answer within 10 seconds);
- Hugging Face elsewhere.

`MODEL_SOURCE=hf` or `MODEL_SOURCE=modelscope` in front of the command
overrides the choice. On a mainland host forced to Hugging Face, also set
`HF_ENDPOINT=https://hf-mirror.com`. The script uses one source per run
because the two hosts publish different hashes under the same file names. A
Hugging Face attempt that adds less than 300 MB in any five minutes counts as
failed; after two such attempts the script clears the directory and switches to
ModelScope, which resumes from the last byte. It ends by running the checks
below and prints `DOWNLOAD_DONE` or `DOWNLOAD_FAILED`.

Measured on 5 November 2026 from a mainland China host: ModelScope averaged
about 15 MB/s and took 94 minutes; the Hugging Face mirror stalled at 630 MB,
then ran at about 1 MB/s. On a direct link outside China, Hugging Face usually
takes 10–40 minutes.

Run it in `tmux`, because it outlasts most agent shell time limits:

```bash
cd "$REPO"
tmux new -d -s model_download "bash 02_extraction/scripts/download_model.sh > work/repro/download.log 2>&1"
```

Check every few minutes with `tail -3 work/repro/download.log`, until the last
line is `DOWNLOAD_DONE` or `DOWNLOAD_FAILED`. On `DOWNLOAD_FAILED`, read
`work/repro/modelscope.log` or `work/repro/hf.log`, then run the same `tmux`
line once more; ModelScope resumes.

A half-finished download already holds `config.json`, so check that all eight
shards are closed and the size is there, not that file. The size is in bytes:
82 GB is 76.5 GiB, so `du -BG` would read 77. Pass check, all three must exit 0:

```bash
cd "$REPO"
D=work/models/Qwen3-Next-80B-A3B-Instruct-FP8
test "$(find "$D" -name '*.safetensors' | wc -l)" -eq 8
test -z "$(find "$D" \( -name '*.incomplete' -o -name '*.partial' \) | head -1)"
test "$(du -sB1 "$D" | cut -f1)" -ge 80000000000
```

## 4. Serve the model and run the smoke test

```bash
cd "$REPO/02_extraction"
bash scripts/run_smoke.sh --runtime "$REPO/work/repro/runtime.yaml" \
    --model "$REPO/work/repro/model.yaml" \
    --task profiles/tasks/criminal_defendant_v1.3 | tee "$REPO/work/repro/smoke.log"
cd "$REPO"
```

This runs the environment doctor, the weight check, starts vLLM and LiteLLM in
tmux, and sends one case through the production prompt. Loading the weights
takes several minutes.

Pass check: `cd "$REPO" && tail -1 work/repro/smoke.log | grep -Fx '{"final":"ENV_STAGE_A_OK"}'`

On failure the driver prints the failed gate and its run directory under
`02_extraction/runs/`; report that gate and the files in that directory. The
services keep running after this step.

### 4b. Confirm the service is up

Run this right before step 6, and again after any pause or shell reset.

Pass check: `curl -s -H "Authorization: Bearer sk-dummy" http://127.0.0.1:4000/v1/models | grep -q legal-extractor-80b`

If it fails, go back to step 4.

## 5. Build the batches

```bash
cd "$REPO"
python examples/reproduction_sample/build_batches.py --out work/repro/batches | tee work/repro/batches.log
```

Pass check: `cd "$REPO" && grep -Fx '1000 items in 29 batch(es) under work/repro/batches' work/repro/batches.log`

## 6. Extract

```bash
cd "$REPO"
export LITELLM_URL=http://127.0.0.1:4000/v1/chat/completions
export LITELLM_MODEL=legal-extractor-80b LITELLM_THINKING=false
python 02_extraction/runner/run_full_extraction.py \
    --pattern pattern/criminal_defendant_v1.3.md \
    --batches work/repro/batches --bundle 4 --concurrency 16 --max-tokens 3072 \
    --work-root work/repro 2>&1 | tee work/repro/extract.log
```

About 6 minutes on 2 × RTX PRO 6000, longer on slower cards; do not stop it early.

Pass check: `cd "$REPO" && grep -q 'FINAL:' work/repro/extract.log && python -c "import json; s=json.load(open('work/repro/state/batch_state.json')); assert len(s)==29 and all(v['status']=='done' for v in s.values())"`

If the run is interrupted, or some batches end as `failed`: run the step 4b
check (if it fails, repeat step 4), then rerun the same command once. It skips
finished batches and retries the rest. Do not pass `--restart`: it discards the
batch state.

## 7. Score

```bash
cd "$REPO"
python examples/reproduction_sample/compare_output.py --work work/repro | tee work/repro/score.log
```

Pass check: `cd "$REPO" && tail -1 work/repro/score.log | grep -Fx 'RESULT: PASS' && python -c "import json; assert json.load(open('work/repro/gates.json'))['all_pass'] is True"`

## 8. Report

- the gate table and the per-field table printed by step 7 (`work/repro/score.log`);
- wall time of step 6 and the failure codes with their counts;
- `nvidia-smi --query-gpu=name --format=csv,noheader` and `uv pip show vllm | head -2`;
- if any gate failed: its name, `work/repro/gates.json`, the failure ledger
  directory `work/repro/failures_for_*/` (that literal name) and `work/repro/extract.log`.

## Wrap-up

After the report, stop the two services. `tmux ls` lists them as
`vllm_serve_*` and `litellm_proxy_*`. Kill each with
`tmux kill-session -t <name>`, or all at once:

```bash
tmux ls -F '#S' | grep -E '^(vllm_serve|litellm_proxy)_' | xargs -r -n1 tmux kill-session -t
```

Pass check: `! tmux ls -F '#S' 2>/dev/null | grep -qE '^(vllm_serve|litellm_proxy)_'`

The instance can then be shut down. Copy `work/repro/` off the host first if
the report needs it.

## Rules

- Do not edit anything under `examples/reproduction_sample/expected/` or the
  `pattern/`, and do not tune the prompt or the gates.
- Do not fetch the corpus. All output stays under `work/`.
- Agreement under 94.0% with failures under 2% means the model or endpoint
  differs from the production pairing: report both numbers and the model identifier.

## Gates

| Gate | Threshold | Source |
|---|---|---|
| Documents returning an accepted record | ≥ 980 of 1,000 | 1,000 minus the 2% failure ceiling, so this gate and the failure-rate gate agree |
| Agreement with recorded values | ≥ 94.0% | production model and prompt: 2,486 of 2,645 items, same 11 fields, items where both sides hold a value |
| Identifier preserved | 100% | `judge_signature_v1.0/acceptance.yaml` (`id_preserved_pct_min`) |
| Record failure rate | ≤ 2% | same file (`fail_rate_max`) |
| Throughput | ≥ 0.80 records/s | floor for a stalled endpoint; production baseline 6.7 items/s at concurrency 48, the measured run did 3.0 at concurrency 16 |
| Deterministic-stage diff | 0 | `check_sample.py` |
