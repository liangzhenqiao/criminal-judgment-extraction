#!/usr/bin/env bash
# =============================================================
# run_smoke.sh — env→smoke 确定性 Driver（把 7 Gate 串成一条不可跳步的流水线）
# =============================================================
# 为什么有这个脚本：散文文档（USAGE/RUNBOOK）保证不了 Agent「严格执行」——
# Agent 可能漏跑 Gate、误读退出码、没验收就喊 PASS。本 driver 把确定性放进代码：
#   - 每个 Gate 都经 run_gate 显式检查退出码，非 0 立即停，不可能跳步
#   - **driver 绝不自己判 GREEN**：只有 smoke_gate.py 真返回 GREEN 才算通过
#   - 不伪造证据：没 --batches 只到 Stage A；没 grounding 证据时 Gate7 如实
#     返回 COVERAGE_INCOMPLETE（除非显式 --allow-partial → PARTIAL_PASS，仍非 GREEN）
#   - 全程落 run-log 审计；最后打印机器可读 final 状态
#
# 用法：
#   # 仅 env→Stage A（无需 50-batch 数据，GPU 上即可跑完整服务+单 case）
#   bash scripts/run_smoke.sh --runtime $RT --model $MD --task $TASK
#
#   # 完整 env→smoke（需真实 50-batch 目录；默认自动生成 grounding json）
#   bash scripts/run_smoke.sh --runtime $RT --model $MD --task $TASK \
#        --batches <50batch_dir>
#   # 已有独立 grounding 时可加 --grounding-json <grounding_5k.json>
#
# 退出码（driver 总码 = 最严重的一步）：
#   0 = ENV+STAGE_A_OK（未跑 Stage C）或 Gate7 GREEN / PARTIAL_PASS
#   1 = 某 Gate ALARM / 门槛未过
#   2 = 前置缺失 / 证据不全（COVERAGE_INCOMPLETE）
#   3 = 环境错误
# =============================================================
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

RT=""; MD=""; TASK=""; BATCHES=""; GROUNDING=""; RUN_DIR=""; RUNNER=""; LITELLM_CONFIG=""
EXPECTED_BATCHES=50; ALLOW_PARTIAL=0; MODEL_ALIAS=""
while [ $# -gt 0 ]; do case "$1" in
  --runtime) RT="$2"; shift 2;;
  --model)   MD="$2"; shift 2;;
  --task)    TASK="$2"; shift 2;;
  --batches) BATCHES="$2"; shift 2;;
  --grounding-json) GROUNDING="$2"; shift 2;;
  --runner) RUNNER="$2"; shift 2;;
  --litellm-config) LITELLM_CONFIG="$2"; shift 2;;
  --expected-batches) EXPECTED_BATCHES="$2"; shift 2;;
  --model-alias) MODEL_ALIAS="$2"; shift 2;;
  --allow-partial) ALLOW_PARTIAL=1; shift;;
  --run-dir) RUN_DIR="$2"; shift 2;;
  *) die "未知参数: $1" 2;;
esac; done

[ -f "${RT:-}" ]   || die "需 --runtime <runtime.yaml>" 2
[ -f "${MD:-}" ]   || die "需 --model <model.yaml>" 2
[ -d "${TASK:-}" ] || die "需 --task <task profile 目录>" 2
PATTERN="$TASK/pattern.md";   [ -f "$PATTERN" ]    || die "task 缺 pattern.md: $PATTERN" 2
SAMPLE="$TASK/sample_case.json"; [ -f "$SAMPLE" ]  || die "task 缺 sample_case.json: $SAMPLE" 2
SCHEMA="$TASK/schema.json"
ACCEPT="$TASK/acceptance.yaml";  [ -f "$ACCEPT" ]  || die "task 缺 acceptance.yaml: $ACCEPT" 2
if [ -z "$RUNNER" ] && [ -f "$TASK/runner.py" ]; then RUNNER="$TASK/runner.py"; fi
[ -z "$RUNNER" ] || [ -f "$RUNNER" ] || die "--runner 文件不存在: $RUNNER" 2
[ -z "$LITELLM_CONFIG" ] || [ -f "$LITELLM_CONFIG" ] || die "--litellm-config 文件不存在: $LITELLM_CONFIG" 2
[ -n "$MODEL_ALIAS" ] || MODEL_ALIAS=$(pf "$MD" litellm.model_alias legal-extractor-80b)
if [ -z "$RUN_DIR" ]; then RUN_DIR="$(new_run_dir)"; else mkdir -p "$RUN_DIR"; fi
PY=$(pf "$RT" bin.python python3); [ -x "$PY" ] || PY=python3
DRIVER_LOG="$RUN_DIR/run_smoke.log"
: > "$DRIVER_LOG"

# 记录每个 Gate 的退出码到 run-log（审计 trail）
record() { printf '[%s] %-22s exit=%s\n' "$(date +%H:%M:%S)" "$1" "$2" | tee -a "$DRIVER_LOG" >&2; }

# run_gate <名称> <命令...> —— 跑一个 Gate，非 0 立即终止整个 driver（带审计 + 提示）
run_gate() {
  local name="$1"; shift
  log "▶ $name"
  "$@"; local rc=$?
  record "$name" "$rc"
  if [ "$rc" -ne 0 ]; then
    warn "✗ $name 失败 (exit=$rc) — driver 终止，不进入下一 Gate。"
    warn "  产物/日志在 $RUN_DIR/。"
    emit_json_kv "$RUN_DIR/run_smoke_final.json" \
      final "ABORTED" aborted_at "$name" exit "$rc" run_dir "$RUN_DIR"
    exit "$rc"
  fi
  ok "✓ $name"
}

log "=== run_smoke driver 启动 | RUN=$RUN_DIR ==="
log "runtime=$RT  model=$MD  task=$TASK"
[ -n "$BATCHES" ] && log "batches=$BATCHES (expected=$EXPECTED_BATCHES)" || log "未给 --batches：只跑到 Stage A（不做 Stage C，结果不会是完整 GREEN）"

# ---------- Gate 0–4：env → Stage A（始终跑）----------
run_gate "Gate0_doctor"          bash scripts/doctor.sh          --runtime "$RT" --model "$MD" --run-dir "$RUN_DIR"
run_gate "Gate1_verify_artifact" bash scripts/verify_artifact.sh --runtime "$RT" --model "$MD" --run-dir "$RUN_DIR"
run_gate "Gate2_launch_vllm"     bash scripts/launch_vllm.sh     --runtime "$RT" --model "$MD" --run-dir "$RUN_DIR"
LITELLM_ARGS=( --runtime "$RT" --model "$MD" --model-alias "$MODEL_ALIAS" --run-dir "$RUN_DIR" )
[ -n "$LITELLM_CONFIG" ] && LITELLM_ARGS+=( --config "$LITELLM_CONFIG" )
run_gate "Gate3_launch_litellm"  bash scripts/launch_litellm.sh "${LITELLM_ARGS[@]}"
STAGE_A_ARGS=( --pattern "$PATTERN" --case "$SAMPLE" --model "$MODEL_ALIAS"
               --out "$RUN_DIR/smoke_stage_a.json" )
[ -f "$SCHEMA" ] && STAGE_A_ARGS+=( --schema "$SCHEMA" )
run_gate "Gate4_smoke_stage_a"   "$PY" scripts/smoke_stage_a.py "${STAGE_A_ARGS[@]}"

# ---------- 未给 batches：到此为止，明确不是完整 GREEN ----------
if [ -z "$BATCHES" ]; then
  emit_json_kv "$RUN_DIR/run_smoke_final.json" \
    final "ENV_STAGE_A_OK" note "Stage_C_NOT_RUN_so_NOT_a_full_GREEN" \
    stage_a "$RUN_DIR/smoke_stage_a.json" run_dir "$RUN_DIR"
  log "================================================================"
  ok  "ENV + Stage A 通过。"
  warn "⚠ 未跑 Stage C（没给 --batches）——这不是完整 GREEN，不得进入 Production。"
  warn "  要完整验收：补 --batches <50batch_dir> 重跑；driver 会自动生成 grounding，或显式传 --grounding-json。"
  echo '{"final":"ENV_STAGE_A_OK"}'   # 机器可读
  exit 0
fi

# ---------- Gate 6：Stage C（--wait 跑完，本步不判 PASS）----------
STAGE_C_ARGS=( --runtime "$RT" --model "$MD" --pattern "$PATTERN"
               --batches "$BATCHES" --expected-batches "$EXPECTED_BATCHES"
               --model-alias "$MODEL_ALIAS" --wait --run-dir "$RUN_DIR" )
[ -n "$RUNNER" ] && STAGE_C_ARGS+=( --runner "$RUNNER" )
run_gate "Gate6_smoke_stage_c" bash scripts/smoke_stage_c.sh "${STAGE_C_ARGS[@]}"

# 从 stage_c_launch.json 取真实 work_root → state 路径（不猜）
LAUNCH="$RUN_DIR/stage_c_launch.json"
[ -f "$LAUNCH" ] || die "Stage C 未落 stage_c_launch.json，无法定位 state" 3
WORK_ROOT=$("$PY" -c "import json;print(json.load(open('$LAUNCH'))['work_root'])")
STATE="$WORK_ROOT/state/batch_state.json"
[ -f "$STATE" ] || die "Stage C 跑完但无 batch_state.json: $STATE（runner 可能异常退出，查 stage_c log）" 3

# ---------- Gate 7：总验收（唯一判 GREEN 的地方）----------
GATE7_ARGS=( --acceptance "$ACCEPT" --sanity "$RUN_DIR/smoke_stage_a.json"
             --state "$STATE" --expected-batches "$EXPECTED_BATCHES"
             --out "$RUN_DIR/smoke_verdict.json" )
if [ -n "$GROUNDING" ]; then
  [ -f "$GROUNDING" ] || die "--grounding-json 文件不存在: $GROUNDING" 2
  GATE7_ARGS+=( --grounding-json "$GROUNDING" )
elif [ "$ALLOW_PARTIAL" = "1" ]; then
  warn "未给 --grounding-json 且 --allow-partial：Gate7 将跳过 grounding → 最多 PARTIAL_PASS（绝不 GREEN）"
  GATE7_ARGS+=( --no-require-grounding )
else
  GROUNDING="$RUN_DIR/grounding_5k.json"
  if [ -f "$TASK/grounding.py" ]; then
    GROUNDING_CMD=( "$PY" "$TASK/grounding.py" )
    GROUNDING_GATE="Gate6b_task_grounding"
  else
    GROUNDING_CMD=( "$PY" scripts/grounding_compare.py )
    GROUNDING_GATE="Gate6b_grounding_compare"
  fi
  GROUNDING_ARGS=( --cases-dir "$BATCHES" --results-dir "$WORK_ROOT/results" --out "$GROUNDING" )
  [ -f "$SCHEMA" ] && GROUNDING_ARGS+=( --schema "$SCHEMA" )
  run_gate "$GROUNDING_GATE" "${GROUNDING_CMD[@]}" "${GROUNDING_ARGS[@]}"
  GATE7_ARGS+=( --grounding-json "$GROUNDING" )
fi

log "▶ Gate7_smoke_gate"
"$PY" scripts/smoke_gate.py "${GATE7_ARGS[@]}"; GATE7_RC=$?
record "Gate7_smoke_gate" "$GATE7_RC"

VERDICT=$("$PY" -c "import json;print(json.load(open('$RUN_DIR/smoke_verdict.json'))['verdict'])" 2>/dev/null || echo "UNKNOWN")
emit_json_kv "$RUN_DIR/run_smoke_final.json" \
  final "$VERDICT" gate7_exit "$GATE7_RC" verdict_file "$RUN_DIR/smoke_verdict.json" run_dir "$RUN_DIR"

log "================================================================"
case "$VERDICT" in
  GREEN)
    ok "★ smoke 全链路 GREEN — 可进 Production。验收凭证: $RUN_DIR/smoke_verdict.json" ;;
  ALARM)
    warn "✗ ALARM — 门槛未过。看 smoke_verdict.json 的 alarms，回对应 Gate 修。" ;;
  COVERAGE_INCOMPLETE)
    warn "✗ COVERAGE_INCOMPLETE — 证据不全（多半缺 grounding）。看 missing_evidence。**这不是 GREEN**。" ;;
  PARTIAL_PASS)
    warn "△ PARTIAL_PASS — 部分验收通过，但未全覆盖。**绝不等于 GREEN，不得进 Production**。" ;;
  *)
    warn "? 未知 verdict（$VERDICT）——查 smoke_verdict.json" ;;
esac
echo "{\"final\":\"$VERDICT\"}"   # 机器可读
exit "$GATE7_RC"
