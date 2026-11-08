#!/usr/bin/env python3
"""profile_reader.py — 读 YAML profile 的某个 dotted key。

依赖友好：优先用 PyYAML；缺失则回退到内置的最小 YAML 子集解析器
（够读本工程的 profile：嵌套 map + 标量 + 注释；不支持锚点/flow-list）。

用法：
  python3 profile_reader.py <profile.yaml> <dotted.key>      # 打印值（找不到 → 空 + exit 1）
  python3 profile_reader.py <profile.yaml> <dotted.key> DEF  # 找不到时打印 DEF（exit 0）
例：
  python3 profile_reader.py runtime.yaml bin.python
  python3 profile_reader.py model.yaml vllm.max_num_seqs
"""
import sys


def _coerce(s):
    s = s.strip()
    if s == "" or s.lower() in ("null", "~", "none"):
        return None
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False
    if (s[0] == s[-1]) and s[0] in ("'", '"') and len(s) >= 2:
        return s[1:-1]
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _minimal_parse(text):
    """缩进式嵌套 map 解析（仅本工程 profile 所需子集）。"""
    root = {}
    stack = [(-1, root)]
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip() if not _in_quote(raw) else raw.rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if ":" not in line:
            continue
        key, _, val = line.lstrip().partition(":")
        key = key.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if val.strip() == "":
            node = {}
            parent[key] = node
            stack.append((indent, node))
        else:
            parent[key] = _coerce(val)
    return root


def _in_quote(line):
    # 简化：含被引号包裹的 # 时不切注释（如 "0,1"）。本工程值无内联 # 注释，保守返回 False。
    return False


def main():
    if len(sys.argv) < 3:
        sys.stderr.write(__doc__)
        sys.exit(2)
    path, dotted = sys.argv[1], sys.argv[2]
    default = sys.argv[3] if len(sys.argv) > 3 else None
    text = open(path, encoding="utf-8").read()
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text)
    except Exception:
        data = _minimal_parse(text)
    cur = data
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            if default is not None:
                print(default)
                sys.exit(0)
            sys.stderr.write(f"[profile_reader] key not found: {dotted} in {path}\n")
            sys.exit(1)
    if cur is None:
        print("")
    elif isinstance(cur, bool):
        print("true" if cur else "false")
    else:
        print(cur)


if __name__ == "__main__":
    main()
