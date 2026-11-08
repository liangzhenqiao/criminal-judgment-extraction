#!/usr/bin/env bash
# Gate 3 — LiteLLM 路由启动 + 健康检查。Gate 3。
# 配置用 hosted_vllm/ 前缀加 router_settings。
# 用法: scripts/launch_litellm.sh --runtime <runtime.yaml> [--model <model.yaml>] [--model-alias <alias>] \
#       [--config <litellm.yaml>] [--run-dir <dir>] [--no-tmux]
#   --model 存在且未传 --config 时，会按 runtime 端口 + model served_model_name 动态生成 config。
#   --config 默认兜底用 profiles/litellm/litellm_config_v5.reference.yaml。
# 退出: 0=就绪  1=健康检查未过  2=前置缺失  3=环境错误
set -u
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

RUNTIME=""; MODEL=""; CONFIG=""; RUN_DIR=""; MODEL_ALIAS=""; USE_TMUX=1
while [ $# -gt 0 ]; do case "$1" in
  --runtime) RUNTIME="$2"; shift 2;;
  --model)   MODEL="$2";   shift 2;;
  --model-alias) MODEL_ALIAS="$2"; shift 2;;
  --config)  CONFIG="$2";  shift 2;;
  --run-dir) RUN_DIR="$2"; shift 2;;
  --no-tmux) USE_TMUX=0;   shift;;
  *) die "未知参数: $1" 2;;
esac; done
[ -f "${RUNTIME:-}" ] || die "需 --runtime" 2
if [ -z "$RUN_DIR" ]; then RUN_DIR="$(new_run_dir)"; else mkdir -p "$RUN_DIR"; fi

if [ -z "$CONFIG" ] && [ -n "$MODEL" ]; then
  [ -f "$MODEL" ] || die "model profile 不存在: $MODEL" 2
  VLLM_PORT_FOR_CONFIG=$(pf "$RUNTIME" ports.vllm 8020)
  SERVED_FOR_CONFIG=$(pf "$MODEL" vllm.served_model_name)
  [ -n "$MODEL_ALIAS" ] || MODEL_ALIAS=$(pf "$MODEL" litellm.model_alias legal-extractor-80b)
  CONFIG="$RUN_DIR/litellm_config.generated.yaml"
  python3 - "$CONFIG" "$MODEL_ALIAS" "$SERVED_FOR_CONFIG" "$VLLM_PORT_FOR_CONFIG" <<'PYEOF'
import sys
out, alias, served, port = sys.argv[1:5]
text = f"""model_list:
  - model_name: {alias}
    litellm_params:
      model: hosted_vllm/{served}
      api_base: http://127.0.0.1:{port}/v1
      api_key: dummy
    model_info:
      tag: generated_from_runtime_model_profile

router_settings:
  routing_strategy: simple-shuffle
  num_retries: 2
  timeout: 600
  allowed_fails: 3
  cooldown_time: 30

litellm_settings:
  drop_params: true
  set_verbose: false
  telemetry: false

# LiteLLM >= 1.108 refuses to start without a master key. The runner and the
# smoke client already send this exact bearer token. Leave database_url out:
# an empty string is read as a URL and rejected for its missing scheme.
general_settings:
  master_key: sk-dummy
  ui: false
"""
open(out, "w", encoding="utf-8").write(text)
PYEOF
  log "未传 --config：已生成 LiteLLM config: $CONFIG"
fi
[ -z "$CONFIG" ] && CONFIG="$AGENT_ROOT/profiles/litellm/litellm_config_v5.reference.yaml"
[ -f "$CONFIG" ] || die "litellm 配置不存在: $CONFIG" 2

LITELLM_BIN=$(pf "$RUNTIME" bin.litellm litellm)
command -v "$LITELLM_BIN" >/dev/null 2>&1 || LITELLM_BIN=litellm
PORT=$(pf "$RUNTIME" ports.litellm 4000)
LOG_DIR=$(pf "$RUNTIME" paths.log_dir /tmp); mkdir -p "$LOG_DIR"
LLOG="$LOG_DIR/litellm_$(date +%Y%m%d_%H%M%S).log"
HEALTH="$RUN_DIR/litellm_health.json"
SESSION_SUFFIX=$(printf "%s" "$(basename "$RUN_DIR")" | tr -c 'A-Za-z0-9_' '_')
TMUX_SESSION="litellm_proxy_${SESSION_SUFFIX}"
# The gateway requires the bearer token on every call, probes included.
LITELLM_KEY="sk-dummy"

# 业务别名（从 config 抓第一个 model_name）
ALIAS=$(grep -E "^\s*-?\s*model_name:" "$CONFIG" | head -1 | sed 's/.*model_name:\s*//' | tr -d ' ')

# --- 幂等：已起且含别名 → 复用 ---
if curl -sS --max-time 5 -H "Authorization: Bearer $LITELLM_KEY" "http://127.0.0.1:$PORT/v1/models" 2>/dev/null | grep -q "${ALIAS:-legal-extractor}"; then
  ok "LiteLLM :$PORT 已就绪并含别名 $ALIAS — 复用"
  emit_json_kv "$HEALTH" status reused port "$PORT" alias "$ALIAS"
  exit 0
fi

log "启动 LiteLLM（config=$CONFIG alias=$ALIAS port=${PORT}）日志: $LLOG"
START_CMD="$LITELLM_BIN --config '$CONFIG' --port $PORT --host 0.0.0.0"
if [ "$USE_TMUX" = "1" ] && command -v tmux >/dev/null 2>&1; then
  tmux new -d -s "$TMUX_SESSION" "{ $START_CMD ; } > '$LLOG' 2>&1" || die "tmux 启动失败" 3
  log "已在 tmux '$TMUX_SESSION' 内启动"
else
  warn "未用 tmux：SSH 断线会带走进程"
  bash -c "{ $START_CMD ; } > '$LLOG' 2>&1 &"
fi

# --- 轮询就绪（LiteLLM 启动快，最长 120s）---
DEADLINE=$(( $(date +%s) + 120 ))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  if curl -sS --max-time 5 -H "Authorization: Bearer $LITELLM_KEY" "http://127.0.0.1:$PORT/v1/models" 2>/dev/null | grep -q "${ALIAS:-legal-extractor}"; then break; fi
  if grep -qiE "ConnectionError|Invalid model name" "$LLOG" 2>/dev/null; then
    warn "LiteLLM 日志出现错误，继续轮询确认..."
  fi
  sleep 4
done

MODELS=$(curl -sS --max-time 5 -H "Authorization: Bearer $LITELLM_KEY" "http://127.0.0.1:$PORT/v1/models" 2>/dev/null)
if echo "$MODELS" | grep -q "${ALIAS:-legal-extractor}"; then
  emit_json_kv "$HEALTH" status ready port "$PORT" alias "$ALIAS" verdict PASS
  ok "Gate 3 PASS — LiteLLM :$PORT 就绪（别名 ${ALIAS}）。健康: $HEALTH"
  exit 0
else
  emit_json_kv "$HEALTH" status fail port "$PORT" alias "$ALIAS" verdict FAIL log "$LLOG"
  # 诊断：vLLM 是否活
  if curl -sS --max-time 5 "http://127.0.0.1:$(pf "$RUNTIME" ports.vllm 8020)/v1/models" >/dev/null 2>&1; then
    die "LiteLLM 未就绪但 vLLM 活 → 查 config api_base / model 前缀(hosted_vllm/)" 1
  else
    die "LiteLLM 未就绪且 vLLM :$(pf "$RUNTIME" ports.vllm 8020) 不通 → 先确认 Gate 2" 1
  fi
fi
