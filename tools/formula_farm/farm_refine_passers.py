# -*- coding: utf-8 -*-
"""farm_refine_passers: 达标公式 2592 组精调 — 每公式 nshards 分片并行。

run 阶段纯 CPU (读 prep 落盘矩阵), 不碰 TDX → 分片并行安全。
每公式 2592 组合 = 3.4~15h 单进程, 拆 nshards 片 + 进程池并行。
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

SWEEP_PY = os.path.join(ROOT, "tools", "gs_5m_sweep.py")
COMBOS = os.path.join(ROOT, "output", "gs_filter", "refine_full_2592.json")

PASSERS = ["GS0607", "GS1318", "GS0737", "GS1333", "GS0651"]


def log(s):
    print(s, flush=True)


def run_shard(gs, start, end, nshards, shard):
    """跑一个公式的一个组合分片, 写 sweep_refine_shard{shard}of{nshards}.csv
    timeout 放宽到 6h: GS0651 类高信号公式单组合回测慢 (2026-09-09 2h 超时炸池教训)"""
    r = subprocess.run(
        [PY, "-X", "utf8", SWEEP_PY, "run", gs,
         "--start", start, "--end", end,
         "--stage", "refine", "--combos-file", COMBOS,
         "--shard", str(shard), "--nshards", str(nshards)],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=21600)
    return gs, shard, r.returncode == 0, (r.stderr or r.stdout)[-150:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20240901")
    ap.add_argument("--end", default=time.strftime("%Y%m%d"))
    ap.add_argument("--nshards", type=int, default=6)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--formulas", default=",".join(PASSERS))
    args = ap.parse_args()
    formulas = [x.strip() for x in args.formulas.split(",") if x.strip()]

    jobs = [(g, args.start, args.end, args.nshards, s)
            for g in formulas for s in range(args.nshards)]
    log("精调任务: %d 公式 × %d 片 = %d 任务, workers=%d"
        % (len(formulas), args.nshards, len(jobs), args.workers))
    t0 = time.time()
    done = 0
    fails = []
    with multiprocessing.Pool(processes=args.workers) as pool:
        for gs, shard, ok, err in pool.starmap(run_shard, jobs):
            done += 1
            if ok:
                log("[%d/%d] %s shard%d/%d ✓ (%.0fs)" %
                    (done, len(jobs), gs, shard, args.nshards, time.time() - t0))
            else:
                fails.append((gs, shard, err))
                log("[%d/%d] %s shard%d/%d ✗ %s" %
                    (done, len(jobs), gs, shard, args.nshards, err[:80]))
    log("精调并行完成: %d 任务 %.0fs, 失败 %d" % (len(jobs), time.time() - t0, len(fails)))
    if fails:
        log("失败清单: " + "; ".join("%s/%d" % (g, s) for g, s, _ in fails))


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
