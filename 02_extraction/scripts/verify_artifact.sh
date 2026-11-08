#!/usr/bin/env bash
# Gate 1 — 模型工件核验（纯只读）。Gate 1。
# 检查权重目录的完整性。
# 用法: scripts/verify_artifact.sh --runtime <runtime.yaml> --model <model.yaml> [--run-dir <dir>]
# 退出: 0=PASS  1=核验未过  2=前置缺失
set -u
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

RUNTIME=""; MODEL=""; RUN_DIR=""
while [ $# -gt 0 ]; do case "$1" in
  --runtime) RUNTIME="$2"; shift 2;;
  --model)   MODEL="$2";   shift 2;;
  --run-dir) RUN_DIR="$2"; shift 2;;
  *) die "未知参数: $1" 2;;
esac; done
[ -f "${RUNTIME:-}" ] || die "需 --runtime" 2
[ -f "${MODEL:-}" ]   || die "需 --model" 2
if [ -z "$RUN_DIR" ]; then RUN_DIR="$(new_run_dir)"; else mkdir -p "$RUN_DIR"; fi
OUT="$RUN_DIR/artifact_report.json"

MODEL_DIR=$(pf "$RUNTIME" paths.model_dir)
PRECISION=$(pf "$MODEL" model.precision fp8)
SIZE_GB=$(pf "$MODEL" model.size_gb 80)
PY=$(pf "$RUNTIME" bin.python python3)
[ -x "$PY" ] || PY=python3

[ -d "$MODEL_DIR" ] || die "模型目录不存在: $MODEL_DIR" 1

"$PY" - "$MODEL_DIR" "$PRECISION" "$SIZE_GB" "$OUT" <<'PYEOF'
import json, os, sys, glob
mdir, precision, size_gb, out = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
r = {"snapshot_path": mdir, "verdict": "PASS", "fails": []}

def fail(msg): r["verdict"]="FAIL"; r["fails"].append(msg)

cfg_p = os.path.join(mdir, "config.json")
r["has_config"] = os.path.isfile(cfg_p)
if not r["has_config"]: fail("config.json 缺失")

tok_ok = any(os.path.isfile(os.path.join(mdir,f)) for f in
             ("tokenizer_config.json","tokenizer.json","vocab.json"))
r["has_tokenizer"] = tok_ok
if not tok_ok: fail("tokenizer 文件缺失")

# chat_template
ctlen = 0
tcp = os.path.join(mdir, "tokenizer_config.json")
if os.path.isfile(tcp):
    try:
        tc = json.load(open(tcp, encoding="utf-8"))
        ct = tc.get("chat_template")
        if ct: ctlen = len(ct)
    except Exception as e: fail(f"tokenizer_config 解析失败: {e}")
r["has_chat_template"] = ctlen > 0
r["chat_template_len"] = ctlen
if ctlen == 0: fail("chat_template 缺失（vLLM 会拒 chat completion）")

# safetensors 数量 vs 索引
sts = glob.glob(os.path.join(mdir, "*.safetensors"))
r["num_safetensors"] = len(sts)
idx_p = os.path.join(mdir, "model.safetensors.index.json")
if os.path.isfile(idx_p):
    try:
        wm = json.load(open(idx_p, encoding="utf-8")).get("weight_map", {})
        need = set(wm.values())
        miss = [f for f in need if not os.path.isfile(os.path.join(mdir, f))]
        r["index_files_expected"] = len(need)
        if miss: fail(f"safetensors 索引缺片: {miss[:3]}")
    except Exception as e: fail(f"safetensors 索引解析失败: {e}")
elif len(sts) == 0:
    fail("无 safetensors 文件")

# config.json 检查 + FP8 细节（fp8_format=e4m3, fp8_activation_scheme=dynamic,
#   weight_block_size=[128,128], quant_method=fp8）
if r["has_config"]:
    try:
        c = json.load(open(cfg_p, encoding="utf-8"))
        r["architecture"] = (c.get("architectures") or [None])[0]
        r["model_type"] = c.get("model_type")
        qc = c.get("quantization_config") or {}
        r["fp8_in_config"] = bool(qc)
        # 记录所有 FP8 细节字段（不同模型字段命名可能不同，全量记录便于审计）
        r["quant_method"] = qc.get("quant_method")
        r["fp8_fmt"] = qc.get("fmt")
        r["fp8_activation_scheme"] = qc.get("activation_scheme")
        r["weight_block_size"] = qc.get("weight_block_size")
        if precision.lower() == "fp8":
            if not qc:
                fail("profile 声明 FP8 但 config 无 quantization_config（这是 BF16，会 fallback 慢一半）")
            else:
                # 判定范围（block-wise FP8）：quant_method/fmt 任一含 fp8，且 weight_block_size 存在
                method_str = str(qc.get("quant_method") or qc.get("fmt") or qc.get("activation_scheme") or "").lower()
                if "fp8" not in method_str and qc.get("fmt") not in ("e4m3", "e5m2"):
                    fail(f"quantization_config 存在但未识别为 FP8（method/fmt={method_str or qc.get('fmt')}）；"
                         f"判定范围=quant_method/fmt 含 fp8 或 fmt∈{{e4m3,e5m2}}")
                if qc.get("weight_block_size") is None:
                    # 非阻断警告：部分 FP8 模型用 per-tensor 而非 block-wise，不算 FAIL
                    r.setdefault("warnings", []).append(
                        "weight_block_size 缺失（可能是 per-tensor FP8，非 block-wise；本工程基线是 [128,128] block-wise）")
    except Exception as e: fail(f"config.json 解析失败: {e}")

# 总大小
total = sum(os.path.getsize(p) for p in sts) if sts else 0
r["total_size_gb"] = round(total/1e9, 2)
if total and abs(r["total_size_gb"] - size_gb) > size_gb*0.25:
    fail(f"总大小 {r['total_size_gb']}GB 与 profile size_gb={size_gb} 偏差>25%")

open(out, "w", encoding="utf-8").write(json.dumps(r, ensure_ascii=False, indent=2))
print(json.dumps({"verdict": r["verdict"], "fails": r["fails"],
                  "size_gb": r["total_size_gb"], "n_safetensors": r["num_safetensors"],
                  "fp8": r.get("fp8_in_config"), "chat_template_len": ctlen}, ensure_ascii=False))
sys.exit(0 if r["verdict"]=="PASS" else 1)
PYEOF
RC=$?
log "工件核验报告: $OUT"
[ $RC -eq 0 ] && { ok "Gate 1 PASS"; exit 0; } || { warn "Gate 1 FAIL（详见 ${OUT}；解法见 README）"; exit 1; }
