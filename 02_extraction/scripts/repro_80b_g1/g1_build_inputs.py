#!/usr/bin/env python3
"""Build the input cases for the production-model reproduction run.

Input mirrors the production contract: one JSON array holding
{id, defendant_segment, facts_segment, judgment_segment, regex_extracted}, with
regex_extracted left null so the model must extract every field. Sections are
split on the two standard judgment boundaries.

Usage:
  python3 g1_build_inputs.py OUT.jsonl            # build
  python3 g1_run.py IN.jsonl OUT.jsonl            # run against the server
"""
import json
import os
import sys
from pathlib import Path

# directory holding the sampled texts; defaults to this folder
ROOT = Path(os.environ.get("G1_ROOT", Path(__file__).resolve().parent))
TEXTS = ROOT / "texts"
KEYS = [
    "id", "sex", "birth_year", "ethnicity", "edu_level", "prior_occupation_raw",
    "hukou_text", "has_prior_conviction", "prior_conviction_desc",
    "is_recidivist_legal", "party_role", "sentence_months", "sentence_type",
    "probation_flag", "fine_yuan", "damage_amount_yuan", "crime_location_raw",
    "prior_years", "prior_offenses", "prior_sentence_months",
    "prior_released_years",
]


def split(text):
    facts_at = min([i for i in (text.find("经审理查明"), text.find("经审查明")) if i > 0] or [0])
    hold_at = text.find("本院认为")
    if facts_at and hold_at > facts_at:
        return text[:facts_at], text[facts_at:hold_at], text[hold_at:]
    if hold_at > 0:
        return text[:hold_at], "", text[hold_at:]
    return text, "", ""


def main():
    out = Path(sys.argv[1])
    n = 0
    with open(out, "w", encoding="utf-8") as f:
        for p in sorted(TEXTS.glob("*.txt")):
            if p.stem.startswith("._"):
                continue
            try:
                raw = p.read_text(encoding="utf-8").strip()
            except UnicodeDecodeError:
                raw = p.read_text(encoding="gb18030", errors="replace").strip()
            if not raw:
                continue
            d, fa, j = split(raw)
            case = {
                "id": p.stem,
                "defendant_segment": d[:8000],
                "facts_segment": fa[:12000],
                "judgment_segment": j[:12000],
                "regex_extracted": {k: None for k in KEYS if k != "id"},
            }
            f.write(json.dumps([case], ensure_ascii=False) + "\n")
            n += 1
    print("cases", n)


if __name__ == "__main__":
    main()
