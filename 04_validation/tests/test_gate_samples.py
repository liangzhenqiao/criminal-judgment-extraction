"""The gate must scan the sample text, not skip it.

The reproduction sample is raw judgment text, so its personal detail is counted
rather than failed — but it has to be counted, or the gate would pass while
blind to 1,000 files and their 3 MB expected output.

The gate's findings on this snapshot are the standing baseline in
``baseline_findings.txt``; the scan has to return exactly that set. Line numbers
are stripped before comparing, because editing a paragraph above a finding
shifts it without changing what the finding is.
"""
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).resolve().parent / "baseline_findings.txt"
SPEC = importlib.util.spec_from_file_location(
    "check_public_release", ROOT / "04_validation" / "check_public_release.py")
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


def baseline():
    return {line for line in BASELINE.read_text(encoding="utf-8").splitlines()
            if line.strip()}


def normalise(findings):
    """Drop the line number, keep file and finding kind."""
    out = set()
    for finding in findings:
        left, sep, kind = finding.partition(": ")
        if sep:
            finding = re.sub(r":\d+$", "", left) + sep + kind
        out.add(finding)
    return out


def test_sample_directories_are_not_skipped():
    rels = {rel for _, rel in GATE.iter_files(ROOT)}
    assert any(r.startswith("examples/reproduction_sample/documents/") for r in rels)
    assert any(r.startswith("examples/reproduction_sample/expected/") for r in rels)


def test_sample_detail_is_counted(capsys):
    problems = GATE.scan(ROOT)
    out = capsys.readouterr().out
    assert normalise(problems) == normalise(baseline())
    assert "by design" in out
    assert "defendant_name=" in out
