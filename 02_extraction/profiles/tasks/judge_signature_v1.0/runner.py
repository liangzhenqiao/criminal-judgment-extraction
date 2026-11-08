#!/usr/bin/env python3
"""Task runner for judge_signature_v1.0.

This wraps the transport and state machinery of runner/ but replaces the
criminal-defendant verdict with a judge-signature verdict and raw_match_id
pairing. It is launched from the repository root by scripts/run_smoke.sh.
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
    "raw_match_id",
    "id",
    "has_judge_signature",
    "judge_name",
    "judge_role",
    "all_judge_candidates",
    "confidence",
    "failure_reason",
    "evidence_text",
]
ALLOWED_ROLES = {"审判长", "审判员", "代理审判员", "助理审判员", "人民陪审员"}
FORBIDDEN_ROLE_PARTS = ("书记员", "代理书记员", "法官助理")
CONFIDENCE = {"high", "medium", "low"}


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
    return re.sub(r"[\s　]+", "", "" if value is None else str(value))


def is_missing(value) -> bool:
    return value is None or value == ""


def input_source_text(case_in: dict) -> str:
    parts = [
        case_in.get("signature_snippet") or "",
        case_in.get("tail_fallback") or "",
    ]
    for item in case_in.get("regex_candidates") or []:
        if isinstance(item, dict):
            parts.append(item.get("evidence_text") or "")
    return "\n".join(parts)


def text_is_grounded(piece, case_in: dict) -> bool:
    piece_c = compact(piece)
    if not piece_c:
        return False
    return piece_c in compact(input_source_text(case_in))


def role_is_allowed(role) -> bool:
    role_s = "" if role is None else str(role)
    if role_s not in ALLOWED_ROLES:
        return False
    return not any(x in role_s for x in FORBIDDEN_ROLE_PARTS)


def candidate_verdict(candidate: dict, case_in: dict) -> dict:
    if not isinstance(candidate, dict):
        return {"ok": False, "code": "candidate_not_dict"}
    role = candidate.get("role")
    name = candidate.get("name")
    evidence = candidate.get("evidence_text")
    if not role_is_allowed(role):
        return {"ok": False, "code": "candidate_forbidden_or_bad_role"}
    if is_missing(name):
        return {"ok": False, "code": "candidate_missing_name"}
    if is_missing(evidence):
        return {"ok": False, "code": "candidate_missing_evidence"}
    ev_c = compact(evidence)
    if compact(role) not in ev_c or compact(name) not in ev_c:
        return {"ok": False, "code": "candidate_evidence_missing_role_or_name"}
    if not text_is_grounded(evidence, case_in):
        return {"ok": False, "code": "candidate_evidence_not_in_input"}
    return {"ok": True}


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

    if str(obj.get("id")) != str(case_in.get("id")):
        return {
            "ok": False,
            "code": "id_mismatch",
            "detail": f"input={case_in.get('id')} output={obj.get('id')}",
        }
    if str(obj.get("raw_match_id")) != str(case_in.get("raw_match_id")):
        return {
            "ok": False,
            "code": "raw_match_id_mismatch",
            "detail": f"input={case_in.get('raw_match_id')} output={obj.get('raw_match_id')}",
        }

    if not isinstance(obj.get("has_judge_signature"), bool):
        return {"ok": False, "code": "schema_violation:type_mismatch:has_judge_signature"}
    if not isinstance(obj.get("all_judge_candidates"), list):
        return {"ok": False, "code": "schema_violation:type_mismatch:all_judge_candidates"}
    if obj.get("confidence") not in CONFIDENCE:
        return {"ok": False, "code": "schema_violation:bad_confidence"}

    for cand in obj["all_judge_candidates"]:
        cv = candidate_verdict(cand, case_in)
        if not cv["ok"]:
            return cv

    if obj["has_judge_signature"]:
        if is_missing(obj.get("judge_name")):
            return {"ok": False, "code": "positive_missing_judge_name"}
        if not role_is_allowed(obj.get("judge_role")):
            return {"ok": False, "code": "positive_bad_judge_role"}
        if obj.get("failure_reason") is not None:
            return {"ok": False, "code": "positive_failure_reason_not_null"}
        if is_missing(obj.get("evidence_text")):
            return {"ok": False, "code": "positive_missing_evidence"}
        ev_c = compact(obj["evidence_text"])
        if compact(obj["judge_role"]) not in ev_c or compact(obj["judge_name"]) not in ev_c:
            return {"ok": False, "code": "positive_evidence_missing_role_or_name"}
        if not text_is_grounded(obj["evidence_text"], case_in):
            return {"ok": False, "code": "positive_evidence_not_in_input"}
        if not obj["all_judge_candidates"]:
            return {"ok": False, "code": "positive_empty_candidates"}
        primary_in_candidates = any(
            compact(c.get("name")) == compact(obj.get("judge_name"))
            and compact(c.get("role")) == compact(obj.get("judge_role"))
            for c in obj["all_judge_candidates"]
            if isinstance(c, dict)
        )
        if not primary_in_candidates:
            return {"ok": False, "code": "positive_primary_not_in_candidates"}
    else:
        if obj.get("judge_name") is not None:
            return {"ok": False, "code": "negative_judge_name_not_null"}
        if obj.get("judge_role") is not None:
            return {"ok": False, "code": "negative_judge_role_not_null"}
        if obj.get("evidence_text") is not None:
            return {"ok": False, "code": "negative_evidence_not_null"}
        if obj["all_judge_candidates"] != []:
            return {"ok": False, "code": "negative_candidates_not_empty"}
        if is_missing(obj.get("failure_reason")):
            return {"ok": False, "code": "negative_missing_failure_reason"}

    return {"ok": True}


def verdict_call(case_in: dict, call_result: dict) -> list:
    if not call_result.get("ok"):
        return [
            (
                None,
                {
                    "ok": False,
                    "code": call_result.get("code", "unknown"),
                    "detail": call_result.get("err") or (call_result.get("raw") or "")[:200],
                },
            )
        ]

    arr = call_result.get("parsed_arr") or []
    if len(arr) != 1:
        return [
            (
                None,
                {
                    "ok": False,
                    "code": "output_count_mismatch",
                    "detail": f"expected 1 object for raw_match_id={case_in.get('raw_match_id')}, got {len(arr)}",
                },
            )
        ]
    obj = arr[0]
    return [(obj, verdict_record(case_in, obj))]


def select_output_for_case(arr: list, case_in: dict) -> list:
    raw_id = str(case_in.get("raw_match_id", ""))
    by_raw = [o for o in arr if isinstance(o, dict) and str(o.get("raw_match_id", "")) == raw_id]
    if by_raw:
        return by_raw
    case_id = str(case_in.get("id", ""))
    return [o for o in arr if isinstance(o, dict) and str(o.get("id", "")) == case_id]


def process_batch(
    batch_path: pathlib.Path,
    batches_root: pathlib.Path,
    results_dir: pathlib.Path,
    fails_dir: pathlib.Path,
    pattern: str,
    bundle: int,
    concurrency: int,
    max_tokens: int,
    timeout: int,
    dry_run_n: int = None,
) -> dict:
    cases = json.loads(batch_path.read_text())
    if dry_run_n:
        cases = cases[:dry_run_n]
    n_total = len(cases)
    chunks = [cases[i : i + bundle] for i in range(0, n_total, bundle)]

    batch_id = batch_path.stem
    rel = batch_path.relative_to(batches_root)
    sub = rel.parent
    res_path = results_dir / sub / f"{batch_id}_result.jsonl"
    fail_path = fails_dir / sub / f"{batch_id}_failures.jsonl"
    res_path.parent.mkdir(parents=True, exist_ok=True)
    fail_path.parent.mkdir(parents=True, exist_ok=True)

    ok_records = []
    fail_records = []
    prompt_tokens = []
    comp_tokens = []
    elapsed_list = []
    fail_codes = {}

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = {
            ex.submit(BASE.call_litellm, ch, pattern, max_tokens, timeout): ch
            for ch in chunks
        }
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
                    sub_call_result = {
                        "ok": True,
                        "parsed_arr": my_objs,
                        "raw": cr.get("raw", ""),
                    }
                else:
                    sub_call_result = cr

                for obj, verdict in verdict_call(case_in, sub_call_result):
                    if verdict["ok"]:
                        ok_records.append(obj)
                    else:
                        code = verdict["code"]
                        fail_codes[code] = fail_codes.get(code, 0) + 1
                        fail_records.append(
                            {
                                "raw_match_id": case_in.get("raw_match_id"),
                                "id": case_in.get("id"),
                                "failure_code": code,
                                "detail": verdict.get("detail", ""),
                                "input_case": case_in,
                                "raw_output": cr.get("raw"),
                                "parsed_output": obj,
                                "first_failed_at": iso_now(),
                            }
                        )

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
        "token_stats": {
            "prompt_min": min(prompt_tokens) if prompt_tokens else None,
            "prompt_p50": sorted(prompt_tokens)[len(prompt_tokens) // 2] if prompt_tokens else None,
            "prompt_p95": sorted(prompt_tokens)[int(len(prompt_tokens) * 0.95)] if len(prompt_tokens) >= 2 else None,
            "prompt_max": max(prompt_tokens) if prompt_tokens else None,
            "comp_min": min(comp_tokens) if comp_tokens else None,
            "comp_p50": sorted(comp_tokens)[len(comp_tokens) // 2] if comp_tokens else None,
            "comp_p95": sorted(comp_tokens)[int(len(comp_tokens) * 0.95)] if len(comp_tokens) >= 2 else None,
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
    ap.add_argument("--bundle", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--max-tokens", type=int, default=2048)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--dry-run-n", type=int, default=None)
    ap.add_argument("--year", default=None)
    ap.add_argument("--restart", action="store_true")
    ap.add_argument("--work-root", default="work")
    args = ap.parse_args()

    work = pathlib.Path(args.work_root)
    results_dir = work / "results"
    fails_dir = work / "failures"
    state_path = work / "state" / "batch_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {} if args.restart else load_state(state_path)

    pattern_text = pathlib.Path(args.pattern).read_text(encoding="utf-8")
    print(f"[{iso_now()}] judge_signature pattern={args.pattern} ({len(pattern_text)} chars)")
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
            result = process_batch(
                bf,
                batches_root,
                results_dir,
                fails_dir,
                pattern_text,
                args.bundle,
                args.concurrency,
                args.max_tokens,
                args.timeout,
                dry_run_n=args.dry_run_n,
            )
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
        print(
            f"[{iso_now()}] === {rel} DONE: ok={result['ok_n']} "
            f"fail={result['fail_n']} wall={result['wall_sec']}s "
            f"tp={result['items_per_sec']} items/s ==="
        )

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
            "task": "judge_signature_v1.0",
            "pairing_key": "raw_match_id",
        },
    }
    summary_path = work / "state" / f"run_summary_{iso_now().replace(':', '')}.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"[{iso_now()}] FINAL: ok={total_ok} fail={total_fail} wall={wall_total:.0f}s")
    print(f"[{iso_now()}] summary -> {summary_path}")


if __name__ == "__main__":
    main()
