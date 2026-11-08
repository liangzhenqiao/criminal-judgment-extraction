#!/usr/bin/env python3
"""G3 stage 3: strict re-judge of free-text privacy candidates.

Replaces the loose stage-2 rubric, which labelled any string containing a digit
as an address. The question asked here is narrow and definition-first: does the
value, as printed, narrow a reader down to a building or a door?

Labels:
  house_number   a doorplate, building, unit, room or lane number tied to a
                 street, road, village or named compound
  compound_only  a named residential compound, village or community, no number
  admin_area     province / city / county / district / town / street only
  not_address    occupation, employer name without a number, court, "at home"
  unclear        too fragmentary to decide

Usage: python3 g3_stage3.py IN_CSV OUT_CSV
"""
import csv
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from openai import OpenAI

BASE = "http://127.0.0.1:8030/v1"
MODEL = "Qwen3.8-27B-FP8"

SYSTEM = """你是判决文书数据的隐私审查员。给你一个字段值和它的字段名，判断这个值会不会让读者定位到具体的房屋或门牌。

只回答一个问题：这个值印在这里，读者能否据此把范围缩小到一栋楼、一个单元或一个门牌？

标签只能是五个之一：
- house_number：有街道/路/村/小区之后的门牌号、楼栋号、单元号、室号、弄号。例："伊通满族自治县福康小区15号楼"、"{{ADDRESS_x}}365号"、"{{ADDRESS_y}}3单元402室"
- compound_only：点名了小区、村、社区、大院，但没有号。例："{{ADDRESS_z}}福康小区"、"{{ADDRESS_w}}村"
- admin_area：只有省、市、县、区、镇、街道一级。例："{{ADDRESS_v}}"、"长春市朝阳区"
- not_address：职业、单位名（无号）、机关、场所泛称、"家中"。例："某公司经营者"、"某饭店经理"、"鲁荣渔50827号船船长"、"被告人家中"、"{{ADDRESS_u}}人民法院"
- unclear：碎片化，判断不了

判断规则：
1. 先看有没有"号/号楼/栋/幢/弄/室/单元"，且这个号是挂在街道、路、村或小区后面的门牌类号，不是年份、案号、刑期、船号、门牌以外的编号。
2. 有号但号是单位内部编号（如"3号车间"）仍算 house_number。
3. 只是单位名、职务、行业，即使带数字也不算地址。
4. 拿不准就 unclear，不要猜。

输出 JSON：{"label": "...", "why": "不超过20字的依据"}"""

FEWSHOT = [
    ("crime_location_raw", "{{ADDRESS_a}}福康小区15号楼", "house_number", "小区后有楼栋号"),
    ("crime_location_raw", "{{ADDRESS_b}}", "admin_area", "只到县一级"),
    ("prior_occupation_raw", "某公司经营者", "not_address", "职业，无地址"),
    ("prior_occupation_raw", "鲁荣渔50827号船船长", "not_address", "船号不是门牌"),
    ("crime_location_raw", "被告人家中", "not_address", "场所泛称"),
]


def judge(client, field, value):
    msgs = [{"role": "system", "content": SYSTEM}]
    for f, v, lab, why in FEWSHOT:
        msgs.append({"role": "user", "content": json.dumps({"field": f, "value": v}, ensure_ascii=False)})
        msgs.append({"role": "assistant", "content": json.dumps({"label": lab, "why": why}, ensure_ascii=False)})
    msgs.append({"role": "user", "content": json.dumps({"field": field, "value": value}, ensure_ascii=False)})
    r = client.chat.completions.create(
        model=MODEL, messages=msgs, temperature=0,
        max_tokens=120, response_format={"type": "json_object"},
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    txt = (r.choices[0].message.content or "").strip()
    try:
        d = json.loads(txt)
        return str(d.get("label", "unclear")), str(d.get("why", ""))[:60]
    except Exception:
        return "parse_error", txt[:60]


def main():
    src, dst = sys.argv[1], sys.argv[2]
    rows = list(csv.DictReader(open(src, encoding="utf-8")))
    client = OpenAI(base_url=BASE, api_key="x")
    lock = threading.Lock()
    done = [0]

    def work(r):
        lab, why = judge(client, r["field"], r["value"])
        with lock:
            done[0] += 1
            if done[0] % 50 == 0:
                print(done[0], flush=True)
        return {**r, "label3": lab, "why3": why}

    with ThreadPoolExecutor(max_workers=8) as ex:
        out = list(ex.map(work, rows))
    with open(dst, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)
    print("done", len(out))


if __name__ == "__main__":
    main()
