#!/usr/bin/env python3
"""Run the production prompt on the prepared cases.

Usage: python3 g1_run.py IN.jsonl OUT.jsonl
"""
import json
import os
import sys
from pathlib import Path
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from openai import OpenAI

BASE = os.environ.get("G1_BASE", "http://127.0.0.1:8031/v1")
MODEL = os.environ.get("G1_MODEL", "Qwen3-Next-80B-A3B-Instruct-FP8")
SYSTEM = open(os.environ.get(
    "G1_PROMPT",
    str(Path(__file__).resolve().parents[2]
        / "profiles/tasks/criminal_defendant_v1.3/pattern.md")),
    encoding="utf-8").read().strip()

client = OpenAI(base_url=BASE, api_key="x", timeout=600)
lock = threading.Lock()
done = [0]


def work(case_json):
    r = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": case_json},
        ],
        temperature=0,
        max_tokens=4096,
        response_format={"type": "json_object"},
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    txt = (r.choices[0].message.content or "").strip()
    cid = json.loads(case_json)[0]["id"]
    try:
        parsed = json.loads(txt)
    except Exception:
        parsed = None
    with lock:
        done[0] += 1
        if done[0] % 25 == 0:
            print(done[0], flush=True)
    return {"id": cid, "ok": parsed is not None, "raw": "" if parsed else txt[:400],
            "parsed": parsed}


def main():
    src, dst = sys.argv[1], sys.argv[2]
    cases = [line.strip() for line in open(src, encoding="utf-8") if line.strip()]
    with ThreadPoolExecutor(max_workers=8) as ex:
        out = list(ex.map(work, cases))
    with open(dst, "w", encoding="utf-8") as f:
        for d in out:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    print("done", len(out), "unparsed", sum(1 for d in out if not d["ok"]))
    print(Counter("ok" if d["ok"] else "unparsed" for d in out))


if __name__ == "__main__":
    main()
