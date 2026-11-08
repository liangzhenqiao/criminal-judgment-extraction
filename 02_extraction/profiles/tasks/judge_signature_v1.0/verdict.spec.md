# judge_signature_v1.0 Verdict Spec

This task extracts the adjudicating judge signature from preselected Chinese
criminal judgment tail snippets.

The runner validates every output record before writing result JSONL:

- exactly 9 keys:
  `raw_match_id`, `id`, `has_judge_signature`, `judge_name`, `judge_role`,
  `all_judge_candidates`, `confidence`, `failure_reason`, `evidence_text`;
- `id` exactly equals the input `id`;
- `raw_match_id` exactly equals the input `raw_match_id`;
- one output object per input object, matched primarily by `raw_match_id`;
- `has_judge_signature` is a JSON boolean;
- `confidence` is `high`, `medium`, or `low`;
- positive rows require non-null `judge_name`, legal `judge_role`,
  non-empty `all_judge_candidates`, and source evidence containing both role
  and name;
- negative rows require `judge_name=null`, `judge_role=null`,
  `evidence_text=null`, `all_judge_candidates=[]`, and non-null
  `failure_reason`;
- `书记员`, `代理书记员`, and `法官助理` are never accepted as judge roles or
  judge candidates.

The grounding hook then compares result JSONL against the input batch snippets
and reports smoke-gate metrics for id preservation, raw-match preservation,
clerk exclusion, evidence grounding, and positive shape.
