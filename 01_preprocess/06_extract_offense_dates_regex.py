#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""offense_facts 薄包 → 作案日期(严格 YMD)+ 程序性时间字段 regex 抽取。

设计(方案 r2 §③ + 普查 20260703):
- 只读薄包;输出 offense_date_regex parquet(不碰任何 master)。
- 严格规则:仅当存在【完整年月日 + 作案语境 + 通过时序 sanity】才算 regex ok;
  其余(仅年/年月、无作案日期、语境不明、空/截断无日期)一律 status→残差给 LLM。
- 负语境优先(普查陷阱:'犯盗窃罪…提起公诉' 中罪名词≠作案语境)。
- 相对日期补全:同年/同月/次日/翌日/当日 基于前一绝对日期。
- 顺带程序性字段:公诉/立案/抓获到案/刑拘/逮捕(各取首个命中)。
用法:
  --sample N   仅前 N 行(诊断模式,打印分类样例)
  全量:--pack <PACK>,输出 <PACK>/offense_date_regex_20260703.parquet
"""
import gzip, glob, json, os, re, sys
from collections import Counter
import pyarrow as pa
import pyarrow.parquet as pq

if "-h" in sys.argv or "--help" in sys.argv or "--pack" not in sys.argv:
    print(__doc__)
    print("必需参数: --pack <facts 薄包目录>（07_offense_date_front.py 生成）")
    sys.exit(0 if ("-h" in sys.argv or "--help" in sys.argv) else 2)
PACK = sys.argv[sys.argv.index("--pack") + 1]
OUT  = os.path.join(PACK, "offense_date_regex_20260703.parquet")

ABS = re.compile(r'(\d{4})年(\d{1,2})月(\d{1,2})日')
REL = re.compile(r'(同年(\d{1,2})月(\d{1,2})日|同月(\d{1,2})日|次日|翌日|当日|当天)')
YM_OFFENSE = re.compile(r'\d{4}年\d{1,2}月(?!\d{0,2}日)')

NEG = re.compile(r'(提起公诉|立案|受理|开庭|审理终结|抓获|到案|归案|投案|自首|刑事拘留|被拘留|行政拘留|逮捕|取保候审|监视居住|羁押|判决|裁定|宣判|送达|释放|刑满|假释|缓刑|考验期|刑期|羁押期|出生|生于|鉴定|勘验|检验|尸检|报案|接警|报警|因涉嫌|民事|调解|离婚|结婚|签订|注册|成立|领取|办理|颁发|吊销|注销|届满|有效期|供述|讯问|辨认|扣押|搜查|发还|退赃|退赔|赔偿|谅解|具结|移送|起诉书|决定书|通知书)')
POS = re.compile(r'(作案|窜至|窜入|潜入|伙同|结伙|纠集|持刀|持枪|持械|殴打|捅|砍|刺伤|击打|抢走|抢得|抢劫|抢夺|夺取|盗走|窃得|窃取|盗窃|盗得|骗取|诈骗|冒充|贩卖|贩毒|运输|走私|制造|种植|吸食|容留|卖淫|嫖娼|驾驶|酒后|醉酒|饮酒|肇事|碰撞|相撞|发生交通事故|发生争执|发生口角|争吵|纠纷|厮打|强奸|猥亵|拐卖|绑架|拘禁|敲诈|勒索|寻衅|滋事|聚众|斗殴|赌博|开设赌场|受贿|行贿|挪用|侵占|职务侵占|放火|投毒|爆炸|毁坏|捕捞|狩猎|采伐|盗伐|滥伐|采矿|收购|贩运|销售假|生产、销售|偷越|组织|强迫|引诱|传播|失火|过失|遗弃|虐待|重婚|危险驾驶|追逐竞驶|超载|超速|无证驾驶|吸毒|注射毒品|持有毒品|藏匿|窝藏|转移|收买|洗钱|集资|吸收公众存款|传销|非法经营|假冒|侵犯|泄露|入户|翻墙|撬|砸|割|剪|爬窗|溜门|扒窃|掩饰|隐瞒)')
SENT_TERM = re.compile(r'(刑期|羁押|考验期)')

P_PROSECUTE = re.compile(r'于?\s*$')          # 占位:程序性字段用窗口法
PROC_PATTERNS = {
    "prosecution_date":  re.compile(r'(提起公诉)'),
    "case_filed_date":   re.compile(r'(立案)'),
    "arrest_date":       re.compile(r'(抓获|到案|归案|投案)'),
    "detention_date":    re.compile(r'(刑事拘留)'),
    "formal_arrest_date":re.compile(r'(逮捕)'),
}

def norm_date(y, m, d):
    y, m, d = int(y), int(m), int(d)
    if not (1970 <= y <= 2026 and 1 <= m <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{m:02d}-{d:02d}"

def scan_dates(fs):
    """线性扫全文:绝对日期 + 相对日期(基于最近前一绝对日期补全)。返回 [(pos,end,iso)]"""
    out = []
    events = []
    for m in ABS.finditer(fs):
        events.append((m.start(), m.end(), "abs", m.groups()))
    for m in REL.finditer(fs):
        events.append((m.start(), m.end(), "rel", m.group(0)))
    events.sort(key=lambda x: x[0])
    last = None  # (y,m,d)
    for s, e, kind, g in events:
        if kind == "abs":
            iso = norm_date(*g)
            if iso:
                last = tuple(int(x) for x in g)
                out.append((s, e, iso))
        else:
            if last is None:
                continue
            y0, m0, d0 = last
            txt = g
            iso = None
            mm = re.match(r'同年(\d{1,2})月(\d{1,2})日', txt)
            if mm:
                iso = norm_date(y0, mm.group(1), mm.group(2))
            elif (mm := re.match(r'同月(\d{1,2})日', txt)):
                iso = norm_date(y0, m0, mm.group(1))
            elif txt in ("次日", "翌日"):
                dd = d0 + 1
                iso = norm_date(y0, m0, dd if dd <= 28 else 28)  # 保守
            elif txt in ("当日", "当天"):
                iso = norm_date(y0, m0, d0)
            if iso:
                out.append((s, e, iso))
    return out

CLOCK = re.compile(r'^\s*(?:(?:凌晨|上午|中午|下午|晚上|晚间|深夜|傍晚|夜间|晚)\s*)?\d{1,2}\s*[时点](?:\d{1,2}分)?(?:许|左右)?|^\s*(?:凌晨|上午|中午|下午|晚上|晚间|深夜|傍晚|夜里|晚)')
ANCHOR_B = re.compile(r'(指控|查明|指控称|审理认定)[:,,:、]?\s*(?:[以下]{0,2}事实)?[:,,:]?\s*\d{0,2}[..、]?\s*$')
CRIME_NAME = re.compile(r'犯[^,。;、]{1,14}罪')

def classify(fs, s, e):
    win_b = fs[max(0, s - 16):s]
    win_a = fs[e:e + 42]
    near_a = fs[e:e + 12]
    if SENT_TERM.search(fs[max(0, s - 8):e + 10]):
        return "neg"
    # ① 钟点特征:程序性日期不带时刻;紧跟时刻词 = 事件(作案)时间
    if CLOCK.search(win_a) and not NEG.search(near_a):
        return "pos"
    # ② 负语境(公诉/立案/拘留/开庭…)优先
    if NEG.search(win_b + "◆" + win_a[:26]):
        return "neg"
    # ③ 段落锚:"指控,/经审理查明:" 后的首个日期
    if ANCHOR_B.search(win_b):
        return "pos"
    # ④ 动作词(前窗剔除 '犯X罪' 罪名段,防罪名词假阳性)
    if POS.search(win_a) or POS.search(CRIME_NAME.sub("", fs[max(0, s - 24):s])):
        return "pos"
    return "unk"

def proc_fields(fs, dates):
    got = {}
    for s, e, iso in dates:
        ctx = fs[e:e + 14]
        for k, pat in PROC_PATTERNS.items():
            if k not in got and pat.search(ctx):
                got[k] = iso
    return got

def process_row(rid, fs):
    empty = (not fs or not fs.strip())
    trunc = (not empty and len(fs) >= 1500)
    rec = dict(id=rid, facts_segment_empty=empty, facts_segment_truncated=trunc,
               offense_start_date=None, offense_end_date=None,
               offense_year_main=None, offense_year_alt=None,
               all_offense_dates_raw=None, span_cross_year=None,
               date_precision=None, extraction_source="regex", confidence=None,
               prosecution_date=None, case_filed_date=None, arrest_date=None,
               detention_date=None, formal_arrest_date=None,
               n_dates_total=0, n_dates_pos=0, n_dates_unk=0,
               regex_status=None)
    if empty:
        rec["regex_status"] = "empty"
        return rec
    dates = scan_dates(fs)
    rec["n_dates_total"] = len(dates)
    for k, v in proc_fields(fs, dates).items():
        rec[k] = v
    labs = [(s, e, iso, classify(fs, s, e)) for s, e, iso in dates]
    pos = [iso for *_, iso, L in [(s, e, iso, L) for s, e, iso, L in labs] if L == "pos"]
    unk = [iso for s, e, iso, L in labs if L == "unk"]
    rec["n_dates_pos"] = len(pos); rec["n_dates_unk"] = len(unk)
    if not dates:
        rec["regex_status"] = "truncated_no_date" if trunc else ("no_date_ym" if YM_OFFENSE.search(fs) else "no_date")
        return rec
    if pos:
        start, end = min(pos), max(pos)
        # 时序 sanity:作案不得晚于公诉
        pr = rec["prosecution_date"]
        if pr and start > pr:
            rec["regex_status"] = "ambiguous_order"
            return rec
        rec.update(offense_start_date=start, offense_end_date=end,
                   offense_year_main=int(start[:4]), offense_year_alt=int(end[:4]),
                   all_offense_dates_raw=json.dumps(sorted(set(pos)), ensure_ascii=False),
                   span_cross_year=(start[:4] != end[:4]),
                   date_precision="YMD",
                   confidence=("high" if not unk else "medium"),
                   regex_status="ok")
        return rec
    rec["regex_status"] = "ambiguous_unk_only" if unk else "no_offense_date"
    return rec

def main():
    sample = 0
    if "--sample" in sys.argv:
        sample = int(sys.argv[sys.argv.index("--sample") + 1])
    rows = []
    st = Counter()
    n = 0
    diag = {"ok": [], "ambiguous_unk_only": [], "no_offense_date": [], "ambiguous_order": []}
    for p in sorted(glob.glob(os.path.join(PACK, "facts_only_part_*.jsonl.gz"))):
        with gzip.open(p, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                rid = d["id"]; rid = rid if isinstance(rid, str) else ("" if rid is None else str(rid))
                fs = d.get("facts_segment") or ""
                rec = process_row(rid, fs)
                st[rec["regex_status"]] += 1
                rows.append(rec)
                n += 1
                if sample and rec["regex_status"] in diag and len(diag[rec["regex_status"]]) < 6:
                    frag = fs[:170].replace("\n", " ")
                    diag[rec["regex_status"]].append(
                        f"[{rec['regex_status']}] start={rec['offense_start_date']} end={rec['offense_end_date']} "
                        f"pos={rec['n_dates_pos']} unk={rec['n_dates_unk']} pr={rec['prosecution_date']}\n    {frag}")
                if sample and n >= sample:
                    break
            if sample and n >= sample:
                break
        if sample and n >= sample:
            break
    print(json.dumps({"rows": n, "status": dict(st),
                      "ok_rate": round(st["ok"] / n, 4)}, ensure_ascii=False, indent=1))
    if sample:
        for k, v in diag.items():
            print(f"\n===== {k} 样例 =====")
            for x in v:
                print("  ", x)
        return
    tbl = pa.Table.from_pylist(rows)
    pq.write_table(tbl, OUT, compression="zstd")
    print("→", OUT, tbl.num_rows, "rows")

if __name__ == "__main__":
    main()
