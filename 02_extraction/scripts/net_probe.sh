#!/usr/bin/env bash
# Measure, on this host, which package index, Python download source and model source to use.
#
# Run from anywhere after uv is installed (AGENT_TASK.md step 0c):
#   bash 02_extraction/scripts/net_probe.sh
# Writes work/repro/net.env (sourced by later steps) and work/repro/net_probe.log (every
# candidate with its HTTP code and speed). The last line of the log ends in NET_PROBE_DONE or
# NET_PROBE_FAILED.
#
# Nothing here depends on the hosting platform. Each candidate is tried with a real ranged
# download of a file the later steps need, and the candidates are ranked by measured speed:
#   - package index: the vllm 0.19.1 wheel (first 16 MiB) from each index that lists it;
#   - Python: the exact interpreter archive this uv would fetch (first 8 MiB);
#   - model: shard 1 of the weights (first 64 MiB).
# A single-connection probe understates what a multi-connection download reaches, so the
# numbers rank candidates; they do not predict download time.
#
# Overrides, honoured as given and put first: UV_DEFAULT_INDEX, UV_PYTHON_INSTALL_MIRROR,
# MODEL_SOURCE (modelscope | hf | hfmirror). Proxy variables in the environment are used as
# they are; turn off any proxy you do not want before running this.
set -u
cd "$(dirname "$0")/../.." || exit 1
export PATH="$HOME/.local/bin:$PATH"
mkdir -p work/repro
LOG=work/repro/net_probe.log
ENV=work/repro/net.env
: > "$LOG"
say(){ echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

REPO_ID=${MODEL_REPO:-Qwen/Qwen3-Next-80B-A3B-Instruct-FP8}
SHARD=${MODEL_PROBE_FILE:-model-00001-of-00008.safetensors}
VLLM_VER=0.19.1

# probe URL BYTES MAXSEC -> "<http_code> <bytes_per_s>"; speed 0 when the code is not 200/206.
probe(){
  local r code sp
  r=$(curl -s -L -o /dev/null --connect-timeout 10 -m "$3" -r "0-$(( $2 - 1 ))" \
        -w '%{http_code} %{speed_download}' "$1" 2>/dev/null)
  code=${r%% *}; sp=${r#* }; sp=${sp%%.*}; sp=${sp//[^0-9]/}
  case "$code" in 200|206) ;; *) sp=0 ;; esac
  echo "${code:-000} ${sp:-0}"
}
mbps(){ awk -v b="$1" 'BEGIN{printf "%.2f", b/1000000}'; }

# ---------------------------------------------------------------- package index
INDEX_CANDIDATES=()
[ -n "${UV_DEFAULT_INDEX:-}" ] && INDEX_CANDIDATES+=("$UV_DEFAULT_INDEX")
[ -n "${PIP_INDEX_URL:-}" ] && INDEX_CANDIDATES+=("$PIP_INDEX_URL")
# An index preset by the image (pip.conf). uv does not read pip.conf, so it is taken out here.
for f in /etc/pip.conf "$HOME/.pip/pip.conf" "$HOME/.config/pip/pip.conf"; do
  [ -f "$f" ] || continue
  u=$(sed -n 's/^[[:space:]]*index-url[[:space:]]*=[[:space:]]*//p' "$f" | head -1)
  [ -n "$u" ] && INDEX_CANDIDATES+=("$u")
done
INDEX_CANDIDATES+=(
  http://mirrors.cloud.aliyuncs.com/pypi/simple       # resolves only inside that cloud
  http://mirrors.tencentyun.com/pypi/simple           # resolves only inside that cloud
  https://pypi.org/simple
  https://pypi.tuna.tsinghua.edu.cn/simple
  https://mirrors.ustc.edu.cn/pypi/simple
  https://mirrors.bfsu.edu.cn/pypi/web/simple
  https://mirror.sjtu.edu.cn/pypi/web/simple
  https://mirror.nju.edu.cn/pypi/web/simple
  https://mirrors.cloud.tencent.com/pypi/simple
  https://repo.huaweicloud.com/repository/pypi/simple
  https://mirrors.aliyun.com/pypi/simple
)
say "package index: ${#INDEX_CANDIDATES[@]} candidates"
RANKED_IDX=$( {
  seen=" "
  for idx in "${INDEX_CANDIDATES[@]}"; do
    idx=${idx%/}
    [[ "$seen" == *" $idx "* ]] && continue; seen="$seen$idx "
    page=$(curl -s -L --connect-timeout 10 -m 20 "$idx/vllm/" 2>/dev/null)
    href=$(printf '%s' "$page" | grep -o "href=\"[^\"]*vllm-$VLLM_VER-[^\"]*\.whl[^\"]*\"" | head -1)
    href=${href#href=\"}; href=${href%\"}; href=${href%%#*}
    if [ -z "$href" ]; then say "  index  --  no vllm $VLLM_VER   $idx" >&2; continue; fi
    if [[ "$href" == http* ]]; then url=$href
    elif [[ "$href" == /* ]]; then url="$(printf '%s' "$idx" | sed -E 's#^(https?://[^/]+).*#\1#')$href"
    else url="$idx/vllm/$href"; fi
    read -r code sp < <(probe "$url" 16777216 25)
    say "  index  $code $(mbps "$sp") MB/s  $idx" >&2
    [ "$sp" -gt 0 ] && echo "$sp $idx"
  done
} | sort -rn | awk '{print $2}' | tr '\n' ' ')
RANKED_IDX=${RANKED_IDX% }

# ---------------------------------------------------------------- Python interpreter
PY_MIRROR=""
if command -v uv >/dev/null; then
  U=$(UV_PYTHON_INSTALL_MIRROR= uv python list 3.11 --only-downloads --show-urls 2>/dev/null \
        | awk '/^cpython-3\.11/{print $2; exit}')
  SUF=${U#*/releases/download/}
  if [ -n "$U" ] && [ "$SUF" != "$U" ]; then
    DEFAULT_BASE=${U%%/releases/download/*}/releases/download
    best=0
    for base in ${UV_PYTHON_INSTALL_MIRROR:-} "$DEFAULT_BASE" \
                https://github.com/astral-sh/python-build-standalone/releases/download \
                https://mirrors.ustc.edu.cn/github-release/astral-sh/python-build-standalone \
                https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone; do
      read -r code sp < <(probe "$base/$SUF" 8388608 25)
      say "  python $code $(mbps "$sp") MB/s  $base"
      if [ "$sp" -gt "$best" ]; then best=$sp; PY_MIRROR=$base; fi
    done
    [ "$PY_MIRROR" = "$DEFAULT_BASE" ] && PY_MIRROR=""   # uv's own default is fastest: leave it
  else
    say "  python: this uv lists no 3.11 download, nothing to probe"
  fi
else
  say "  python: uv not on PATH, skipped"
fi

# ---------------------------------------------------------------- model
declare -A MURL=(
  [modelscope]="https://www.modelscope.cn/models/$REPO_ID/resolve/master/$SHARD"
  [hf]="https://huggingface.co/$REPO_ID/resolve/main/$SHARD"
  [hfmirror]="https://hf-mirror.com/$REPO_ID/resolve/main/$SHARD"
)
FAST=(); SLOW=(); DEAD=()
RANKED_SRC=$( {
  for s in modelscope hf hfmirror; do
    read -r code sp < <(probe "${MURL[$s]}" 67108864 30)
    say "  model  $code $(mbps "$sp") MB/s  $s" >&2
    echo "$sp $s"
  done
} | sort -rn)
while read -r sp s; do
  [ -z "$s" ] && continue
  if [ "$sp" -ge 2000000 ]; then FAST+=("$s"); elif [ "$sp" -gt 0 ]; then SLOW+=("$s"); else DEAD+=("$s"); fi
done <<< "$RANKED_SRC"
ORDER=("${FAST[@]}" "${SLOW[@]}" "${DEAD[@]}")
[ ${#SLOW[@]} -gt 0 ] && say "  model: below 2 MB/s, kept last: ${SLOW[*]}"
[ ${#FAST[@]} -eq 0 ] && say "  model: WARNING no source reached 2 MB/s on the probe"
if [ -n "${MODEL_SOURCE:-}" ]; then
  ORDER=("$MODEL_SOURCE" $(printf '%s\n' "${ORDER[@]}" | grep -vx "$MODEL_SOURCE"))
fi
MODEL_SOURCES=$(IFS=,; echo "${ORDER[*]}")

# ---------------------------------------------------------------- write
{
  echo "# written by 02_extraction/scripts/net_probe.sh on $(date '+%F %T')"
  echo "export PYPI_INDEXES=\"$RANKED_IDX\""
  echo "export UV_DEFAULT_INDEX=${RANKED_IDX%% *}"
  if [ -n "$PY_MIRROR" ]; then echo "export UV_PYTHON_INSTALL_MIRROR=$PY_MIRROR"
  else echo "unset UV_PYTHON_INSTALL_MIRROR"; fi
  echo "export MODEL_SOURCES=$MODEL_SOURCES"
} > "$ENV"
say "chosen: index=${RANKED_IDX%% *} python=${PY_MIRROR:-uv default} model=$MODEL_SOURCES"
if [ -z "$RANKED_IDX" ]; then
  say "no package index served vllm $VLLM_VER; NET_PROBE_FAILED"
  exit 1
fi
say NET_PROBE_DONE
