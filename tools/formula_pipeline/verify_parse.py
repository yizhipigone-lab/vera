# -*- coding: utf-8 -*-
"""解析覆盖率验证 (验收协议第三条, 2026-08-26)。

对 S0 幸存公式逐个走「解析+求值」全链路 (小矩阵假数据, 不读K线),
输出可执行率与出局原因分布 → <run>/stage0_scan/parse_coverage.json

用法:
    python tools/formula_pipeline/verify_parse.py [--run-dir <dir>]
"""
import argparse
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from tools.formula_pipeline.common import (  # noqa: E402
    GS_DIR, load_json, read_formula_txt, save_json)
from tools.formula_pipeline.interpreter.functions import (  # noqa: E402
    UnsupportedFormula)
from tools.formula_pipeline.interpreter.runner import run_formula  # noqa: E402

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    s0 = load_json(run_dir / "stage0_scan" / "report.json")
    ok_list = s0["ok"]

    T, N = 60, 3
    rng = np.random.default_rng(42)
    data = {
        "open": rng.random((T, N)) * 10 + 5,
        "high": rng.random((T, N)) * 2 + 6,
        "low": rng.random((T, N)) * 2 + 1,
        "close": rng.random((T, N)) * 10 + 2,
        "volume": rng.random((T, N)) * 1000 + 100,
        "amount": rng.random((T, N)) * 1e6 + 1e4,
        "__index__": pd.date_range("2024-01-01", periods=T),
    }
    codes = ["600000.SH", "000001.SZ", "300001.SZ"]

    stat = {"executable": 0, "exec_error": 0, "unsupported": 0}
    reasons, errors = Counter(), []
    t0 = time.time()
    for e in ok_list:
        try:
            f = read_formula_txt(GS_DIR / e["file"])
            run_formula(f["source"], f["params"], data, codes)
            stat["executable"] += 1
        except UnsupportedFormula as ex:
            stat["unsupported"] += 1
            reasons[str(ex)[:60]] += 1
        except Exception as ex:
            stat["exec_error"] += 1
            errors.append({"name": e["name"],
                           "err": f"{type(ex).__name__}: {str(ex)[:80]}"})
    elapsed = time.time() - t0

    cov = {
        "s0_survivors": len(ok_list),
        "executable": stat["executable"],
        "coverage_pct": round(stat["executable"] / len(ok_list) * 100, 1),
        "unsupported": stat["unsupported"],
        "exec_error": stat["exec_error"],
        "elapsed_s": round(elapsed, 1),
        "unsupported_reasons": dict(reasons.most_common()),
        "exec_errors": errors,
    }
    out = run_dir / "stage0_scan" / "parse_coverage.json"
    save_json(out, cov)
    print(f"[parse] 可执行 {cov['executable']}/{cov['s0_survivors']} "
          f"= {cov['coverage_pct']}%  (unsupported {stat['unsupported']}, "
          f"exec_error {stat['exec_error']}, {cov['elapsed_s']}s)")
    print(f"[OUT] {out}")


if __name__ == "__main__":
    main()
