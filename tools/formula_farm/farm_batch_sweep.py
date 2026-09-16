# -*- coding: utf-8 -*-
"""farm_batch_sweep: 826 条入库公式全量粗扫 + 淘金留档(winners)。

- 遍历 onboard.json 全部 ok 公式(GSxxxx), 逐条 gs_5m_sweep prep/run(36组合)/report;
- 断点续跑: 已有 sweep csv 的跳过;
- 达标口径唯一真相源 = core/farm_rules.py (年化≥15% 且 |回撤|≤15% 且 笔数≥20;
  笔数不足记「样本不足」, 不判达标也不参与最优评选 —— GS1292 事件教训)。
  达标者写入 data/formula_farm/winners/<GS>_<标题>.md
  (源码+最优组合+四指标+口径) 和 winners/index.md 总榜;
- 全量跑完(或被中止)后, 已扫部分也能出榜(--report-only 只出榜不扫)。
"""
import argparse
import csv
import glob
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
PY = sys.executable

from tools.formula_farm import intake as _intake  # noqa: E402
from core import farm_rules  # noqa: E402  达标口径唯一真相源 (2026-09-15 收口, 原手写 ANN_MIN/DD_MAX 缺笔数守卫)

RUNS = os.path.join(ROOT, "data", "formula_farm", "runs")
WINNERS = os.path.join(ROOT, "data", "formula_farm", "winners")
SWEEP_PY = os.path.join(ROOT, "tools", "gs_5m_sweep.py")
COMBOS36 = os.path.join(ROOT, "output", "gs_filter", "coarse_subset36.json")


def log(s):
    print(s, flush=True)


def load_ok_formulas():
    ob = json.load(open(os.path.join(RUNS, "2026-09-06", "onboard.json"), encoding="utf-8"))
    return [x for x in ob["items"] if x.get("ok")]


def sweep_csv(gs):
    fs = glob.glob(os.path.join(ROOT, "output", "gs_5m_sweep", gs, "sweep_*.csv"))
    return fs[0] if fs else None


def best_row(gs):
    # 精调后目录里会有粗扫+多分片 csv; report_merged.csv 是 gs_5m_sweep report
    # 合并去重(按 key)后的全集, 优先读它 (2026-09-08: 精调分片适配)
    fp = os.path.join(ROOT, "output", "gs_5m_sweep", gs, "report_merged.csv")
    if not os.path.exists(fp):
        fs = glob.glob(os.path.join(ROOT, "output", "gs_5m_sweep", gs, "sweep_*.csv"))
        fp = fs[0] if fs else None
    if not fp:
        return None
    rows = []
    with open(fp, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("error"):
                continue
            try:
                float(r["annret"])
            except Exception:
                continue
            rows.append(r)
    if not rows:
        return None
    # 最优组合只在笔数≥20 的行里选 (farm_rules.pick_best), 全不足样本才退回
    # 最高年化 —— 由下方 is_pass 判「样本不足」挡住, 不把噪声写进 winners
    return farm_rules.pick_best(rows)


def run_one(gs, start, end, universe_type):
    """返回 (ok, err, no_signal, no_kline)。no_signal=True = prep 判定区间零信号;
    no_kline=True = prep 窗口取数空 (无缓存, run 无从谈起, 下轮重试)。"""
    prep = subprocess.run([PY, "-X", "utf8", SWEEP_PY, "prep", gs,
                           "--start", start, "--end", end,
                           "--universe-type", str(universe_type)],
                          cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=3600)
    out = (prep.stdout or "") + (prep.stderr or "")
    if '"status": "no_signals"' in out or "零信号" in out:
        return True, "零信号(无可回测)", True, False
    if prep.returncode != 0:
        return False, out[-200:], False, False
    # 2026-09-08: no_kline 是 rc=0 的合法空结果 (无 meta.json), 直接跳过不跑 run
    if '"status": "no_kline"' in out:
        return True, "窗口取数空(下轮重试)", False, True
    for cmd, extra in (("run", ["--stage", "refine", "--combos-file", COMBOS36]),
                       ("report", [])):
        r = subprocess.run([PY, "-X", "utf8", SWEEP_PY, cmd, gs,
                            "--start", start, "--end", end] + extra,
                           cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=3600)
        if r.returncode != 0:
            return False, (r.stderr or r.stdout)[-200:], False, False
    return True, "", False, False


def emit_winners(items, start, end):
    """按门槛出 winners MD + index.md。"""
    os.makedirs(WINNERS, exist_ok=True)
    code_by_file = {}
    for rec in _intake.iter_intake():
        code_by_file[rec["file"]] = rec
    board = []
    for it in items:
        gs = it["gs"]
        b = best_row(gs)
        if not b:
            continue
        ann, dd = float(b["annret"]), float(b["maxdd"])
        row = {"gs": gs, "file": it["file"], "url": it.get("url", ""),
               "ann": ann, "dd": dd, "calmar": float(b["calmar"]),
               "winrate": float(b["winrate"]), "trades": int(float(b["trades"])),
               "key": b["key"], "pass": farm_rules.is_pass(b)}
        board.append(row)
    board.sort(key=lambda x: x["ann"], reverse=True)
    for r in board:
        if not r["pass"]:
            continue
        rec = code_by_file.get(r["file"], {})
        title = re.sub(r'[\\/:*?"<>|]', "_", os.path.splitext(r["file"])[0])
        md = ["# %s — 粗扫达标(年化≥15%%)\n" % r["gs"],
              "- 来源: %s" % r["url"],
              "- 区间/口径: %s~%s, 沪深300, 5m, 移动止盈优先, T收盘买入" % (start, end),
              "- 最优组合: `%s`" % r["key"],
              "- 年化 **%.1f%%** | 卡玛 %.2f | 最大回撤 %.1f%% | 胜率 %.1f%% | 交易 %d 笔"
              % (r["ann"] * 100, r["calmar"], r["dd"] * 100, r["winrate"] * 100, r["trades"]),
              "\n## 公式源码\n```\n%s\n```" % rec.get("code", "(源码缺失)")]
        with open(os.path.join(WINNERS, "%s_%s.md" % (r["gs"], title[:50])),
                  "w", encoding="utf-8") as f:
            f.write("\n".join(md))
    passed = [r for r in board if r["pass"]]
    idx = ["# 公式农场粗扫总榜 — %s\n" % time.strftime("%Y-%m-%d"),
           "%s | 口径: %s~%s 沪深300 5m\n" % (farm_rules.describe(), start, end),
           "| 排名 | 公式 | 年化 | 卡玛 | 回撤 | 胜率 | 笔数 | 最优组合 |", "|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(passed, 1):
        idx.append("| %d | %s (%s) | %.1f%% | %.2f | %.1f%% | %.1f%% | %d | %s |"
                   % (i, r["gs"], r["file"][:16], r["ann"] * 100, r["calmar"],
                      r["dd"] * 100, r["winrate"] * 100, r["trades"], r["key"]))
    idx.append("\n> 已扫 %d / 达标 %d。粗扫仅初筛, 达标者进入精调(§8 两阶段+样本外)流程。" % (len(board), len(passed)))
    with open(os.path.join(WINNERS, "index.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(idx))
    return len(board), len(passed)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20240901")
    ap.add_argument("--end", default=time.strftime("%Y%m%d"))
    ap.add_argument("--universe-type", type=int, default=23)
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    items = load_ok_formulas()
    log("待扫公式: %d (%s)" % (len(items), farm_rules.describe()))
    if not args.report_only:
        done = 0
        zero = 0
        nok = 0
        for it in items[: args.limit or None]:
            gs = it["gs"]
            if sweep_csv(gs):
                continue  # 断点: 已扫跳过
            t0 = time.time()
            ok, err, no_signal, no_kline = run_one(gs, args.start, args.end, args.universe_type)
            done += 1
            if no_signal:
                zero += 1
                log("[%d/%d] %s 零信号跳过 (%.0fs)" % (done, len(items), gs, time.time() - t0))
            elif no_kline:
                nok += 1
                log("[%d/%d] %s 窗口取数空,下轮重试 (%.0fs)" % (done, len(items), gs, time.time() - t0))
            else:
                log("[%d/%d] %s %s (%.0fs)" % (done, len(items), gs,
                                               "✓" if ok else "✗ " + err[:60], time.time() - t0))
        log("本轮: 处理 %d, 零信号 %d, 窗口空 %d" % (done, zero, nok))
    scanned, passed = emit_winners(items, args.start, args.end)
    log("出榜: 已扫 %d, 达标 %d -> %s" % (scanned, passed, WINNERS))


if __name__ == "__main__":
    main()
