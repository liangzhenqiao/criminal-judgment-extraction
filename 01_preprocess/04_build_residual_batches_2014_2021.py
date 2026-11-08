"""
02c_llm_extract_residual.py · NER Layer 3 LLM 残差抽取

筛选 Layer 2 regex 抽取**未达标**的记录, 打包成 batched JSON 发给 Fabric+LLM 处理。

设计:
- Batch size: 默认 10 条/请求 (摊薄 system.md overhead)
- 调用方式: HTTP POST 到 fabric serve (localhost:8080) 调用 pattern criminal_defendant
- 残差判定: 任一关键字段缺失 → 进入残差池
  必填: sex, birth_year, edu_level, prior_occupation_raw, hukou_text
  缺任一 → LLM 补

输入: 03_masterdata/case_events_regex_extracted/
输出:
- 03_masterdata/llm_input_batches/batch_NNNN.json (上游打包)
- 03_masterdata/llm_output/criminal_defendant/batch_NNNN.json (下游 LLM 输出)
- 03_masterdata/llm_extraction_report.csv

使用:
  # 步骤 1: 仅生成 batch JSON 文件 (本地, 不调 LLM)
  python 02_scripts/02c_llm_extract_residual.py --build-batches-only

  # 步骤 2: dry-run 测试连通性 (调 Fabric serve, 单 batch)
  python 02_scripts/02c_llm_extract_residual.py --dry-run

  # 步骤 3: 实际全量跑 (需用户授权)
  python 02_scripts/02c_llm_extract_residual.py --run-llm
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import aiohttp
import pandas as pd
import pyarrow.dataset as ds

REQUIRED_FIELDS = ["sex", "birth_year", "edu_level",
                   "prior_occupation_raw", "hukou_text"]

FABRIC_URL = "http://localhost:8080/chat"
PATTERN_NAME = "criminal_defendant"
FABRIC_PATTERN_DIR = Path.home() / ".config/fabric/patterns"


def assert_pattern_installed() -> None:
    """前置检查：Fabric pattern 必须存在，否则 Fabric 静默返回空 content."""
    p = FABRIC_PATTERN_DIR / PATTERN_NAME / "system.md"
    if not p.exists():
        raise SystemExit(
            f"[FATAL] Fabric pattern 未安装: {p}\n"
            f"       请先 cp 02_scripts/patterns/{PATTERN_NAME}/system.md → {p.parent}/"
        )


def strip_markdown_fence(text: str) -> str:
    """LLM 偶尔用 ```json ... ``` 包裹 JSON 输出，剥掉 fence 以便下游 json.loads."""
    s = text.strip()
    if s.startswith("```"):
        # 去掉开头 fence 行
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1:]
        # 去掉结尾 fence
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3].rstrip()
    return s


def is_residual(row) -> bool:
    """判断一行是否需要送 LLM."""
    return any(pd.isna(row[c]) for c in REQUIRED_FIELDS if c in row)


def _safe(v, kind=None):
    """统一 NaN → None；kind='int' 强制整数化."""
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
        # pandas 可能给 np.bool_；转 Python bool
        return bool(v)
    return v


def build_batch_input(rows: list[dict]) -> list[dict]:
    """构造 LLM 输入数组（纳入 facts_segment + 扩展 regex_extracted hint 至 12 字段）."""
    batch = []
    for r in rows:
        item = {
            "id": r["case_id"],
            "defendant_segment": (r.get("defendant_segment") or "")[:1500],
            "facts_segment": (r.get("facts_segment") or "")[:1500],
            "judgment_segment": (r.get("judgment_segment") or "")[:1000],
            "regex_extracted": {
                # 被告段 regex 字段
                "sex": _safe(r.get("sex")),
                "birth_year": _safe(r.get("birth_year"), "int"),
                "edu_level": _safe(r.get("edu_level")),
                "prior_occupation_raw": _safe(r.get("prior_occupation_raw")),
                "hukou_text": _safe(r.get("hukou_text")),
                # 判决段 regex 字段
                "sentence_type": _safe(r.get("sentence_type")),
                "sentence_months": _safe(r.get("sentence_months"), "float"),
                "fine_yuan": _safe(r.get("fine_yuan"), "int"),
                # 布尔字段（regex 已抽）
                "has_recidivism_history": _safe(r.get("has_recidivism_history"), "bool"),
                "has_self_surrender": _safe(r.get("has_self_surrender"), "bool"),
                "has_truthful_confession": _safe(r.get("has_truthful_confession"), "bool"),
                "has_plea_leniency": _safe(r.get("has_plea_leniency"), "bool"),
            }
        }
        batch.append(item)
    return batch


# LLM 输出 schema 的字段清单（下游合并脚本消费；此处仅作记录，不参与抽取）
LLM_OUTPUT_FIELDS = [
    "id", "sex", "birth_year", "ethnicity", "edu_level",
    "prior_occupation_raw", "hukou_text",
    "has_prior_conviction", "prior_conviction_desc", "is_recidivist_legal", "party_role",
    "sentence_months", "sentence_type", "probation_flag", "fine_amount_yuan",
]


# pending_rows 需传给 build_batch_input 的全部列（含 facts_segment + 判决段字段）
BATCH_INPUT_COLS = [
    "case_id", "defendant_name", "judge_year",
    "defendant_segment", "facts_segment", "judgment_segment",
    # defendant 段 regex
    "sex", "birth_year", "edu_level", "prior_occupation_raw", "hukou_text",
    # judgment 段 regex
    "sentence_type", "sentence_months", "fine_yuan",
    # 布尔
    "has_recidivism_history", "has_self_surrender",
    "has_truthful_confession", "has_plea_leniency",
]


def build_batches(input_dataset: Path, output_batch_dir: Path,
                   batch_size: int = 10, sample_n: int | None = None,
                   cohort_whitelist: set | None = None,
                   year_range: tuple | None = None) -> dict:
    """筛选残差 + 分 batch 写 JSON 文件。

    附加过滤:
    - cohort_whitelist: set of (case_id, defendant_name) tuples; 若提供，只保留白名单内的行（dedup 到 first-judgment）
    - year_range: (start_year, end_year) 仅处理 judge_year 在此区间的行
    """
    d = ds.dataset(input_dataset, format="parquet", partitioning="hive")
    output_batch_dir.mkdir(parents=True, exist_ok=True)
    fragments = list(d.get_fragments())
    if sample_n:
        fragments = fragments[:sample_n]

    stats = {"total_rows": 0, "residual_rows": 0, "batches_written": 0,
             "skipped_not_in_cohort": 0, "skipped_not_in_year": 0}
    pending_rows = []
    batch_idx = 0

    for i, frag in enumerate(fragments):
        df = frag.to_table().to_pandas()
        stats["total_rows"] += len(df)

        # 年份过滤（若指定）
        if year_range is not None and "judge_year" in df.columns:
            before = len(df)
            df = df[(df["judge_year"] >= year_range[0]) & (df["judge_year"] <= year_range[1])]
            stats["skipped_not_in_year"] += before - len(df)

        # cohort 白名单过滤（dedup 到 first-judgment per defendant）
        if cohort_whitelist is not None and "case_id" in df.columns and "defendant_name" in df.columns:
            before = len(df)
            mask = [
                (cid, dname) in cohort_whitelist
                for cid, dname in zip(df["case_id"], df["defendant_name"].fillna(""))
            ]
            df = df[mask]
            stats["skipped_not_in_cohort"] += before - len(df)

        if len(df) == 0:
            continue

        # 仅保留有 defendant_segment 的（无段则无信息可补）
        df = df[df["defendant_segment"].fillna("").str.len() > 50]
        # 残差筛选
        residual = df[df.apply(is_residual, axis=1)]
        stats["residual_rows"] += len(residual)

        # 转 records（保留 build_batch_input 所需全部列）
        keep_cols = [c for c in BATCH_INPUT_COLS if c in residual.columns]
        pending_rows.extend(residual[keep_cols].to_dict("records"))

        # 写 batch 直到 pool < batch_size
        while len(pending_rows) >= batch_size:
            batch = pending_rows[:batch_size]
            pending_rows = pending_rows[batch_size:]
            batch_input = build_batch_input(batch)
            out_file = output_batch_dir / f"batch_{batch_idx:06d}.json"
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(batch_input, f, ensure_ascii=False)
            batch_idx += 1
            stats["batches_written"] += 1

    # 残余 < batch_size
    if pending_rows:
        out_file = output_batch_dir / f"batch_{batch_idx:06d}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(build_batch_input(pending_rows), f, ensure_ascii=False)
        stats["batches_written"] += 1

    return stats


def load_cohort_whitelist(cohort_parquet: Path) -> set:
    """读 cohort parquet, 返回 (case_id, defendant_name) set."""
    df = pd.read_parquet(cohort_parquet, columns=["case_id", "defendant_name"])
    wl = set(zip(df["case_id"], df["defendant_name"].fillna("")))
    print(f"[cohort-filter] loaded {len(wl):,} (case_id, defendant_name) pairs from {cohort_parquet.name}")
    return wl


async def call_fabric(session: aiohttp.ClientSession, batch: list[dict],
                       model: str, vendor: str = "openai",
                       pattern: str = PATTERN_NAME) -> str:
    """调 Fabric serve 处理一个 batch."""
    # 16 字段输出更长, 每条 ~500-700 字符 ≈ 300 tokens；batch=15 需 ~4.5K；给 2x 余量
    max_tokens = max(4096, len(batch) * 600)
    payload = {
        "prompts": [{
            "userInput": json.dumps(batch, ensure_ascii=False),
            "patternName": pattern,
            "model": model,
            "vendor": vendor,
        }],
        "temperature": 0.0,
        "maxTokens": max_tokens,
    }
    async with session.post(FABRIC_URL, json=payload, timeout=300) as resp:
        text = ""
        async for line in resp.content:
            ln = line.decode("utf-8").strip()
            if ln.startswith("data:"):
                try:
                    data = json.loads(ln[5:])
                    text += data.get("content", "")
                except Exception:
                    pass
        return text


def _is_output_valid(out_path: Path) -> bool:
    """已存在且能 JSON 解析 + 含 'data' 数组 → 视为有效，可 skip."""
    if not out_path.exists() or out_path.stat().st_size == 0:
        return False
    try:
        txt = strip_markdown_fence(out_path.read_text(encoding="utf-8"))
        obj = json.loads(txt)
        return isinstance(obj, dict) and isinstance(obj.get("data"), list) and len(obj["data"]) > 0
    except Exception:
        return False


async def run_one_batch(batch_path: Path, output_dir: Path,
                          model: str, session: aiohttp.ClientSession,
                          max_retries: int = 3) -> str:
    """跑单个 batch 并保存 LLM 输出。返回 'skip'/'ok'/'fail'。"""
    out = output_dir / batch_path.name
    # 断点续传: 已完成且有效 → skip
    if _is_output_valid(out):
        return "skip"

    with open(batch_path, encoding="utf-8") as f:
        batch = json.load(f)

    for attempt in range(max_retries):
        try:
            result = await call_fabric(session, batch, model=model)
            result = strip_markdown_fence(result)
            if not result.strip():
                raise RuntimeError("empty LLM response")
            # 预验证: 能解析 + 含 data
            obj = json.loads(result)
            if not (isinstance(obj, dict) and isinstance(obj.get("data"), list)):
                raise RuntimeError(f"invalid shape: {type(obj).__name__}")
            out.write_text(result, encoding="utf-8")
            return "ok"
        except Exception as e:
            print(f"[WARN] {batch_path.name} attempt {attempt+1}/{max_retries}: {e!r}")
            await asyncio.sleep(3)
    print(f"[ERROR] {batch_path.name} FAILED after {max_retries} attempts")
    return "fail"


async def run_llm_async(batch_dir: Path, output_dir: Path,
                          model: str, concurrent: int = 8,
                          dry_run: bool = False,
                          resume: bool = True,
                          progress_path: Path | None = None,
                          limit: int | None = None) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    batches = sorted(batch_dir.glob("batch_*.json"))
    if dry_run:
        batches = batches[:1]
        print(f"[dry-run] 只跑 1 个 batch")
    elif limit is not None and limit > 0:
        batches = batches[:limit]
        print(f"[limit] 只跑前 {limit} 个 batch（pilot 模式）")

    # 预扫描已完成的 batch（resume 模式）
    already_done = 0
    to_run = []
    if resume:
        for b in batches:
            if _is_output_valid(output_dir / b.name):
                already_done += 1
            else:
                to_run.append(b)
        print(f"[resume] 总 batches: {len(batches)}; 已完成: {already_done}; 待跑: {len(to_run)}")
    else:
        to_run = batches
        print(f"[run] {len(to_run)} batches (resume=off, 全量重跑)")

    print(f"[model] {model}; concurrent={concurrent}")

    sem = asyncio.Semaphore(concurrent)

    async def with_sem(b, session):
        async with sem:
            return await run_one_batch(b, output_dir, model, session)

    success = already_done
    skipped = already_done
    failed: list[str] = []
    async with aiohttp.ClientSession() as session:
        tasks = [with_sem(b, session) for b in to_run]
        for i, t in enumerate(asyncio.as_completed(tasks)):
            status = await t
            if status == "ok":
                success += 1
            elif status == "skip":
                skipped += 1
                success += 1
            elif status == "fail":
                failed.append(f"batch_{i}")
            # 每 20 个输出进度 + 落盘 progress.json
            if (i + 1) % 20 == 0 or (i + 1) == len(to_run):
                pct = 100 * (success) / max(len(batches), 1)
                print(f"  progress: {i+1}/{len(to_run)} run-new, {success}/{len(batches)} total ({pct:.1f}%), failed={len(failed)}")
                if progress_path is not None:
                    progress_path.write_text(json.dumps({
                        "total": len(batches),
                        "completed": success,
                        "skipped_resumed": already_done,
                        "failed_count": len(failed),
                        "failed_samples": failed[:20],
                        "pct": pct,
                        "model": model,
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"total": len(batches), "success": success,
            "skipped_resumed": already_done, "failed": failed}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path,
                        default=Path("03_masterdata/case_events_regex_extracted"))
    parser.add_argument("--batch-dir", type=Path,
                        default=Path("03_masterdata/llm_input_batches"))
    parser.add_argument("--output-dir", type=Path,
                        default=Path("03_masterdata/llm_output/criminal_defendant"))
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--sample-fragments", type=int, default=None)
    parser.add_argument("--model", default="deepseek/deepseek-chat-v3-0324")
    parser.add_argument("--concurrent", type=int, default=8)
    parser.add_argument("--limit", type=int, default=None,
                        help="仅跑前 N 个 batch（pilot/小规模测试用；配合 --batch-dir 指向已 build 好的目录）")
    parser.add_argument("--build-batches-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="构造 batch + 调 1 个 batch 测试连通性")
    parser.add_argument("--run-llm", action="store_true",
                        help="实际跑全量 LLM（需用户授权）")
    parser.add_argument("--no-resume", action="store_true",
                        help="禁用断点续传，强制从头重跑所有 batches")
    parser.add_argument("--progress-file", type=Path,
                        default=Path("03_masterdata/llm_output/progress.json"),
                        help="落盘进度 json 位置（每 20 个 batch 更新一次）")
    parser.add_argument("--retry-failed-only", action="store_true",
                        help="只针对已经失败/缺失的 batch 重跑（前提：batches 已建）")
    parser.add_argument("--cohort-filter", type=Path, default=None,
                        help="cohort parquet 路径；只处理 (case_id, defendant_name) 在 cohort 里的行（dedup 到 first-judgment）")
    parser.add_argument("--year", type=str, default=None,
                        help="只处理指定 judge_year；格式: YYYY 或 YYYY-YYYY (如 '2014' 或 '2014-2015')")
    args = parser.parse_args()

    # 解析 --year
    year_range = None
    if args.year:
        if "-" in args.year:
            a, b = args.year.split("-")
            year_range = (int(a), int(b))
        else:
            y = int(args.year)
            year_range = (y, y)
        print(f"[year-filter] 只处理 judge_year in {year_range[0]}-{year_range[1]}")

    # 加载 cohort 白名单
    cohort_whitelist = None
    if args.cohort_filter is not None:
        cohort_whitelist = load_cohort_whitelist(args.cohort_filter)

    # 前置检查：pattern 必须存在
    if args.dry_run or args.run_llm:
        assert_pattern_installed()

    t0 = time.time()
    # 跳过 build_batches 的条件（任一满足即跳）：
    #   1. --retry-failed-only 明确声明
    #   2. --batch-dir 已存在且含 batch_*.json 文件（防止错指已有 batch 目录触发重建 +
    #      命名冲突 → 累积数十万文件）
    existing_batches = list(args.batch_dir.glob("batch_*.json")) if args.batch_dir.exists() else []
    skip_build = args.retry_failed_only or len(existing_batches) > 0
    if skip_build:
        reason = "--retry-failed-only" if args.retry_failed_only else f"batch-dir 已有 {len(existing_batches)} 个 batch 文件"
        print(f"[skip-build] {reason} → 使用已有 {args.batch_dir}")
    else:
        print(f"[1/2] 构造残差 batches → {args.batch_dir}")
        stats = build_batches(args.input, args.batch_dir,
                                batch_size=args.batch_size,
                                sample_n=args.sample_fragments,
                                cohort_whitelist=cohort_whitelist,
                                year_range=year_range)
        print(f"  total rows: {stats['total_rows']:,}")
        if cohort_whitelist is not None:
            print(f"  skipped (not in cohort): {stats['skipped_not_in_cohort']:,}")
        if year_range is not None:
            print(f"  skipped (not in year): {stats['skipped_not_in_year']:,}")
        print(f"  residual rows: {stats['residual_rows']:,} "
              f"({stats['residual_rows']/max(stats['total_rows'],1)*100:.1f}%)")
        print(f"  batches written: {stats['batches_written']}")
        print(f"  elapsed: {time.time()-t0:.1f}s")

    if args.build_batches_only:
        print("\n[done] --build-batches-only 模式")
        return 0

    if args.dry_run or args.run_llm:
        print(f"\n[2/2] 调用 Fabric LLM (dry={args.dry_run}, full={args.run_llm})")
        args.progress_file.parent.mkdir(parents=True, exist_ok=True)
        result = asyncio.run(run_llm_async(
            args.batch_dir, args.output_dir,
            model=args.model, concurrent=args.concurrent,
            dry_run=args.dry_run,
            resume=(not args.no_resume),
            progress_path=args.progress_file,
            limit=args.limit,
        ))
        print(f"\n=== final ===")
        print(f"  total: {result['total']}")
        print(f"  success (含 resume skip): {result['success']}")
        print(f"  resume-skipped (本轮未动): {result['skipped_resumed']}")
        print(f"  failed this run: {len(result['failed'])}")
        if result['failed']:
            print(f"  failed samples (首 10): {result['failed'][:10]}")
            print(f"  → 再次 --run-llm (默认 resume 开启) 将只重试 failed/missing")
        return 0

    print("\n[skip] 未指定 --dry-run 或 --run-llm，仅构造了 batches")


if __name__ == "__main__":
    raise SystemExit(main())
