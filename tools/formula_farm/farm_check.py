# -*- coding: utf-8 -*-
"""farm_check: 闸门① — 采集+去重+体检(零 TDX, 零 GUI, 纯文本)。

写 data/formula_farm/runs/<date>/check.json 供闸门②消费。
阶段行 [1/3].. 供 FarmRunner 解析进度。
"""
import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
PY = sys.executable

from tools.formula_farm import common, intake, dedupe, static_vetting  # noqa: E402

RUNS = os.path.join(ROOT, "data", "formula_farm", "runs")


def log(s):
    print(s, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--max-new", type=int, default=30)
    args = ap.parse_args()

    date_str = time.strftime("%Y-%m-%d")
    log("[1/3] 采集股旁网(选股栏目 + 改编选股)...")
    r = subprocess.run(
        [PY, "-X", "utf8", os.path.join(ROOT, "tools", "formula_farm", "crawl_gupang.py"),
         "--pages", str(args.pages), "--max-new", str(args.max_new)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    crawl_tail = r.stdout.strip().splitlines()[-6:] if r.stdout else []
    for l in crawl_tail:
        log("   " + l)
    if r.returncode != 0:
        log("   ⚠ 采集退出码 %d(继续处理已有归档)" % r.returncode)

    log("[2/3] 读归档 + 去重(已做集: gongshi + gs_txt 全库)...")
    records = intake.iter_intake()
    done = dedupe.build_done_set(common.GONGSHI_DIR, common.GS_TXT_DIR)
    fresh, dup = [], []
    for rec in records:
        hits = dedupe.is_dup(rec, done)
        (fresh if not hits else dup).append({**rec, "hits": hits})
    log("   归档 %d | 判新 %d | 判重跳过 %d" % (len(records), len(fresh), len(dup)))

    log("[3/3] 运行时体检(未来函数/筹码函数硬闸)...")
    vetted, excluded = [], []
    for rec in fresh:
        ok, reasons = static_vetting.vet_runtime(rec)
        (vetted if ok else excluded).append(
            {"file": rec["file"], "url": rec["url"], "date": rec.get("date", ""),
             "reasons": reasons} if not ok else
            {"file": rec["file"], "url": rec["url"], "date": rec.get("date", ""),
             "code": rec["code"]})
    for e in excluded:
        log("   淘汰: %s — %s" % (e["file"][:44], "; ".join(e["reasons"])))
    log("   体检通过 %d / 淘汰 %d" % (len(vetted), len(excluded)))

    os.makedirs(os.path.join(RUNS, date_str), exist_ok=True)
    out = {"date": date_str, "intake": len(records),
           "dup": [{"file": d["file"], "hits": d["hits"]} for d in dup],
           "vetted": vetted, "excluded": excluded,
           "crawl_tail": crawl_tail, "finished_at": time.strftime("%H:%M:%S")}
    fp = os.path.join(RUNS, date_str, "check.json")
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    log("完成: 新增可入库 %d 条 (墙/剔除 %d, 重复 %d) -> %s"
        % (len(vetted), len(excluded), len(dup), fp))


if __name__ == "__main__":
    main()
