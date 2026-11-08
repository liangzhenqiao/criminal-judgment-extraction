"""Shared path configuration for the pipeline scripts in this directory.

Every machine-specific path is read from a YAML file instead of being written
into the scripts. The file is looked up in this order:

  1. the path given by ``--config PATH`` on the command line;
  2. the environment variable ``PIPELINE_CONFIG``;
  3. ``config.yaml`` in the repository root.

Copy ``config.example.yaml`` to ``config.yaml`` and edit the values. Relative
paths are resolved against the directory that holds the config file.

Scripts without their own argument parser call ``load(handle_help=True)``; it
prints the script's docstring and the config keys it reads for ``-h/--help``
and exits without touching any data.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class Config:
    def __init__(self, data: dict, base: Path, source: Path | None):
        self._data = data
        self._base = base
        self.source = source

    def path(self, key: str) -> Path:
        if self.source is None:
            raise SystemExit(
                f"[config] no config file found; copy config.example.yaml to config.yaml "
                f"in {REPO_ROOT} (or pass --config PATH) and set '{key}'")
        if key not in self._data or self._data[key] in (None, ""):
            raise SystemExit(f"[config] key '{key}' is missing in {self.source}")
        p = Path(os.path.expanduser(str(self._data[key])))
        return p if p.is_absolute() else (self._base / p).resolve()

    def get(self, key: str, default=None):
        return self._data.get(key, default)


def _config_arg(argv: list[str]) -> str | None:
    for i, a in enumerate(argv):
        if a == "--config" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--config="):
            return a.split("=", 1)[1]
    return None


def load(handle_help: bool = False, keys: tuple[str, ...] = ()) -> Config:
    if handle_help and any(a in ("-h", "--help") for a in sys.argv[1:]):
        main = sys.modules.get("__main__")
        print((getattr(main, "__doc__", None) or "").strip())
        print("\noptions:\n  --config PATH   pipeline config YAML (default: config.yaml in the repository root)")
        if keys:
            print("\nconfig keys read by this script:\n  " + "\n  ".join(keys))
        raise SystemExit(0)
    cand = _config_arg(sys.argv[1:]) or os.environ.get("PIPELINE_CONFIG")
    path = Path(cand).expanduser().resolve() if cand else REPO_ROOT / "config.yaml"
    if not path.exists():
        if cand:
            raise SystemExit(f"[config] config file not found: {path}")
        return Config({}, REPO_ROOT, None)
    import yaml  # PyYAML

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Config(data, path.parent, path)
