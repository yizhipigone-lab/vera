"""
gs_txt 单公式全A选股 — 独立进程 (2026-07-18)

为什么单独一个脚本: StockSelector.run() 每次 initialize+close TQ 连接,
单进程内串行跑多公式会断连 (实测). subprocess 独立进程是 CLAUDE.md 认可的批量模式.

用法:
    python -X utf8 tools/gs_select_one.py 黑马绝技 20240801 20260717
stdout 最后一行 JSON:
    {"status":"ok","formula":"...","signals":N,"stocks":N,"elapsed":..}
"""
import sys
import os
import time
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.config_loader import ConfigLoader  # noqa: E402
from selection.selector import StockSelector  # noqa: E402
import logging  # noqa: E402

logging.getLogger().setLevel(logging.WARNING)  # 压掉批次刷屏


def main():
    name = sys.argv[1]
    start = sys.argv[2] if len(sys.argv) > 2 else "20240801"
    end = sys.argv[3] if len(sys.argv) > 3 else "20260717"
    defaults = ConfigLoader.load_defaults()
    sel_cfg = {"formula_name": name, "formula_arg": "",
               "universe": defaults["selection"].get("universe", {}),
               "period": "1d", "dividend_type": 1}
    t0 = time.time()
    try:
        sel = StockSelector(sel_cfg).run(start_time=start, end_time=end)
        n = 0 if sel is None else len(sel)
        ns = 0 if sel is None else sel["stock_code"].nunique()
        print(json.dumps({"status": "ok", "formula": name, "signals": n,
                          "stocks": ns, "elapsed": round(time.time() - t0, 1)}))
    except Exception as e:
        print(json.dumps({"status": "error", "formula": name, "signals": 0,
                          "stocks": 0, "elapsed": round(time.time() - t0, 1),
                          "err": f"{type(e).__name__}: {str(e)[:80]}"}))


if __name__ == "__main__":
    main()
