# -*- coding: utf-8 -*-
"""Top10 全A 并行 prep (2026-07-23)

并行 prep 10 公式。每个子进程独立连 TDX, TdxW.exe 服务端扛并发。
已有 全A cache 的公式 prep 内部缓存命中秒过。
"""
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(ROOT, "output", "top10_prep_parallel_log.txt")

FORMULAS = [
    "黑马选股1", "TKXG", "W-105", "GUPIAO_070", "成交组合",
    "双佛手向上", "突破指标", "枪挑小梁王", "次日涨停选股", "有涨停",
]
MAX_WORKERS = 3  # 2026-07-23: 沪深300 轻量, 3并发安全


def _log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def prep_one(formula):
    t0 = time.time()
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, os.path.join("tools", "gs_5m_sweep.py"),
         "prep", formula, "--start", "20240801", "--end", "20260717",
         "--priority", "stop_first"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=3600)
    minutes = round((time.time() - t0) / 60, 1)
    tail = (proc.stdout or "").strip().splitlines()
    last = tail[-1][:200] if tail else ""
    if proc.returncode == 0:
        return {"formula": formula, "ok": True, "minutes": minutes, "summary": last}
    else:
        err = (proc.stderr or "")[-200:]
        return {"formula": formula, "ok": False, "minutes": minutes, "error": err, "summary": last}


def main():
    _log(f"=== Top10 全A 并行 prep 启动 (config type=50, workers={MAX_WORKERS}) ===")
    t_start = time.time()

    results = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(FORMULAS))) as pool:
        futures = {pool.submit(prep_one, f): f for f in FORMULAS}
        for future in as_completed(futures):
            r = future.result()
            results[r["formula"]] = r
            n_ok = sum(1 for v in results.values() if v["ok"])
            _log(f"[{n_ok}/{len(results)}] {r['formula']}: {'OK' if r['ok'] else 'FAIL'} "
                 f"({r['minutes']}min) {r.get('summary', '')[:120]}")

    total = round((time.time() - t_start) / 60, 1)
    n_ok = sum(1 for v in results.values() if v["ok"])
    _log(f"=== 并行 prep 结束: {n_ok}/{len(FORMULAS)} 成功, 耗时 {total}min ===")
    for f, r in results.items():
        if not r["ok"]:
            _log(f"  FAIL {f}: {r.get('error', '?')}")


if __name__ == "__main__":
    main()
