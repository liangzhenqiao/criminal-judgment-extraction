#!/usr/bin/env python3
"""Task runner for offense_name_v1.0.

同族跑法：复用 runner/run_full_extraction.py 的
transport（call_litellm：{"data":[...]} 解析 / <think> 剥离 / fence 清理 /
context_overflow 检测）与 _stop_flag，但把 criminal_defendant 的 21 键 verdict
替换为 offense_name 的 5 键契约校验 + 逐字 grounding。

由仓库根通过 scripts/smoke_stage_c.sh 启动，或直接：
  LITELLM_URL=http://127.0.0.1:4000/v1/chat/completions LITELLM_MODEL=legal-extractor-80b \
  LITELLM_THINKING=false python3 runner.py --pattern <pattern.md> --batches <dir> \
    --bundle 4 --concurrency 48 --max-tokens 3072 --work-root <WORK>

输出契约（每 case 一条，恰 7 键）：
  id, offense_start_date, offense_end_date, date_precision,
  all_offense_dates_raw, confidence, failure_reason
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime


REQUIRED_KEYS = [
    "id",
    "offense_name",
    "all_offense_names_raw",
    "confidence",
    "failure_reason",
]
CONFIDENCE = {"high", "medium", "low"}
FAILURE_REASONS = {None, "no_offense_name_in_text", "text_garbled_or_empty"}

RE_YMD = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RE_YM = re.compile(r"^\d{4}-\d{2}$")
RE_Y = re.compile(r"^\d{4}$")


def find_agent_root() -> pathlib.Path:
    candidates = []
    env = os.environ.get("LLM_EXTRACTION_AGENT_ROOT")
    if env:
        candidates.append(pathlib.Path(env).expanduser().resolve())
    candidates.append(pathlib.Path.cwd().resolve())
    here = pathlib.Path(__file__).resolve()
    candidates.extend(here.parents)
    for root in candidates:
        marker = root / "runner" / "run_full_extraction.py"
        if marker.is_file():
            return root
    raise SystemExit(
        "Cannot locate llm-extraction-agent root. Run from that repo root or set "
        "LLM_EXTRACTION_AGENT_ROOT."
    )


def load_base_runner(root: pathlib.Path):
    path = root / "runner" / "run_full_extraction.py"
    spec = importlib.util.spec_from_file_location("base_full_extraction", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Cannot import base runner: {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["base_full_extraction"] = mod
    spec.loader.exec_module(mod)
    return mod


ROOT = find_agent_root()
BASE = load_base_runner(ROOT)


def iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def compact(value) -> str:
    # 与 judge_signature 同族：去掉所有空白与全角空格后做逐字子串比对
    return re.sub(r"[\s　]+", "", "" if value is None else str(value))


def is_missing(value) -> bool:
    return value is None or value == ""


def date_matches_precision(value: str, precision: str) -> bool:
    if precision == "YMD":
        return bool(RE_YMD.match(value))
    if precision == "YM":
        return bool(RE_YM.match(value))
    if precision == "Y":
        return bool(RE_Y.match(value))
    return False


RANK = {"Y": 0, "YM": 1, "YMD": 2}
TRUNC = {"Y": 4, "YM": 7, "YMD": 10}


def date_fmt(value):
    """返回日期字符串自身的精度 YMD/YM/Y；格式非法返回 None。"""
    if not isinstance(value, str):
        return None
    if RE_YMD.match(value):
        return "YMD"
    if RE_YM.match(value):
        return "YM"
    if RE_Y.match(value):
        return "Y"
    return None


def coarser(a: str, b: str) -> str:
    return a if RANK[a] <= RANK[b] else b


def verdict_record(case_in: dict, obj: dict) -> dict:
    if not isinstance(obj, dict):
        return {"ok": False, "code": "obj_not_dict"}

    keys = set(obj.keys())
    required = set(REQUIRED_KEYS)
    missing = sorted(required - keys)
    extra = sorted(keys - required)
    if missing:
        return {"ok": False, "code": f"schema_violation:missing_key:{missing[0]}"}
    if extra:
        return {"ok": False, "code": f"schema_violation:extra_key:{extra[0]}"}

    # id 逐字保真
    if str(obj.get("id")) != str(case_in.get("id")):
        return {
            "ok": False,
            "code": "id_mismatch",
            "detail": f"input={case_in.get('id')} output={obj.get('id')}",
        }

    # 枚举字段
    if obj.get("confidence") not in CONFIDENCE:
        return {"ok": False, "code": "schema_violation:bad_confidence",
                "detail": str(obj.get("confidence"))}
    fr = obj.get("failure_reason")
    if fr not in FAILURE_REASONS:
        return {"ok": False, "code": "schema_violation:bad_failure_reason",
                "detail": str(fr)}

    raw = obj.get("all_offense_names_raw")
    if not isinstance(raw, list):
        return {"ok": False, "code": "schema_violation:type_mismatch:all_offense_names_raw"}

    name = obj.get("offense_name")

    if fr is not None:
        if name is not None:
            return {"ok": False, "code": "failure_fields_not_null", "detail": str(name)}
        if raw != []:
            return {"ok": False, "code": "failure_raw_not_empty"}
        return {"ok": True}

    # 成功分支
    if not isinstance(name, str) or not (2 <= len(name) <= 24):
        return {"ok": False, "code": "success_bad_name", "detail": str(name)[:40]}
    if re.search(r"[0-9A-Za-z]", name):
        return {"ok": False, "code": "success_bad_name_charset", "detail": name[:40]}
    if not raw:
        return {"ok": False, "code": "success_raw_empty"}

    # grounding（硬）：raw 每元素 ∈ 三文本字段拼接的逐字子串（compact）
    source_c = compact(str(case_in.get("case_title") or "")
                       + str(case_in.get("judgment_segment") or "")
                       + str(case_in.get("facts_segment") or ""))
    for elem in raw:
        elem_c = compact(elem)
        if not elem_c:
            return {"ok": False, "code": "raw_element_empty"}
        if elem_c not in source_c:
            return {"ok": False, "code": "grounding_not_substring",
                    "detail": str(elem)[:80]}

    # 反幻觉核心：offense_name（去尾'罪'后）必须挂在某个 raw 元素上
    name_c = compact(name)
    variants = {name_c, name_c + "罪"}
    if not any(v in compact(e) for e in raw for v in variants):
        return {"ok": False, "code": "name_not_in_raw", "detail": name[:40]}

    return {"ok": True}



def select_output_for_case(arr: list, case_in: dict) -> list:
    case_id = str(case_in.get("id", ""))
    return [o for o in arr if isinstance(o, dict) and str(o.get("id", "")) == case_id]


def verdict_call(case_in: dict, call_result: dict) -> list:
    if not call_result.get("ok"):
        return [(
            None,
            {
                "ok": False,
                "code": call_result.get("code", "unknown"),
                "detail": call_result.get("err") or (call_result.get("raw") or "")[:200],
            },
        )]
    arr = call_result.get("parsed_arr") or []
    if len(arr) != 1:
        return [(
            None,
            {
                "ok": False,
                "code": "output_count_mismatch",
                "detail": f"expected 1 obj for id={case_in.get('id')}, got {len(arr)}",
            },
        )]
    obj = arr[0]
    return [(obj, verdict_record(case_in, obj))]


def process_batch(batch_path, batches_root, results_dir, fails_dir, pattern,
                  bundle, concurrency, max_tokens, timeout, dry_run_n=None) -> dict:
    cases = json.loads(batch_path.read_text(encoding="utf-8"))
    if dry_run_n:
        cases = cases[:dry_run_n]
    n_total = len(cases)
    chunks = [cases[i:i + bundle] for i in range(0, n_total, bundle)]

    batch_id = batch_path.stem
    rel = batch_path.relative_to(batches_root)
    sub = rel.parent
    res_path = results_dir / sub / f"{batch_id}_result.jsonl"
    fail_path = fails_dir / sub / f"{batch_id}_failures.jsonl"
    res_path.parent.mkdir(parents=True, exist_ok=True)
    fail_path.parent.mkdir(parents=True, exist_ok=True)

    ok_records, fail_records = [], []
    prompt_tokens, comp_tokens, elapsed_list = [], [], []
    fail_codes = {}
    warn_codes = {}

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = {ex.submit(BASE.call_litellm, ch, pattern, max_tokens, timeout): ch
                for ch in chunks}
        for fut in as_completed(futs):
            chunk_in = futs[fut]
            cr = fut.result()
            if cr.get("prompt_tokens"):
                prompt_tokens.append(cr["prompt_tokens"])
            if cr.get("completion_tokens"):
                comp_tokens.append(cr["completion_tokens"])
            if cr.get("elapsed"):
                elapsed_list.append(cr["elapsed"])

            for case_in in chunk_in:
                if cr.get("ok"):
                    my_objs = select_output_for_case(cr.get("parsed_arr") or [], case_in)
                    sub_cr = {"ok": True, "parsed_arr": my_objs, "raw": cr.get("raw", "")}
                else:
                    sub_cr = cr
                for obj, verdict in verdict_call(case_in, sub_cr):
                    if verdict["ok"]:
                        ok_records.append(obj)
                        w = verdict.get("warn")
                        if w:
                            warn_codes[w] = warn_codes.get(w, 0) + 1
                    else:
                        code = verdict["code"]
                        fail_codes[code] = fail_codes.get(code, 0) + 1
                        fail_records.append({
                            "id": case_in.get("id"),
                            "failure_code": code,
                            "detail": verdict.get("detail", ""),
                            "input_case": case_in,
                            "raw_output": cr.get("raw"),
                            "parsed_output": obj,
                            "first_failed_at": iso_now(),
                        })

    wall = round(time.time() - t0, 1)
    with res_path.open("w", encoding="utf-8") as f:
        for r in ok_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with fail_path.open("w", encoding="utf-8") as f:
        for r in fail_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    return {
        "ok_n": len(ok_records),
        "fail_n": len(fail_records),
        "calls_n": len(chunks),
        "wall_sec": wall,
        "items_per_sec": round(n_total / max(wall, 1), 3),
        "fail_codes": fail_codes,
        "warn_codes": warn_codes,
        "token_stats": {
            "prompt_p50": sorted(prompt_tokens)[len(prompt_tokens) // 2] if prompt_tokens else None,
            "prompt_max": max(prompt_tokens) if prompt_tokens else None,
            "comp_p50": sorted(comp_tokens)[len(comp_tokens) // 2] if comp_tokens else None,
            "comp_max": max(comp_tokens) if comp_tokens else None,
        },
        "elapsed_p50": sorted(elapsed_list)[len(elapsed_list) // 2] if elapsed_list else None,
        "elapsed_p95": sorted(elapsed_list)[int(len(elapsed_list) * 0.95)] if len(elapsed_list) >= 2 else None,
    }


def load_state(path: pathlib.Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(path: pathlib.Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pattern", required=True)
    ap.add_argument("--batches", required=True)
    ap.add_argument("--bundle", type=int, default=4)
    ap.add_argument("--concurrency", type=int, default=48)
    ap.add_argument("--max-tokens", type=int, default=3072)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--dry-run-n", type=int, default=None)
    ap.add_argument("--year", default=None)
    ap.add_argument("--restart", action="store_true")
    ap.add_argument("--work-root", default="./work")
    args = ap.parse_args()

    work = pathlib.Path(args.work_root)
    results_dir = work / "results"
    fails_dir = work / "failures_for_*"
    state_path = work / "state" / "batch_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {} if args.restart else load_state(state_path)

    pattern_text = pathlib.Path(args.pattern).read_text(encoding="utf-8")
    print(f"[{iso_now()}] offense_date pattern={args.pattern} ({len(pattern_text)} chars)")
    print(f"[{iso_now()}] config: bundle={args.bundle} conc={args.concurrency} max_tokens={args.max_tokens}")

    batches_root = pathlib.Path(args.batches)
    batch_files = (
        sorted(batches_root.glob(f"{args.year}/batch_*.json"))
        if args.year
        else sorted(batches_root.glob("**/batch_*.json"))
    )
    print(f"[{iso_now()}] found {len(batch_files)} batches")

    if args.dry_run_n is not None:
        batch_files = batch_files[:1]
        print(f"[{iso_now()}] DRY RUN MODE: first batch, first {args.dry_run_n} items")

    total_ok = total_fail = total_calls = 0
    all_fail_codes = {}
    t_start = time.time()

    for bf in batch_files:
        if BASE._stop_flag["stop"]:
            print(f"[{iso_now()}] stop requested, breaking loop")
            break
        rel = str(bf.relative_to(batches_root))
        st = state.get(rel, {})
        if st.get("status") == "done" and not args.restart:
            print(f"[{iso_now()}] skip done: {rel}")
            continue

        state[rel] = {"status": "in_progress", "last_attempt_at": iso_now()}
        save_state(state_path, state)
        print(f"[{iso_now()}] === {rel} START ===")

        try:
            result = process_batch(bf, batches_root, results_dir, fails_dir,
                                   pattern_text, args.bundle, args.concurrency,
                                   args.max_tokens, args.timeout, dry_run_n=args.dry_run_n)
        except Exception as exc:
            print(f"[{iso_now()}] batch error: {exc}")
            state[rel] = {"status": "failed", "error": str(exc)[:300], "last_attempt_at": iso_now()}
            save_state(state_path, state)
            continue

        state[rel] = {
            "status": "done",
            "ok_n": result["ok_n"],
            "fail_n": result["fail_n"],
            "calls_n": result["calls_n"],
            "wall_sec": result["wall_sec"],
            "items_per_sec": result["items_per_sec"],
            "fail_codes": result["fail_codes"],
            "warn_codes": result["warn_codes"],
            "token_stats": result["token_stats"],
            "elapsed_p50": result["elapsed_p50"],
            "elapsed_p95": result["elapsed_p95"],
            "last_attempt_at": iso_now(),
        }
        save_state(state_path, state)

        total_ok += result["ok_n"]
        total_fail += result["fail_n"]
        total_calls += result["calls_n"]
        for key, value in result["fail_codes"].items():
            all_fail_codes[key] = all_fail_codes.get(key, 0) + value
        print(f"[{iso_now()}] === {rel} DONE: ok={result['ok_n']} "
              f"fail={result['fail_n']} wall={result['wall_sec']}s "
              f"tp={result['items_per_sec']} items/s ===")

    wall_total = time.time() - t_start
    summary = {
        "started_at": datetime.fromtimestamp(t_start).isoformat(timespec="seconds"),
        "ended_at": iso_now(),
        "wall_sec": round(wall_total, 1),
        "total_ok": total_ok,
        "total_fail": total_fail,
        "total_calls": total_calls,
        "fail_codes": all_fail_codes,
        "config": {
            "bundle": args.bundle,
            "concurrency": args.concurrency,
            "max_tokens": args.max_tokens,
            "pattern": args.pattern,
            "task": "offense_name_v1.0",
            "pairing_key": "id",
        },
    }
    summary_path = work / "state" / f"run_summary_{iso_now().replace(':', '')}.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"[{iso_now()}] FINAL: ok={total_ok} fail={total_fail} wall={wall_total:.0f}s")
    print(f"[{iso_now()}] summary -> {summary_path}")


if __name__ == "__main__":
    main()
