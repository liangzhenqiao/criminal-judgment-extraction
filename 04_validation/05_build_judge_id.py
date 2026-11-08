"""Replace judge_name by judge_id (dry run by default).

judge_id = "J-" + HMAC-SHA256(secret, 规范化姓名) 的前 20 个 hex。
secret 只来自 --secret-file，不写进本脚本。

dry-run 只统计，把 JSON 写到 <validation_out_dir>/judge_id_dryrun.json（或 --json-out）。
真正落盘必须同时给出：

  --apply --out <新的绝对路径目录> --secret-file <路径> --private-map <路径>

--out 不得已存在。--private-map 不得位于 --out 之内。映射与碰撞报告写在数据包外面。
同一文书内不同法官同名，在「一行一个被告人、每行只有一个 judge_name」的表上无法观察，
本脚本不声称已记录这种冲突。

用法（项目根）：
  .venv/bin/python -I 04_validation/05_build_judge_id.py
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import re
import resource
import sys
import time
import unicodedata
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

__import__("sys").path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_config  # paths come from config.yaml (see config.example.yaml)
CFG = pipeline_config.load(handle_help=False, keys=('release_dir', 'validation_out_dir',))

PARQUET_NAMES = ("china_criminal_judgment_records_1985_2021.parquet", "china_criminal_defendant_panel_1985_2021.parquet")  # current release name, then the former one


def _pick(d: Path) -> Path:
    for name in PARQUET_NAMES:
        if (d / name).exists():
            return d / name
    return d / PARQUET_NAMES[0]
HEX20 = 20

_ANON_EXACT = frozenset({
    "x", "xx", "xxx", "未知", "不详", "无名", "无名氏", "匿名", "无", "空",
    "null", "none", "n/a", "na", "×", "××", "×××", "*", "**",
})
_TOKEN = re.compile(r"^\{\{[A-Za-z]+_[A-Za-z0-9]+\}\}$")
_XMASK = re.compile(r"^[一-鿿]{0,2}[Xx]{1,3}$|^[Xx]{1,3}[一-鿿]{0,2}$")


def release_parquet(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    return _pick(CFG.path("release_dir"))


def normalize_label(raw: str) -> str:
    """NFKC，Unicode 空格折成普通空格，去首尾空白，连续空格压成一个。不删内部单空格。"""
    s = unicodedata.normalize("NFKC", raw)
    chars = []
    for ch in s:
        if ch.isspace() or unicodedata.category(ch) == "Zs":
            chars.append(" ")
        else:
            chars.append(ch)
    return re.sub(r" +", " ", "".join(chars)).strip()


def is_anonymous(normalized: str) -> bool:
    if normalized == "":
        return True
    if normalized.casefold() in _ANON_EXACT:
        return True
    if any(mark in normalized for mark in ("某", "×", "✕", "＊")):
        return True
    if _XMASK.fullmatch(normalized):
        return True
    if _TOKEN.fullmatch(normalized):
        return True
    if re.fullmatch(r"[Xx*＊.\-—_]+", normalized):
        return True
    return False


def judge_id_for(secret: bytes, normalized: str) -> str | None:
    """匿名或空标签返回 None。不读其它字段。"""
    if is_anonymous(normalized):
        return None
    digest = hmac.new(secret, normalized.encode("utf-8"), hashlib.sha256).hexdigest()
    return "J-" + digest[:HEX20]


def load_secret(path: Path) -> bytes:
    data = path.read_bytes()
    if data.endswith(b"\n"):
        data = data[:-1]
    if data.endswith(b"\r"):
        data = data[:-1]
    if not data:
        raise SystemExit(f"secret 文件是空的: {path}")
    return data


def rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return rss / (1024 * 1024)
    return rss / 1024


def _accumulate(raw: str | None, groups: dict[str, dict[str, int]], counts: dict) -> None:
    if raw is None:
        counts["n_null"] += 1
        return
    if raw.strip() == "":
        counts["n_blank"] += 1
        counts["n_anonymous_rows"] += 1
        return
    counts["n_nonempty"] += 1
    norm = normalize_label(raw)
    anon = is_anonymous(norm)
    g = groups.get(norm)
    if g is None:
        groups[norm] = {"__rows": 1, raw: 1, "__anon": 1 if anon else 0}
    else:
        g["__rows"] += 1
        g[raw] = g.get(raw, 0) + 1
    if anon:
        counts["n_anonymous_rows"] += 1


def summarise(groups: dict[str, dict], counts: dict, n_rows: int, elapsed: float, parquet: Path) -> dict:
    n_unique_norm = len(groups)
    n_collisions = 0
    n_collisions_assigned = 0
    n_ids = 0
    for norm, g in groups.items():
        raw_variants = [k for k in g if not k.startswith("__")]
        anon = bool(g.get("__anon"))
        if len(raw_variants) >= 2:
            n_collisions += 1
            if not anon:
                n_collisions_assigned += 1
        if not anon:
            n_ids += 1
    return {
        "mode": "dry-run",
        "input": str(parquet),
        "n_rows": n_rows,
        "n_columns_expected_note": "apply 时删除 judge_name，在原位置写入 judge_id；其余列不改值、不改类型；保留 judge_role",
        "n_nonempty_judge_name_rows": counts["n_nonempty"],
        "n_null_judge_name_rows": counts["n_null"],
        "n_blank_judge_name_rows": counts["n_blank"],
        "n_unique_normalized_labels": n_unique_norm,
        "n_anonymous_rows": counts["n_anonymous_rows"],
        "n_normalization_collisions": n_collisions,
        "n_normalization_collisions_assigned_id": n_collisions_assigned,
        "n_expected_unique_judge_id": n_ids,
        "same_document_homonym_observable": False,
        "same_document_homonym_note": (
            "释出表一行一个被告人，每行只有一个 judge_name。"
            "同一文书里两名法官印成同一个姓名时，这一列看不出来。"
            "dry-run 与 apply 都不记录、也不解决这种冲突。"
            "不能写成「同文书重名冲突已记录」。"
        ),
        "definitions": {
            "n_nonempty_judge_name_rows": "judge_name 非 null 且 strip 后非空的行数",
            "n_unique_normalized_labels": "上述非空行规范化后的不同标签数，含匿名标签，不含纯空白",
            "n_anonymous_rows": "匿名标签行数，含纯空白；不含 null。匿名标签不分配 judge_id",
            "n_normalization_collisions": "规范化后相同、原始字符串不同的标签组数（含匿名标签）",
            "n_expected_unique_judge_id": "会分配 judge_id 的规范化标签数（已去掉匿名标签）",
            "hmac": "HMAC-SHA256(secret, normalized label) 取前 20 hex，前缀 J-。公开脚本不含 secret",
        },
        "elapsed_sec": round(elapsed, 1),
        "peak_rss_mb": round(rss_mb(), 1),
        "apply_not_run": True,
    }


def run_dry(parquet: Path) -> dict:
    pf = pq.ParquetFile(parquet)
    if "judge_name" not in pf.schema_arrow.names:
        raise SystemExit("输入没有 judge_name 列")
    if "judge_role" not in pf.schema_arrow.names:
        raise SystemExit("输入没有 judge_role 列")
    t0 = time.perf_counter()
    groups: dict[str, dict] = {}
    counts = {"n_null": 0, "n_blank": 0, "n_nonempty": 0, "n_anonymous_rows": 0}
    n_seen = 0
    for batch in pf.iter_batches(batch_size=65536, columns=["judge_name"]):
        for raw in batch.column(0).to_pylist():
            _accumulate(raw, groups, counts)
        n_seen += batch.num_rows
    if n_seen != pf.metadata.num_rows:
        raise SystemExit(f"行数不一致: {n_seen} vs {pf.metadata.num_rows}")
    elapsed = time.perf_counter() - t0
    out = summarise(groups, counts, n_seen, elapsed, parquet)
    out["n_columns"] = pf.metadata.num_columns
    out["judge_name_index"] = pf.schema_arrow.get_field_index("judge_name")
    out["judge_role_index"] = pf.schema_arrow.get_field_index("judge_role")
    return out


def _check_apply_paths(out: Path, private_map: Path, parquet: Path) -> None:
    if not out.is_absolute():
        raise SystemExit("--out 必须是绝对路径")
    if out.exists():
        raise SystemExit(f"--out 已存在，拒绝覆盖: {out}")
    if private_map.parent.resolve() == Path("/"):
        raise SystemExit("--private-map 必须是文件路径")
    if not private_map.is_absolute():
        raise SystemExit("--private-map 必须是绝对路径")
    out_r = out.resolve()
    map_r = private_map.resolve()
    if out_r == map_r or out_r in map_r.parents:
        raise SystemExit("--private-map 不得位于 --out 之内")
    src_dir = parquet.parent.resolve()
    # 只拦现有释出目录。测试用的小 parquet 不在 data_cleaning 下，允许写到别处。
    if "data_cleaning" in src_dir.parts:
        if out_r == src_dir or src_dir in out_r.parents:
            raise SystemExit("拒绝把新版本写进现有释出目录内部或自身")
        if src_dir == map_r or src_dir in map_r.parents:
            raise SystemExit("拒绝把映射写进现有释出目录")


def collision_report_path(private_map: Path) -> Path:
    return private_map.with_name(private_map.name + ".collisions.json")


def run_apply(parquet: Path, out: Path, secret: bytes, private_map: Path) -> dict:
    _check_apply_paths(out, private_map, parquet)
    private_map.parent.mkdir(parents=True, exist_ok=True)
    pf = pq.ParquetFile(parquet)
    schema = pf.schema_arrow
    idx = schema.get_field_index("judge_name")
    if idx < 0:
        raise SystemExit("输入没有 judge_name 列")
    if schema.get_field_index("judge_role") < 0:
        raise SystemExit("输入没有 judge_role 列")
    names = list(schema.names)
    new_names = names.copy()
    new_names[idx] = "judge_id"
    if new_names.count("judge_id") != 1:
        raise SystemExit("schema 里将出现重复的 judge_id")

    groups: dict[str, dict] = {}
    counts = {"n_null": 0, "n_blank": 0, "n_nonempty": 0, "n_anonymous_rows": 0}
    # 第一遍只建映射，避免边写边发现碰撞时文件已经落在 out 里
    for batch in pf.iter_batches(batch_size=65536, columns=["judge_name"]):
        for raw in batch.column(0).to_pylist():
            _accumulate(raw, groups, counts)

    id_of_norm: dict[str, str | None] = {}
    collisions = []
    with private_map.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["judge_name_raw", "normalized", "judge_id", "anonymous"])
        for norm, g in groups.items():
            jid = judge_id_for(secret, norm)
            id_of_norm[norm] = jid
            raw_variants = [k for k in g if not k.startswith("__")]
            anon = bool(g.get("__anon")) or jid is None
            for raw in raw_variants:
                w.writerow([raw, norm, jid or "", "1" if anon else "0"])
            if len(raw_variants) >= 2:
                collisions.append({
                    "normalized": norm,
                    "anonymous": anon,
                    "raw_variants": sorted(raw_variants),
                    "n_rows": g["__rows"],
                })
    collision_report_path(private_map).write_text(
        json.dumps({
            "n_groups": len(collisions),
            "same_document_homonym_observable": False,
            "note": "只含规范化合并。同一文书内不同人同名无法从单列观察，此处没有这种记录。",
            "groups": collisions,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    out.mkdir(parents=False)
    new_fields = []
    for i, field in enumerate(schema):
        if i == idx:
            new_fields.append(pa.field("judge_id", field.type))
        else:
            new_fields.append(field)
    new_schema = pa.schema(new_fields, metadata=schema.metadata)
    writer = pq.ParquetWriter(out / PARQUET_NAMES[0], new_schema)
    n_seen = 0
    try:
        for batch in pf.iter_batches(batch_size=65536):
            raws = batch.column(idx).to_pylist()
            ids = []
            for raw in raws:
                if raw is None or str(raw).strip() == "":
                    ids.append(None)
                else:
                    ids.append(id_of_norm[normalize_label(str(raw))])
            arr = pa.array(ids, type=schema.field(idx).type)
            batch = batch.set_column(idx, new_schema.field(idx), arr)
            writer.write_batch(batch)
            n_seen += len(raws)
    finally:
        writer.close()
    if n_seen != pf.metadata.num_rows:
        raise SystemExit(f"写入行数不一致: {n_seen}")
    # 映射文件不得出现在 out 里
    if any(p.name.startswith(private_map.name) for p in out.iterdir()):
        raise SystemExit("映射文件进入了数据包目录")
    summary = summarise(groups, counts, n_seen, 0.0, parquet)
    summary["mode"] = "apply"
    summary["apply_not_run"] = False
    summary["out"] = str(out)
    summary["private_map"] = str(private_map)
    summary["collision_report"] = str(collision_report_path(private_map))
    summary["n_columns"] = len(new_schema)
    summary["judge_id_index"] = idx
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=None)
    ap.add_argument("--json-out", default=None, help="默认 <validation_out_dir>/judge_id_dryrun.json")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--out", default=None)
    ap.add_argument("--secret-file", default=None)
    ap.add_argument("--private-map", default=None)
    args = ap.parse_args()
    parquet = release_parquet(args.input)
    if not parquet.is_file():
        raise SystemExit(f"找不到 parquet: {parquet}")

    if not args.apply:
        if args.out or args.secret_file or args.private_map:
            raise SystemExit("没有 --apply 时不允许 --out / --secret-file / --private-map")
        result = run_dry(parquet)
        dest = Path(args.json_out) if args.json_out else CFG.path("validation_out_dir") / "judge_id_dryrun.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({k: result[k] for k in (
            "n_rows", "n_nonempty_judge_name_rows", "n_unique_normalized_labels",
            "n_anonymous_rows", "n_normalization_collisions", "n_expected_unique_judge_id",
            "elapsed_sec", "peak_rss_mb", "apply_not_run",
        )}, ensure_ascii=False))
        return

    missing = [name for name, val in (("--out", args.out), ("--secret-file", args.secret_file), ("--private-map", args.private_map)) if not val]
    if missing:
        raise SystemExit("apply 缺少参数: " + " ".join(missing))
    secret_path = Path(args.secret_file)
    if not secret_path.is_file():
        raise SystemExit(f"找不到 secret 文件: {secret_path}")
    secret = load_secret(secret_path)
    result = run_apply(parquet, Path(args.out), secret, Path(args.private_map))
    print(json.dumps({
        "mode": "apply",
        "n_rows": result["n_rows"],
        "n_expected_unique_judge_id": result["n_expected_unique_judge_id"],
        "out": result["out"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
