#!/usr/bin/env bash
# Gate 2 — vLLM 启动 + 健康检查。Gate 2。
# 就绪判定读 /v1/models 和启动日志里的回退与模板缺失。
# 用法: scripts/launch_vllm.sh --runtime <runtime.yaml> --model <model.yaml> [--run-dir <dir>] [--no-tmux]
# 退出: 0=就绪  1=健康检查未过  2=前置缺失  3=环境错误
# 幂等: 若目标端口已就绪同名模型 → 复用，不重复启动。
set -u
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

RUNTIME=""; MODEL=""; RUN_DIR=""; USE_TMUX=1
while [ $# -gt 0 ]; do case "$1" in
  --runtime) RUNTIME="$2"; shift 2;;
  --model)   MODEL="$2";   shift 2;;
  --run-dir) RUN_DIR="$2"; shift 2;;
  --no-tmux) USE_TMUX=0;   shift;;
  *) die "未知参数: $1" 2;;
esac; done
[ -f "${RUNTIME:-}" ] || die "需 --runtime" 2
[ -f "${MODEL:-}" ]   || die "需 --model" 2
if [ -z "$RUN_DIR" ]; then RUN_DIR="$(new_run_dir)"; else mkdir -p "$RUN_DIR"; fi

PY=$(pf "$RUNTIME" bin.python python3)
MODEL_DIR=$(pf "$RUNTIME" paths.model_dir)
HF_HOME=$(pf "$RUNTIME" paths.hf_home)
LOG_DIR=$(pf "$RUNTIME" paths.log_dir /tmp)
CVD=$(pf "$RUNTIME" env.CUDA_VISIBLE_DEVICES "0,1")
PORT=$(pf "$RUNTIME" ports.vllm 8020)
TP=$(pf "$MODEL" vllm.tensor_parallel_size 2)
MML=$(pf "$MODEL" vllm.max_model_len 24576)
MNS=$(pf "$MODEL" vllm.max_num_seqs 48)
GMU=$(pf "$MODEL" vllm.gpu_memory_utilization 0.90)
SERVED=$(pf "$MODEL" vllm.served_model_name "Qwen/Qwen3-Next-80B-A3B-Instruct-FP8")
PREFIX_CACHE=$(pf "$MODEL" vllm.enable_prefix_caching true)
EXTRA_ARGS=$(
  "$PY" - "$MODEL" <<'PYEOF'
import shlex, sys

try:
    import yaml
except ImportError as e:
    raise SystemExit("PyYAML is required to read vllm.extra_args") from e

model_path = sys.argv[1]
data = yaml.safe_load(open(model_path, encoding="utf-8")) or {}
val = ((data.get("vllm") or {}).get("extra_args") or [])
if isinstance(val, str):
    args = shlex.split(val)
elif isinstance(val, list):
    args = [str(x) for x in val if str(x).strip()]
else:
    raise SystemExit(f"vllm.extra_args must be a list or string, got {type(val).__name__}")
print(" ".join(shlex.quote(arg) for arg in args))
PYEOF
) || die "读取 vllm.extra_args 失败（需 PyYAML；值需为 list 或 string）" 2
mkdir -p "$LOG_DIR"
VLOG="$LOG_DIR/vllm_$(date +%Y%m%d_%H%M%S).log"
READY="$RUN_DIR/vllm_health.json"
SESSION_SUFFIX=$(printf "%s" "$(basename "$RUN_DIR")" | tr -c 'A-Za-z0-9_' '_')
TMUX_SESSION="vllm_serve_${SESSION_SUFFIX}"

[ -f "$MODEL_DIR/config.json" ] || die "模型 config.json 不存在: ${MODEL_DIR}（先过 Gate 1）" 2

# --- 幂等：端口已起且 /v1/models 含目标模型 → 复用 ---
if curl -sS --max-time 5 "http://127.0.0.1:$PORT/v1/models" 2>/dev/null | grep -q "$SERVED"; then
  ok "vLLM :$PORT 已就绪并含模型 $SERVED — 复用，不重启"
  emit_json_kv "$READY" status reused port "$PORT" model "$SERVED" extra_args "$EXTRA_ARGS"
  exit 0
fi

# --- 组装启动命令 ---
EXTRA_PREFIX=""; [ "$PREFIX_CACHE" = "true" ] && EXTRA_PREFIX="--enable-prefix-caching"
START_CMD=$(cat <<EOF
export HF_HOME=$HF_HOME HUGGINGFACE_HUB_CACHE=$HF_HOME
unset HF_ENDPOINT; unset OMP_NUM_THREADS
export VLLM_WORKER_MULTIPROC_METHOD=spawn
export CUDA_VISIBLE_DEVICES=$CVD
exec $PY -m vllm.entrypoints.openai.api_server \
  --host 127.0.0.1 --port $PORT \
  --model "$MODEL_DIR" \
  --served-model-name $SERVED \
  --tensor-parallel-size $TP \
  --max-model-len $MML \
  --max-num-seqs $MNS \
  --gpu-memory-utilization $GMU \
  $EXTRA_PREFIX \
  $EXTRA_ARGS \
  --trust-remote-code
EOF
)

log "启动 vLLM（TP=$TP max_model_len=$MML max_num_seqs=$MNS gmu=$GMU prefix_cache=${PREFIX_CACHE} extra_args=${EXTRA_ARGS:-<none>}）"
log "日志: $VLOG"
if [ "$USE_TMUX" = "1" ] && command -v tmux >/dev/null 2>&1; then
  tmux new -d -s "$TMUX_SESSION" "{ $START_CMD ; } > '$VLOG' 2>&1" || die "tmux 启动失败" 3
  log "已在 tmux session '$TMUX_SESSION' 内启动（detach 安全；attach: tmux attach -t $TMUX_SESSION）"
else
  warn "未用 tmux（--no-tmux 或 tmux 缺失）：SSH 断线会带走进程，仅适合本地/前台调试"
  bash -c "{ $START_CMD ; } > '$VLOG' 2>&1 &"
fi

# --- 轮询 /v1/models（最长 ~10min，cudagraph + 80B 加载约 4min）---
log "轮询 /v1/models 直到就绪（最长 600s）..."
DEADLINE=$(( $(date +%s) + 600 ))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  if curl -sS --max-time 5 "http://127.0.0.1:$PORT/v1/models" 2>/dev/null | grep -q "$SERVED"; then
    break
  fi
  if grep -qiE "OutOfMemoryError|CUDA out of memory" "$VLOG" 2>/dev/null; then
    die "vLLM 启动 OOM（见 ${VLOG}）→ 降 gpu_memory_utilization/max_num_seqs/max_model_len" 3
  fi
  if grep -qiE "Architecture .* not supported|unsupported architecture" "$VLOG" 2>/dev/null; then
    die "vLLM 不支持该架构 → 升级 vllm>=0.19.1" 3
  fi
  sleep 8
done

# --- 健康检查 + 日志红线 ---
if ! curl -sS --max-time 5 "http://127.0.0.1:$PORT/v1/models" 2>/dev/null | grep -q "$SERVED"; then
  die "超时未就绪（见 ${VLOG}；CUDA graphs 可能 >15min，可加 --enforce-eager）" 1
fi
BF16_FALLBACK=false
grep -qiE "BF16 fallback|fallback.*bf16|dtype=bfloat16" "$VLOG" 2>/dev/null && BF16_FALLBACK=true
CHAT_MISSING=false
grep -qiE "chat template not found|chat_template missing" "$VLOG" 2>/dev/null && CHAT_MISSING=true

"$PY" - "$READY" "$PORT" "$SERVED" "$BF16_FALLBACK" "$CHAT_MISSING" "$VLOG" "$EXTRA_ARGS" <<'PYEOF'
import json, sys
out, port, served, bf16, chat_missing, vlog, extra_args = sys.argv[1:8]
r = {"status":"ready","port":int(port),"model_id":served,
     "bf16_fallback": bf16=="true","chat_template_missing": chat_missing=="true",
     "vllm_log": vlog, "extra_args": extra_args}
r["verdict"] = "PASS" if (not r["bf16_fallback"] and not r["chat_template_missing"]) else "FAIL"
open(out,"w",encoding="utf-8").write(json.dumps(r,ensure_ascii=False,indent=2))
print(r["verdict"])
PYEOF
if [ "$BF16_FALLBACK" = "true" ]; then die "FP8 fallback 到 BF16（速度减半）→ 升级 vllm / 查 compute_cap≥sm_89" 1; fi
if [ "$CHAT_MISSING" = "true" ]; then die "chat_template not found → --chat-template 显式指定" 1; fi
ok "Gate 2 PASS — vLLM :$PORT 就绪（${SERVED}），无 BF16 fallback。健康: $READY"
exit 0
