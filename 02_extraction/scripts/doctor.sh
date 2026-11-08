#!/usr/bin/env bash
# Gate 0 — 环境自检 doctor（纯只读）。Gate 0。
# 用法: scripts/doctor.sh --runtime <runtime.yaml> --model <model.yaml> [--run-dir <dir>]
# 退出: 0=全 PASS  2=前置缺失  3=环境错误（含关键项 FAIL）
# 产物: runs/<id>/doctor_report.md
set -u
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

RUNTIME=""; MODEL=""; RUN_DIR=""
while [ $# -gt 0 ]; do case "$1" in
  --runtime) RUNTIME="$2"; shift 2;;
  --model)   MODEL="$2";   shift 2;;
  --run-dir) RUN_DIR="$2"; shift 2;;
  *) die "未知参数: $1" 2;;
esac; done
[ -n "$RUNTIME" ] && [ -f "$RUNTIME" ] || die "需 --runtime <存在的 runtime.yaml>" 2
[ -n "$MODEL" ]   && [ -f "$MODEL" ]   || die "需 --model <存在的 model.yaml>" 2
if [ -z "$RUN_DIR" ]; then RUN_DIR="$(new_run_dir)"; else mkdir -p "$RUN_DIR"; fi
REPORT="$RUN_DIR/doctor_report.md"

PY=$(pf "$RUNTIME" bin.python)
MODEL_DIR=$(pf "$RUNTIME" paths.model_dir)
WORK_ROOT=$(pf "$RUNTIME" paths.work_root)
HF_HOME=$(pf "$RUNTIME" paths.hf_home)
VLLM_PORT=$(pf "$RUNTIME" ports.vllm 8020)
LITELLM_PORT=$(pf "$RUNTIME" ports.litellm 4000)
MIN_DISK=$(pf "$RUNTIME" doctor.min_data_disk_avail_gb 50)
TP=$(pf "$MODEL" vllm.tensor_parallel_size 2)
SIZE_GB=$(pf "$MODEL" model.size_gb 80)
MIN_GPU_MEM=$(pf "$MODEL" doctor.min_gpu_mem_mib_per_card "")

PASS=0; FAIL=0
{
  echo "# Doctor Report"
  echo "- generated: $(date -Iseconds)"
  echo "- runtime: \`$RUNTIME\`  model: \`$MODEL\`"
  echo
  echo "| 检查 | 期望 | 实测 | 结果 |"
  echo "|---|---|---|---|"
} > "$REPORT"

row() { # name expect actual verdict
  echo "| $1 | $2 | $3 | $4 |" >> "$REPORT"
  if [ "$4" = "PASS" ]; then PASS=$((PASS+1)); else FAIL=$((FAIL+1)); fi
}

# --- GPU（nvidia-smi）---
if command -v nvidia-smi >/dev/null 2>&1; then
  GPU_N=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | wc -l | tr -d ' ')
  GPU_MEM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')
  CC=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ')
  [ "${GPU_N:-0}" -ge "${TP:-2}" ] && row "GPU 数量" ">= TP($TP)" "$GPU_N" "PASS" || row "GPU 数量" ">= TP($TP)" "${GPU_N:-0}" "FAIL"
  # 单卡显存 MiB。若 model profile 声明实测下限，优先用硬门槛；否则退回权重分片粗估。
  if [ -n "${GPU_MEM:-}" ]; then
    if [ -n "${MIN_GPU_MEM:-}" ]; then
      NEED="$MIN_GPU_MEM"
      EXPECT=">= ${NEED} (model doctor.min_gpu_mem_mib_per_card)"
    else
      NEED=$(python3 -c "print(int(${SIZE_GB:-80}*1024/max(${TP:-2},1)))" 2>/dev/null)
      EXPECT=">= ${NEED} (weight shard rough check)"
    fi
    [ "${GPU_MEM%.*}" -ge "${NEED:-40000}" ] && row "单卡显存(MiB)" "$EXPECT" "$GPU_MEM" "PASS" || row "单卡显存(MiB)" "$EXPECT" "$GPU_MEM" "FAIL"
  fi
  case "${CC:-}" in
    sm_8[9]*|sm_9*|sm_1*|*12.0*|8.9|9.*|12.*) row "compute_cap(FP8)" ">= sm_89" "$CC" "PASS";;
    "") row "compute_cap(FP8)" ">= sm_89" "未知" "FAIL";;
    *) row "compute_cap(FP8)" ">= sm_89" "$CC" "FAIL";;
  esac
else
  row "nvidia-smi" "可用" "缺失" "FAIL"
fi

# --- Python / vllm / litellm 依赖 ---
MIN_VLLM="0.19.1"
if [ -n "$PY" ] && [ -x "$PY" ]; then
  row "python bin" "可执行" "$PY" "PASS"
  VV=$("$PY" -c "import vllm,sys;sys.stdout.write(vllm.__version__)" 2>/dev/null)
  if [ -n "$VV" ]; then
    # 真正做版本比较（packaging 优先；缺则按 release tuple 手工比）
    VOK=$("$PY" - "$VV" "$MIN_VLLM" <<'PYEOF' 2>/dev/null
import sys
cur, need = sys.argv[1], sys.argv[2]
def rel(v):
    out=[]
    for p in v.split("+")[0].split("."):
        n=""
        for ch in p:
            if ch.isdigit(): n+=ch
            else: break
        out.append(int(n) if n else 0)
    return tuple(out)
try:
    from packaging.version import Version
    ok = Version(cur) >= Version(need)
except Exception:
    a, b = rel(cur), rel(need)
    a = a + (0,)*(len(b)-len(a)); b = b + (0,)*(len(a)-len(b))
    ok = a >= b
print("yes" if ok else "no")
PYEOF
)
    [ "$VOK" = "yes" ] && row "vllm 版本" ">=$MIN_VLLM" "$VV" "PASS" || row "vllm 版本" ">=$MIN_VLLM" "$VV (过低)" "FAIL"
  else
    row "vllm 版本" ">=$MIN_VLLM" "import 失败" "FAIL"
  fi
  "$PY" -c "import litellm" 2>/dev/null && row "litellm import" "可用" "ok" "PASS" || row "litellm import" "可用" "失败" "FAIL"
  "$PY" -c "import requests" 2>/dev/null && row "requests(runner依赖)" "可用" "ok" "PASS" || row "requests" "可用" "失败" "FAIL"
  "$PY" -c "import yaml" 2>/dev/null && row "PyYAML(acceptance解析)" "可用" "ok" "PASS" || row "PyYAML(acceptance解析)" "可用" "失败" "FAIL"
else
  row "python bin" "可执行" "${PY:-未配置}" "FAIL"
fi

# --- 模型目录 + config.json（启 vLLM 前置）---
if [ -n "$MODEL_DIR" ] && [ -f "$MODEL_DIR/config.json" ]; then
  row "模型 config.json" "存在" "$MODEL_DIR/config.json" "PASS"
else
  row "模型 config.json" "存在" "${MODEL_DIR:-未配置}/config.json 缺" "FAIL"
fi

# --- 端口空闲 ---
port_busy() { ss -lntH 2>/dev/null | grep -q ":$1 " || netstat -lnt 2>/dev/null | grep -q ":$1 "; }
for p in "$VLLM_PORT" "$LITELLM_PORT"; do
  if port_busy "$p"; then
    PID=$(ss -lntpH 2>/dev/null | grep ":$p " | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
    row "端口 $p 空闲" "空闲" "占用 pid=${PID:-?}（不自动 kill）" "FAIL"
  else
    row "端口 $p 空闲" "空闲" "空闲" "PASS"
  fi
done

# --- 数据盘空间（df -k 可移植：Linux/macOS 均支持，KB→GB）---
if [ -n "$WORK_ROOT" ]; then
  CHECK_PATH="$WORK_ROOT"; [ -d "$CHECK_PATH" ] || CHECK_PATH="$(dirname "$WORK_ROOT")"
  [ -d "$CHECK_PATH" ] || CHECK_PATH="/"
  AVAIL=$(df -k "$CHECK_PATH" 2>/dev/null | awk 'NR==2{printf "%d", $4/1048576}')
  if [ -n "${AVAIL:-}" ] && [ "${AVAIL}" -ge "${MIN_DISK}" ]; then
    row "数据盘剩余(GB)" ">= $MIN_DISK" "$AVAIL ($CHECK_PATH)" "PASS"
  else
    row "数据盘剩余(GB)" ">= $MIN_DISK" "${AVAIL:-未知} ($CHECK_PATH)" "FAIL"
  fi
fi

# --- tmux（长任务必需，toolkit 坑2）---
command -v tmux >/dev/null 2>&1 && row "tmux" "可用" "ok" "PASS" || row "tmux" "可用" "缺失(长任务无法 detach)" "FAIL"

{
  echo
  echo "## 汇总"
  echo "- PASS: $PASS  FAIL: $FAIL"
  if [ "$FAIL" -eq 0 ]; then echo "- verdict: **GREEN** — 可进 Gate 1"; else echo "- verdict: **BLOCKED** — 修复 FAIL 项后重跑（端口、显存、依赖见 README）"; fi
} >> "$REPORT"

log "doctor 报告: $REPORT (PASS=$PASS FAIL=$FAIL)"

[ "$FAIL" -eq 0 ] && { ok "Gate 0 PASS"; exit 0; } || { warn "Gate 0 有 $FAIL 项 FAIL"; exit 3; }
