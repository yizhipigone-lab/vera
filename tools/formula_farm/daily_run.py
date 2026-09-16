# -*- coding: utf-8 -*-
"""daily_run: 公式农场每日编排(2026-09-06)。

链路: 采集(crawl_gupang) → 去重(dedupe vs gongshi+gs_txt) → 运行时体检(vet_runtime 硬闸)
→ 上架(gui_onboard.add_formula, 逐条, GS 全局计数) → 日报 + index.json 留痕。

用法:
  python -X utf8 tools/formula_farm/daily_run.py                # 全流程(爬+上架)
  python -X utf8 tools/formula_farm/daily_run.py --no-crawl     # 只处理 intake 已有
  python -X utf8 tools/formula_farm/daily_run.py --max-add 3    # 本轮最多上架几条
  python -X utf8 tools/formula_farm/daily_run.py --dry-run      # 不上架不写盘, 只报告
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
PY = sys.executable

from tools.formula_farm import common, intake, dedupe, static_vetting, gui_onboard  # noqa: E402

DATA_ROOT = os.path.join(ROOT, "data", "formula_farm")
INTAKE_DIR = os.path.join(DATA_ROOT, "intake")
RUNS_DIR = os.path.join(DATA_ROOT, "runs")
REPORTS_DIR = os.path.join(DATA_ROOT, "reports")
GS_TXT = common.GS_TXT_DIR


def log(s=""):
    print(s, flush=True)


def step_crawl(pages, max_new):
    log("[1] 采集(爬两栏目)")
    r = subprocess.run([PY, "-X", "utf8",
                        os.path.join(ROOT, "tools", "formula_farm", "crawl_gupang.py"),
                        "--pages", str(pages), "--max-new", str(max_new)],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = r.stdout.strip().splitlines()[-8:]
    for l in tail:
        log("   " + l)
    if r.returncode != 0:
        log("   ⚠ 采集脚本退出码 %d(不阻断: 已有的 intake 继续处理)" % r.returncode)


def step_load_records():
    recs = intake.iter_intake(INTAKE_DIR)
    log("[2] 读 intake: %d 篇" % len(recs))
    return recs


def step_dedupe(recs):
    done = dedupe.build_done_set(common.GONGSHI_DIR, GS_TXT)
    log("[3] 去重: 已做集 URL=%d 哈希=%d" % (len(done["urls"]), len(done["hashes"])))
    fresh, dup = [], []
    for r in recs:
        hits = dedupe.is_dup(r, done)
        if hits:
            dup.append({"file": r["file"], "url": r["url"], "hits": hits})
        else:
            fresh.append(r)
    log("    判新 %d / 判重(跳过) %d" % (len(fresh), len(dup)))
    for d in dup[:6]:
        log("      重复: %s (%s)" % (d["file"][:40], "+".join(d["hits"])))
    return fresh, dup


def step_vet(fresh):
    ok_list, bad = [], []
    for r in fresh:
        ok, reasons = static_vetting.vet_runtime(r)
        (ok_list if ok else bad).append({**r, "reasons": reasons})
    log("[4] 运行时体检: 通过 %d / 淘汰 %d" % (len(ok_list), len(bad)))
    for b in bad:
        log("      淘汰: %s — %s" % (b["file"][:40], "; ".join(b["reasons"])))
    return ok_list, bad


def _next_gs_name():
    """GS 全局计数: 取 gs_txt 文件名 + TDX 其他类型树节点里的最大编号 +1。"""
    best = 0
    import glob as _g
    for p in _g.glob(os.path.join(GS_TXT, "gs_*_GS*.txt")):
        m = re.search(r"GS(\d+)", os.path.basename(p))
        if m:
            best = max(best, int(m.group(1)))
    try:
        win = gui_onboard._manager()
        tv = win.child_window(title="Tree1", class_name="SysTreeView32")
        for k in tv.get_item(gui_onboard.TREE_PATH).children():
            m = re.search(r"GS(\d+)", k.text() or "")
            if m:
                best = max(best, int(m.group(1)))
    except Exception as e:
        log("   ⚠ 读 TDX 树失败(用 gs_txt 的编号): %r" % e)
    return best + 1


def step_onboard(cands, max_add, dry_run):
    log("[5] 上架(本轮上限 %d 条)%s" % (max_add, " [dry-run 只列清单]" if dry_run else ""))
    if dry_run or not cands:
        for r in cands[:max_add]:
            log("      将上架: %s (%s)" % (r["file"][:40], r["url"]))
        return []
    n = _next_gs_name()
    log("   编号起点: GS%04d" % n)
    results, fail_streak = [], 0
    for r in cands[:max_add]:
        name = "GS%04d" % n
        n += 1
        t0 = time.time()
        try:
            ok, msg = gui_onboard.add_formula(name, r["code"], desc=r["url"])
        except Exception as e:
            ok, msg = False, "EXC %r" % e
        log("   %s %s <- %s | %s (%.0fs)" % ("✓" if ok else "✗", name,
                                            r["file"][:36], msg, time.time() - t0))
        results.append({"gs": name, "file": r["file"], "url": r["url"],
                        "ok": ok, "msg": msg})
        fail_streak = 0 if ok else fail_streak + 1
        if fail_streak >= 2:
            log("   ✗ 连续 2 次失败, 终止本批(防带病批量)")
            break
    return results


def step_report(date_str, crawl_note, dup, bad, results, ok_list):
    """日报 + run 留痕。"""
    os.makedirs(RUNS_DIR, exist_ok=True)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    run_dir = os.path.join(RUNS_DIR, date_str)
    os.makedirs(run_dir, exist_ok=True)
    idx = {"date": date_str, "crawl": crawl_note, "dup": dup,
           "excluded": [{"file": b["file"], "reasons": b["reasons"]} for b in bad],
           "onboard": results,
           "vetted_pass": [{"file": r["file"], "url": r["url"]} for r in ok_list]}
    with open(os.path.join(run_dir, "index.json"), "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1)

    md = []
    md.append("# 公式农场日报 — %s\n" % date_str)
    md.append("## 总览\n")
    md.append("| 环节 | 数量 |\n|---|---|")
    md.append("| 采集 | %s |" % crawl_note)
    md.append("| 判重跳过 | %d |" % len(dup))
    md.append("| 体检淘汰 | %d |" % len(bad))
    md.append("| 体检通过 | %d |" % len(ok_list))
    md.append("| 上架 | 成功 %d / 失败 %d |\n" % (
        sum(1 for x in results if x["ok"]), sum(1 for x in results if not x["ok"])))
    if results:
        md.append("## 上架明细\n")
        for x in results:
            md.append("- %s **%s** %s — %s ([来源](%s))" %
                      ("✅" if x["ok"] else "❌", x["gs"], x["file"], x["msg"], x["url"]))
    if bad:
        md.append("\n## 淘汰明细(原因)\n")
        for b in bad:
            md.append("- %s — %s" % (b["file"], "; ".join(b["reasons"])))
    if dup:
        md.append("\n## 判重跳过\n")
        for d in dup:
            md.append("- %s (%s)" % (d["file"], "+".join(d["hits"])))
    md.append("\n> 上架口径: 条件选股→其他类型; 回测验证走下一轮(粗扫/精调)。")
    rpt = os.path.join(REPORTS_DIR, "%s_公式农场日报.md" % date_str)
    with open(rpt, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    return rpt, run_dir


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-crawl", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--max-new", type=int, default=15)
    ap.add_argument("--max-add", type=int, default=3, help="本轮最多上架几条")
    args = ap.parse_args()

    date_str = time.strftime("%Y-%m-%d")
    crawl_note = "跳过(--no-crawl)"
    if not args.no_crawl:
        step_crawl(args.pages, args.max_new)
        crawl_note = "pages=%d max-new=%d" % (args.pages, args.max_new)
    recs = step_load_records()
    fresh, dup = step_dedupe(recs)
    ok_list, bad = step_vet(fresh)
    results = step_onboard(ok_list, args.max_add, args.dry_run)
    rpt, run_dir = step_report(date_str, crawl_note, dup, bad, results, ok_list)
    log("\n日报: %s\n留痕: %s" % (rpt, os.path.join(run_dir, "index.json")))


if __name__ == "__main__":
    main()
