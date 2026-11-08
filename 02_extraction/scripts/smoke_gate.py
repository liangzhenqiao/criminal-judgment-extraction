#!/usr/bin/env python3
"""Gate 7 — Smoke 总验收。Gate 7。

读 Stage A sanity.json + runner batch_state.json + grounding json + task acceptance.yaml，
把实测值与门槛对比 → smoke_verdict.json。

⚠️ 防假 GREEN 的核心契约:
  GREEN 必须同时满足三项「覆盖 + 达标」:
    1. Stage A: sanity.json 存在且 verdict==PASS
    2. Stage C: batch_state.json 存在、batch 数 == expected_batches、
       batch 全部 done、且所有 stage_c 门槛达标
    3. Grounding: grounding json 存在且三项指标（hallucination/arrays_aligned/grounding）齐全且达标
  任一证据缺失 → verdict = COVERAGE_INCOMPLETE，exit 2（不是 GREEN，也不静默 skip）。
  任一门槛不达标 → verdict = ALARM，exit 1。
  仅当三项全覆盖且全达标 → GREEN，exit 0。

  可用 --no-require-stage-c / --no-require-grounding 显式声明本次只验到某一层，
  但此时 verdict 最多是 PARTIAL_PASS（exit 0 但绝不叫 GREEN），且产物显式标注未覆盖项。

用法:
  smoke_gate.py --acceptance <acceptance.yaml>
                --sanity <sanity.json>
                --state <batch_state.json> [--expected-batches 50]
                --grounding-json <grounding_5k.json>
                [--no-require-stage-c] [--no-require-grounding]
                [--out <smoke_verdict.json>]
退出: 0=GREEN 或 PARTIAL_PASS  1=ALARM  2=COVERAGE_INCOMPLETE/前置缺失
"""
import argparse, json, statistics, sys, os


def read_yaml(path):
    try:
        import yaml
    except ImportError as e:
        raise SystemExit(
            "PyYAML is required for acceptance.yaml; install with `pip install PyYAML` "
            "or run Gate0 doctor to catch this before Gate7."
        ) from e
    try:
        return yaml.safe_load(open(path, encoding="utf-8")) or {}
    except Exception as e:
        raise SystemExit(f"acceptance.yaml parse failed: {path}: {e}") from e


NETWORK_CODES = ("timeout", "request_error")


DEFAULT_GROUNDING_METRICS = [
    {"name": "hallucination", "path": "no_hpc_arr_filled_violation",
     "op": "<=", "threshold_key": "hallucination_max", "default": 0},
    {"name": "arrays_aligned_pct", "path": "arrays_aligned_pct",
     "op": ">=", "threshold_key": "arrays_aligned_pct_min", "default": 99.0},
    {"name": "prior_offenses_grounding_pct", "path": "prior_offenses.grounded_with_split_pct",
     "op": ">=", "threshold_key": "prior_offenses_grounding_pct_min", "default": 95.0},
]


def dotted_get(obj, path):
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def compare(actual, op, threshold):
    if actual is None:
        return False
    if op in ("<=", "le"):
        return actual <= threshold
    if op in (">=", "ge"):
        return actual >= threshold
    if op in ("==", "eq"):
        return actual == threshold
    if op in ("<", "lt"):
        return actual < threshold
    if op in (">", "gt"):
        return actual > threshold
    raise ValueError(f"unsupported op: {op}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--acceptance", required=True)
    ap.add_argument("--sanity", help="Stage A sanity.json")
    ap.add_argument("--state", help="runner batch_state.json（Stage C）")
    ap.add_argument("--grounding-json", help="grounding_compare 输出（含 hallucination/aligned/grounding）")
    ap.add_argument("--expected-batches", type=int, default=None,
                    help="Stage C 期望 batch 数（缺省取 acceptance.stage_c.expected_batches 或 50）")
    ap.add_argument("--no-require-stage-c", action="store_true",
                    help="显式声明本次不验 Stage C（结果最多 PARTIAL_PASS，绝不 GREEN）")
    ap.add_argument("--no-require-grounding", action="store_true",
                    help="显式声明本次不验 grounding（结果最多 PARTIAL_PASS，绝不 GREEN）")
    ap.add_argument("--out", default="smoke_verdict.json")
    a = ap.parse_args()

    acc = read_yaml(a.acceptance)
    sc = acc.get("stage_c", {})
    exp_batches = (a.expected_batches if a.expected_batches is not None
                   else sc.get("expected_batches", 50))

    gates, alarms, missing = [], [], []

    def check(name, ok, actual, thr):
        gates.append({"gate": name, "actual": actual, "threshold": thr, "pass": bool(ok)})
        if not ok:
            alarms.append(name)

    # ========== Stage A（始终必需）==========
    if not a.sanity or not os.path.isfile(a.sanity):
        missing.append("stage_a:sanity.json 缺失（Stage A 是 GREEN 的必要证据）")
    else:
        s = json.load(open(a.sanity, encoding="utf-8"))
        check("stage_a.verdict", s.get("verdict") == "PASS", s.get("verdict"), "PASS")

    # ========== Stage C ==========
    require_c = not a.no_require_stage_c
    if not a.state or not os.path.isfile(a.state):
        if require_c:
            missing.append("stage_c:batch_state.json 缺失（Stage C 是 GREEN 的必要证据；"
                           "若本次确实只验到 Stage A，请显式 --no-require-stage-c → PARTIAL_PASS）")
    else:
        st = json.load(open(a.state, encoding="utf-8"))
        batches = {k: v for k, v in st.items() if isinstance(v, dict) and "status" in v}
        done = [v for v in batches.values() if v.get("status") == "done"]
        bad = {k: v.get("status") for k, v in batches.items()
               if v.get("status") != "done"}
        # 完成性门槛（防"部分 done / unknown status 也 GREEN"）
        check("stage_c.batch_count_eq_expected", len(batches) == exp_batches,
              f"batches={len(batches)}", f"== {exp_batches}")
        check("stage_c.all_batches_done", len(bad) == 0,
              f"{len(bad)} non-done ({dict(list(bad.items())[:5])})", "all status == done")
        check("stage_c.done_eq_expected", len(done) == exp_batches,
              f"done={len(done)}", f"== {exp_batches}")
        if done:
            tps = [v["items_per_sec"] for v in done if v.get("items_per_sec")]
            oks = sum(v.get("ok_n", 0) for v in done)
            fails = sum(v.get("fail_n", 0) for v in done)
            total = oks + fails
            fail_rate = fails / total if total else 1.0
            tp_mean = statistics.mean(tps) if tps else 0.0
            n3 = max(len(tps) // 3, 1)
            drift = 0.0
            if len(tps) >= 3:
                f3, l3 = statistics.mean(tps[:n3]), statistics.mean(tps[-n3:])
                drift = abs((l3 - f3) / f3 * 100) if f3 else 0.0
            fc = {}
            for v in done:
                for k, n in (v.get("fail_codes") or {}).items():
                    fc[k] = fc.get(k, 0) + n
            overflow = sum(n for k, n in fc.items() if "context_overflow" in k)
            overflow_rate = overflow / total if total else 0.0
            net = sum(n for k, n in fc.items()
                      if k in NETWORK_CODES or k.startswith("http_5"))
            check("throughput_it_s", tp_mean >= sc.get("throughput_it_s_min", 5.0),
                  round(tp_mean, 3), f">= {sc.get('throughput_it_s_min', 5.0)}")
            check("fail_rate", fail_rate <= sc.get("fail_rate_max", 0.01),
                  round(fail_rate, 4), f"<= {sc.get('fail_rate_max', 0.01)}")
            check("context_overflow_rate", overflow_rate <= sc.get("context_overflow_rate_max", 0.001),
                  round(overflow_rate, 5), f"<= {sc.get('context_overflow_rate_max', 0.001)}")
            check("throughput_drift_pct", drift <= sc.get("throughput_drift_pct_max", 10.0),
                  round(drift, 2), f"<= {sc.get('throughput_drift_pct_max', 10.0)}")
            check("network_hw_fail", net <= sc.get("network_hw_fail_max", 0),
                  net, f"<= {sc.get('network_hw_fail_max', 0)}")
        else:
            # state 存在但无 done → 视为未完成（不是静默 skip）
            check("stage_c.has_done_batch", False, "0 done", ">= 1")

    # ========== Grounding（默认使用 criminal_defendant_v1.3 三项；也可由 acceptance.grounding.metrics 覆盖）==========
    require_g = not a.no_require_grounding
    if not a.grounding_json or not os.path.isfile(a.grounding_json):
        if require_g:
            missing.append("grounding:grounding json 缺失（hallucination/arrays_aligned/grounding 是 GREEN 的必要证据；"
                           "用 $TASK/grounding.py 产出；无任务级 hook 时才回退 scripts/grounding_compare.py；"
                           "若确实不验，请显式 --no-require-grounding → PARTIAL_PASS）")
    else:
        g = json.load(open(a.grounding_json, encoding="utf-8"))
        gv = g.get("v1") or g.get("baseline") or g
        grounding_cfg = acc.get("grounding") or {}
        raw_metrics = grounding_cfg.get("metrics")
        if "metrics" in grounding_cfg and not isinstance(raw_metrics, list):
            check("grounding.metrics_config", False, type(raw_metrics).__name__, "list")
            metrics = []
        else:
            metrics = raw_metrics if isinstance(raw_metrics, list) else DEFAULT_GROUNDING_METRICS
        for m in metrics:
            if not isinstance(m, dict) or not m.get("name") or not m.get("path"):
                check("grounding.metrics_config", False, repr(m)[:120], "items with name/path")
                continue
            name = m["name"]
            path = m["path"]
            op = m.get("op", ">=")
            if "threshold" in m:
                threshold = m["threshold"]
            else:
                threshold = sc.get(m.get("threshold_key", ""), m.get("default"))
            actual = dotted_get(gv, path)
            if actual is None:
                check(f"grounding.{name}.present", False, f"缺字段: {path}", "字段存在")
            elif threshold is None:
                check(f"grounding.{name}.threshold", False, "缺 threshold/default", "threshold present")
            else:
                try:
                    ok = compare(actual, op, threshold)
                except Exception as e:
                    check(f"grounding.{name}.op", False, str(e), "supported op")
                else:
                    check(name, ok, actual, f"{op} {threshold}")

    # ========== 裁决 ==========
    partial = a.no_require_stage_c or a.no_require_grounding
    if missing:
        verdict = "COVERAGE_INCOMPLETE"; rc = 2
    elif alarms:
        verdict = "ALARM"; rc = 1
    elif partial:
        verdict = "PARTIAL_PASS"; rc = 0   # 显式降级：通过但未全覆盖，绝不叫 GREEN
    else:
        verdict = "GREEN"; rc = 0

    coverage = {
        "stage_a": bool(a.sanity and os.path.isfile(a.sanity)),
        "stage_c": bool(a.state and os.path.isfile(a.state)) if require_c else "waived",
        "grounding": bool(a.grounding_json and os.path.isfile(a.grounding_json)) if require_g else "waived",
        "expected_batches": exp_batches,
    }
    out = {"verdict": verdict, "alarms": alarms, "missing_evidence": missing,
           "coverage": coverage, "gates": gates, "task": acc.get("task")}
    open(a.out, "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps({"verdict": verdict, "alarms": alarms,
                      "missing_evidence_count": len(missing)}, ensure_ascii=False))
    sys.exit(rc)


if __name__ == "__main__":
    main()
