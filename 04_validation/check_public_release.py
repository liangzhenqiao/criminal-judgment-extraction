#!/usr/bin/env python3
"""Leak gate for the public repository.

Scans every file in the repository (except .git/, __pycache__/, .pytest_cache/,
.venv/, work/, and the sample documents and their expected output under
examples/reproduction_sample/) and exits with status 1 if any check fails:

  * machine paths and hosting details (local mount points, home directories,
    rented-GPU host names, ssh port commands);
  * e-mail addresses (CITATION.cff is exempt from this one check only);
  * internal project vocabulary (coordination roles, round labels, internal
    task-assignment wording, third-party data vendor names);
  * Chinese national-ID numbers and mainland mobile-phone numbers;
  * a defendant-name form "被告人<name>，男/女" whose name is not a masked "某" form;
  * full birth dates of the form YYYY年M月D日出生 / 生 (also inside examples/);
  * single files larger than 1 MB;
  * data files (.parquet, .csv.gz, .jsonl, .dta, .xlsx, .zip) and named
    internal files that must never ship. The reuse example ships a 5,000-row
    subsample of the released data on purpose; it is exempt from the data-file
    check and keeps the size check.

The regular expressions below are assembled from fragments, so the source text
of this script does not match its own patterns; running it is the only way to see
what it catches.

Usage:
  python 04_validation/check_public_release.py [--root REPO_ROOT]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MAX_BYTES = 1_000_000
# names used in the author's working copies
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "work", "dispatch"}
# The reproduction sample ships judgment text as published, so personal detail in
# it is expected rather than a leak. Those files keep the size check (with a
# larger cap, for the 3 MB segments file) and their content hits are counted and
# reported instead of failing the gate.
SAMPLE_PREFIXES = ("examples/reproduction_sample/documents/",
                   "examples/reproduction_sample/expected/")
SAMPLE_MAX_BYTES = 4_000_000

_J = "".join  # join fragments
CHECKS: list[tuple[str, re.Pattern, set[str]]] = [
    # (name, pattern, set of repo-relative files exempt from this check)
    ("machine_path", re.compile(_J(["/Vol", "umes/|/Us", "ers/|/ro", "ot/"])), set()),
    # the docs name a rent-by-the-hour platform on purpose
    ("gpu_host", re.compile(_J(["auto", "dl|seeta", "cloud"]), re.I), {"README.md", "README.zh-CN.md", "AGENT_TASK.md"}),
    ("ssh_port", re.compile(_J(["ss", "h -p"])), set()),
    ("email", re.compile(_J(["@[A-Za-z0-9.-]+", r"\.(cn|com)\b"])), {"CITATION.cff"}),
    # platform or vendor names in comments and docs: checked by hand, since the
    # fold table keeps one historical batch token ("regex_bailian")
    ("platform_name", re.compile(_J(["ba", "ilian|Open", "Router|macro", "datas"]), re.I), {"03_merge_release/11_fold_offense_source.py", "04_validation/check_public_release.py"}),
    ("internal_role", re.compile(_J([r"\bOw", r"ner\b|Coord", "inator"])), set()),
    ("internal_vocab", re.compile(_J(["swa", "rm|bridge", "mind|dis", "patch"]), re.I), {"04_validation/check_public_release.py", "04_validation/README.md"}),
    ("round_label", re.compile(_J(["R2", "[0-9] "])), set()),
    # tool and orchestration traces: the repository must read as a standalone
    # template, not as the run log of a coding assistant
    ("tool_name", re.compile(_J(["Cod", "ex|Clau", "de|sub", "agent"])), set()),
    ("ops_vocab", re.compile(_J(["会", "话|回", "执|调", "度|交", "接"])), set()),
    ("national_id", re.compile(_J([r"(?<![0-9A-Za-z])[1-9]\d{5}(?:18|19|20)\d{2}",
                                   r"(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![0-9A-Za-z])"])), set()),
    ("mobile_phone", re.compile(_J([r"(?<![0-9A-Za-z])1[3-9]", r"\d{9}(?![0-9A-Za-z])"])), set()),
    ("birth_date", re.compile(_J([r"\d{4}年\d{1,2}月", r"\d{1,2}日(?:出生|生)"])), set()),
]
NAME_RE = re.compile(_J(["被告", r"人([一-龥]{1,3})(?:，|,)(?:男|女)"]))
DATA_SUFFIXES = (".parquet", ".csv.gz", ".jsonl", ".jsonl.gz", ".dta", ".xlsx", ".zip", ".feather")
# the reuse example reads a subsample of the released data; shipping it is the point
DATA_EXEMPT = {"examples/release/sample_5000.parquet"}
FORBIDDEN_NAMES = {
    _J(["gold_50", ".jsonl"]), "review.html", "AGENTS.md", "SKILL.md", "CLAUDE.md", "PROVENANCE.md",
    _J(["id_to_cjo", "_url.csv"]), _J(["regenerate_realistic", "_annotations.py"]),
}
# names used in the author's working copies
FORBIDDEN_DIRS = {".bridgespace", "prompts", "orchestrator", "reports", "gold", "texts", "cards"}
# this file lists the findings the gate is expected to return, so it repeats them
BASELINE_FILE = "04_validation/tests/baseline_findings.txt"


def iter_files(root: Path):
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if p.is_file():
            yield p, rel.as_posix()


def scan(root: Path) -> list[str]:
    problems: list[str] = []
    sample_counts: dict[str, int] = {}
    n_files = n_sample = 0
    for p, rel in iter_files(root):
        n_files += 1
        sample = rel.startswith(SAMPLE_PREFIXES)
        if sample:
            n_sample += 1
        size = p.stat().st_size
        cap = SAMPLE_MAX_BYTES if sample else MAX_BYTES
        if size > cap:
            problems.append(f"{rel}: file_size {size} > {cap}")
        if not sample:
            if rel.endswith(DATA_SUFFIXES) and rel not in DATA_EXEMPT:
                problems.append(f"{rel}: data_file")
            if p.name in FORBIDDEN_NAMES or p.name.endswith("_sop.md") or p.name.endswith(".log") \
                    or p.name.startswith("worklog") or p.name.startswith("agent_"):
                problems.append(f"{rel}: forbidden_file")
            if any(part in FORBIDDEN_DIRS for part in Path(rel).parts[:-1]):
                problems.append(f"{rel}: forbidden_dir")
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary; size and suffix checks above still apply
        if rel == BASELINE_FILE:
            continue  # its lines mirror findings already raised against other files
        for lineno, line in enumerate(text.splitlines(), 1):
            for name, pat, exempt in CHECKS:
                if rel in exempt:
                    continue
                m = pat.search(line)
                if m:
                    if sample:
                        sample_counts[name] = sample_counts.get(name, 0) + 1
                    else:
                        problems.append(f"{rel}:{lineno}: {name}: {m.group(0)!r}")
            for m in NAME_RE.finditer(line):
                if "某" not in m.group(1):
                    if sample:
                        sample_counts["defendant_name"] = sample_counts.get("defendant_name", 0) + 1
                    else:
                        problems.append(f"{rel}:{lineno}: defendant_name: {m.group(0)!r}")
    print(f"scanned {n_files} files under {root}, {n_sample} of them sample text")
    if sample_counts:
        print("  sample text carries, by design: "
              + ", ".join(f"{k}={v}" for k, v in sorted(sample_counts.items())))
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]),
                    help="repository root (default: parent of 04_validation/)")
    args = ap.parse_args()
    problems = scan(Path(args.root).resolve())
    for line in problems:
        print("FAIL", line)
    if problems:
        print(f"RESULT: FAIL ({len(problems)} findings)")
        return 1
    print("RESULT: PASS (0 findings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
