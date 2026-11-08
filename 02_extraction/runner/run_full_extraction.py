#!/usr/bin/env python3
"""run_full_extraction.py — 全量抽取 runner（断点续传 + 失败回流）.

使用：
  python3 run_full_extraction.py \
    --pattern pattern/criminal_defendant_v1.3.md \
    --batches batches/ --bundle 4 --concurrency 16

  # 只跑第一个 batch 的前 N 条
  python3 run_full_extraction.py \
    --pattern pattern/criminal_defendant_v1.3.md \
    --batches batches/ --bundle 1 --concurrency 4 --dry-run-n 200
"""
from __future__ import annotations
import argparse, json, os, pathlib, re, signal, sys, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import requests

# ===== 常量 =====
LITELLM_URL = os.environ.get("LITELLM_URL", "http://127.0.0.1:4000/v1/chat/completions")
MODEL = os.environ.get("LITELLM_MODEL", "legal-extractor-80b")

# ===== Schema：21 keys，含 4 个 prior_* 并行数组（无嵌套、无派生字段）=====
REQUIRED_KEYS_21 = [
    "id",
    # 人口学
    "sex", "birth_year", "ethnicity", "edu_level", "prior_occupation_raw", "hukou_text",
    # 前科 / 累犯
    "has_prior_conviction", "prior_conviction_desc", "is_recidivist_legal",
    # 当案
    "party_role", "sentence_months", "sentence_type", "probation_flag",
    "fine_yuan",  # 与 input regex 字段同名
    "damage_amount_yuan", "crime_location_raw",
    # 4 个并行数组（替代嵌套的 prior_records 和派生字段）
    "prior_years", "prior_offenses", "prior_sentence_months", "prior_released_years",
]
assert len(REQUIRED_KEYS_21) == 21, f"REQUIRED_KEYS_21 has {len(REQUIRED_KEYS_21)} keys"

# LLM 不应输出这 8 个字段（4 个派生字段 + 4 个 carry-over booleans）
# 若 LLM 误输出 → warn + 删除（不算失败）
FORBIDDEN_KEYS = {
    # 派生字段，由后处理计算
    "prior_records", "prior_count", "prior_total_months", "age_at_first_crime",
    # carry-over booleans，合并阶段从 input.regex_extracted 复制
    "has_recidivism_history", "has_self_surrender",
    "has_truthful_confession", "has_plea_leniency",
}
PRIOR_ARRAYS = ("prior_years", "prior_offenses", "prior_sentence_months", "prior_released_years")


# ===== Stop 信号 =====
_stop_flag = {"stop": False}
def _on_term(signum, frame):
    _stop_flag["stop"] = True
    print(f"\n[{iso_now()}] received signal {signum}, finishing current batch then exit", flush=True)
signal.signal(signal.SIGTERM, _on_term)
signal.signal(signal.SIGINT, _on_term)

def iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")

# ===== LiteLLM 调用 =====
def call_litellm(items: list, pattern: str, max_tokens: int, timeout: int) -> dict:
    """单次 chat call。items 是 1..bundle 个 case。返回 {ok, raw, parsed_arr, finish_reason, ...}."""
    t0 = time.time()
    user_content = json.dumps(items if len(items) > 1 else items[0], ensure_ascii=False)
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": pattern},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": os.environ.get("LITELLM_THINKING", "false").lower() == "true"},
    }
    try:
        r = requests.post(LITELLM_URL,
                          headers={"Authorization": "Bearer sk-dummy",
                                   "Content-Type": "application/json"},
                          json=payload, timeout=timeout)
    except requests.exceptions.Timeout:
        return {"ok": False, "code": "timeout", "elapsed": round(time.time()-t0,1)}
    except Exception as e:
        return {"ok": False, "code": "request_error", "err": str(e)[:200], "elapsed": round(time.time()-t0,1)}

    elapsed = round(time.time()-t0, 1)
    if r.status_code != 200:
        body = r.text[:500]
        # http 400 + ContextWindowExceeded → context_overflow
        code = "context_overflow" if "ContextWindow" in body else f"http_{r.status_code}"
        return {"ok": False, "code": code, "http": r.status_code, "raw": body, "elapsed": elapsed}

    try:
        body = r.json()
        choice = body["choices"][0]
        msg = choice["message"]["content"]
        usage = body.get("usage", {}) or {}
    except Exception as e:
        return {"ok": False, "code": "response_parse_error", "err": str(e)[:200], "elapsed": elapsed}

    finish = choice.get("finish_reason")
    out = {
        "elapsed": elapsed,
        "raw": msg,
        "finish_reason": finish,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
    }

    # 截断检测
    if finish == "length":
        out["ok"] = False; out["code"] = "context_overflow"
        return out

    # 解析 content
    content = msg.strip()
    if content.startswith("<think>"):
        m = re.search(r"</think>\s*", content)
        if not m:
            out["ok"] = False; out["code"] = "thinking_unterminated"
            return out
        content = content[m.end():].strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```\s*$", "", content)

    try:
        parsed = json.loads(content)
    except Exception as e:
        out["ok"] = False; out["code"] = "json_parse_error"; out["err"] = str(e)[:200]
        return out

    arr = parsed.get("data") if isinstance(parsed, dict) else parsed
    if not isinstance(arr, list):
        out["ok"] = False; out["code"] = "not_data_array"
        return out

    out["ok"] = True
    out["parsed_arr"] = arr
    return out

# ===== Verdict =====
def is_missing(v) -> bool:
    return v is None or v == ""

def verdict_record(case_in: dict, obj: dict) -> dict:
    """对一条 output obj 做 schema 检查。返回 {ok, code (if fail)}.

    - 4 个 prior_* 数组必须等长
    - 输出禁止字段不算失败，但删除（让 obj 保持 21 keys 干净）
    """
    # F3: id 校验
    obj_id = obj.get("id")
    if not obj_id:
        return {"ok": False, "code": "id_mismatch", "detail": "output_no_id"}
    if str(case_in["id"]) not in str(obj_id):
        return {"ok": False, "code": "id_mismatch", "detail": f"input={case_in['id']} vs output={obj_id}"}

    # F8 (warn-only): 输出禁止字段 → 删除
    extras = FORBIDDEN_KEYS & obj.keys()
    if extras:
        for k in extras:
            obj.pop(k, None)
        # 不算失败，但记一个 warn 标记（可被上层日志拾到）
        obj["__warn_forbidden_removed"] = sorted(extras)

    # F2: schema 缺 key
    missing = [k for k in REQUIRED_KEYS_21 if k not in obj]
    if missing:
        return {"ok": False, "code": f"schema_violation:missing_key:{missing[0]}", "detail": "key_absent"}

    # F2b: 4 个 prior_* 必为 list
    for k in PRIOR_ARRAYS:
        if not isinstance(obj.get(k), list):
            return {"ok": False, "code": f"schema_violation:type_mismatch:{k}",
                    "detail": f"got {type(obj.get(k)).__name__}"}

    # 4 个 prior_* 数组等长
    lens = {k: len(obj[k]) for k in PRIOR_ARRAYS}
    if len(set(lens.values())) != 1:
        return {"ok": False, "code": "consistency_violation:prior_arrays_not_aligned",
                "detail": f"lens={lens}"}

    return {"ok": True}

def verdict_call(case_in: dict, call_result: dict) -> list:
    """对一次 call 的结果做检查，返回 list of (obj_or_None, verdict_dict)."""
    if not call_result.get("ok"):
        # F1/F4/F5/etc — 整 call 失败，输出 1 条 fail 记录（input_case 完整保留）
        return [(None, {"ok": False, "code": call_result.get("code", "unknown"),
                        "detail": call_result.get("err") or call_result.get("raw","")[:200]})]

    arr = call_result["parsed_arr"]

    # F6: 多被告人输出 vs 单被告输入
    seg = case_in.get("defendant_segment", "") or ""
    # 简单检测：|||分隔符 或 多个"被告人"前缀
    sep_count = seg.count("|||")
    bgr_count = seg.count("被告人")
    multi_in = sep_count >= 2 or bgr_count >= 3   # 阈值保守
    if len(arr) > 1 and not multi_in:
        return [(None, {"ok": False, "code": "multi_defendant_mismatch",
                        "detail": f"arr_len={len(arr)} but seg sep={sep_count} bgr={bgr_count}"})]

    # 逐 obj 验证
    results = []
    for i, obj in enumerate(arr):
        if not isinstance(obj, dict):
            results.append((obj, {"ok": False, "code": "obj_not_dict"}))
            continue
        # 多被告时若 obj.id 没加后缀，则补
        if len(arr) > 1 and "id" in obj and str(obj["id"]) == str(case_in["id"]):
            obj["id"] = f"{case_in['id']}_d{i+1}"
        v = verdict_record(case_in, obj)
        results.append((obj, v))
    return results

# ===== 批处理 =====
def process_batch(batch_path: pathlib.Path, batches_root: pathlib.Path,
                  results_dir: pathlib.Path, fails_dir: pathlib.Path,
                  pattern: str, bundle: int, concurrency: int, max_tokens: int, timeout: int,
                  dry_run_n: int = None) -> dict:
    """处理一个 batch 文件，返回 {ok_n, fail_n, wall_sec, token_stats}."""
    cases = json.loads(batch_path.read_text())
    if dry_run_n:
        cases = cases[:dry_run_n]
    n_total = len(cases)
    chunks = [cases[i:i+bundle] for i in range(0, n_total, bundle)]

    batch_id = batch_path.stem
    rel = batch_path.relative_to(batches_root)  # eg residual/y2014_b200/batch_000000.json
    sub = rel.parent
    res_path = results_dir / sub / f"{batch_id}_result.jsonl"
    fail_path = fails_dir / sub / f"{batch_id}_failures.jsonl"
    res_path.parent.mkdir(parents=True, exist_ok=True)
    fail_path.parent.mkdir(parents=True, exist_ok=True)

    ok_records, fail_records = [], []
    prompt_tokens, comp_tokens, elapsed_list = [], [], []
    fail_codes = {}

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = {ex.submit(call_litellm, ch, pattern, max_tokens, timeout): ch for ch in chunks}
        for fut in as_completed(futs):
            chunk_in = futs[fut]
            cr = fut.result()
            if cr.get("prompt_tokens"): prompt_tokens.append(cr["prompt_tokens"])
            if cr.get("completion_tokens"): comp_tokens.append(cr["completion_tokens"])
            if cr.get("elapsed"): elapsed_list.append(cr["elapsed"])

            # 把 chunk 内每条 case 配对 verdict
            # 注意：bundle=1 时 chunk_in 1 条，cr.parsed_arr 也应 1 条
            for case_in in chunk_in:
                if cr.get("ok"):
                    # 找属于这条 input id 的 obj（多 case bundle 时）
                    arr = cr["parsed_arr"]
                    if bundle == 1:
                        sub_call_result = cr   # 1:1
                    else:
                        # 找匹配 id 的子集
                        my_objs = [o for o in arr if isinstance(o, dict) and str(o.get("id","")).startswith(str(case_in["id"]))]
                        sub_call_result = {"ok": True, "parsed_arr": my_objs} if my_objs else \
                            {"ok": False, "code": "id_not_in_arr", "raw": cr.get("raw","")[:300]}
                else:
                    sub_call_result = cr

                verdicts = verdict_call(case_in, sub_call_result)
                for (obj, v) in verdicts:
                    if v["ok"]:
                        ok_records.append(obj)
                    else:
                        fail_codes[v["code"]] = fail_codes.get(v["code"],0)+1
                        fail_records.append({
                            "id": case_in["id"],
                            "failure_code": v["code"],
                            "detail": v.get("detail",""),
                            "input_case": case_in,
                            "raw_output": cr.get("raw"),
                            "parsed_output": obj,
                            "first_failed_at": iso_now()
                        })

    wall = round(time.time()-t0, 1)
    with res_path.open("w", encoding="utf-8") as f:
        for r in ok_records: f.write(json.dumps(r, ensure_ascii=False)+"\n")
    with fail_path.open("w", encoding="utf-8") as f:
        for r in fail_records: f.write(json.dumps(r, ensure_ascii=False)+"\n")

    return {
        "ok_n": len(ok_records),
        "fail_n": len(fail_records),
        "calls_n": len(chunks),
        "wall_sec": wall,
        "items_per_sec": round(n_total / max(wall,1), 3),
        "fail_codes": fail_codes,
        "token_stats": {
            "prompt_min": min(prompt_tokens) if prompt_tokens else None,
            "prompt_p50": sorted(prompt_tokens)[len(prompt_tokens)//2] if prompt_tokens else None,
            "prompt_p95": sorted(prompt_tokens)[int(len(prompt_tokens)*0.95)] if len(prompt_tokens)>=2 else None,
            "prompt_max": max(prompt_tokens) if prompt_tokens else None,
            "comp_min": min(comp_tokens) if comp_tokens else None,
            "comp_p50": sorted(comp_tokens)[len(comp_tokens)//2] if comp_tokens else None,
            "comp_p95": sorted(comp_tokens)[int(len(comp_tokens)*0.95)] if len(comp_tokens)>=2 else None,
            "comp_max": max(comp_tokens) if comp_tokens else None,
        },
        "elapsed_p50": sorted(elapsed_list)[len(elapsed_list)//2] if elapsed_list else None,
        "elapsed_p95": sorted(elapsed_list)[int(len(elapsed_list)*0.95)] if len(elapsed_list)>=2 else None,
    }

# ===== state =====
def load_state(state_path: pathlib.Path) -> dict:
    if state_path.exists():
        return json.loads(state_path.read_text())
    return {}

def save_state(state_path: pathlib.Path, state: dict) -> None:
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(state_path)

# ===== main =====
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pattern", required=True)
    ap.add_argument("--batches", required=True, help="batches root dir")
    ap.add_argument("--bundle", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--max-tokens", type=int, default=2048)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--dry-run-n", type=int, default=None, help="if set, only process first N items of first batch then stop")
    ap.add_argument("--year", default=None, help="filter year subdir, eg 'y2014_b200'")
    ap.add_argument("--restart", action="store_true", help="ignore state, restart all")
    ap.add_argument("--work-root", default="./work")
    args = ap.parse_args()

    work = pathlib.Path(args.work_root)
    results_dir = work / "results"
    fails_dir = work / "failures_for_*"
    state_path = work / "state" / "batch_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {} if args.restart else load_state(state_path)

    pattern_text = pathlib.Path(args.pattern).read_text()
    print(f"[{iso_now()}] pattern={args.pattern} ({len(pattern_text)} chars)")
    print(f"[{iso_now()}] config: bundle={args.bundle} conc={args.concurrency} max_tokens={args.max_tokens}")

    # 找 batches
    batches_root = pathlib.Path(args.batches)
    if args.year:
        batch_files = sorted(batches_root.glob(f"{args.year}/batch_*.json"))
    else:
        batch_files = sorted(batches_root.glob("**/batch_*.json"))
    print(f"[{iso_now()}] found {len(batch_files)} batches")

    if args.dry_run_n is not None:
        batch_files = batch_files[:1]
        print(f"[{iso_now()}] DRY RUN MODE: process only first batch, first {args.dry_run_n} items")

    # 总计
    total_ok = total_fail = total_calls = 0
    all_prompt = []; all_comp = []; all_fail_codes = {}
    t_start = time.time()

    for bf in batch_files:
        if _stop_flag["stop"]:
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
            r = process_batch(bf, batches_root, results_dir, fails_dir, pattern_text,
                              args.bundle, args.concurrency, args.max_tokens, args.timeout,
                              dry_run_n=args.dry_run_n)
        except Exception as e:
            print(f"[{iso_now()}] batch error: {e}")
            state[rel] = {"status": "failed", "error": str(e)[:300], "last_attempt_at": iso_now()}
            save_state(state_path, state)
            continue

        state[rel] = {
            "status": "done",
            "ok_n": r["ok_n"], "fail_n": r["fail_n"], "calls_n": r["calls_n"],
            "wall_sec": r["wall_sec"], "items_per_sec": r["items_per_sec"],
            "fail_codes": r["fail_codes"], "token_stats": r["token_stats"],
            "elapsed_p50": r["elapsed_p50"], "elapsed_p95": r["elapsed_p95"],
            "last_attempt_at": iso_now(),
        }
        save_state(state_path, state)

        total_ok += r["ok_n"]; total_fail += r["fail_n"]; total_calls += r["calls_n"]
        if r["token_stats"]["prompt_p50"]: all_prompt.append(r["token_stats"]["prompt_p50"])
        if r["token_stats"]["comp_p50"]: all_comp.append(r["token_stats"]["comp_p50"])
        for k, v in r["fail_codes"].items():
            all_fail_codes[k] = all_fail_codes.get(k, 0) + v

        print(f"[{iso_now()}] === {rel} DONE: ok={r['ok_n']} fail={r['fail_n']} "
              f"wall={r['wall_sec']}s tp={r['items_per_sec']} items/s ===")

    wall_total = time.time() - t_start
    summary = {
        "started_at": datetime.fromtimestamp(t_start).isoformat(timespec="seconds"),
        "ended_at": iso_now(),
        "wall_sec": round(wall_total, 1),
        "total_ok": total_ok, "total_fail": total_fail, "total_calls": total_calls,
        "fail_codes": all_fail_codes,
        "config": {"bundle": args.bundle, "concurrency": args.concurrency,
                   "max_tokens": args.max_tokens, "pattern": args.pattern},
    }
    summary_path = work / "state" / f"run_summary_{iso_now().replace(':','')}.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"[{iso_now()}] FINAL: ok={total_ok} fail={total_fail} wall={wall_total:.0f}s")
    print(f"[{iso_now()}] summary → {summary_path}")

if __name__ == "__main__":
    main()
