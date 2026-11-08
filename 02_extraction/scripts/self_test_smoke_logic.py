#!/usr/bin/env python3
"""Self-contained no-GPU regression tests for smoke gate logic.

These tests encode the adversarial probes that previously found false-green
paths. They use only local temp files, mock HTTP, and tiny shell fixtures.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str], *, env: dict[str, str] | None = None, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def assert_true(ok: bool, msg: str) -> None:
    if not ok:
        raise AssertionError(msg)


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def test_smoke_gate_states(tmp: Path) -> None:
    sanity = tmp / "sanity.json"
    grounding = tmp / "grounding.json"
    good_state = tmp / "state_good.json"
    queued_state = tmp / "state_with_queued.json"
    no_yaml = tmp / "no_yaml"
    no_yaml.mkdir()
    (no_yaml / "yaml.py").write_text("raise ImportError('forced no yaml')\n", encoding="utf-8")

    write_json(sanity, {"verdict": "PASS"})
    write_json(grounding, {
        "v1": {
            "no_hpc_arr_filled_violation": 0,
            "arrays_aligned_pct": 100.0,
            "prior_offenses": {"grounded_with_split_pct": 100.0},
        }
    })
    done = {"status": "done", "items_per_sec": 6.5, "ok_n": 100, "fail_n": 0, "fail_codes": {}}
    write_json(good_state, {"batch_000": done})
    write_json(queued_state, {"batch_000": done, "batch_999": {"status": "queued"}})

    good_out = tmp / "good_verdict.json"
    res = run([
        sys.executable, "scripts/smoke_gate.py",
        "--acceptance", "profiles/tasks/criminal_defendant_v1.3/acceptance.yaml",
        "--sanity", str(sanity),
        "--state", str(good_state),
        "--expected-batches", "1",
        "--grounding-json", str(grounding),
        "--out", str(good_out),
    ])
    assert_true(res.returncode == 0, f"good gate should GREEN: {res.stderr or res.stdout}")
    assert_true(json.load(open(good_out, encoding="utf-8"))["verdict"] == "GREEN", "good gate verdict")

    queued_out = tmp / "queued_verdict.json"
    res = run([
        sys.executable, "scripts/smoke_gate.py",
        "--acceptance", "profiles/tasks/criminal_defendant_v1.3/acceptance.yaml",
        "--sanity", str(sanity),
        "--state", str(queued_state),
        "--expected-batches", "1",
        "--grounding-json", str(grounding),
        "--out", str(queued_out),
    ])
    qv = json.load(open(queued_out, encoding="utf-8"))
    assert_true(res.returncode == 1, "queued state must ALARM")
    assert_true(qv["verdict"] == "ALARM", "queued state verdict")
    assert_true("stage_c.all_batches_done" in qv["alarms"], "queued alarm should include all_batches_done")

    custom_acceptance = tmp / "custom_acceptance.yaml"
    custom_acceptance.write_text(
        """task: custom_metric_probe
stage_c:
  expected_batches: 1
  throughput_it_s_min: 5.0
  fail_rate_max: 0.01
  context_overflow_rate_max: 0.001
  throughput_drift_pct_max: 10.0
  network_hw_fail_max: 0
grounding:
  metrics:
    - name: impossible_custom_metric
      path: custom.missing.metric
      op: ">="
      threshold: 1
""",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(no_yaml)
    res = run([
        sys.executable, "scripts/smoke_gate.py",
        "--acceptance", str(custom_acceptance),
        "--sanity", str(sanity),
        "--state", str(good_state),
        "--expected-batches", "1",
        "--grounding-json", str(grounding),
        "--out", str(tmp / "no_yaml_verdict.json"),
    ], env=env)
    assert_true(res.returncode != 0, "missing PyYAML must fail closed")
    assert_true("PyYAML is required" in (res.stderr + res.stdout), "missing PyYAML message")


def test_task_grounding_hook(tmp: Path) -> None:
    cases = tmp / "cases"
    results = tmp / "results"
    cases.mkdir()
    results.mkdir()
    write_json(cases / "batch_000.json", [{"id": "c1", "defendant_segment": "曾因盗窃罪被判刑。"}])
    (results / "out.jsonl").write_text(
        json.dumps({
            "id": "c1",
            "has_prior_conviction": True,
            "prior_years": [2019],
            "prior_offenses": ["盗窃罪"],
            "prior_sentence_months": [12],
            "prior_released_years": [2020],
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    out = tmp / "task_grounding.json"
    res = run([
        sys.executable, "profiles/tasks/criminal_defendant_v1.3/grounding.py",
        "--cases-dir", str(cases),
        "--results-dir", str(results),
        "--schema", "profiles/tasks/criminal_defendant_v1.3/schema.json",
        "--out", str(out),
    ])
    assert_true(res.returncode == 0, f"task grounding failed: {res.stderr or res.stdout}")
    data = json.load(open(out, encoding="utf-8"))
    assert_true(data["v1"]["cases_compared"] == 1, "task grounding should compare one case")


class _StageAHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("content-length", "0")))
        body = {
            "choices": [{
                "message": {"content": json.dumps([{"id": "case_1_extra"}], ensure_ascii=False)},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        return


def test_stage_a_exact_id(tmp: Path) -> None:
    pattern = tmp / "pattern.md"
    case = tmp / "case.json"
    schema = tmp / "schema.json"
    out = tmp / "sanity.json"
    pattern.write_text("pattern", encoding="utf-8")
    write_json(case, {"id": "case_1"})
    write_json(schema, {
        "required_keys": ["id"],
        "forbidden_keys": [],
        "id_key": "id",
        "id_match_mode": "exact",
        "array_fields": [],
        "exact_key_count": True,
    })
    srv = HTTPServer(("127.0.0.1", 0), _StageAHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        res = run([
            sys.executable, "scripts/smoke_stage_a.py",
            "--pattern", str(pattern),
            "--case", str(case),
            "--schema", str(schema),
            "--litellm-url", f"http://127.0.0.1:{srv.server_port}/v1/chat/completions",
            "--model", "x",
            "--out", str(out),
        ])
    finally:
        srv.shutdown()
    data = json.load(open(out, encoding="utf-8"))
    assert_true(res.returncode == 1, "substring id should fail exact id check")
    assert_true("id_match" in data["fails"], "Stage A should report id_match")


def write_shell(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def test_stage_c_alias_and_vllm_extra_args(tmp: Path) -> None:
    bin_dir = tmp / "bin"
    logs = tmp / "logs"
    work = tmp / "work"
    model_dir = tmp / "model_dir"
    batches = tmp / "batches"
    for d in (bin_dir, logs, work, model_dir, batches):
        d.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (batches / "batch_000.json").write_text("[]", encoding="utf-8")
    pattern = tmp / "pattern.md"
    pattern.write_text("pattern", encoding="utf-8")

    runtime = tmp / "runtime.yaml"
    runtime.write_text(f"""bin:
  python: {sys.executable}
paths:
  model_dir: {model_dir}
  hf_home: {tmp / "hf"}
  work_root: {work}
  log_dir: {logs}
ports:
  vllm: 8020
  litellm: 4000
""", encoding="utf-8")
    model = tmp / "model.yaml"
    model.write_text("""model:
  repo: Dummy/Model
  size_gb: 1
vllm:
  tensor_parallel_size: 1
  max_model_len: 1024
  max_num_seqs: 1
  gpu_memory_utilization: 0.5
  served_model_name: Dummy/Model
  extra_args:
    - --enforce-eager
    - --max-num-batched-tokens
    - "1234"
litellm:
  model_alias: custom-alias
runner:
  bundle: 1
  concurrency: 1
  max_tokens: 16
""", encoding="utf-8")
    runner = tmp / "runner.py"
    runner.write_text("""#!/usr/bin/env python3
import argparse, json, os, pathlib
ap = argparse.ArgumentParser()
for arg in ("--pattern", "--batches", "--bundle", "--concurrency", "--max-tokens", "--work-root"):
    ap.add_argument(arg)
a = ap.parse_args()
root = pathlib.Path(a.work_root)
root.mkdir(parents=True, exist_ok=True)
(root / "env.json").write_text(json.dumps({"LITELLM_MODEL": os.environ.get("LITELLM_MODEL")}, ensure_ascii=False))
""", encoding="utf-8")
    write_shell(bin_dir / "curl", "#!/usr/bin/env bash\nprintf %s '{\"data\":[{\"id\":\"Dummy/Model custom-alias\"}]}'\n")

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    run_dir = tmp / "run_stage_c"
    res = run([
        "bash", "scripts/smoke_stage_c.sh",
        "--runtime", str(runtime),
        "--model", str(model),
        "--pattern", str(pattern),
        "--batches", str(batches),
        "--expected-batches", "1",
        "--model-alias", "custom-alias",
        "--runner", str(runner),
        "--wait",
        "--run-dir", str(run_dir),
    ], env=env)
    assert_true(res.returncode == 0, f"stage C mock failed: {res.stderr or res.stdout}")
    launch = json.load(open(run_dir / "stage_c_launch.json", encoding="utf-8"))
    assert_true(launch["model_alias"] == "custom-alias", "stage C launch should record alias")
    state = json.load(open(work / "_smoke_runs" / "run_stage_c" / "env.json", encoding="utf-8"))
    assert_true(state["LITELLM_MODEL"] == "custom-alias", "runner should receive alias")

    run_vllm = tmp / "run_vllm"
    res = run(["bash", "scripts/launch_vllm.sh", "--runtime", str(runtime), "--model", str(model), "--run-dir", str(run_vllm)], env=env)
    assert_true(res.returncode == 0, f"launch_vllm mock failed: {res.stderr or res.stdout}")
    health = json.load(open(run_vllm / "vllm_health.json", encoding="utf-8"))
    assert_true(health["extra_args"] == "--enforce-eager --max-num-batched-tokens 1234", "extra_args should be preserved")


def main() -> int:
    tests = [
        test_smoke_gate_states,
        test_task_grounding_hook,
        test_stage_a_exact_id,
        test_stage_c_alias_and_vllm_extra_args,
    ]
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for fn in tests:
            tmp = root / fn.__name__
            tmp.mkdir()
            fn(tmp)
            print(f"PASS {fn.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
