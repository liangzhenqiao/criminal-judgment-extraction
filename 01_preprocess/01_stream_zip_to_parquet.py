"""
01_stream_zip_to_parquet.py
流式解压 zip 内月度 CSV → 过滤刑事案件 → 按裁判日期分区写 Parquet。

- 不落盘解压：zipfile.ZipFile + io 内存解压 → 分块 pandas read
- 过滤：case_type_code == 4（刑事）
- 分区：judge_year × province
- 压缩：snappy
- 输出：--output 指定的 Parquet dataset 根目录

幂等性说明:
- Parquet 文件名按 `{zip_stem}_{csv_stem}_chunk{XXXX}.parquet` 确定性命名
- 只要 CHUNK_SIZE 不变，重跑会覆盖同名文件，不产生重复
- 若中断后续跑，残留的"半完成 CSV"分片不会有重复，但 partial 数据不完整
- 推荐：中断后用 `rm -rf judge_year=YYYY/` 清空该年残留再重跑

断点续传 (--resume) 未实现：跨年匹配阶段需要它时再加

使用:
    python 01_preprocess/01_stream_zip_to_parquet.py \
        --zip <corpus_zip_dir>/2014年裁判文书数据.zip \
        --output archives/parquet_prototype \
        --months 01          # 可选：只跑指定月份（测试用），默认全量
"""
from __future__ import annotations

import os

import argparse
import gc
import io
import json
import sys
import time
import zipfile
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

# CSV 源字段（α 探测验证）
EXPECTED_COLS = [
    "原始链接", "案号", "案件名称", "法院", "所属地区", "案件类型", "案件类型编码",
    "来源", "审理程序", "裁判日期", "公开日期", "当事人", "案由", "法律依据", "全文",
]

# 分块读 CSV，防止大文件撑爆内存
CHUNK_SIZE = 200_000

# 省份/地级市/区县 → 省份名映射（首次调用时 lazy build）
# 行政区划字典路径：环境变量 PCA_JSON_PATH（run_all_preprocess.sh 从 config.yaml 的
# admin_division_json 读取后导出）
PCA_DICT_PATH = Path(os.environ.get("PCA_JSON_PATH", "pca-code.json"))
_REGION_TO_PROV: dict[str, str] | None = None


def build_region_map() -> dict[str, str]:
    """构造 name → province_name 字典（展平 pca-code.json 3 层树）。"""
    global _REGION_TO_PROV
    if _REGION_TO_PROV is not None:
        return _REGION_TO_PROV

    with open(PCA_DICT_PATH, encoding="utf-8") as f:
        tree = json.load(f)

    # 省份规范短名映射（处理民族自治地区）
    PROV_SHORT = {
        "北京市": "北京", "天津市": "天津", "上海市": "上海", "重庆市": "重庆",
        "河北省": "河北", "山西省": "山西", "辽宁省": "辽宁", "吉林省": "吉林",
        "黑龙江省": "黑龙江", "江苏省": "江苏", "浙江省": "浙江", "安徽省": "安徽",
        "福建省": "福建", "江西省": "江西", "山东省": "山东", "河南省": "河南",
        "湖北省": "湖北", "湖南省": "湖南", "广东省": "广东", "海南省": "海南",
        "四川省": "四川", "贵州省": "贵州", "云南省": "云南", "陕西省": "陕西",
        "甘肃省": "甘肃", "青海省": "青海", "台湾省": "台湾",
        "内蒙古自治区": "内蒙古", "广西壮族自治区": "广西",
        "西藏自治区": "西藏", "宁夏回族自治区": "宁夏",
        "新疆维吾尔自治区": "新疆",
        "香港特别行政区": "香港", "澳门特别行政区": "澳门",
    }

    mapping: dict[str, str] = {}
    for prov in tree:
        pname = prov["name"]
        pshort = PROV_SHORT.get(pname, pname)
        mapping[pname] = pshort
        mapping[pshort] = pshort
        for city in prov.get("children", []):
            cname = city["name"]
            mapping.setdefault(cname, pshort)
            for dist in city.get("children", []):
                mapping.setdefault(dist["name"], pshort)
    _REGION_TO_PROV = mapping
    return mapping


def extract_province(region: str, court_name: str = "") -> str:
    """从所属地区 / 法院名称 反查省份（使用 pca 字典展平）。"""
    if not isinstance(region, str):
        region = ""
    if not isinstance(court_name, str):
        court_name = ""
    m = build_region_map()

    # 先试 court_region（最精准：包含区县/市/省名）
    # 按词长倒序试以避免"北"命中"北京" → "北"（简短 key 少的优先）
    for s in (region, court_name):
        if not s:
            continue
        # 直接匹配
        if s in m:
            return m[s]
        # 子字符串匹配（由长到短）
        for key in sorted(m.keys(), key=len, reverse=True):
            if key and key in s:
                return m[key]
    return "UNKNOWN"


# 要保留的 procedure（新判决；排除减刑/假释/执行变更）
PROCEDURE_KEEP = {"一审", "刑事一审", "二审", "刑事二审", "再审", "刑事再审"}


def precheck_zip(zip_path: Path, sample_rows: int = 5000) -> dict:
    """年份预检查：扫描各月 CSV 前 N 行，汇报字段一致性 + 关键分布。

    不写 Parquet；返回字段清单、缺失率、各关键列的取值分布，用于判断该年份
    是否与既定 schema 兼容。
    """
    report = {
        "zip": str(zip_path),
        "source_zip_year": int(zip_path.name[:4]),
        "csv_files": [],
        "columns_union": [],
        "columns_missing_vs_expected": [],
        "columns_extra_vs_expected": [],
        "case_type_distribution": {},
        "procedure_top10": {},
        "judge_year_distribution": {},
        "region_empty_rate": 0.0,
        "sample_rows_each_month": sample_rows,
    }
    all_cols: set[str] = set()
    all_ct: dict[int, int] = {}
    all_proc: dict[str, int] = {}
    all_jy: dict[int, int] = {}
    region_empty = 0
    region_total = 0

    with zipfile.ZipFile(zip_path, "r") as z:
        csv_names = sorted(n for n in z.namelist() if n.endswith(".csv"))
        for csv_name in csv_names:
            report["csv_files"].append(csv_name)
            with z.open(csv_name) as raw_fp:
                try:
                    df = pd.read_csv(
                        raw_fp, nrows=sample_rows, encoding="utf-8",
                        low_memory=False, dtype=str, on_bad_lines="skip"
                    )
                except UnicodeDecodeError:
                    with z.open(csv_name) as raw_fp2:
                        df = pd.read_csv(
                            raw_fp2, nrows=sample_rows, encoding="gb18030",
                            low_memory=False, dtype=str, on_bad_lines="skip"
                        )
            all_cols.update(df.columns)
            if "案件类型编码" in df.columns:
                ct = pd.to_numeric(df["案件类型编码"], errors="coerce").dropna().astype(int)
                for k, v in ct.value_counts().items():
                    all_ct[int(k)] = all_ct.get(int(k), 0) + int(v)
            if "审理程序" in df.columns:
                for k, v in df["审理程序"].fillna("<NA>").value_counts().head(20).items():
                    all_proc[k] = all_proc.get(k, 0) + int(v)
            if "裁判日期" in df.columns:
                jd = pd.to_datetime(df["裁判日期"], errors="coerce").dt.year.dropna().astype(int)
                for k, v in jd.value_counts().items():
                    all_jy[int(k)] = all_jy.get(int(k), 0) + int(v)
            if "所属地区" in df.columns:
                region_total += len(df)
                region_empty += int(df["所属地区"].isna().sum() + (df["所属地区"].str.strip() == "").sum())

    report["columns_union"] = sorted(all_cols)
    report["columns_missing_vs_expected"] = [c for c in EXPECTED_COLS if c not in all_cols]
    report["columns_extra_vs_expected"] = sorted(all_cols - set(EXPECTED_COLS))
    report["case_type_distribution"] = dict(sorted(all_ct.items()))
    report["procedure_top10"] = dict(sorted(all_proc.items(), key=lambda kv: -kv[1])[:10])
    report["judge_year_distribution"] = dict(sorted(all_jy.items()))
    report["region_empty_rate"] = round(region_empty / max(region_total, 1), 4)
    return report


def process_chunk(df: pd.DataFrame, source_zip_year: int,
                   keep_procedure: set[str] | None = None) -> pd.DataFrame:
    """过滤刑事 + 派生字段。

    keep_procedure: 若提供，只保留 审理程序 ∈ 该集合（典型：一审 / 刑事一审）。
    """
    # 1. 过滤刑事
    df = df[df["案件类型编码"] == 4].copy()
    if df.empty:
        return df

    # 1b. 过滤新判决程序（排除减刑假释）
    if keep_procedure:
        df = df[df["审理程序"].isin(keep_procedure)].copy()
        if df.empty:
            return df

    # 2. 解析裁判日期
    df["judge_date"] = pd.to_datetime(df["裁判日期"], errors="coerce")
    df = df[df["judge_date"].notna()].copy()
    # 统一 int32 避免不同 chunk 间 int16/int32 漂移（pyarrow.dataset 合并要求一致）
    df["judge_year"] = df["judge_date"].dt.year.astype("int32")

    # 3. 省份派生（pca 字典 + court_name 兜底）
    df["province"] = [
        extract_province(r, c)
        for r, c in zip(df["所属地区"].fillna(""), df["法院"].fillna(""))
    ]

    # 4. 来源审计
    df["source_zip_year"] = source_zip_year

    # 5. 重命名字段到 snake_case schema
    df = df.rename(columns={
        "原始链接": "case_url",
        "案号": "case_id",
        "案件名称": "case_title",
        "法院": "court_name",
        "所属地区": "court_region",
        "案件类型": "case_type",
        "案件类型编码": "case_type_code",
        "来源": "source_site",
        "审理程序": "procedure",
        "裁判日期": "judge_date_str",
        "公开日期": "disclosure_date",
        "当事人": "parties",
        "案由": "charge_cause",
        "法律依据": "law_basis",
        "全文": "full_text",
    })

    # 6. 精简 dtype
    df["case_type_code"] = df["case_type_code"].astype("int32")
    df["source_zip_year"] = df["source_zip_year"].astype("int32")
    return df


def stream_zip(
    zip_path: Path,
    output_root: Path,
    months_filter: list[str] | None = None,
    keep_procedure: set[str] | None = None,
) -> dict:
    """主流程：遍历 zip 内 CSV → 过滤 → 写分区 Parquet。"""
    source_zip_year = int(zip_path.name[:4])
    stats = {
        "source_zip": str(zip_path),
        "files_processed": 0,
        "rows_total_read": 0,
        "rows_criminal_kept": 0,
        "rows_by_judge_year": {},
        "elapsed_seconds": 0.0,
    }

    t0 = time.time()
    output_root.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as z:
        csv_names = sorted(n for n in z.namelist() if n.endswith(".csv"))
        if months_filter:
            csv_names = [
                n for n in csv_names
                if any(f"{m}月" in n for m in months_filter)
            ]

        print(f"[INFO] {zip_path.name}: {len(csv_names)} CSV files to scan")

        for csv_name in csv_names:
            print(f"[INFO] processing: {csv_name}", flush=True)
            stats["files_processed"] += 1

            with z.open(csv_name) as raw_fp:
                # 大文件用 chunksize 流式读，避免一次性加载
                # pandas read_csv 需要 text-mode 或 binary-mode 包装
                binary = io.BytesIO(raw_fp.read())  # 单文件 2–12 GB，仍在内存可行
                try:
                    reader = pd.read_csv(
                        binary,
                        chunksize=CHUNK_SIZE,
                        encoding="utf-8",
                        low_memory=False,
                        dtype=str,  # 全部读成 string 再派生类型
                        on_bad_lines="skip",
                    )
                except UnicodeDecodeError:
                    binary.seek(0)
                    reader = pd.read_csv(
                        binary,
                        chunksize=CHUNK_SIZE,
                        encoding="gb18030",
                        low_memory=False,
                        dtype=str,
                        on_bad_lines="skip",
                    )

                for chunk_idx, chunk in enumerate(reader):
                    raw_len = len(chunk)
                    stats["rows_total_read"] += raw_len

                    # 字段列名一致性检查（第一 chunk 做）
                    if chunk_idx == 0:
                        actual = list(chunk.columns)
                        missing = [c for c in EXPECTED_COLS if c not in actual]
                        if missing:
                            print(f"[WARN] missing cols: {missing}")

                    # 类型转换 + 过滤
                    chunk["案件类型编码"] = pd.to_numeric(
                        chunk["案件类型编码"], errors="coerce"
                    )
                    processed = process_chunk(chunk, source_zip_year,
                                               keep_procedure=keep_procedure)
                    if processed.empty:
                        continue

                    stats["rows_criminal_kept"] += len(processed)

                    # 按 judge_year × province 分区写
                    for (jy, prov), grp in processed.groupby(
                        ["judge_year", "province"], sort=False
                    ):
                        part_dir = (
                            output_root
                            / f"judge_year={jy}"
                            / f"province={prov}"
                        )
                        part_dir.mkdir(parents=True, exist_ok=True)
                        # 每个 CSV chunk 生成一个 parquet 分片（后续可 compact）
                        part_file = part_dir / (
                            f"{zip_path.stem}_{Path(csv_name).stem}_"
                            f"chunk{chunk_idx:04d}.parquet"
                        )
                        table = pa.Table.from_pandas(grp, preserve_index=False)
                        # 统一 large_string -> string，防止不同 chunk schema 漂移
                        new_fields = []
                        for field in table.schema:
                            if field.type == pa.large_string():
                                new_fields.append(field.with_type(pa.string()))
                            else:
                                new_fields.append(field)
                        table = table.cast(pa.schema(new_fields))
                        pq.write_table(
                            table, part_file,
                            compression="snappy",
                            use_dictionary=True,
                        )
                        stats["rows_by_judge_year"][int(jy)] = (
                            stats["rows_by_judge_year"].get(int(jy), 0) + len(grp)
                        )

                    del processed
                    gc.collect()

                del binary
                gc.collect()

    stats["elapsed_seconds"] = round(time.time() - t0, 1)
    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", required=True, type=Path, help="源 zip 路径")
    parser.add_argument("--output", type=Path, default=None,
                        help="Parquet dataset 输出根目录（precheck 模式下忽略）")
    parser.add_argument("--months", default=None,
                        help="逗号分隔的月份（01,02,...），默认全量")
    parser.add_argument("--precheck", action="store_true",
                        help="仅预检查该年份 schema 与关键分布；不写 Parquet")
    parser.add_argument("--sample-rows", type=int, default=5000,
                        help="precheck 时每月 CSV 采样行数，默认 5000")
    parser.add_argument("--keep-all-procedure", action="store_true",
                        help="保留所有审理程序（含减刑假释）；默认只保留新判决 {一审/刑事一审/二审/...}")
    args = parser.parse_args()

    if args.precheck:
        report = precheck_zip(args.zip, sample_rows=args.sample_rows)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.output is None:
        print("ERROR: --output required when not in --precheck mode", file=sys.stderr)
        return 2

    months = args.months.split(",") if args.months else None
    keep_proc = None if args.keep_all_procedure else PROCEDURE_KEEP
    stats = stream_zip(args.zip, args.output,
                        months_filter=months, keep_procedure=keep_proc)

    print("\n" + "=" * 60)
    print("STATS")
    print("=" * 60)
    for k, v in stats.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
