# -*- coding: utf-8 -*-
"""公式流水线总控入口 (2026-08-26, 全新编写)。

一键串联: S0 静态扫描 → 解析覆盖率 → S1-S3 日线三阶段 → S5 池子对比
→ (可选 --with-5m) S4 五分钟 → HTML 报告。

用法:
    python tools/formula_pipeline/run_pipeline.py [--run-dir <dir>]
        [--with-5m] [--report-only]

环境: 自动设置 VERA_KLINE_READONLY=1 (跑批禁止回源拉网)。
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
PY = sys.executable

STEPS = [
    ("S0 静态扫描", ["stage0_scan.py", "--run-dir"], "stage0_scan/report.json"),
    ("S0 解析覆盖率", ["verify_parse.py", "--run-dir"],
     "stage0_scan/parse_coverage.json"),
    ("S1-S3 日线三阶段", ["stage_d1.py", "--run-dir"], "stage_d1/results.json"),
]


def latest_run_dir(root: Path) -> Path:
    runs = sorted(root.glob("run_*"))
    if not runs:
        raise SystemExit("无 run 目录 — 先不带 --run-dir 跑一次")
    return runs[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", default=None, help="复用 run 目录 (缺省新建)")
    ap.add_argument("--with-5m", action="store_true", help="含 S4 五分钟阶段")
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()

    os.environ.setdefault("VERA_KLINE_READONLY", "1")
    root = HERE.parent.parent / "output" / "formula_pipeline"
    run_dir = Path(args.run_dir) if args.run_dir else latest_run_dir(root)
    print(f"[pipeline] run_dir = {run_dir}")

    def run_step(name, script_args, marker):
        marker_fp = run_dir / marker
        if marker_fp.exists():
            print(f"[pipeline] {name}: 已有产物, 跳过 ({marker})")
            return True
        t0 = time.time()
        r = subprocess.run([PY, str(HERE / script_args[0]),
                            *script_args[1:], str(run_dir)],
                           cwd=str(HERE.parent.parent))
        ok = r.returncode == 0 and marker_fp.exists()
        print(f"[pipeline] {name}: {'OK' if ok else 'FAIL'} "
              f"({time.time()-t0:.0f}s)")
        return ok

    if not args.report_only:
        for name, sa, marker in STEPS:
            if not run_step(name, sa, marker):
                print(f"[pipeline] {name} 失败, 中止")
                sys.exit(1)
        if args.with_5m:
            run_step("S4 五分钟", ["stage_5m.py", "--run-dir"],
                     "stage4_5m/results_5m.json")
        run_step("S5 池子对比", ["stage_pools.py", "--run-dir"],
                 "stage5_pools/pools.json")

    run_step("HTML 报告",
             [str(HERE / "report" / "build_html.py").replace(
                 str(HERE) + "\\", "").replace("\\", "/")],
             "report/final_report.html")
    print("[pipeline] 全部完成")


if __name__ == "__main__":
    main()
