#!/usr/bin/env python3
"""13_build_offense_name_profile · 生成 offense_name_v1.0 task-profile + LLM 残差批

产出 data_cleaning/raw/offense_name_gpu_push_<TS>/
  task_profiles/offense_name_v1.0/{pattern.md, schema.json, acceptance.yaml,
                                   sample_case.json, verdict.spec.md, runner.py, grounding.py}
  offense_name_llm_batches/batch_%06d.json   (200 case/批, {id,case_title,judgment_segment,facts_segment})
  MANIFEST.json + SHA256SUMS.txt
runner.py = offense_date_v1.0 runner 的 fork（同族 wire 协议），只改任务常量 + verdict + grounding 源。
"""
import hashlib
import json
import sys
from pathlib import Path

TS = "20260709115253"
__import__("sys").path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_config  # paths come from config.yaml (see config.example.yaml)
CFG = pipeline_config.load(handle_help=True, keys=('work_dir',))
ROOT = CFG.path("work_dir")  # intermediates live under <work_dir>/data_cleaning/{raw,master}
# runner.py of the offense-date task profile is forked for the offense-name task
SRC_RUNNER = (Path(__file__).resolve().parents[1]
              / "02_extraction/profiles/tasks/offense_date_v1.0/runner.py")
RES_POOL = ROOT / f"data_cleaning/raw/offense_name_residual_{TS}/residual_pool.jsonl"
OUT = ROOT / f"data_cleaning/raw/offense_name_gpu_push_{TS}"
PROF = OUT / "task_profiles/offense_name_v1.0"
BATCHES = OUT / "offense_name_llm_batches"

PATTERN = """# 身份与目的

你是司法文书罪名抽取 AI。输入是一批中国刑事裁判文书的**标题 + 判决主文段 + 事实段**（已切段），
这些案例都是**结构化案由字段缺失、且词典正则无法确定本案罪名**的难例（标题不含标准罪名后缀、
OCR 噪声、罕见/新罪名、程序性文书混入）。你的任务：抽取**本案（当前案件）被告被判/被指控的罪名**，
逐字取自原文，绝不猜测。

# 输入

JSON 数组，每元素：`{"id": "<案号键>", "case_title": "<标题>", "judgment_segment": "<判决主文段>", "facts_segment": "<事实段>"}`
（三个文本字段任何一个都可能为空串。）

# 输出契约（每案一行，5 键，顺序固定）

```
{"id": "<原样保留>", "offense_name": "<本案罪名，不带'罪'字尾>"|null, "all_offense_names_raw": ["<原文罪名表述子串>", ...], "confidence": "high"|"medium"|"low", "failure_reason": null|"no_offense_name_in_text"|"text_garbled_or_empty"}
```

# 核心规则

1. **id 原样保留**，一字节不改。
2. **只认本案罪名**。以下**绝不是**本案罪名：前科/犯罪史段落里的罪名（"曾因犯盗窃罪被判…"
   "因抢劫罪刑满释放"）；同案其他文书引用的罪名；辩护意见中被否定的罪名指控。判定优先级：
   判决主文（"犯X罪，判处…"" 以X罪判处…"）＞ 标题（"…X罪一审刑事判决书"）＞ 事实段指控
   （"指控…犯X罪"）。
3. **逐字 grounding（硬）**：`all_offense_names_raw` 的每个元素必须是输入三个文本字段之一的
   **逐字原文子串**（如"危险驾驶罪""偷越国（边）境"）；`offense_name` 必须能在某个 raw 元素中
   找到（允许去掉"罪"字尾，如 raw="盗窃罪" → offense_name="盗窃"）。
4. **多罪名（数罪并罚）**：raw 里把判决主文出现的所有本案罪名都列上；`offense_name` 取判决主文
   第一项（首要罪名）。
5. **无"罪"字尾的罪名**（如"偷越国（边）境"）：照抽，raw 放原文表述。
6. **宁缺毋滥**：确实无本案罪名（如撤诉/撤回自诉裁定、纯程序文书）→ offense_name=null +
   failure_reason="no_offense_name_in_text"；文本乱码/空 → "text_garbled_or_empty"。
   **不用世界知识、不从案情叙述推断罪名。**
7. confidence：判决主文明确 = high；仅标题/指控 = medium；仅间接表述 = low。

# 示例

输入：`{"id": "X1", "case_title": "欧阳某某偷越国（边）境一审刑事判决书", "judgment_segment": "被告人欧阳某某犯偷越国（边）境罪，判处拘役五个月。", "facts_segment": "…"}`
输出：`{"id": "X1", "offense_name": "偷越国（边）境", "all_offense_names_raw": ["偷越国（边）境罪"], "confidence": "high", "failure_reason": null}`

输入：`{"id": "X2", "case_title": "（2016）349号准许高某撤回自诉裁定书", "judgment_segment": "准许自诉人高某撤回自诉。", "facts_segment": ""}`
输出：`{"id": "X2", "offense_name": null, "all_offense_names_raw": [], "confidence": "low", "failure_reason": "no_offense_name_in_text"}`

输入：`{"id": "X3", "case_title": "李某判决书", "judgment_segment": "被告人李某犯盗窃罪、故意毁坏财物罪，数罪并罚…", "facts_segment": "李某曾因犯抢劫罪于2010年被判刑。2015年…"}`
输出：`{"id": "X3", "offense_name": "盗窃", "all_offense_names_raw": ["盗窃罪", "故意毁坏财物罪"], "confidence": "high", "failure_reason": null}`
（注意：事实段里的"抢劫罪"是前科，不列入。）

# 禁止

- 输出契约外任何键 / 任何解释文字
- 修改 id；把前科罪名当本案罪名；编造原文没有的罪名表述；raw 元素不是原文子串

---

# 传输格式（批处理 wire 协议 —— 与 offense_date_v1.0 同族，runner 适配层）

一次处理一批（N=bundle=4）。输出**顶层唯一键 `data`**（数组，与输入同序同长），纯 JSON 无 markdown。

```json
{"data": [ {"id": "...", "offense_name": ..., "all_offense_names_raw": [...], "confidence": "...", "failure_reason": ...} ]}
```

- 每元素恰好 5 键，顺序固定：id, offense_name, all_offense_names_raw, confidence, failure_reason。
- id 逐字复制；数组与输入严格对齐；可为 null 的键输出 JSON null。
"""

SCHEMA = {
    "required": ["id", "offense_name", "all_offense_names_raw", "confidence", "failure_reason"],
    "forbidden": [],
    "array_fields": ["all_offense_names_raw"],
    "id_field": "id",
}

ACCEPTANCE = """# offense_name_v1.0 · 验收门槛（门槛即代码；smoke_gate/grounding 消费）
task: offense_name_v1.0
thresholds:
  throughput_it_s: ">= 0.3"
  fail_rate: "<= 0.05"
  context_overflow_rate: "<= 0.0"     # 输入 <=2000 字, 物理上不应 overflow; 沿家族硬零纪律
  throughput_drift_pct: "<= 25.0"
  network_hw_fail: "<= 0"
grounding_metrics:
  id_preserved_pct: ">= 100.0"
  duplicate_id_results: "<= 0"
  missing_input_for_result: "<= 0"
  grounded_pct: ">= 98.0"             # raw 元素 ⊆ (title+judgment+facts) compact 子串
  name_not_in_raw_count: "<= 0"       # offense_name 必须能挂到某个 raw 元素（反幻觉核心）
monitoring_only:
  failure_reason_nonnull_pct: "5.0"   # 残差池含程序性文书, 高于此值报告不阻断
"""

VERDICT_SPEC = """# verdict.spec.md · offense_name_v1.0（fork runner 时的 verdict 契约）
逐 case 检查（对齐 offense_date_v1.0 同族结构）：
1. obj 是 dict；恰好 5 键（REQUIRED_KEYS，无缺无多）。
2. id 与输入逐字一致。
3. confidence ∈ {high, medium, low}；failure_reason ∈ {null, no_offense_name_in_text, text_garbled_or_empty}。
4. 失败分支：offense_name 必须 null 且 raw == []（否则 failure_fields_not_null / failure_raw_not_empty）。
5. 成功分支：offense_name 为 2..24 字非空字符串（不含数字/字母）；raw 非空；
   每个 raw 元素 compact 后 ∈ compact(case_title+judgment_segment+facts_segment)（grounding_not_substring）；
   offense_name（compact，可去尾字"罪"）必须是某个 raw 元素的子串（name_not_in_raw）。
6. 其余（JSON 解析/输出计数/超时等）沿引擎通用 failure_code。
"""


def build_runner():
    src = SRC_RUNNER.read_text(encoding="utf-8")
    # ---- 常量块 ----
    old_const = '''REQUIRED_KEYS = [
    "id",
    "offense_start_date",
    "offense_end_date",
    "date_precision",
    "all_offense_dates_raw",
    "confidence",
    "failure_reason",
]
CONFIDENCE = {"high", "medium", "low"}
PRECISION = {"YMD", "YM", "Y"}
FAILURE_REASONS = {None, "no_offense_time_in_text", "text_garbled_or_empty"}'''
    new_const = '''REQUIRED_KEYS = [
    "id",
    "offense_name",
    "all_offense_names_raw",
    "confidence",
    "failure_reason",
]
CONFIDENCE = {"high", "medium", "low"}
FAILURE_REASONS = {None, "no_offense_name_in_text", "text_garbled_or_empty"}'''
    assert old_const in src
    src = src.replace(old_const, new_const)

    # ---- verdict_record 任务体 ----
    start_marker = '''    prec = obj.get("date_precision")
    fr = obj.get("failure_reason")'''
    end_marker = '''    return {"ok": True, "warn": warn} if warn else {"ok": True}'''
    i = src.find(start_marker)
    j = src.find(end_marker)
    assert i > 0 and j > i
    new_body = '''    fr = obj.get("failure_reason")
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
'''
    src = src[:i] + new_body + src[j + len(end_marker):]
    # 清理 date 专用помощники的调用点已移除；保留未用函数无害（py_compile 过即可）
    src = src.replace("offense_date_v1.0", "offense_name_v1.0")
    return src


GROUNDING = '''#!/usr/bin/env python3
"""Grounding / quality checks for offense_name_v1.0 runs.
读 batch_*.json + *_result.jsonl，复核 id 保真/dup/grounded/name链接/failure 占比。
输出扁平 JSON（acceptance.yaml 的 grounding_metrics 消费）。"""
from __future__ import annotations
import argparse, glob, json, pathlib, re

def compact(v):
    return re.sub(r"[\\s\\u3000]+", "", "" if v is None else str(v))

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
'''


def main():
    PROF.mkdir(parents=True, exist_ok=True)
    BATCHES.mkdir(parents=True, exist_ok=True)
    (PROF / "pattern.md").write_text(PATTERN, encoding="utf-8")
    (PROF / "schema.json").write_text(json.dumps(SCHEMA, ensure_ascii=False, indent=2))
    (PROF / "acceptance.yaml").write_text(ACCEPTANCE, encoding="utf-8")
    (PROF / "verdict.spec.md").write_text(VERDICT_SPEC, encoding="utf-8")
    (PROF / "runner.py").write_text(build_runner(), encoding="utf-8")
    (PROF / "grounding.py").write_text(GROUNDING, encoding="utf-8")

    # sample_case = 残差池第一条
    pool = [json.loads(l) for l in open(RES_POOL, encoding="utf-8") if l.strip()]
    (PROF / "sample_case.json").write_text(json.dumps(pool[0], ensure_ascii=False, indent=2))

    # 批（200/批）
    n = 0
    for b in range(0, len(pool), 200):
        chunk = pool[b:b + 200]
        (BATCHES / f"batch_{n:06d}.json").write_text(json.dumps(chunk, ensure_ascii=False))
        n += 1
    manifest = {"ts": TS, "task": "offense_name_v1.0",
                "residual_cases": len(pool), "batches": n, "batch_size": 200,
                "note": "本案罪名 LLM 残差（本地三层瀑布 97.93% 后）; runner 为 offense_date 同族 fork"}
    (OUT / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    shas = []
    for f in sorted(OUT.rglob("*")):
        if f.is_file() and f.name != "SHA256SUMS.txt":
            shas.append(f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {f.relative_to(OUT)}")
    (OUT / "SHA256SUMS.txt").write_text("\n".join(shas) + "\n")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print("OUT:", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
