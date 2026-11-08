#!/usr/bin/env bash
# common.sh — 所有 wrapper 脚本共享的 bash 工具。
# source 本文件后可用：log / die / pf（读 profile）/ new_run_dir / emit_json_kv / require_cmd
# 退出码约定（）：0=PASS  1=门槛未过(ALARM)  2=前置缺失  3=环境错误
set -o pipefail

AGENT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LIB_DIR="$AGENT_ROOT/scripts/lib"
PROFILE_READER="$LIB_DIR/profile_reader.py"

log()  { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
ok()   { printf '[%s] ✓ %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
warn() { printf '[%s] ⚠ %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die()  { printf '[%s] ✗ %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit "${2:-3}"; }

# pf <profile.yaml> <dotted.key> [default] — 读 profile 值
pf() { python3 "$PROFILE_READER" "$@"; }

require_cmd() { command -v "$1" >/dev/null 2>&1 || die "缺少命令: $1" 3; }

# new_run_dir [run_id] — 在 AGENT_ROOT/runs/ 下建运行目录，echo 其绝对路径
new_run_dir() {
  local rid="${1:-run_$(date +%Y%m%d_%H%M%S)}"
  local d="$AGENT_ROOT/runs/$rid"
  mkdir -p "$d"
  echo "$d"
}

# emit_json_kv <file> <k1> <v1> [<k2> <v2> ...] — 写一个扁平 JSON（字符串值）
emit_json_kv() {
  local f="$1"; shift
  python3 - "$f" "$@" <<'PYEOF'
import json, sys
f = sys.argv[1]
kv = sys.argv[2:]
d = {kv[i]: kv[i+1] for i in range(0, len(kv)-1, 2)}
open(f, "w", encoding="utf-8").write(json.dumps(d, ensure_ascii=False, indent=2))
PYEOF
}

# http_json <url> — curl 取 JSON，失败返回非 0
http_json() { curl -sS --max-time 15 "$1"; }
