# Units and sentinels

Read this before writing any number out of the record set. Every line points to the source of the rule.

| Item | Rule | Where it is defined |
|---|---|---|
| `sentence_months` | Months. Null for life and death-type sentences, so read `sentence_type` alongside it. | the data item's field dictionary, `sentence_months` and `sentence_type` rows; the data item's README, "Before you use it" item 2 |
| `fine_yuan` | Yuan (CNY). | the data item's field dictionary, `fine_yuan` row |
| `damage_amount_yuan` | Yuan (CNY), where stated. | the data item's field dictionary, `damage_amount_yuan` row |
| `9999` in `prior_sentence_months` | Means life imprisonment or death with reprieve, not 9,999 months. It is excluded from `prior_total_months`. | the data item's field dictionary, `prior_sentence_months` and `prior_total_months` rows |
| `prior_years`, `prior_offenses`, `prior_sentence_months`, `prior_released_years` | Four arrays, aligned by position and meant to equal `prior_count`; 55 rows have unequal lengths and 46 rows have a `prior_count` that differs from `len(prior_years)`. JSON-encoded in both Parquet and CSV, so decode before use; one cell is a nested array. | the data item's field dictionary, the four `prior_*` rows and `prior_count`; the data item's README, "Before you use it" item 4 |
| `judge_id` | `J-` plus 20 hex of an HMAC over the printed name. A source-label pseudonym, not a verified judicial identity; the same printed label always maps to the same id. An empty value does not mean no judge signed. | the data item's field dictionary, `judge_id` row |
| Null | One of three documented meanings: not stated in the source document, not applicable to the record type, or not processed or derivable. The flag fields separate the last two: `offense_source` gives `llm_no_time` (attested absent) and `failed` (unknown); `offense_name_source` gives `llm_no_name`. Null is never a zero. | the data item's field dictionary, `offense_source` and `offense_name_source` rows |
| `case_id_collision` | `TRUE` where the published case number is non-unique in the source corpus; offense-name, category and offense-date fields are null for these rows, sentence, fine, probation, damage and location values are kept. Strip the release-internal `_dN` suffix before joining to `id` elsewhere. | the data item's field dictionary, `id` and `case_id_collision` rows |
