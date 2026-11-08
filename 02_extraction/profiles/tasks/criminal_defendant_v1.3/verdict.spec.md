# Verdict Spec — criminal_defendant_v1.3（源码实证）

> 本文件描述 runner verdict 的检查项，**实证自 `runner/run_full_extraction.py` 源码**（非文档推测）。
> fork runner 换任务时，据此清单改写 `verdict_record` / `verdict_call`。
> 详见 `runner/run_full_extraction.py`。

## REQUIRED_KEYS_21（源码 `REQUIRED_KEYS_21`，带 `assert len==21`）

```
id
sex birth_year ethnicity edu_level prior_occupation_raw hukou_text
has_prior_conviction prior_conviction_desc is_recidivist_legal
party_role sentence_months sentence_type probation_flag fine_yuan
damage_amount_yuan crime_location_raw
prior_years prior_offenses prior_sentence_months prior_released_years   # 4 个并行数组
```

## FORBIDDEN_KEYS（源码 `FORBIDDEN_KEYS`，warn-only 删除，不算失败）

```
prior_records prior_count prior_total_months age_at_first_crime          # derived in v1.2, moved to merge post-processing
has_recidivism_history has_self_surrender has_truthful_confession has_plea_leniency  # carry-over, filled in the merge stage
```

## verdict_record 检查顺序（源码实证）

| # | 检查 | failure_code | 说明 |
|---|---|---|---|
| 1 | obj 有 id | `id_mismatch`(detail=output_no_id) | — |
| 2 | `str(input.id) in str(obj.id)` | `id_mismatch` | 容忍 obj.id 含后缀 |
| 3 | 删 FORBIDDEN_KEYS | （warn，不失败）| 打 `__warn_forbidden_removed` 标记 |
| 4 | 21 keys 齐全 | `schema_violation:missing_key:<k>` | 报首个缺失 key |
| 5 | 4 个 prior_* 是 list | `schema_violation:type_mismatch:<k>` | — |
| 6 | 4 个 prior_* 等长 | `consistency_violation:prior_arrays_not_aligned` | QC5 |

## verdict_call 额外检查（源码实证）

| 检查 | failure_code |
|---|---|
| call 整体失败（网络/解析） | 透传 call_result.code（见下表）|
| `arr_len>1` 且输入非多被告（`\|\|\|`<2 且 "被告人"<3） | `multi_defendant_mismatch` |
| bundle>1 且 obj.id==input.id 未加后缀 | 自动补 `_d{i+1}`（不失败）|
| obj 非 dict | `obj_not_dict` |
| bundle>1 找不到匹配 id 子集 | `id_not_in_arr` |

## call_litellm 层 failure_code（源码实证）

`timeout` / `request_error` / `http_<status>` / `context_overflow`(finish=length 或 ContextWindow 400) / `response_parse_error` / `thinking_unterminated` / `json_parse_error` / `not_data_array`

## ⚠️ 反幻觉边界（关键，与文档不符）

runner verdict **不检查** `consistency_violation:hpc_false_arr_nonempty`（"has_prior=false → prior_* 必须全空"）。
- 该规则由 **pattern（system prompt §3.17 + 输出指令）约束模型**；
- 由 **任务级 grounding 脚本（`grounding.py` 委托到 `grounding_compare.py` 的 `no_hpc_arr_filled_violation`）抽样测量**；
- **不是 runner verdict 的硬拦截**。
- 若新任务要 verdict 级硬拦截反幻觉，fork runner 时在 verdict_record 末尾新增此检查。

## 换任务必改清单（fork runner 时）

1. `REQUIRED_KEYS_*`（新 schema 字段集）
2. `FORBIDDEN_KEYS`（新任务的禁止字段）
3. 数组型字段名（本任务是 4 个 prior_*）+ 对齐检查
4. 多被告检测的分隔符/触发词（本任务 `|||` / "被告人"）
5. （可选）反幻觉硬拦截规则
