"""The visible copy of the production pattern must match the one the pipeline ships.

`pattern/` exists so a reader can open the exact prompt from the repository root;
the profile directory keeps its own copy beside the schema. Two copies drift, so
this test fails on any divergence.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VISIBLE = ROOT / "pattern" / "criminal_defendant_v1.3.md"
PROFILE = (ROOT / "02_extraction" / "profiles" / "tasks"
           / "criminal_defendant_v1.3" / "pattern.md")


def test_pattern_copies_are_identical():
    assert VISIBLE.is_file(), f"missing {VISIBLE}"
    assert PROFILE.is_file(), f"missing {PROFILE}"
    assert VISIBLE.read_bytes() == PROFILE.read_bytes()


def test_visible_pattern_is_not_empty_of_the_contract():
    text = VISIBLE.read_text(encoding="utf-8")
    for key in ("prior_years", "prior_offenses", "sentence_months", "fine_yuan"):
        assert key in text
