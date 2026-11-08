#!/usr/bin/env python3
"""s4_build_llm_batches.py · 补抽 21-key LLM 输入批构造（fork 自 04 的 build 段）

与 2014–2021 全量抽取的两点已文档化差异（其余逐字段同构）:
1. 不做残差筛选 —— prior_* 数组字段本就必须 LLM，每个 case 都要过 LLM
   （与 2014–2021 全量抽取最终 residual+non-residual 并集口径一致）。
2. 按 judge_year 分片输出 y{Y}_b200/batch_%06d.json（每年独立计数器），
   并做全局 case_id 去重（首见保留），杜绝 duplicate_id gate 触发。

输入: --input case_events_regex_extracted (hive parquet dataset)
输出: --batch-root/y{Y}_b200/batch_NNNNNN.json (每文件 ≤200 item 的 JSON 数组)
      --batch-root/s4_build_stats.json
item 五键: id · defendant_segment[:1500] · facts_segment[:1500] · judgment_segment[:1000]
           · regex_extracted{12 hint 字段}
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pandas as pd
import pyarrow.dataset as ds

BATCH_SIZE_DEFAULT = 200
MIN_DEFENDANT_SEG = 50


def _safe(v, kind=None):
    if pd.isna(v):
        return None
    if kind == "int":
        try:
            return int(v)
        except Exception:
            return None
    if kind == "float":
        try:
            return float(v)
        except Exception:
            return None
    if kind == "bool":
        return bool(v)
    return v


def build_item(r: dict) -> dict:
    """与 04_build_residual_batches_2014_2021.py 的 build_batch_input 逐字段同构的五键 item。"""
    return {
        "id": r["case_id"],
        "defendant_segment": (r.get("defendant_segment") or "")[:1500],
        "facts_segment": (r.get("facts_segment") or "")[:1500],
        "judgment_segment": (r.get("judgment_segment") or "")[:1000],
        "regex_extracted": {
            "sex": _safe(r.get("sex")),
            "birth_year": _safe(r.get("birth_year"), "int"),
            "edu_level": _safe(r.get("edu_level")),
            "prior_occupation_raw": _safe(r.get("prior_occupation_raw")),
            "hukou_text": _safe(r.get("hukou_text")),
            "sentence_type": _safe(r.get("sentence_type")),
            "sentence_months": _safe(r.get("sentence_months"), "float"),
            "fine_yuan": _safe(r.get("fine_yuan"), "int"),
            "has_recidivism_history": _safe(r.get("has_recidivism_history"), "bool"),
            "has_self_surrender": _safe(r.get("has_self_surrender"), "bool"),
            "has_truthful_confession": _safe(r.get("has_truthful_confession"), "bool"),
            "has_plea_leniency": _safe(r.get("has_plea_leniency"), "bool"),
        },
    }


NEED_COLS = ["case_id", "judge_year",
             "defendant_segment", "facts_segment", "judgment_segment",
             "sex", "birth_year", "edu_level", "prior_occupation_raw", "hukou_text",
             "sentence_type", "sentence_months", "fine_yuan",
             "has_recidivism_history", "has_self_surrender",
             "has_truthful_confession", "has_plea_leniency"]


class YearShard:
    def __init__(self, root: Path, year: int, batch_size: int):
        self.dir = root / f"y{year}_b200"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.batch_size = batch_size
        self.pool: list[dict] = []
        self.idx = 0
        self.rows = 0

    def add(self, item: dict):
        self.pool.append(item)
        self.rows += 1
        if len(self.pool) >= self.batch_size:
            self.flush()

    def flush(self):
        if not self.pool:
            return
        out = self.dir / f"batch_{self.idx:06d}.json"
        out.write_text(json.dumps(self.pool, ensure_ascii=False), encoding="utf-8")
        self.idx += 1
        self.pool = []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, required=True,
                    help="case_events_regex_extracted dataset 根目录")
    ap.add_argument("--batch-root", type=Path, required=True)
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE_DEFAULT)
    args = ap.parse_args()

    t0 = time.time()
    d = ds.dataset(args.input, format="parquet", partitioning="hive")
    args.batch_root.mkdir(parents=True, exist_ok=True)

    shards: dict[int, YearShard] = {}
    seen: set[str] = set()
    stats = {"rows_scanned": 0, "rows_no_defendant_seg": 0,
             "rows_dup_case_id": 0, "rows_batched": 0, "per_year": {}}

    fragments = list(d.get_fragments())
    print(f"[s4] {len(fragments)} fragments", flush=True)
    for i, frag in enumerate(fragments):
        df = frag.to_table().to_pandas()
        n_before = len(df)
        stats["rows_scanned"] += n_before
        df = df[df["defendant_segment"].fillna("").str.len() > MIN_DEFENDANT_SEG]
        stats["rows_no_defendant_seg"] += n_before - len(df)
        keep = [c for c in NEED_COLS if c in df.columns]
        for r in df[keep].to_dict("records"):
            cid = r.get("case_id")
            if not cid or not isinstance(cid, str):
                continue
            if cid in seen:
                stats["rows_dup_case_id"] += 1
                continue
            seen.add(cid)
            try:
                jy = int(r.get("judge_year"))
            except Exception:
                continue
            sh = shards.get(jy)
            if sh is None:
                sh = shards[jy] = YearShard(args.batch_root, jy, args.batch_size)
            sh.add(build_item(r))
            stats["rows_batched"] += 1
        if (i + 1) % 200 == 0 or i == len(fragments) - 1:
            print(f"  {i+1}/{len(fragments)} | batched={stats['rows_batched']:,}", flush=True)

    for jy, sh in shards.items():
        sh.flush()
        stats["per_year"][str(jy)] = {"rows": sh.rows, "batches": sh.idx}
    stats["elapsed_s"] = round(time.time() - t0, 1)
    (args.batch_root / "s4_build_stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in stats.items() if k != "per_year"}, ensure_ascii=False))
    print("per_year:", json.dumps(stats["per_year"], ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(main())
