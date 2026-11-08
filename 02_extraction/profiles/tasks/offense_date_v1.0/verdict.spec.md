# offense_date_v1.0 — Verdict / 契约规范

## 输出契约（每 case 恰 7 键，顺序固定）
`id, offense_start_date, offense_end_date, date_precision, all_offense_dates_raw, confidence, failure_reason`

## runner verdict_record 判定（失败即入 failures_for_*，不进 results）
- `obj_not_dict` — 非对象
- `schema_violation:missing_key:*` / `extra_key:*` — 键集不精确（必须恰 7 键）
- `id_mismatch` — id 未逐字保真
- `schema_violation:bad_confidence` — confidence ∉ {high,medium,low}
- `schema_violation:bad_precision` — date_precision ∉ {YMD,YM,Y,null}
- `schema_violation:bad_failure_reason` — failure_reason ∉ {null, no_offense_time_in_text, text_garbled_or_empty}
- `schema_violation:type_mismatch:all_offense_dates_raw` — 非数组
- 失败分支（failure_reason≠null）：`failure_dates_not_null`（三日期键须全 null）/ `failure_raw_not_empty`（数组须为 []）
- 成功分支（failure_reason=null）：
  - `success_precision_null` / `success_missing_date` / `success_raw_empty`
  - `precision_dishonest_start|end` — 日期格式与 date_precision 不符（堵死 YM 补 YMD）
  - `start_after_end` — 起始晚于结束
  - `grounding_not_substring` — raw 元素非 facts_segment 逐字子串（compact 比对）

## 接受门槛（acceptance.yaml → smoke_gate.py）
- stage_a：HTTP200 + JSON + 恰 7 键 + id 精确 + all_offense_dates_raw 是数组
- stage_c：全 batch done、fail_rate≤5%、context_overflow_rate=0、throughput_drift≤25%、network_hw_fail=0
- grounding：id_preserved=100%、duplicate_id=0、missing_input=0、grounded_pct≥98%、precision_dishonest=0

## 巡检数据质量阈值（非 GREEN 判定，破线暂停报告）
- failure_reason 非 null 占比 ≤ 5%
- grounded_pct ≥ 98%

## 传输 wire 协议
输入 JSON 数组（N=bundle，默认 4）；输出 `{"data":[...N...]}`，纯 JSON，顺序对齐输入。
pattern.md = 原始 offense_date_v1.0 抽取语义（逐字）+ 追加传输层。
