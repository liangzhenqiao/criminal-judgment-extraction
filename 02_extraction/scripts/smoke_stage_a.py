#!/usr/bin/env python3
"""Gate 4 — Smoke Stage A: 1-case sanity。Gate 4。

复用一条 call_once 逻辑（LiteLLM 直连 + <think> 剥离 + JSON 解析），
对 1 个 case 发请求，对照 task pattern 的 21-key schema 检查。输出 sanity.json。

用法:
	  smoke_stage_a.py --pattern <pattern.md> --case <sample_case.json>
	                   [--schema <schema.json>]
	                   [--litellm-url http://127.0.0.1:4000/v1/chat/completions]
	                   [--model legal-extractor-80b] [--out <sanity.json>]
	检查（精确）:
	  HTTP200 + JSON 可解析 + schema.required_keys 齐全 + 无 forbidden_keys
	  + id 一致 + array_fields 是 list 且等长
退出: 0=PASS  1=门槛未过
依赖: requests（runner 同款）
"""
import argparse, json, re, sys, time
try:
    import requests
except ImportError:
    sys.stderr.write("需要 requests（与 runner 同款依赖）\n"); sys.exit(1)


def call_once(url, model, pattern, case, timeout=300):
    """一次调用：temperature=0，剥 <think>，清 markdown fence。"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": pattern},
            {"role": "user", "content": json.dumps(case, ensure_ascii=False)},
        ],
        "temperature": 0,
    }
    t0 = time.time()
    r = requests.post(url, headers={"Authorization": "Bearer sk-dummy",
                                    "Content-Type": "application/json"},
                      json=payload, timeout=timeout)
    elapsed = round(time.time() - t0, 2)
    out = {"http": r.status_code, "elapsed_sec": elapsed}
    if r.status_code != 200:
        out["ok"] = False; out["err"] = f"http {r.status_code}: {r.text[:200]}"
        return out
    body = r.json()
    msg = body["choices"][0]["message"]["content"]
    usage = body.get("usage", {}) or {}
    out["prompt_tokens"] = usage.get("prompt_tokens")
    out["completion_tokens"] = usage.get("completion_tokens")
    out["finish_reason"] = body["choices"][0].get("finish_reason")
    msg = re.sub(r"<think>[\s\S]*?</think>\s*", "", msg, flags=re.DOTALL)
    content = msg.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```\s*$", "", content)
    out["raw_head"] = content[:200]
    try:
        parsed = json.loads(content)
        arr = parsed.get("data") if isinstance(parsed, dict) else parsed
        out["ok"] = isinstance(arr, list) and len(arr) >= 1
        out["parsed"] = arr[0] if out["ok"] else None
    except Exception as e:
        out["ok"] = False; out["err"] = f"json parse: {e}"
    return out


# 与 runner/run_full_extraction.py 的 REQUIRED_KEYS_21 相同（带 assert len==21）。
# 与 runner verdict 同一字段集——避免 Stage A 和 runner 判定口径不一致。
REQUIRED_KEYS_21 = [
    "id",
    "sex", "birth_year", "ethnicity", "edu_level", "prior_occupation_raw", "hukou_text",
    "has_prior_conviction", "prior_conviction_desc", "is_recidivist_legal",
    "party_role", "sentence_months", "sentence_type", "probation_flag", "fine_yuan",
    "damage_amount_yuan", "crime_location_raw",
    "prior_years", "prior_offenses", "prior_sentence_months", "prior_released_years",
]
# runner FORBIDDEN_KEYS：派生字段 + 4 carry-over booleans，LLM 不应输出
FORBIDDEN_KEYS = {
    "prior_records", "prior_count", "prior_total_months", "age_at_first_crime",
    "has_recidivism_history", "has_self_surrender",
    "has_truthful_confession", "has_plea_leniency",
}
PRIOR = ["prior_years", "prior_offenses", "prior_sentence_months", "prior_released_years"]


def default_schema():
    return {
        "required_keys": REQUIRED_KEYS_21,
        "forbidden_keys": sorted(FORBIDDEN_KEYS),
        "id_key": "id",
        "id_match_mode": "exact",
        "array_fields": PRIOR,
        "exact_key_count": True,
    }


def load_schema(path):
    if not path:
        return default_schema()
    schema = json.load(open(path, encoding="utf-8"))
    if "required_keys" not in schema:
        raise SystemExit(f"schema 缺 required_keys: {path}")
    schema.setdefault("forbidden_keys", [])
    schema.setdefault("id_key", "id")
    schema.setdefault("id_match_mode", "exact")
    schema.setdefault("array_fields", [])
    schema.setdefault("exact_key_count", True)
    return schema


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pattern", required=True)
    ap.add_argument("--case", required=True, help="单 case JSON（数组取第一条，或单对象）")
    ap.add_argument("--schema", help="task schema JSON（required_keys/forbidden_keys/array_fields/id_key）")
    ap.add_argument("--litellm-url", default="http://127.0.0.1:4000/v1/chat/completions")
    ap.add_argument("--model", default="legal-extractor-80b")
    ap.add_argument("--out", default="sanity.json")
    ap.add_argument("--timeout", type=int, default=300)
    a = ap.parse_args()

    pattern = open(a.pattern, encoding="utf-8").read()
    schema = load_schema(a.schema)
    required_keys = list(schema.get("required_keys") or [])
    forbidden_keys = set(schema.get("forbidden_keys") or [])
    array_fields = list(schema.get("array_fields") or [])
    id_key = schema.get("id_key") or "id"
    id_match_mode = schema.get("id_match_mode") or "exact"
    exact_key_count = bool(schema.get("exact_key_count", True))
    raw = json.load(open(a.case, encoding="utf-8"))
    case = raw[0] if isinstance(raw, list) else raw

    res = call_once(a.litellm_url, a.model, pattern, case, a.timeout)

    checks, fails, detail = {}, [], {}
    checks["http_200"] = res.get("http") == 200
    checks["json_valid"] = bool(res.get("ok"))
    obj = res.get("parsed") if isinstance(res.get("parsed"), dict) else None

    if obj is not None:
        keys = set(obj.keys())
        req = set(required_keys)
        missing_keys = sorted(req - keys)
        forbidden_present = sorted(forbidden_keys & keys)
        extra_keys = sorted(keys - req - forbidden_keys)
        # 精确字段集：必须含全部必填，且不得含禁止字段
        checks["required_keys_complete"] = (len(missing_keys) == 0)
        checks["no_forbidden_keys"] = (len(forbidden_present) == 0)
        checks["key_count_eq_required"] = (len(obj) == len(required_keys)) if exact_key_count else True
        expected_id = str(case.get(id_key))
        actual_id = str(obj.get(id_key, ""))
        if id_match_mode == "contains":
            checks["id_match"] = expected_id in actual_id
        else:
            checks["id_match"] = actual_id == expected_id
        # 任务声明的数组字段必须是 list 且等长
        array_types_ok = all(isinstance(obj.get(k), list) for k in array_fields)
        checks["array_fields_are_lists"] = array_types_ok
        if array_types_ok and array_fields:
            lens = {k: len(obj[k]) for k in array_fields}
            checks["array_fields_aligned"] = (len(set(lens.values())) == 1)
            detail["array_lens"] = lens
        elif array_fields:
            checks["array_fields_aligned"] = False
        else:
            checks["array_fields_aligned"] = True
        detail["missing_keys"] = missing_keys
        detail["forbidden_present"] = forbidden_present
        detail["extra_keys"] = extra_keys
        detail["id_match_mode"] = id_match_mode
        detail["expected_id"] = expected_id
        detail["actual_id"] = actual_id
        detail["schema_path"] = a.schema or "<builtin:criminal_defendant_v1.3>"
    else:
        for k in ("required_keys_complete", "no_forbidden_keys", "key_count_eq_required",
                  "id_match", "array_fields_are_lists", "array_fields_aligned"):
            checks[k] = False

    for k, v in checks.items():
        if not v: fails.append(k)

    verdict = "PASS" if not fails else "FAIL"
    report = {
        "stage": "A_sanity", "verdict": verdict, "fails": fails, "checks": checks,
        "detail": detail,
        "http": res.get("http"), "elapsed_sec": res.get("elapsed_sec"),
        "prompt_tokens": res.get("prompt_tokens"), "completion_tokens": res.get("completion_tokens"),
        "finish_reason": res.get("finish_reason"),
        "key_count_actual": len(obj) if obj else 0,
        "raw_head": res.get("raw_head"), "err": res.get("err"),
    }
    open(a.out, "w", encoding="utf-8").write(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"verdict": verdict, "fails": fails,
                      "elapsed_sec": res.get("elapsed_sec"),
                      "key_count": report["key_count_actual"]}, ensure_ascii=False))
    sys.exit(0 if verdict == "PASS" else 1)


if __name__ == "__main__":
    main()
