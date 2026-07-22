# -*- coding: utf-8 -*-
"""Top10 × 2592 止损优先(stop_first) 批量续跑驱动 (2026-07-23)

背景: 昨晚批跑 (output/top10_stopfirst_5m_log.txt) 在 W-105 1041/2592 处进程死亡.
gs_5m_sweep.py run 内置断点续跑 (已完成 key 自动跳过), 本驱动只做:
  1. 逐公式 subprocess 调 gs_5m_sweep run --priority stop_first
  2. 已完成 2592 的公式直接跳过
  3. 子进程异常死亡时重试 (最多 3 次/公式)
  4. SetThreadExecutionState 防系统休眠 (重蹈昨晚覆辙)
  5. 追加写 output/top10_stopfirst_5m_log.txt (utf-8)

规则3: 独立新文件, 不修改 gs_5m_sweep.py / gs_batch_sweep.py.
"""
import csv
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_BASE = os.path.join(ROOT, "output", "gs_5m_sweep_stop_first")
LOG_PATH = os.path.join(ROOT, "output", "top10_stopfirst_5m_log.txt")
TOTAL = 2592
MAX_RETRY = 3

# 顺序沿用昨晚批跑日志; 前 2 个已完成会自动跳过
FORMULAS = [
    "黑马选股1", "TKXG", "W-105", "GUPIAO_070", "成交组合",
    "双佛手向上", "突破指标", "枪挑小梁王", "次日涨停选股", "有涨停",
]


def _prevent_sleep():
    """Windows 防休眠: 告诉系统有任务在跑, 不许睡."""
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    except Exception:
        pass  # 非 Windows 或失败都不阻塞主流程


def _csv_done_count(formula):
    """读已有 sweep CSV, 返回 annret 非空的组合数 (断点依据)."""
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
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_formula(formula):
    """跑单公式, 子进程死掉自动重试. 返回 (ok, done_count)."""
    for attempt in range(1, MAX_RETRY + 1):
        t0 = time.time()
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        proc = subprocess.run(
            [sys.executable, os.path.join("tools", "gs_5m_sweep.py"),
             "run", formula, "--shard", "0", "--nshards", "1",
             "--priority", "stop_first"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=86400)
        minutes = round((time.time() - t0) / 60, 1)
        done = _csv_done_count(formula)
        # 取子进程最后一行 JSON 摘要 (gs_5m_sweep 结尾 print)
        tail = (proc.stdout or "").strip().splitlines()
        summary = tail[-1][:200] if tail else ""
        if done >= TOTAL:
            _log(f"{formula}: OK {done}/{TOTAL} ({minutes}min, 第{attempt}次) {summary}")
            return True, done
        _log(f"{formula}: 未完成 {done}/{TOTAL} rc={proc.returncode} "
             f"({minutes}min, 第{attempt}次) err={(proc.stderr or '')[-150:]!r}")
    return False, done


def main():
    _prevent_sleep()
    _log(f"=== Top10 止损优先 2592 续跑启动 (pid={os.getpid()}) ===")
    results = {}
    for i, formula in enumerate(FORMULAS, 1):
        done = _csv_done_count(formula)
        if done >= TOTAL:
            _log(f"[{i}/10] {formula} 已完成 {done}/{TOTAL}, 跳过")
            results[formula] = {"status": "skip_done", "done": done}
            continue
        _log(f"[{i}/10] {formula} 续跑 {done}/{TOTAL} -> {TOTAL}")
        ok, done = run_formula(formula)
        results[formula] = {"status": "ok" if ok else "incomplete", "done": done}
    n_ok = sum(1 for r in results.values() if r["done"] >= TOTAL)
    _log(f"=== 批跑结束: {n_ok}/10 公式完成 2592 ===")
    _log(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
