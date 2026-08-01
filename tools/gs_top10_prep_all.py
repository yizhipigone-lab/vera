# -*- coding: utf-8 -*-
"""Top10 全A 批量 prep (2026-07-23)

串行 prep 10 公式 (通达信瓶颈, 不能并行).
已有 全A cache 的公式自动跳过 (prep 内部 selections.csv 命中即跳过).
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(ROOT, "output", "top10_prep_all_log.txt")

FORMULAS = [
    "黑马选股1", "TKXG", "W-105", "GUPIAO_070", "成交组合",
    "双佛手向上", "突破指标", "枪挑小梁王", "次日涨停选股", "有涨停",
]


def _log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    _log("=== Top10 全A prep 启动 (config type=50) ===")
    t_start = time.time()
    ok = 0
    for i, f in enumerate(FORMULAS, 1):
        _log(f"[{i}/10] {f}: prep 开始...")
        t0 = time.time()
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        proc = subprocess.run(
            [sys.executable, os.path.join("tools", "gs_5m_sweep.py"),
             "prep", f, "--start", "20240801", "--end", "20260717",
             "--priority", "stop_first"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=3600)
        minutes = round((time.time() - t0) / 60, 1)
        if proc.returncode == 0:
            ok += 1
            tail = (proc.stdout or "").strip().splitlines()
            last = tail[-1][:200] if tail else ""
            _log(f"[{i}/10] {f}: OK ({minutes}min) {last}")
        else:
            err = (proc.stderr or "")[-200:]
            _log(f"[{i}/10] {f}: FAIL rc={proc.returncode} ({minutes}min) {err!r}")
    total = round((time.time() - t_start) / 60, 1)
    _log(f"=== prep 结束: {ok}/10 成功, 耗时 {total}min ===")


if __name__ == "__main__":
    main()
