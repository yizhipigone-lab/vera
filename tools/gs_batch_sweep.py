"""
阶段B 两阶段扫描调度器 (2026-07-19)

读 signals_probe_all.json 有信号清单, 智能分流:
  - 大信号(>10万): prep + run coarse_subset36 (只粗筛, 防全量2592过慢)
  - 适中(<=10万): prep + run 全量2592 (找最优止盈止损)
ThreadPoolExecutor N=2 (通达信承受上限) + 防休眠 + 断点续跑(有report_merged.csv跳过).
gs_5m_sweep prep/run 幂等=中断重启不丢.

用法:
  python tools/gs_batch_sweep.py --workers 2
  python tools/gs_batch_sweep.py --workers 2 --limit 10        # 先跑10个试
  python tools/gs_batch_sweep.py --workers 2 --mode filter_all  # 全部只粗筛(快)
"""
import argparse
import json
import os
import subprocess
import sys
import time

from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SWEEP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gs_5m_sweep.py")
SUBSET = "output/gs_filter/coarse_subset36.json"
ALL = "output/gs_filter/signals_probe_all.json"
LOG = "output/gs_5m_sweep/batch_log.json"
START, END = "20240801", "20260717"
BIG_THRESH = 100_000  # >10万信号 = 大信号, 只粗筛


def call(cmd, timeout=1800):
    p = subprocess.run([sys.executable, "-X", "utf8"] + cmd,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    return p.returncode, (p.stdout or ""), (p.stderr or "")


def safe_dir(name):
    import re
    s = re.sub(r'[\\/:*?"<>|]', "_", name).strip().rstrip(".")
    return s or "UNNAMED"


def sweep_one(item, mode_override):
    formula = item["name"]
    signals = item["signals"]
    if mode_override == "filter_all":
        mode = "filter"
    elif mode_override == "full_all":
        mode = "full"
    else:
        mode = "filter" if signals > BIG_THRESH else "full"
    cache_meta = os.path.join("output", "gs_5m_sweep", safe_dir(formula),
                              "cache", "meta.json")
    report_csv = os.path.join("output", "gs_5m_sweep", safe_dir(formula),
                              "report_merged.csv")
    t0 = time.time()
    # 断点续跑: 已有 report_merged.csv 跳过 (除非 --force)
    if os.path.exists(report_csv):
        return {"formula": formula, "status": "skip_done", "mode": mode,
                "elapsed": 0.0}
    try:
        # prep (幂等: 有 meta.json 跳过选股取数)
        if not os.path.exists(cache_meta):
            rc, out, err = call([SWEEP, "prep", formula, "--start", START,
                                 "--end", END])
            if "no_signals" in out:
                return {"formula": formula, "status": "no_signals", "mode": mode,
                        "elapsed": round(time.time() - t0, 1)}
            if rc != 0:
                return {"formula": formula, "status": "prep_fail", "mode": mode,
                        "err": err[-80:], "elapsed": round(time.time() - t0, 1)}
        # run
        if mode == "filter":
            # --stage refine 让 --combos-file 生效 (否则走 coarse 2592). 修 bug: 2026-07-19
            cmd = [SWEEP, "run", formula, "--shard", "0", "--nshards", "1",
                   "--stage", "refine", "--combos-file", SUBSET]
        else:
            cmd = [SWEEP, "run", formula, "--shard", "0", "--nshards", "1",
                   "--stage", "coarse"]
        rc, out, err = call(cmd)
        return {"formula": formula, "status": "ok" if rc == 0 else "run_fail",
                "mode": mode, "err": err[-80:] if rc else "",
                "elapsed": round(time.time() - t0, 1)}
    except subprocess.TimeoutExpired:
        return {"formula": formula, "status": "timeout", "mode": mode,
                "elapsed": round(time.time() - t0, 1)}
    except Exception as e:
        return {"formula": formula, "status": "error", "mode": mode,
                "err": f"{type(e).__name__}: {str(e)[:70]}",
                "elapsed": round(time.time() - t0, 1)}


def main():
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
    except Exception:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--mode", choices=["auto", "filter_all", "full_all"],
                    default="auto")
    args = ap.parse_args()

    data = json.load(open(ALL, encoding="utf-8"))
    items = data["has_sorted"]
    if args.limit:
        items = items[: args.limit]
    n_big = sum(1 for i in items if i["signals"] > BIG_THRESH)
    print(f"[batch] 总 {len(items)}: 大信号(粗筛) {n_big} / 适中(全量) {len(items)-n_big}")
    print(f"[batch] workers={args.workers} mode={args.mode}")

    results = []
    if os.path.exists(LOG):
        try:
            results = json.load(open(LOG, encoding="utf-8"))
        except Exception:
            results = []
    done = {r["formula"] for r in results}
    todo = [it for it in items if it["name"] not in done]
    print(f"[batch] 已完成 {len(done)}, 待跑 {len(todo)}")

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(sweep_one, it, args.mode): it for it in todo}
        for fut in as_completed(futs):
            r = fut.result()
            results.append(r)
            # 增量落盘
            json.dump(results, open(LOG, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
            ok = sum(1 for x in results if x["status"] == "ok")
            print(f"[{len(results)}/{len(items)}] {r['formula']} -> "
                  f"{r['status']} ({r['mode']}, {r['elapsed']}s) | 累计ok={ok}",
                  flush=True)
    print(f"[DONE] {len(results)} 个, 用时 {(time.time()-t0)/60:.1f}min")


if __name__ == "__main__":
    main()
