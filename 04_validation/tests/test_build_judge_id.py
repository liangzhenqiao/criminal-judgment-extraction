"""Small-sample test for judge_id. Synthetic tables only; no release data read."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(__file__).resolve().parents[1] / "05_build_judge_id.py"


def load_mod():
    spec = importlib.util.spec_from_file_location("build_judge_id", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = load_mod()
SECRET_A = b"test-secret-a"
SECRET_B = b"test-secret-b"


def test_stable_and_prefixed():
    a = M.judge_id_for(SECRET_A, "张三")
    b = M.judge_id_for(SECRET_A, "张三")
    assert a == b
    assert a.startswith("J-")
    assert len(a) == 22
    assert all(c in "0123456789abcdef" for c in a[2:])


def test_different_secret_different_id():
    assert M.judge_id_for(SECRET_A, "张三") != M.judge_id_for(SECRET_B, "张三")


def test_anonymous_is_null():
    for raw in ("", "   ", "某", "张某", "张某某", "某甲", "未知", "不详", "×", "××", "X", "XX", "张X", "＊"):
        norm = M.normalize_label(raw)
        assert M.judge_id_for(SECRET_A, norm) is None, raw


def test_normalize_collision_and_internal_space_kept():
    assert M.normalize_label("张  三") == M.normalize_label("张 三")
    assert M.normalize_label("张　三") == "张 三"
    assert M.normalize_label("张三") != M.normalize_label("张 三")
    assert M.normalize_label("Ｘ") == "X"
    assert M.judge_id_for(SECRET_A, M.normalize_label("张  三")) == M.judge_id_for(SECRET_A, "张 三")
    assert M.judge_id_for(SECRET_A, "张三") != M.judge_id_for(SECRET_A, "张 三")


def test_fullwidth_nfkc_stable():
    # 全角拉丁不应出现在中文姓名里；NFKC 后与半角同一 id
    assert M.normalize_label("ＡＢ") == "AB"
    assert M.judge_id_for(SECRET_A, M.normalize_label("ＡＢ")) == M.judge_id_for(SECRET_A, "AB")


def _tiny_table():
    return pa.table({
        "id": ["a", "b", "c", "d", "e", "f", "g"],
        "judge_name": ["张三", "张三", "张  三", "张 三", "某", "   ", None],
        "judge_role": ["审判长", "审判长", "审判员", "审判员", "审判长", None, None],
        "fine_yuan": pa.array([1.0, 2.0, 3.0, 4.0, None, 6.0, 7.0], type=pa.float64()),
    })


def test_dry_run_counts(tmp_path):
    path = tmp_path / "in.parquet"
    pq.write_table(_tiny_table(), path)
    out = M.run_dry(path)
    assert out["n_rows"] == 7
    assert out["n_nonempty_judge_name_rows"] == 5  # 不含纯空白与 null
    assert out["n_blank_judge_name_rows"] == 1
    assert out["n_null_judge_name_rows"] == 1
    assert out["n_anonymous_rows"] == 2  # 某 + 空白
    assert out["n_unique_normalized_labels"] == 3  # 张三, 张 三, 某
    assert out["n_normalization_collisions"] == 1  # 张  三 与 张 三
    assert out["n_expected_unique_judge_id"] == 2  # 张三 与 张 三
    assert out["same_document_homonym_observable"] is False
    assert out["apply_not_run"] is True


def test_apply_refuses_without_secret(tmp_path):
    path = tmp_path / "in.parquet"
    pq.write_table(_tiny_table(), path)
    proc = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--apply", "--input", str(path),
         "--out", str(tmp_path / "out"), "--private-map", str(tmp_path / "map.csv")],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert not (tmp_path / "out").exists()
    assert "secret" in (proc.stderr + proc.stdout)


def test_apply_refuses_missing_secret_file(tmp_path):
    path = tmp_path / "in.parquet"
    pq.write_table(_tiny_table(), path)
    proc = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--apply", "--input", str(path),
         "--out", str(tmp_path / "out"),
         "--secret-file", str(tmp_path / "no-such-secret"),
         "--private-map", str(tmp_path / "map.csv")],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert not (tmp_path / "out").exists()


def test_dry_run_cli_rejects_out_flag(tmp_path):
    path = tmp_path / "in.parquet"
    pq.write_table(_tiny_table(), path)
    proc = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--input", str(path),
         "--json-out", str(tmp_path / "dry.json"),
         "--out", str(tmp_path / "should-not")],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert not (tmp_path / "should-not").exists()


def test_apply_on_tiny_table(tmp_path):
    path = tmp_path / "in.parquet"
    src = _tiny_table()
    pq.write_table(src, path)
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"unit-test-secret\n")
    out = tmp_path / "release_new"
    private_map = tmp_path / "private" / "map.csv"
    proc = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--apply", "--input", str(path),
         "--out", str(out), "--secret-file", str(secret),
         "--private-map", str(private_map)],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    got = pq.read_table(out / "china_criminal_judgment_records_1985_2021.parquet")
    assert got.column_names == ["id", "judge_id", "judge_role", "fine_yuan"]
    assert got.column("id").to_pylist() == src.column("id").to_pylist()
    assert got.column("judge_role").to_pylist() == src.column("judge_role").to_pylist()
    assert got.column("fine_yuan").to_pylist() == src.column("fine_yuan").to_pylist()
    ids = got.column("judge_id").to_pylist()
    assert ids[0] == ids[1]
    assert ids[0] != ids[2]  # 张三 vs 张 三
    assert ids[2] == ids[3]  # 张  三 与 张 三 合并
    assert ids[4] is None and ids[5] is None and ids[6] is None
    assert ids[0].startswith("J-")
    text = private_map.read_text(encoding="utf-8")
    assert "张三" in text and "某" in text
    assert not any(p.suffix == ".csv" for p in out.iterdir())
    report = json.loads((tmp_path / "private" / "map.csv.collisions.json").read_text(encoding="utf-8"))
    assert report["same_document_homonym_observable"] is False
    assert report["n_groups"] == 1
    assert not (ROOT / "release_new").exists()


def test_apply_refuses_map_inside_out(tmp_path):
    path = tmp_path / "in.parquet"
    pq.write_table(_tiny_table(), path)
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"unit-test-secret")
    out = tmp_path / "release_new"
    proc = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--apply", "--input", str(path),
         "--out", str(out), "--secret-file", str(secret),
         "--private-map", str(out / "map.csv")],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert not out.exists()
