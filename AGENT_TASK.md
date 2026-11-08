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

After step 0a, all commands run from the repository root, held in `$REPO`. Every
block starts with `cd "$REPO"`. If the shell was reset and `$REPO` is empty or
`python` is not the venv one, run `source ~/.repro_env` (written in step 0a).

From an empty host the whole run took 2 h 07 min to 2 h 12 min on 8 and 9
November 2026 (2 × RTX PRO 6000, mainland China), most of it the model
download. Some steps run long: step 1 7 to 55 minutes with an empty package
cache, step 3 one to two hours,
step 6 several minutes. Many agent shells stop a command after a few minutes.
Step 3 therefore runs in `tmux` and you poll its log. If your shell has such a
limit, run the long command of step 1 or step 6 the same way:
`tmux new -d -s <name> 'bash -lc "<command> > <log> 2>&1"'`, then read the log
until it finishes. Do not start a second copy while one is running.

### Network: how sources are chosen

Nothing in this file depends on which provider rents the host. Every download
has an ordered list of sources. Step 0c measures them on this host with a short
real download and ranks them by speed; the scripts then go down the list on
their own when a source fails or stalls. Each step below also prints that list
as a table: if a script itself breaks, take the next row by hand. Stop and
report only when every row of a table has failed.

| What | Chosen by | Sources, in the order tried when the probe cannot rank them |
|---|---|---|
| repository | step 0a | direct HTTPS (or the proxy already set in `HTTPS_PROXY`) → AutoDL's academic route, only on AutoDL → ask for a tarball |
| `uv` | step 0b | already installed → `pip install uv` from the image's index, `pypi.org`, Tsinghua, USTC → the `astral.sh` installer |
| package index | step 0c | the image's preset index → cloud-internal mirrors → `pypi.org` → Tsinghua, USTC, BFSU, SJTU, NJU, Tencent, Huawei, Aliyun |
| Python 3.11 | step 0c | uv's default source (GitHub) → NJU and USTC mirrors → AutoDL's academic route, only on AutoDL |
| model weights | step 0c | ModelScope, Hugging Face, hf-mirror.com, ranked by speed; an existing copy in `MODEL_DIR` comes first |

The one provider-specific line is AutoDL's academic route,
`source /etc/network_turbo`. That file exists only on AutoDL hosts. The route
reaches GitHub, but it slows package mirrors and model downloads, so this file
turns it on only for a GitHub fetch that failed without it and turns it off
straight after. On any other provider, skip those lines; the probe finds the
sources that work there.

## 0. Preconditions

Pass checks. Run each line. Each must exit 0.

```bash
test "$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | awk '$1>=90000' | wc -l)" -ge 2   # GPUs: 2 cards, each >= 90000 MiB
test "$(df -BG --output=avail . | tail -1 | tr -dc 0-9)" -ge 165   # disk: weights 82 GB, environment ~15 GB, the doctor keeps 50 GiB (54 GB) free, plus 11 GB for the rewrite of one shard. AutoDL: pass expand_system_disk_by_gb 150 at creation, the default 80 G system disk is too small
test "$(command -v git curl nvidia-smi | wc -l)" -eq 3   # tools: all 3 present
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

### 0a. Clone the repository

A public repository clones over plain HTTPS with no credentials. A private one
needs a read-only token in `$GITHUB_TOKEN`; the token also raises the rate
limits. `GIT_TERMINAL_PROMPT=0` makes `git` fail at once instead of waiting for
a password nobody will type. When the direct clone fails and the host is an
AutoDL host, the clone is retried once through AutoDL's academic route, inside
`( … )` so the route ends with that line.

```bash
export GIT_TERMINAL_PROMPT=0
GH=github.com
if [ -n "${GITHUB_TOKEN:-}" ]; then
  REPO_URL="https://x-access-token:$GITHUB_TOKEN@$GH/liangzhenqiao/criminal-judgment-extraction.git"
else
  REPO_URL="https://$GH/liangzhenqiao/criminal-judgment-extraction.git"
fi
timeout 900 git clone "$REPO_URL" criminal-judgment-extraction \
  || { [ -f /etc/network_turbo ] && rm -rf criminal-judgment-extraction \
       && ( source /etc/network_turbo && timeout 900 git clone "$REPO_URL" criminal-judgment-extraction ); }
cd criminal-judgment-extraction && REPO=$PWD
mkdir -p work/repro work/models
cat > ~/.repro_env <<EOF
export PATH="\$HOME/.local/bin:\$PATH"
export REPO=$REPO
cd "\$REPO"
[ -f work/repro/net.env ] && . work/repro/net.env
[ -f .venv/bin/activate ] && source .venv/bin/activate
EOF
```

`~/.repro_env` restores `$REPO`, the sources chosen in step 0c and the venv
after a shell reset.

If both clones fail, stop and ask for a readable tarball or a read-only link.
Unpack it into an empty directory, `cd` into it and continue from the
`REPO=$PWD` line. If a local copy is handed over instead, `rsync` it into an
empty directory and do the same.

Pass check: `cd "$REPO" && test -f AGENT_TASK.md && test -f ~/.repro_env`

### 0b. Install `uv` and `tmux`

`uv` comes from the Python package index, with the image's own `pip` settings
first, then `pypi.org`, Tsinghua and USTC. Some images have no `python3` on the
`PATH` of a non-interactive shell; the loop also tries the usual conda paths.
The `astral.sh` installer is the last resort, because its second download comes
from GitHub, and that fetch has hung for minutes on a mainland China host.
`timeout` bounds every attempt.

```bash
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null; then
  for py in python3 python "$HOME/miniconda3/bin/python" /opt/conda/bin/python /usr/bin/python3; do
    command -v "$py" >/dev/null || continue
    for idx in "" https://pypi.org/simple https://pypi.tuna.tsinghua.edu.cn/simple https://mirrors.ustc.edu.cn/pypi/simple; do
      { timeout 300 "$py" -m pip install -q --user ${idx:+-i "$idx"} uv \
        || timeout 300 "$py" -m pip install -q --user --break-system-packages ${idx:+-i "$idx"} uv; } \
        && command -v uv >/dev/null && break 2
    done
  done
fi
command -v uv >/dev/null || timeout -k 10 300 sh -c 'curl -m 60 -LsSf https://astral.sh/uv/install.sh | sh' \
  || { [ -f /etc/network_turbo ] && ( source /etc/network_turbo && timeout -k 10 300 sh -c 'curl -m 60 -LsSf https://astral.sh/uv/install.sh | sh' ); }
```

Some images ship no `tmux`, which steps 3, 4 and 6 need. If the default
package mirror of the image fails, the second line points the Ubuntu sources at
the Tsinghua mirror (a `.bak` copy of each file is kept) and tries again.

```bash
command -v tmux || { timeout 600 apt-get update -qq && timeout 600 apt-get install -y -qq tmux; } \
  || { sed -i.bak -E 's#https?://([a-z]{2}\.)?(archive|security)\.ubuntu\.com#https://mirrors.tuna.tsinghua.edu.cn#' \
         /etc/apt/sources.list /etc/apt/sources.list.d/*.sources 2>/dev/null
       timeout 600 apt-get update -qq && timeout 600 apt-get install -y -qq tmux; }
```

Pass check: `test "$(command -v git curl tmux uv nvidia-smi | wc -l)" -eq 5`

### 0c. Measure the download sources

```bash
cd "$REPO"
bash 02_extraction/scripts/net_probe.sh
```

The script downloads a short piece of the real files from every candidate in
the table above: the `vllm` wheel from each package index, the Python archive
this `uv` would fetch, and the first model shard. It ranks them by speed and
writes `work/repro/net.env`, which steps 1 and 3 read; the candidates and
their speeds are in `work/repro/net_probe.log`. It takes one to five minutes.
It uses whatever proxy is set in the environment; unset any proxy you do not
want before running it. To force a choice, set `UV_DEFAULT_INDEX`,
`UV_PYTHON_INSTALL_MIRROR` or `MODEL_SOURCE` (`modelscope`, `hf` or `hfmirror`)
before the command; the script puts that candidate first.

Pass check: `cd "$REPO" && tail -1 work/repro/net_probe.log | grep -q 'NET_PROBE_DONE$' && . work/repro/net.env && test -n "$PYPI_INDEXES"`

On `NET_PROBE_FAILED` no index served the `vllm` wheel: report
`work/repro/net_probe.log`.

## 1. Install

```bash
cd "$REPO" && . work/repro/net.env
uv venv .venv --python 3.11 \
  || { rm -rf .venv; UV_PYTHON_INSTALL_MIRROR=https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone uv venv .venv --python 3.11; } \
  || { rm -rf .venv; UV_PYTHON_INSTALL_MIRROR=https://mirrors.ustc.edu.cn/github-release/astral-sh/python-build-standalone uv venv .venv --python 3.11; } \
  || { rm -rf .venv; [ -f /etc/network_turbo ] && ( source /etc/network_turbo && unset UV_PYTHON_INSTALL_MIRROR && uv venv .venv --python 3.11 ); }
source .venv/bin/activate
ok=0
for idx in $PYPI_INDEXES; do
  echo "== package index: $idx"
  if UV_DEFAULT_INDEX=$idx uv pip install -r requirements.txt -r 02_extraction/requirements-serving.txt; then
    echo "export UV_DEFAULT_INDEX=$idx" >> work/repro/net.env; ok=1; break
  fi
done
test "$ok" = 1
```

`uv venv` fetches Python 3.11 from the source step 0c chose; the next three
lines are the other sources, the last of them only on AutoDL. The NJU mirror
comes before USTC because USTC answered HTTP 403 for the build `uv` 0.13 asks
for on 8 November 2026. The loop installs from the fastest index and moves to
the next one when an index fails; on 8 November 2026 the Aliyun mirror refused
one AutoDL host with HTTP 403 and the Tsinghua mirror worked, while on another
AutoDL host the same evening Aliyun served everything. The index that worked is
appended to `work/repro/net.env`, so the later `uv` calls use it too.

The `uv pip install` line downloads about 10 GB of packages (`torch`, `vllm`,
CUDA libraries). With an empty `uv` cache it took 7 minutes from the index
step 0c ranked first (16.9 MB/s on its probe) and 23 to 53 minutes from
mirrors picked by hand, on mainland China hosts in November 2026; with a warm
cache it takes a few minutes. Long
silences between `Downloaded` lines are normal. If no new `Downloaded` line
appears for 20 minutes, stop the command and rerun the loop without the index
it was using: `PYPI_INDEXES="<the rest of the list>"`.

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
disk for them. ModelScope, Hugging Face and hf-mirror.com serve the same bytes:
every file has the same SHA-256 on all three, and
`02_extraction/profiles/models/qwen3-next-80b-fp8.sha256` lists them.
`02_extraction/scripts/download_model.sh` therefore keeps every file already
downloaded when it changes source, and accepts a file only when its hash
matches the list. It works down this chain until all 15 files pass:

| Order | Source | When it moves on |
|---|---|---|
| 1 | an existing copy named by `MODEL_DIR=<path>` (a provider's shared model disk, a mounted volume, an earlier run); linked in, nothing downloaded | any file fails its hash |
| 2 | each source in the order of `MODEL_SOURCES` in `work/repro/net.env`, with its own client (`modelscope download`, `hf download`), two attempts each | the client exits with files missing, or adds under 300 MB in five minutes |
| 3 | plain `curl` with resume from each source in the same order, four files at a time | under 1 MB/s for five minutes, or three failed retries |
| 4 | rows 2 and 3 once more, for files still missing or failing their hash | — |

Measured from mainland China hosts: ModelScope took 77 to 96 minutes at 14 to
18 MB/s on 5, 8 and 9 November 2026; the Hugging
Face mirror stalled at 630 MB, then ran at about 1 MB/s. On a direct link
outside China, Hugging Face usually takes 10–40 minutes.

Run it in `tmux`, because it outlasts most agent shell time limits:

```bash
cd "$REPO"
tmux new -d -s model_download "bash 02_extraction/scripts/download_model.sh > work/repro/download.log 2>&1"
```

Check every few minutes with `tail -3 work/repro/download.log`, until the last
line ends in `DOWNLOAD_DONE` or `DOWNLOAD_FAILED` (each line starts with a
timestamp). Hashing the 82 GB at the end takes a few minutes. On
`DOWNLOAD_FAILED`, the lines above it name the files still missing and the
client logs (`work/repro/modelscope.log`, `hf.log`, `curl.log`); run the same
`tmux` line once more, it resumes. If it fails again, put a source that works in
front by hand, for example `MODEL_SOURCE=hfmirror bash 02_extraction/...`, or
skip the clients with `MODEL_CLI=0`. If the script itself breaks, fetch the
missing files with `curl -L -C - -o <file>` from
`https://www.modelscope.cn/models/Qwen/Qwen3-Next-80B-A3B-Instruct-FP8/resolve/master/<file>`
or `https://hf-mirror.com/Qwen/Qwen3-Next-80B-A3B-Instruct-FP8/resolve/main/<file>`
into the model directory; the hash check below decides.

Pass check, all three must exit 0:

```bash
cd "$REPO"
D=work/models/Qwen3-Next-80B-A3B-Instruct-FP8
tail -1 work/repro/download.log | grep -q 'DOWNLOAD_DONE$'
(cd "$D" && sha256sum --quiet -c "$REPO/02_extraction/profiles/models/qwen3-next-80b-fp8.sha256")
test "$(du -sLB1 "$D" | cut -f1)" -ge 80000000000
```

The second line hashes the 82 GB again and takes a few minutes.

## 4. Serve the model and run the smoke test

```bash
cd "$REPO/02_extraction"
bash scripts/run_smoke.sh --runtime "$REPO/work/repro/runtime.yaml" \
    --model "$REPO/work/repro/model.yaml" \
    --task profiles/tasks/criminal_defendant_v1.3 \
    --run-dir "$REPO/work/repro/smoke_run" | tee "$REPO/work/repro/smoke.log"
cd "$REPO"
```

This runs the environment doctor, the weight check, starts vLLM and LiteLLM in
tmux, and sends one case through the production prompt. Loading the weights
takes several minutes; the whole step took about 5 minutes on 8 November 2026.
The terminal shows two warnings, `未给 --batches：只跑到 Stage A` and
`未跑 Stage C（没给 --batches）——这不是完整 GREEN，不得进入 Production`. Both
are expected: this step runs only Stage A, and steps 5 to 7 do the batch run.
Only the last line of `smoke.log` counts.

Pass check: `cd "$REPO" && tail -1 work/repro/smoke.log | grep -Fx '{"final":"ENV_STAGE_A_OK"}'`

On failure the driver prints the failed gate; its logs are in
`work/repro/smoke_run/`. Report that gate and the files in that directory. The
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
