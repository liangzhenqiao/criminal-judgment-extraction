#!/usr/bin/env python3
"""Grounding / quality checks for offense_name_v1.0 runs.
读 batch_*.json + *_result.jsonl，复核 id 保真/dup/grounded/name链接/failure 占比。
输出扁平 JSON（acceptance.yaml 的 grounding_metrics 消费）。"""
from __future__ import annotations
import argparse, glob, json, pathlib, re

def compact(v):
    return re.sub(r"[\s\u3000]+", "", "" if v is None else str(v))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases-dir", required=True)
    ap.add_argument("--results-dir", required=True)
    ap.add_argument("--schema", required=False)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    cases = {}
    for f in glob.glob(str(pathlib.Path(a.cases_dir) / "**" / "batch_*.json"), recursive=True):
        for c in json.load(open(f, encoding="utf-8")):
            cases[str(c["id"])] = c
    res, dup = {}, 0
    for f in glob.glob(str(pathlib.Path(a.results_dir) / "**" / "*_result.jsonl"), recursive=True):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if not line: continue
            d = json.loads(line)
            rid = str(d.get("id"))
            if rid in res: dup += 1
            res[rid] = d
    missing_input = sum(1 for r in res if r not in cases)
    succ = {i: d for i, d in res.items() if d.get("failure_reason") is None}
    nul = len(res) - len(succ)
    grounded = viol = name_viol = 0
    for i, d in succ.items():
        c = cases.get(i, {})
        src = compact(str(c.get("case_title") or "") + str(c.get("judgment_segment") or "")
                      + str(c.get("facts_segment") or ""))
        raws = d.get("all_offense_names_raw") or []
        ok = bool(raws) and all(compact(e) and compact(e) in src for e in raws)
        if ok: grounded += 1
        else: viol += 1
        nm = compact(d.get("offense_name"))
        if not any(v in compact(e) for e in raws for v in (nm, nm + "罪")):
            name_viol += 1
    out = {
        "task": "offense_name_v1.0",
        "cases_total": len(cases), "result_rows": len(res),
        "missing_input_for_result": missing_input,
        "duplicate_id_results": dup,
        "id_preserved_pct": 100.0 if missing_input == 0 else round((len(res)-missing_input)/max(len(res),1)*100, 3),
        "success_rows": len(succ), "failure_rows": nul,
        "failure_reason_nonnull_pct": round(nul / max(len(res), 1) * 100, 2),
        "grounded_pct": round(grounded / max(len(succ), 1) * 100, 3),
        "grounding_violation_count": viol,
        "name_not_in_raw_count": name_viol,
    }
    pathlib.Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps(out, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
