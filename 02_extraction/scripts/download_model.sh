#!/usr/bin/env bash
# Download Qwen3-Next-80B-A3B-Instruct-FP8 into work/models and check every file against a fixed
# SHA-256 list, whichever source the bytes came from.
#
# Run from anywhere; paths resolve against the repository root. Meant to run inside tmux
# (AGENT_TASK.md step 3), because it can take one to two hours:
#   tmux new -d -s model_download "bash 02_extraction/scripts/download_model.sh > work/repro/download.log 2>&1"
# The last line of the log ends in DOWNLOAD_DONE or DOWNLOAD_FAILED. Rerunning resumes.
#
# Sources, in the order work/repro/net.env gives (net_probe.sh ranks them by measured speed):
#   modelscope  www.modelscope.cn         hf  huggingface.co         hfmirror  hf-mirror.com
# MODEL_SOURCE=<one of them> puts that source first. Without net.env the order is
# modelscope,hf,hfmirror.
#
# The weights are byte-identical on all three: the SHA-256 of every file in
# 02_extraction/profiles/models/qwen3-next-80b-fp8.sha256 was read from both the ModelScope and
# the Hugging Face file listings on 2026-11-08 and they agree. So a switch of source keeps every
# file already downloaded and fetches only what is missing or fails its hash.
#
# The fallback chain, run in this order until every file passes:
#   1. MODEL_DIR=<path>: an existing copy (a platform's shared model disk, a mounted volume, an
#      earlier run). If it passes the hash check it is linked in, nothing is downloaded.
#   2. each source in turn with its own client (modelscope download / hf download), two
#      attempts each; a client that adds under 300 MB in five minutes is stopped as stalled.
#   3. plain curl with resume (-C -) from each source in turn, four files at a time, with the
#      same stall limit. Needs nothing but curl.
#   4. the whole chain once more, for files that failed their hash.
# MODEL_CLI=0 skips step 2; MODEL_CURL=0 skips step 3.
set -u
cd "$(dirname "$0")/../.." || exit 1
export PATH="$HOME/.local/bin:$PATH"
REPO_ID=${MODEL_REPO:-Qwen/Qwen3-Next-80B-A3B-Instruct-FP8}
D=${MODEL_TARGET:-work/models/Qwen3-Next-80B-A3B-Instruct-FP8}
MANIFEST=${MODEL_MANIFEST:-02_extraction/profiles/models/qwen3-next-80b-fp8.sha256}
MIN_BYTES=${MODEL_MIN_BYTES:-80000000000}
STALL_SEC=${MODEL_STALL_SEC:-300}
STALL_BYTES=${MODEL_STALL_BYTES:-300000000}
PY=.venv/bin/python
W=work/repro
mkdir -p work/models "$W"
say(){ echo "$(date '+%F %T') $*"; }

[ -f "$W/net.env" ] && . "$W/net.env"
SRC=()
for s in ${MODEL_SOURCE:-} $(echo "${MODEL_SOURCES:-modelscope,hf,hfmirror}" | tr ',' ' '); do
  case " ${SRC[*]:-} " in *" $s "*) ;; *) SRC+=("$s") ;; esac
done
FILES=$(awk '{print $2}' "$MANIFEST")
NFILES=$(wc -l < "$MANIFEST" | tr -d ' ')
say "sources: ${SRC[*]}"
say "disk available: $(df -BG --output=avail work/models | tail -1 | tr -dc 0-9) GiB"

url(){
  case "$1" in
    modelscope) echo "https://www.modelscope.cn/models/$REPO_ID/resolve/master/$2" ;;
    hf)         echo "https://huggingface.co/$REPO_ID/resolve/main/$2" ;;
    hfmirror)   echo "https://hf-mirror.com/$REPO_ID/resolve/main/$2" ;;
  esac
}

# check DIR: 0 when every listed file is present with its listed hash. Writes the files still to
# fetch to $W/model_bad.txt and the passing "hash  name" lines to $W/model_sha256.txt, deletes
# present files whose hash is wrong (a killed client can leave a short file under the final
# name), and caches hashes by path, size and mtime so a rerun does not hash 82 GB again.
check(){
  local dir=$1 real f want got key todo=()
  real=$(cd "$dir" && pwd -P) || return 1
  touch "$W/model_sha_cache.txt"
  : > "$W/model_bad.txt"; : > "$W/model_sha256.txt"
  for f in $FILES; do
    [ -f "$dir/$f" ] || continue
    key="$real/$f $(stat -Lc '%s %Y' "$dir/$f")"
    grep -qF "$key " "$W/model_sha_cache.txt" || todo+=("$f")
  done
  if [ ${#todo[@]} -gt 0 ]; then
    say "hashing ${#todo[@]} file(s) in $dir"
    for f in "${todo[@]}"; do echo "$f"; done \
      | (cd "$dir" && xargs -r -P 8 -n 1 sha256sum) \
      | while read -r got f; do
          echo "$real/$f $(stat -Lc '%s %Y' "$dir/$f") $got" >> "$W/model_sha_cache.txt"
        done
  fi
  while read -r want f; do
    if [ ! -f "$dir/$f" ]; then echo "$f" >> "$W/model_bad.txt"; continue; fi
    key="$real/$f $(stat -Lc '%s %Y' "$dir/$f")"
    got=$(grep -F "$key " "$W/model_sha_cache.txt" | tail -1 | awk '{print $4}')
    if [ "$got" = "$want" ]; then
      echo "$want  $f" >> "$W/model_sha256.txt"
    else
      say "hash mismatch, removing $f"
      rm -f "$dir/$f"; echo "$f" >> "$W/model_bad.txt"
    fi
  done < "$MANIFEST"
  [ ! -s "$W/model_bad.txt" ]
}

dsize(){ du -sB1 "$D/" 2>/dev/null | cut -f1; }

# watched LOG CMD...: run CMD in its own session; stop it when $D grows under STALL_BYTES in any
# STALL_SEC window.
watched(){
  local log=$1 pid start mark now next; shift
  setsid "$@" >> "$log" 2>&1 &
  pid=$!; start=$(date +%s); next=$((start + STALL_SEC)); mark=$(dsize); mark=${mark:-0}
  while kill -0 "$pid" 2>/dev/null; do
    sleep 15
    [ "$(date +%s)" -lt "$next" ] && continue
    now=$(dsize); now=${now:-0}
    say "  $((now / 1000000)) MB in $D after $(( $(date +%s) - start )) s"
    if [ $((now - mark)) -lt "$STALL_BYTES" ]; then
      say "  stalled: under $((STALL_BYTES / 1000000)) MB in ${STALL_SEC} s, stopping the client"
      kill -TERM -- -"$pid" 2>/dev/null; sleep 5; kill -KILL -- -"$pid" 2>/dev/null
      wait "$pid" 2>/dev/null; return 124
    fi
    mark=$now; next=$(( $(date +%s) + STALL_SEC ))
  done
  wait "$pid"
}

client(){ # SOURCE FILE...
  local s=$1; shift
  case "$s" in
    modelscope)
      test -x .venv/bin/modelscope || uv pip install --python "$PY" modelscope > /dev/null
      watched "$W/modelscope.log" .venv/bin/modelscope download --model "$REPO_ID" "$@" --local-dir "$D" ;;
    hf|hfmirror)
      test -x .venv/bin/hf || uv pip install --python "$PY" "huggingface_hub>=0.34,<2" > /dev/null
      if [ "$s" = hf ]; then
        watched "$W/hf.log" env -u HF_ENDPOINT .venv/bin/hf download "$REPO_ID" "$@" --local-dir "$D"
      else
        watched "$W/hf.log" env HF_ENDPOINT=https://hf-mirror.com .venv/bin/hf download "$REPO_ID" "$@" --local-dir "$D"
      fi ;;
    *) say "unknown source $s"; return 2 ;;
  esac
}

fetch_curl(){ # FILE: try each source; the partial file is kept across sources (same bytes)
  local f=$1 s
  for s in "${SRC[@]}"; do
    if curl -sS -L --fail -C - --retry 3 --retry-delay 10 --connect-timeout 15 \
         --speed-limit $((STALL_BYTES / STALL_SEC)) --speed-time "$STALL_SEC" \
         -o "$D/$f.part" "$(url "$s" "$f")" >> "$W/curl.log" 2>&1; then
      mv -f "$D/$f.part" "$D/$f"; say "  curl: $f from $s"; return 0
    fi
    say "  curl: $f failed from $s"
  done
  return 1
}

# ---- 1. an existing copy
if [ -n "${MODEL_DIR:-}" ]; then
  if [ -d "$MODEL_DIR" ] && check "$MODEL_DIR"; then
    if [ -L "$D" ] || [ -z "$(ls -A "$D" 2>/dev/null)" ]; then
      rm -rf "$D"; ln -s "$(cd "$MODEL_DIR" && pwd -P)" "$D"
      say "MODEL_DIR passed the hash check, linked $D -> $MODEL_DIR"
    else
      say "MODEL_DIR passed the hash check, but $D already holds files; downloading into $D instead"
    fi
  else
    say "MODEL_DIR=$MODEL_DIR is missing or failed the hash check; ignoring it"
  fi
fi
mkdir -p "$D"

# ---- 2-4. sources, then curl, then once more
for round in 1 2; do
  check "$D" && break
  if [ "${MODEL_CLI:-1}" != 0 ]; then
    for s in "${SRC[@]}"; do
      for n in 1 2; do
        check "$D" && break 3
        say "round $round, $s attempt $n: $(wc -l < "$W/model_bad.txt" | tr -d ' ') file(s) to fetch"
        # shellcheck disable=SC2046
        client "$s" $(cat "$W/model_bad.txt"); rc=$?
        say "round $round, $s attempt $n: client exit $rc"
      done
    done
  fi
  check "$D" && break
  if [ "${MODEL_CURL:-1}" != 0 ]; then
    say "round $round, curl: $(wc -l < "$W/model_bad.txt" | tr -d ' ') file(s) to fetch"
    while read -r f; do
      fetch_curl "$f" &
      while [ "$(jobs -rp | wc -l)" -ge 4 ]; do sleep 2; done
    done < "$W/model_bad.txt"
    wait
  fi
done

# Temporary files of the two clients, not weights.
rm -rf "$D/.cache/huggingface" "$D/._____temp"
check "$D"; ok=$?
shards=$(find -L "$D/" -maxdepth 1 -name '*.safetensors' | wc -l)
bytes=$(du -sLB1 "$D/" 2>/dev/null | cut -f1); bytes=${bytes:-0}
say "files_ok=$(wc -l < "$W/model_sha256.txt" | tr -d ' ')/$NFILES shards=$shards bytes=$bytes disk_left=$(df -BG --output=avail work/models | tail -1 | tr -dc 0-9) GiB"
if [ "$ok" -eq 0 ] && [ "$bytes" -ge "$MIN_BYTES" ]; then
  say DOWNLOAD_DONE
else
  say "still missing or wrong: $(tr '\n' ' ' < "$W/model_bad.txt")"
  say "logs: $W/modelscope.log $W/hf.log $W/curl.log"
  say DOWNLOAD_FAILED
  exit 1
fi
