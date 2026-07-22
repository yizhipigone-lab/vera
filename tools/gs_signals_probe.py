"""
gs_txt 公式全A选股信号探测 (2026-07-18)

公式存在 != 有信号. GUPIAO_001 输出 IF(cond,30,0) 不产生 Value==1, 全A 0 信号.
本脚本对每个公式全A选股, 淘汰 0 信号, 得到真正可回测的公式清单.
(单公式全A选股 ~60s, 519 串行 ~9h; 支持 --shard/--nshards subprocess 并行)

用法:
  python tools/gs_signals_probe.py --names-file output/gs_filter/names_sample.txt
  python tools/gs_signals_probe.py --limit 20
  python tools/gs_signals_probe.py --shard 0 --nshards 6
  python tools/gs_signals_probe.py                 # 全量 519
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPORT = "output/gs_filter/report.json"
OUT = "output/gs_filter/signals_probe.json"
SUB_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gs_select_one.py")


def probe_one(name, start, end, timeout=300):
    """subprocess 调 gs_select_one.py — 独立进程避免 TQ 连接复用断连 bug."""
    t0 = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", SUB_SCRIPT, name, start, end],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace")
        lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip().startswith("{")]
        if not lines:
            return {"status": "error", "signals": 0, "stocks": 0,
                    "elapsed": round(time.time() - t0, 1),
                    "err": (proc.stderr or proc.stdout)[-100:].strip()}
        r = json.loads(lines[-1])
        r["elapsed"] = round(time.time() - t0, 1)
        return r
    except subprocess.TimeoutExpired:
        return {"status": "error", "signals": 0, "stocks": 0,
                "elapsed": round(time.time() - t0, 1), "err": f"timeout>{timeout}s"}
    except Exception as e:
        return {"status": "error", "signals": 0, "stocks": 0,
                "elapsed": round(time.time() - t0, 1),
                "err": f"{type(e).__name__}: {str(e)[:60]}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--names", default=None)
    ap.add_argument("--names-file", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--nshards", type=int, default=1)
    ap.add_argument("--start", default="20240801")
    ap.add_argument("--end", default="20260717")
    ap.add_argument("--timeout", type=int, default=300, help="单公式选股超时秒")
    ap.add_argument("--resume", action="store_true", help="断点续跑: 跳过已完成的公式")
    args = ap.parse_args()

    # 防系统自动休眠 (跑期间; 进程退出自动恢复, 无需改电源设置). 2026-07-19
    # 原因: 用户睡觉机器休眠 → 进程冻结 → 6h 实际只跑 13.5min
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
    except Exception:
        pass

    if args.names_file:
        with open(args.names_file, encoding="utf-8") as f:
            names = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    elif args.names:
        names = args.names.split(",")
    else:
        r = json.load(open(REPORT, encoding="utf-8"))
        names = [e["name"] for e in r["ok"]]
    names = [n for i, n in enumerate(names) if i % args.nshards == args.shard]
    if args.limit:
        names = names[: args.limit]

    if args.nshards > 1:
        out_path = f"output/gs_filter/signals_probe_shard{args.shard}of{args.nshards}.json"
    else:
        out_path = OUT

    res = {"total": len(names), "has_signals": [], "zero_signals": [],
           "errors": [], "details": []}
    # 断点续跑: 只继承成功的(status=ok), 错误的重跑. 修 bug: 2026-07-19
    if args.resume and os.path.exists(out_path):
        prev = json.load(open(out_path, encoding="utf-8"))
        prev_ok = [d for d in prev.get("details", []) if d.get("status") == "ok"]
        done = set(d["name"] for d in prev_ok)
        before = len(names)
        names = [n for n in names if n not in done]
        res["details"] = list(prev_ok)
        res["has_signals"] = [{"name": d["name"], "signals": d["signals"],
                               "stocks": d.get("stocks", 0)}
                              for d in prev_ok if d.get("signals", 0) > 0]
        res["zero_signals"] = [d["name"] for d in prev_ok if d.get("signals", 0) == 0]
        res["errors"] = []
        print(f"[resume] keep {len(prev_ok)} ok, retry {len(names)} failed",
              flush=True)
    os.makedirs("output/gs_filter", exist_ok=True)
    t0 = time.time()
    for i, name in enumerate(names):
        r = probe_one(name, args.start, args.end, timeout=args.timeout)
        r["name"] = name
        res["details"].append(r)
        if r["status"] == "error":
            res["errors"].append(name)
        elif r["signals"] == 0:
            res["zero_signals"].append(name)
        else:
            res["has_signals"].append({"name": name, "signals": r["signals"],
                                       "stocks": r["stocks"]})
        res["seconds"] = round(time.time() - t0, 1)
        # 增量落盘: 每公式后写 json, 防休眠/中断丢进度, 支持 --resume 续跑
        json.dump(res, open(out_path, "w", encoding="utf-8"), ensure_ascii=False)
        print(f"[{i+1}/{len(names)}] {name} -> {r['status']} "
              f"signals={r['signals']} stocks={r['stocks']} ({r['elapsed']}s)",
              flush=True)
    res["seconds"] = round(time.time() - t0, 1)
    json.dump(res, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[OK] total={res['total']} has_signals={len(res['has_signals'])} "
          f"zero={len(res['zero_signals'])} err={len(res['errors'])} {res['seconds']}s")
    print(f"[OUT] {out_path}")


if __name__ == "__main__":
    main()
