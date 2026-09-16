# -*- coding: utf-8 -*-
"""farm_parallel_sweep: farm_batch_sweep 的并行版 — run 阶段多进程并行。

为什么并行安全 (2026-09-08):
- prep (选股+5m 稀疏窗口取数) 会碰 TDX → 必须单飞, 串行做;
- run (36 组合回测) + report 是纯本地 CPU 计算 (读 cache npy 矩阵) → 不碰 TDX,
  可以 N 进程并行, 机器 32 线程 / 30GB 内存足够;
- GS1238 实测: prep 秒回 (L2 缓存), run 才是慢大头 (GS1103 run 780s)。

断点续跑与 farm_batch_sweep 一致: 已有 sweep csv 的公式跳过。
"""
import argparse
import glob
import json
import multiprocessing
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
PY = sys.executable

from tools.formula_farm.farm_batch_sweep import (  # noqa: E402
    COMBOS36,
    RUNS,
    SWEEP_PY,
    WINNERS,
    emit_winners,
    load_ok_formulas,
    log,
    sweep_csv,
)

WINNERS_DIR = WINNERS
WORKERS_DEFAULT = 6
ZERO_MEMO = os.path.join(RUNS, "zero_signal_memo.json")  # 零信号/窗口空记忆, 免重复 prep


def load_zero_memo():
    if os.path.exists(ZERO_MEMO):
        try:
            return set(json.load(open(ZERO_MEMO, encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_zero_memo(memo):
    json.dump(sorted(memo), open(ZERO_MEMO, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)


def run_one_worker(gs, start, end, universe_type):
    """worker 进程入口: 对已 prep 的公式跑 run(refine 36 组合) + report。
    纯本地 CPU, 无 TDX 依赖。返回 (gs, ok, err)。"""
    t0 = time.time()
    for cmd, extra in (("run", ["--stage", "refine", "--combos-file", COMBOS36]),
                       ("report", [])):
        r = subprocess.run([PY, "-X", "utf8", SWEEP_PY, cmd, gs,
                            "--start", start, "--end", end] + extra,
                           cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=7200)
        if r.returncode != 0:
            return gs, False, (r.stderr or r.stdout)[-200:]
    return gs, True, "%.0fs" % (time.time() - t0)


def prep_one(gs, start, end, universe_type):
    """串行 prep (TDX 单飞)。返回状态: ok / no_signals / no_kline / fail[:err]。"""
    r = subprocess.run([PY, "-X", "utf8", SWEEP_PY, "prep", gs,
                        "--start", start, "--end", end,
                        "--universe-type", str(universe_type)],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=3600)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        # 记下错误尾部 (traceback 末几行), 便于诊断
        tail = out.strip().splitlines()
        err = " | ".join(x.strip() for x in tail[-4:])[-200:]
        return "fail:" + err
    if '"status": "no_signals"' in out:
        return "no_signals"
    if '"status": "no_kline"' in out:
        return "no_kline"
    return "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20240901")
    ap.add_argument("--end", default=time.strftime("%Y%m%d"))
    ap.add_argument("--universe-type", type=int, default=23)
    ap.add_argument("--workers", type=int, default=WORKERS_DEFAULT)
    ap.add_argument("--prep-only", action="store_true",
                    help="只跑 prep 阶段(串行), 不并行 run; 适合先铺缓存")
    args = ap.parse_args()

    items = load_ok_formulas()
    pending = [it for it in items if not sweep_csv(it["gs"])]
    log("待处理公式: %d (已测跳过)" % len(pending))

    # ---- 阶段 1: prep 串行 (TDX 单飞), 零信号记忆免重复 ----
    zero_memo = load_zero_memo()
    ready = []
    stats = {"no_signals": 0, "no_kline": 0, "fail": 0, "memo_skip": 0}
    t_p = time.time()
    for i, it in enumerate(pending, 1):
        gs = it["gs"]
        if gs in zero_memo:
            stats["memo_skip"] += 1
            continue  # 历史已判零信号/窗口空, 免重复 prep
        st = prep_one(gs, args.start, args.end, args.universe_type)
        if st == "ok":
            ready.append(it)
            log("[prep %d/%d] %s ok (%.0fs)" % (i, len(pending), gs, time.time() - t_p))
            t_p = time.time()
        else:
            if st.startswith("fail"):
                stats["fail"] = stats.get("fail", 0) + 1
            else:
                stats[st] = stats.get(st, 0) + 1
            if st in ("no_signals", "no_kline"):
                zero_memo.add(gs)
            log("[prep %d/%d] %s %s (%.0fs)" % (i, len(pending), gs, st[:80], time.time() - t_p))
            t_p = time.time()
    save_zero_memo(zero_memo)
    log("prep 完成: 就绪 %d, 零信号 %d, 窗口空 %d, 失败 %d, 记忆跳过 %d"
        % (len(ready), stats.get("no_signals", 0), stats.get("no_kline", 0),
           stats.get("fail", 0), stats.get("memo_skip", 0)))
    if args.prep_only or not ready:
        if ready:
            log("--prep-only 模式结束 (run 另跑)")
        return

    # ---- 阶段 2: run + report 并行 (纯 CPU) ----
    jobs = [(it["gs"], args.start, args.end, args.universe_type) for it in ready]
    log("并行 run+report: %d 条, workers=%d" % (len(jobs), args.workers))
    t0 = time.time()
    done = 0
    fail_list = []
    with multiprocessing.Pool(processes=args.workers) as pool:
        for gs, ok, err in pool.starmap(run_one_worker, jobs):
            done += 1
            if ok:
                log("[run %d/%d] %s ✓ (%s)" % (done, len(jobs), gs, err))
            else:
                fail_list.append((gs, err))
                log("[run %d/%d] %s ✗ %s" % (done, len(jobs), gs, err[:80]))
    log("run 并行完成: %d 条, %.0fs, 失败 %d" % (len(jobs), time.time() - t0, len(fail_list)))

    scanned, passed = emit_winners(items, args.start, args.end)
    log("出榜: 已扫 %d, 达标 %d -> %s" % (scanned, passed, WINNERS))


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
