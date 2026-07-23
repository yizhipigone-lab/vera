# -*- coding: utf-8 -*-
"""Top10 × 2592 止损优先 并行驱动 (2026-07-23)

与 gs_top10_stopfirst_batch.py 的区别: 并行而非串行。
所有公式同时 subprocess 调 gs_5m_sweep run --priority stop_first,
用 ThreadPoolExecutor 管理并发度。

规则3: 独立新文件, 不修改 gs_5m_sweep.py / gs_batch_sweep.py.
"""
import csv
import json
import os
import subprocess
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_BASE = os.path.join(ROOT, "output", "gs_5m_sweep_stop_first")
LOG_PATH = os.path.join(ROOT, "output", "top10_parallel_5m_log.txt")
TOTAL = 2592
MAX_WORKERS = 7  # 7 个公式同时跑

# 已完成 2592 的跳过, 其余并行跑
DONE_FORMULAS = set()  # 2026-07-23: 全A重跑, 旧沪深300结果已清, 全部重跑
FORMULAS = [
    "黑马选股1", "TKXG", "W-105", "GUPIAO_070", "成交组合",
    "双佛手向上", "突破指标", "枪挑小梁王", "次日涨停选股", "有涨停",
]

_lock = threading.Lock()


def _prevent_sleep():
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    except Exception:
        pass


def _csv_done_count(formula):
    path = os.path.join(OUT_BASE, formula, "sweep_coarse_shard0of1.csv")
    if not os.path.exists(path):
        return 0
    n = 0
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("annret"):
                n += 1
    return n


def _log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with _lock:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def run_formula(formula):
    """跑单公式, 子进程死掉自动重试最多3次."""
    for attempt in range(1, 4):
        t0 = time.time()
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, os.path.join("tools", "gs_5m_sweep.py"),
                 "run", formula, "--shard", "0", "--nshards", "1",
                 "--priority", "stop_first"],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=86400)
        except subprocess.TimeoutExpired:
            _log(f"{formula}: 超时 (第{attempt}次)")
            continue
        minutes = round((time.time() - t0) / 60, 1)
        done = _csv_done_count(formula)
        tail = (proc.stdout or "").strip().splitlines()
        summary = tail[-1][:200] if tail else ""
        if done >= TOTAL:
            _log(f"{formula}: OK {done}/{TOTAL} ({minutes}min, 第{attempt}次) {summary}")
            return {"formula": formula, "status": "ok", "done": done, "minutes": minutes}
        _log(f"{formula}: 未完成 {done}/{TOTAL} rc={proc.returncode} "
             f"({minutes}min, 第{attempt}次) err={(proc.stderr or '')[-150:]!r}")
    return {"formula": formula, "status": "incomplete", "done": _csv_done_count(formula)}


def main():
    _prevent_sleep()
    _log(f"=== Top10 止损优先 2592 并行启动 (pid={os.getpid()}, workers={MAX_WORKERS}) ===")

    # 重置 GUPIAO_070 部分结果 (只有 276 行, 不完整)
    gupiao_csv = os.path.join(OUT_BASE, "GUPIAO_070", "sweep_coarse_shard0of1.csv")
    if os.path.exists(gupiao_csv):
        done = _csv_done_count("GUPIAO_070")
        if done < TOTAL:
            os.remove(gupiao_csv)
            _log(f"GUPIAO_070: 重置部分结果 ({done} 行 -> 0), 重新跑")

    # 分拣: 已完成 vs 待跑
    todo = []
    for f in FORMULAS:
        if f in DONE_FORMULAS:
            _log(f"{f}: 已完成 2592, 跳过")
        else:
            todo.append(f)

    _log(f"待跑 {len(todo)} 公式: {', '.join(todo)}")
    t_start = time.time()

    results = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(todo))) as pool:
        futures = {pool.submit(run_formula, f): f for f in todo}
        for future in as_completed(futures):
            r = future.result()
            results[r["formula"]] = r
            n_done = sum(1 for v in results.values() if v["status"] == "ok")
            _log(f"进度: {n_done}/{len(todo)} 完成 ({r['formula']}: {r['status']})")

    total_min = round((time.time() - t_start) / 60, 1)
    n_ok = sum(1 for r in results.values() if r["status"] == "ok")
    _log(f"=== 并行批跑结束: {n_ok}/{len(todo)} 新完成, 总计 {len(DONE_FORMULAS)+n_ok}/10, 耗时 {total_min}min ===")
    _log(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
