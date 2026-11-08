#!/bin/bash
# run_all_preprocess.sh · 1985–2013 补抽预处理总驱动（CPU 段, 不需要 GPU）
# 用法:  bash 01_preprocess/run_all_preprocess.sh [WORK_ROOT]
#   需要 config.yaml: corpus_zip_dir（年度 zip 目录）、admin_division_json（pca-code.json）
# 环境变量:
#   PY      python 解释器 (默认 python3)
#   YEARS   只处理指定年份, 空格分隔 (默认 raw_zips 全部; dry-run 用)
#   MONTHS  传给 s1 的 --months (默认全月; dry-run 用)
# 产出: $WORK/case_events{,_segmented,_regex_extracted}/ · $WORK/llm_input_batches/y*_b200/
#       $WORK/offense_pack/ · $WORK/offense_llm_batches/ · $WORK/BATCH_MANIFEST.md
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
CFG_FILE="${PIPELINE_CONFIG:-$REPO/config.yaml}"
cfg() { python3 -c 'import sys,yaml;print(yaml.safe_load(open(sys.argv[1]))[sys.argv[2]])' "$CFG_FILE" "$1"; }
ZIP_DIR="$(cfg corpus_zip_dir)"
WORK="${1:-./work/supplement_1985_2013}"
PY="${PY:-python3}"
mkdir -p "$WORK"
LOG="$WORK/preprocess_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

echo "== preprocess == zips=$ZIP_DIR work=$WORK py=$PY"
export PCA_JSON_PATH="$(cfg admin_division_json)"
[ -f "$PCA_JSON_PATH" ] || { echo "FATAL: pca-code.json missing"; exit 3; }

# ---- S1: 逐 zip 流式解析 + 刑事新判决过滤 → hive parquet -------------------
ZIPS=()
if [ -n "${YEARS:-}" ]; then
  for y in $YEARS; do ZIPS+=("$ZIP_DIR/${y}年裁判文书数据.zip"); done
else
  while IFS= read -r z; do ZIPS+=("$z"); done < <(ls "$ZIP_DIR"/*.zip | sort)
fi
echo "== S1: ${#ZIPS[@]} zips =="
for z in "${ZIPS[@]}"; do
  echo "-- s1: $(basename "$z")"
  if [ -n "${MONTHS:-}" ]; then
    "$PY" "$HERE/01_stream_zip_to_parquet.py" --zip "$z" \
      --output "$WORK/case_events" --months "$MONTHS"
  else
    "$PY" "$HERE/01_stream_zip_to_parquet.py" --zip "$z" \
      --output "$WORK/case_events"
  fi
done

# ---- S2/S3: 切段 + hint 正则 ------------------------------------------------
echo "== S2: segment =="
"$PY" "$HERE/02_segment_paragraphs.py" \
  --input "$WORK/case_events" --output "$WORK/case_events_segmented"
echo "== S3: regex hints =="
"$PY" "$HERE/03_regex_prefill.py" \
  --input "$WORK/case_events_segmented" --output "$WORK/case_events_regex_extracted"

# ---- S4: 21-key LLM 输入批 ---------------------------------------------------
echo "== S4: build 21-key batches =="
"$PY" "$HERE/05_build_llm_batches_1985_2013.py" \
  --input "$WORK/case_events_regex_extracted" --batch-root "$WORK/llm_input_batches"

# ---- S5: offense regex 前置 + 残差批 -----------------------------------------
echo "== S5: offense hybrid front =="
"$PY" "$HERE/07_offense_date_front.py" \
  --segmented "$WORK/case_events_segmented" \
  --pack "$WORK/offense_pack" --batch-dir "$WORK/offense_llm_batches"

# ---- BATCH_MANIFEST ----------------------------------------------------------
echo "== MANIFEST =="
"$PY" - "$WORK" << 'PYEOF'
import json, sys
from pathlib import Path
W = Path(sys.argv[1])
s4 = json.loads((W / "llm_input_batches" / "s4_build_stats.json").read_text())
s5 = json.loads((W / "offense_pack" / "s5_offense_stats.json").read_text())
lines = ["# BATCH_MANIFEST — 补抽 1985–2013 预处理产物清单", ""]
lines += [f"- 扫描行(刑事新判决, 过滤后): {s4['rows_scanned']:,}",
          f"- 无被告段剔除: {s4['rows_no_defendant_seg']:,} · case_id 去重剔除: {s4['rows_dup_case_id']:,}",
          f"- **21-key 待抽 case: {s4['rows_batched']:,}**",
          f"- offense regex ok: {s5['regex_ok']:,}/{s5['regex_rows']:,} ({s5['regex_ok_rate']*100:.1f}%)",
          f"- **offense LLM 残差 case: {s5['residual_rows_batched']:,} ({s5['residual_batches']} batches)**", "",
          "## 21-key 逐年 batch", "", "| year | rows | batches |", "|---|---|---|"]
tot_b = 0
for y in sorted(s4["per_year"]):
    v = s4["per_year"][y]
    tot_b += v["batches"]
    lines.append(f"| {y} | {v['rows']:,} | {v['batches']} |")
lines += ["", f"**21-key batch 总数: {tot_b}**（Stage C smoke 用其中 50 个; runner --batches 指向 llm_input_batches/）",
          "", "## GPU 时间粗估 (it/s = cases/s, 全量抽取实测)",
          f"- 21-key: ≈ {s4['rows_batched']/6.72/3600:.1f} h @6.72 it/s · offense 残差: ≈ {s5['residual_rows_batched']/14.7/3600:.1f} h @14.7 it/s"]
(W / "BATCH_MANIFEST.md").write_text("\n".join(lines), encoding="utf-8")
print("\n".join(lines))
PYEOF

echo "== preprocess DONE == manifest: $WORK/BATCH_MANIFEST.md"
