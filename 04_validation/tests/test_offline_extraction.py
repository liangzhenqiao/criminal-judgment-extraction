"""Offline (no GPU) tests for the extraction stage.

* the smoke-gate self-test (mock HTTP, temp files);
* Smoke Stage A against a local mock of the OpenAI-compatible endpoint that
  answers with the synthetic example output in examples/;
* the synthetic examples conform to the three task schemas and every raw
  evidence string is a verbatim substring of its input text.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "02_extraction"
EX = REPO / "examples"
TASKS = ROOT / "profiles" / "tasks"


def load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def test_smoke_logic_self_test():
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "self_test_smoke_logic.py")],
                          cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]


def _mock_server(answer: dict):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n))
            case = json.loads(req["messages"][1]["content"])
            case = case[0] if isinstance(case, list) else case
            data = [r for r in answer["data"] if r["id"] == case["id"]]
            body = json.dumps({"choices": [{"message": {"content": json.dumps({"data": data}, ensure_ascii=False)},
                                            "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_smoke_stage_a_offline(tmp_path):
    srv = _mock_server(load(EX / "criminal_defendant_output.json"))
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions"
        prof = TASKS / "criminal_defendant_v1.3"
        out = tmp_path / "sanity.json"
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "smoke_stage_a.py"),
             "--pattern", str(prof / "pattern.md"), "--case", str(EX / "criminal_defendant_input.json"),
             "--schema", str(prof / "schema.json"), "--litellm-url", url, "--out", str(out)],
            cwd=ROOT, capture_output=True, text=True, timeout=120)
    finally:
        srv.shutdown()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out.exists()


def _keys(schema: dict) -> list[str]:
    return schema.get("required_keys") or schema.get("required")


def test_examples_match_task_schemas():
    pairs = [("criminal_defendant_v1.3", "criminal_defendant"),
             ("offense_date_v1.0", "offense_date"),
             ("offense_name_v1.0", "offense_name")]
    for task, stem in pairs:
        schema = load(TASKS / task / "schema.json")
        inp = load(EX / f"{stem}_input.json")
        out = load(EX / f"{stem}_output.json")["data"]
        assert [r["id"] for r in out] == [r["id"] for r in inp], task
        for rec in out:
            assert set(rec) == set(_keys(schema)), (task, set(rec) ^ set(_keys(schema)))
            for k in schema.get("forbidden_keys", []):
                assert k not in rec


def test_examples_evidence_is_verbatim():
    for stem, field, texts in [("offense_date", "all_offense_dates_raw", ("facts_segment",)),
                               ("offense_name", "all_offense_names_raw",
                                ("case_title", "judgment_segment", "facts_segment"))]:
        inp = {r["id"]: r for r in load(EX / f"{stem}_input.json")}
        for rec in load(EX / f"{stem}_output.json")["data"]:
            src = "".join(inp[rec["id"]][t] for t in texts)
            for raw in rec[field]:
                assert raw in src, (stem, raw)


def test_prior_arrays_aligned():
    for rec in load(EX / "criminal_defendant_output.json")["data"]:
        lens = {len(rec[k]) for k in ("prior_years", "prior_offenses",
                                      "prior_sentence_months", "prior_released_years")}
        assert len(lens) == 1
