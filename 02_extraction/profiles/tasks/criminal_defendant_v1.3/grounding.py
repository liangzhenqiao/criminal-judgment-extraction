#!/usr/bin/env python3
"""Task-level grounding hook for criminal_defendant_v1.3.

The implementation delegates to the repo-level criminal grounding helper. New
tasks should copy this file only as a shape reference and replace the metric
logic with task-specific evidence checks.
"""
from __future__ import annotations

import runpy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
runpy.run_path(str(ROOT / "scripts" / "grounding_compare.py"), run_name="__main__")
