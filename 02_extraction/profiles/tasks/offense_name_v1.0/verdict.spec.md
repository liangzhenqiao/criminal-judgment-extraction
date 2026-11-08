# verdict.spec.md · offense_name_v1.0（fork runner 时的 verdict 契约）
逐 case 检查（对齐 offense_date_v1.0 同族结构）：
1. obj 是 dict；恰好 5 键（REQUIRED_KEYS，无缺无多）。
2. id 与输入逐字一致。
3. confidence ∈ {high, medium, low}；failure_reason ∈ {null, no_offense_name_in_text, text_garbled_or_empty}。
4. 失败分支：offense_name 必须 null 且 raw == []（否则 failure_fields_not_null / failure_raw_not_empty）。
5. 成功分支：offense_name 为 2..24 字非空字符串（不含数字/字母）；raw 非空；
   每个 raw 元素 compact 后 ∈ compact(case_title+judgment_segment+facts_segment)（grounding_not_substring）；
   offense_name（compact，可去尾字"罪"）必须是某个 raw 元素的子串（name_not_in_raw）。
6. 其余（JSON 解析/输出计数/超时等）沿引擎通用 failure_code。
