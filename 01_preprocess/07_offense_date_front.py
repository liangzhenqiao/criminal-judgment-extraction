#!/usr/bin/env python3
"""s5_offense_pipeline.py · 作案时间 hybrid 前半（regex 前置 + 残差批构造）

三步（与 offense_date_v1.0 流程同构）:
  A. 从 case_events_segmented 生成薄包 facts_only_part_*.jsonl.gz（{id, facts_segment}）
  B. 调 06_extract_offense_dates_regex.py --pack <pack>（严格 YMD+作案语境 regex；
     顺带程序性时间 5 字段）→ <pack>/offense_date_regex_20260703.parquet
  C. regex_status != "ok" 的行 → offense LLM 残差批 batch_%06d.json
     （{id, facts_segment} 200/批, 供 offense_date_v1.0 profile runner 消费）

输出: --pack 目录（薄包+regex parquet）· --batch-dir 残差批 · s5_offense_stats.json
"""
from __future__ import annotations

import argparse
import gzip
import json
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import pyarrow.dataset as ds
import pyarrow.parquet as pq

PART_ROWS = 200_000


def step_a_thin_pack(segmented: Path, pack: Path, seen_guard: set[str]) -> int:
    pack.mkdir(parents=True, exist_ok=True)
    d = ds.dataset(segmented, format="parquet", partitioning="hive")
    n = 0
    part_idx = 0
    buf: list[str] = []

    def flush():
        nonlocal part_idx, buf
        if not buf:
            return
        out = pack / f"facts_only_part_{part_idx:04d}.jsonl.gz"
        with gzip.open(out, "wt", encoding="utf-8") as f:
            f.write("\n".join(buf) + "\n")
        part_idx += 1
        buf = []

    for frag in d.get_fragments():
        df = frag.to_table().to_pandas()
        for cid, fs in zip(df["case_id"], df["facts_segment"].fillna("")):
            if not isinstance(cid, str) or not cid or cid in seen_guard:
                continue
            seen_guard.add(cid)
            buf.append(json.dumps({"id": cid, "facts_segment": fs}, ensure_ascii=False))
            n += 1
            if len(buf) >= PART_ROWS:
                flush()
    flush()
    return n


def step_c_residual_batches(pack: Path, batch_dir: Path, batch_size: int = 200) -> dict:
    regex_pq = next(pack.glob("offense_date_regex_*.parquet"))
    reg = pq.read_table(regex_pq, columns=["id", "regex_status"]).to_pandas()
    residual_ids = set(reg.loc[reg["regex_status"] != "ok", "id"])
    stats = {"regex_rows": int(len(reg)),
             "regex_ok": int((reg["regex_status"] == "ok").sum()),
             "status_counts": reg["regex_status"].value_counts().to_dict(),
             "residual_rows": len(residual_ids)}
    stats["regex_ok_rate"] = round(stats["regex_ok"] / max(stats["regex_rows"], 1), 4)

    batch_dir.mkdir(parents=True, exist_ok=True)
    pool: list[dict] = []
    idx = 0
    written = 0

    def flush():
        nonlocal idx, pool, written
        if not pool:
            return
        (batch_dir / f"batch_{idx:06d}.json").write_text(
            json.dumps(pool, ensure_ascii=False), encoding="utf-8")
        idx += 1
        written += len(pool)
        pool = []

    for p in sorted(pack.glob("facts_only_part_*.jsonl.gz")):
        with gzip.open(p, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                if d["id"] in residual_ids:
                    pool.append(d)
                    if len(pool) >= batch_size:
                        flush()
    flush()
    stats["residual_batches"] = idx
    stats["residual_rows_batched"] = written
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--segmented", type=Path, required=True)
    ap.add_argument("--pack", type=Path, required=True)
    ap.add_argument("--batch-dir", type=Path, required=True)
    ap.add_argument("--batch-size", type=int, default=200)
    args = ap.parse_args()

    t0 = time.time()
    print("[s5-A] thin pack ...", flush=True)
    n = step_a_thin_pack(args.segmented, args.pack, set())
    print(f"  facts rows: {n:,}")

    print("[s5-B] offense regex ...", flush=True)
    s5b = Path(__file__).resolve().parent / "06_extract_offense_dates_regex.py"
    subprocess.run([sys.executable, str(s5b), "--pack", str(args.pack)], check=True)

    print("[s5-C] residual batches ...", flush=True)
    stats = step_c_residual_batches(args.pack, args.batch_dir, args.batch_size)
    stats["facts_rows"] = n
    stats["elapsed_s"] = round(time.time() - t0, 1)
    (args.pack / "s5_offense_stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    raise SystemExit(main())
