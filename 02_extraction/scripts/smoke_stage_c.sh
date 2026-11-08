#!/usr/bin/env bash
# Gate 6 — Smoke Stage C: 50-batch 压测（调真实 runner）。Gate 6。
# 调 runner/run_full_extraction.py（生产参数走 CLI 显式传）。
#
# ⚠️ 重要语义:
#   本脚本只负责"启动/运行" runner，**不判 PASS**。Gate 6 是否 PASS 由 Gate 7（smoke_gate.py
#   读 batch_state.json 对门槛）裁决。
#   - 后台 tmux 启动 → 状态 LAUNCHED（exit 0，但产物明确标"未完成、未验收"）
#   - --wait / --no-tmux 前台跑完 → 状态 COMPLETED（仍需 Gate 7 验收才算 PASS）
#   绝不打印 "Gate 6 PASS"，绝不诱导直接进 Gate 7 之前略过验收。
#
# ⚠️ 防误跑全量 + 防复用旧 state:
#   - 对 --batches 目录的 batch_*.json 数量做检查，默认必须 == --expected-batches（默认 50）；
#     不符则拒绝（避免把全量 2000+ batch 目录当 smoke 跑）。用 --expected-batches N 显式覆盖。
#   - work_root 默认隔离到 <runtime.work_root>/_smoke_runs/<run_id>，绝不默认复用共享 ${WORK_BASE}/work
#     （否则会读到生产旧 state，done 跳过 → 假完成）。可用 --work-root 显式指定。
#
# 用法: scripts/smoke_stage_c.sh --runtime <runtime.yaml> --model <model.yaml> \
#         --pattern <pattern.md> --batches <50batch 目录> \
#         [--expected-batches 50] [--model-alias <alias>] [--runner <runner.py>] \
#         [--work-root <smoke 专用 dir>] [--run-dir <dir>] [--wait] [--no-tmux]
# 退出: 0=已启动(LAUNCHED)或已跑完(COMPLETED)  2=前置缺失/batch 数不符  3=环境错误
# 注意: Stage C 需 GPU 实际推理（无卡模式不可跑）。
set -u
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

RUNTIME=""; MODEL=""; PATTERN=""; BATCHES=""; WORK_ROOT=""; RUN_DIR=""; RUNNER=""; MODEL_ALIAS=""; USE_TMUX=1; WAIT=0; EXPECTED_BATCHES=50
while [ $# -gt 0 ]; do case "$1" in
  --runtime) RUNTIME="$2"; shift 2;;
  --model)   MODEL="$2";   shift 2;;
  --pattern) PATTERN="$2"; shift 2;;
  --batches) BATCHES="$2"; shift 2;;
  --expected-batches) EXPECTED_BATCHES="$2"; shift 2;;
  --model-alias) MODEL_ALIAS="$2"; shift 2;;
  --runner) RUNNER="$2"; shift 2;;
  --work-root) WORK_ROOT="$2"; shift 2;;
  --run-dir) RUN_DIR="$2"; shift 2;;
  --wait) WAIT=1; shift;;
  --no-tmux) USE_TMUX=0; shift;;
  *) die "未知参数: $1" 2;;
esac; done
[ -f "${RUNTIME:-}" ] || die "需 --runtime" 2
[ -f "${MODEL:-}" ]   || die "需 --model" 2
[ -f "${PATTERN:-}" ] || die "需 --pattern <pattern.md>" 2
[ -d "${BATCHES:-}" ] || die "需 --batches <存在的 batch 目录>" 2
if [ -z "$RUN_DIR" ]; then RUN_DIR="$(new_run_dir)"; else mkdir -p "$RUN_DIR"; fi
[ -n "$RUNNER" ] || RUNNER="$AGENT_ROOT/runner/run_full_extraction.py"
[ -f "$RUNNER" ] || die "runner 缺失: ${RUNNER}（换任务时可传 --runner <task runner>）" 2

# --- 防误跑全量：batch 文件数检查 ---
N_BATCH=$(find "$BATCHES" -name "batch_*.json" 2>/dev/null | wc -l | tr -d ' ')
if [ "$N_BATCH" -ne "$EXPECTED_BATCHES" ]; then
  die "batch 数=${N_BATCH} ≠ expected=${EXPECTED_BATCHES}（防误跑全量目录）。确认是 smoke 子集；如确属预期请显式 --expected-batches ${N_BATCH}" 2
fi

PY=$(pf "$RUNTIME" bin.python python3); [ -x "$PY" ] || PY=python3
RT_WORK=$(pf "$RUNTIME" paths.work_root "${WORK_BASE:-.}/work")
# --- work_root 隔离：默认落到 smoke 专用子目录，绝不复用共享生产 work_root ---
if [ -z "$WORK_ROOT" ]; then
  RID=$(basename "$RUN_DIR")
  WORK_ROOT="$RT_WORK/_smoke_runs/$RID"
  log "未指定 --work-root，自动隔离到 ${WORK_ROOT}（不复用生产 ${RT_WORK} 的旧 state）"
fi
case "$WORK_ROOT" in
  "$RT_WORK") warn "⚠️ work_root 指向共享生产目录 ${RT_WORK}，会读到旧 state 导致假完成！建议改用隔离目录" ;;
esac

LITELLM_PORT=$(pf "$RUNTIME" ports.litellm 4000)
[ -n "$MODEL_ALIAS" ] || MODEL_ALIAS=$(pf "$MODEL" litellm.model_alias legal-extractor-80b)
LL_ALIAS="$MODEL_ALIAS"   # runner 用业务别名（与 litellm config model_name 一致）
BUNDLE=$(pf "$MODEL" runner.bundle 4)
CONC=$(pf "$MODEL" runner.concurrency 48)
MAXTOK=$(pf "$MODEL" runner.max_tokens 3072)
LOG_DIR=$(pf "$RUNTIME" paths.log_dir /tmp); mkdir -p "$LOG_DIR" "$WORK_ROOT"
RLOG="$LOG_DIR/stage_c_$(date +%Y%m%d_%H%M%S).log"
SESSION_SUFFIX=$(printf "%s" "$(basename "$RUN_DIR")" | tr -c 'A-Za-z0-9_' '_')
TMUX_SESSION="smoke_stage_c_${SESSION_SUFFIX}"

# 前置：LiteLLM 必须就绪
curl -sS --max-time 5 -H "Authorization: Bearer sk-dummy" "http://127.0.0.1:$LITELLM_PORT/v1/models" 2>/dev/null | grep -q "$LL_ALIAS" \
  || die "LiteLLM :$LITELLM_PORT 未就绪含 ${LL_ALIAS}（先过 Gate 3）" 2

log "Stage C: runner 生产配置 bundle=$BUNDLE conc=$CONC max_tokens=$MAXTOK"
log "batches=${BATCHES}（$N_BATCH 个）work_root=$WORK_ROOT 日志=$RLOG"
RUN_CMD="LITELLM_URL=http://127.0.0.1:$LITELLM_PORT/v1/chat/completions LITELLM_MODEL=$LL_ALIAS LITELLM_THINKING=false \
$PY -u '$RUNNER' --pattern '$PATTERN' --batches '$BATCHES' \
  --bundle $BUNDLE --concurrency $CONC --max-tokens $MAXTOK --work-root '$WORK_ROOT'"

STATE_PATH="$WORK_ROOT/state/batch_state.json"

write_launch_json() { # status
  emit_json_kv "$RUN_DIR/stage_c_launch.json" \
    status "$1" runner "$RUNNER" work_root "$WORK_ROOT" state "$STATE_PATH" \
    expected_batches "$EXPECTED_BATCHES" bundle "$BUNDLE" concurrency "$CONC" \
    max_tokens "$MAXTOK" model_alias "$LL_ALIAS" tmux_session "$TMUX_SESSION" log "$RLOG" \
    note "Gate6_NOT_PASS_until_Gate7_verifies_$STATE_PATH"
}

if [ "$USE_TMUX" = "1" ] && [ "$WAIT" = "0" ] && command -v tmux >/dev/null 2>&1; then
  tmux new -d -s "$TMUX_SESSION" "{ $RUN_CMD ; } > '$RLOG' 2>&1" || die "tmux 启动失败" 3
  write_launch_json "LAUNCHED"
  log "状态=LAUNCHED：runner 在 tmux '$TMUX_SESSION' 后台跑（attach: tmux attach -t $TMUX_SESSION）"
  warn "⚠️ Stage C 尚未完成，更未验收。Gate 6 PASS 与否由 Gate 7 裁决——"
  warn "    等 tmux 任务结束后执行: scripts/smoke_gate.py --state $STATE_PATH --expected-batches $EXPECTED_BATCHES ..."
  warn "    在此之前不得认为 Stage C 通过、不得进入 Production。"
  exit 0
else
  [ "$WAIT" = "1" ] && log "（--wait）前台运行，阻塞至完成..." || warn "前台运行（--no-tmux）：阻塞至完成"
  eval "$RUN_CMD" > "$RLOG" 2>&1
  RC=$?
  if [ "$RC" -ne 0 ]; then
    write_launch_json "FAILED"
    die "Stage C runner 非 0 退出 (exit=${RC})，日志 ${RLOG}" "$RC"
  fi
  write_launch_json "COMPLETED"
  log "状态=COMPLETED：runner 已跑完，日志 $RLOG"
  warn "⚠️ COMPLETED ≠ PASS。必须跑 Gate 7 验收: scripts/smoke_gate.py --state $STATE_PATH --expected-batches $EXPECTED_BATCHES ..."
  exit 0
fi
