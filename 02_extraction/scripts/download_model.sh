#!/usr/bin/env bash
# Download Qwen3-Next-80B-A3B-Instruct-FP8 into work/models from one source, then check it.
#
# Run from anywhere; paths resolve against the repository root. Meant to run inside tmux
# (AGENT_TASK.md step 3), because it can take one to two hours:
#   tmux new -d -s model_download "bash 02_extraction/scripts/download_model.sh > work/repro/download.log 2>&1"
# The last line of the log is DOWNLOAD_DONE or DOWNLOAD_FAILED.
#
# Source choice: ModelScope on a mainland China host (one with /etc/network_turbo, or where
# huggingface.co does not answer within 10 s), Hugging Face elsewhere. MODEL_SOURCE=hf or
# MODEL_SOURCE=modelscope overrides it. The two hosts publish different hashes under the same
# file names, so one run uses one source; a switch clears the directory first.
#
# Measured 2026-11-05 on a mainland AutoDL host: the Hugging Face mirror stalled at 630 MB and
# then ran at about 1 MB/s; ModelScope averaged about 15 MB/s, 94 minutes for 82 GB.
set -u
cd "$(dirname "$0")/../.." || exit 1
export PATH="$HOME/.local/bin:$PATH"
REPO_ID=Qwen/Qwen3-Next-80B-A3B-Instruct-FP8
D=work/models/Qwen3-Next-80B-A3B-Instruct-FP8
PY=.venv/bin/python
mkdir -p work/models work/repro
say(){ echo "$(date '+%F %T') $*"; }

if [ -z "${MODEL_SOURCE:-}" ]; then
  if [ -f /etc/network_turbo ] || ! curl -s -m 10 -o /dev/null https://huggingface.co; then
    MODEL_SOURCE=modelscope
  else
    MODEL_SOURCE=hf
  fi
fi
say "source: $MODEL_SOURCE"
say "disk available: $(df -BG --output=avail . | tail -1 | tr -dc 0-9) GiB"

# One Hugging Face attempt. A run that adds less than 300 MB in any five-minute window
# (under 1 MB/s, a day for 82 GB) counts as failed: this covers both a full stall and the
# slow redirect to cas-bridge.xethub.hf.co seen from mainland China.
hf_once() {
  .venv/bin/hf download "$REPO_ID" --local-dir "$D" > work/repro/hf.log 2>&1 &
  local pid=$! t=0 mark=0 now
  while kill -0 "$pid" 2>/dev/null; do
    sleep 30; t=$((t + 30))
    if [ $((t % 300)) -eq 0 ]; then
      now=$(du -sB1 "$D" 2>/dev/null | cut -f1); now=${now:-0}
      say "hf: $((now / 1000000)) MB after ${t}s"
      if [ $((now - mark)) -lt 300000000 ]; then
        kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; return 1
      fi
      mark=$now
    fi
  done
  wait "$pid"
}

if [ "$MODEL_SOURCE" = hf ]; then
  test -x .venv/bin/hf || uv pip install --python "$PY" "huggingface_hub>=0.34,<2"
  ok=0
  for n in 1 2; do
    # A killed download leaves a short file under the final shard name, and hf treats it as
    # done on the next run, so every attempt starts from an empty directory.
    rm -rf "$D"
    say "hf attempt $n"
    if hf_once; then ok=1; break; fi
    say "hf attempt $n failed"
  done
  if [ "$ok" = 0 ]; then
    say "hf failed twice, switching to ModelScope"
    rm -rf "$D"
    MODEL_SOURCE=modelscope
  fi
fi

if [ "$MODEL_SOURCE" = modelscope ]; then
  test -x .venv/bin/modelscope || uv pip install --python "$PY" modelscope
  # ModelScope resumes from the last byte, so its own partial files stay between attempts.
  for n in 1 2 3; do
    say "modelscope attempt $n"
    .venv/bin/modelscope download --model "$REPO_ID" --local-dir "$D" >> work/repro/modelscope.log 2>&1 && break
    say "modelscope attempt $n failed, resuming"
  done
fi

# Leftovers of an interrupted hf run are temporary files, not weights.
rm -rf "$D/.cache/huggingface"

shards=$(find "$D" -name '*.safetensors' 2>/dev/null | wc -l)
partial=$(find "$D" \( -name '*.incomplete' -o -name '*.partial' \) 2>/dev/null | head -1)
bytes=$(du -sB1 "$D" 2>/dev/null | cut -f1); bytes=${bytes:-0}
say "shards=$shards bytes=$bytes partial=${partial:-none} disk_left=$(df -BG --output=avail . | tail -1 | tr -dc 0-9) GiB"
if [ "$shards" -eq 8 ] && [ -z "$partial" ] && [ "$bytes" -ge 80000000000 ]; then
  say DOWNLOAD_DONE
else
  say DOWNLOAD_FAILED
  exit 1
fi
