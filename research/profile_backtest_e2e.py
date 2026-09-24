"""回测端到端性能画像 (2026-08-16, 1m 瓶颈定位)。

量化"取数 / 建矩阵 / 核心循环"三段在真实 1m 回测里的时间占比,
判断 1m 慢到底慢在哪 (取数 or 循环)。依赖 TDX 连接 + 1m 数据
(深度仅 2026-01-26 起)。

用法:
    python -X utf8 research/profile_backtest_e2e.py [n_stocks] [start] [end]
默认: 20 只股, 2026-06-15 ~ 2026-08-01, period=1m, 冷跑+热跑各一次。
"""
from __future__ import annotations

import cProfile
import io
import os
import pstats
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from backtest.engine import BacktestEngine

# 20 只高流动性代表股 (覆盖沪深主板/创业板, 确保 1m 有数据)
_LIQUID_CODES = [
    "600519.SH", "000001.SZ", "300750.SZ", "601318.SH", "000858.SZ",
    "600036.SH", "000333.SZ", "601899.SH", "002594.SZ", "300059.SZ",
    "600900.SH", "000651.SZ", "601012.SH", "002415.SZ", "600030.SH",
    "000725.SZ", "601398.SH", "600276.SH", "300124.SZ", "002475.SZ",
]

BT_CFG = {
    "initial_capital": 1_000_000.0,
    "commission": 0.0003,
    "slippage": 0.001,
    "period": "1m",
    "use_kline_cache": True,
    "matrix_cache": False,   # 先关, 测真实取数+建矩阵; 想测"命中后只剩循环"再开
    "position_sizing": {"min_buy_amount": 2000.0, "max_buy_amount": 20000.0,
                        "lot_size": 100, "min_lots": 1},
}

STOP_CFG = {
    "priority": "stop_first",
    "cost_stop": {"enabled": True, "threshold": -0.06},
    "trailing_stop": {"enabled": True, "activation": 0.035, "drawdown": 0.01,
                      "confirm": "real"},
    "ladder_tp": {"enabled": False, "levels": []},
    "time_stop": {"enabled": True, "max_hold_days": 12},
    "cond_time_stop": {"enabled": False},
    "first_day": {"enabled": False},
    "formula_sell": {"enabled": False},
    "capabilities": {"formula_exit": True, "gap_protection": True, "delisting": True},
}


def make_selections(codes, signal_date):
    return pd.DataFrame([{"stock_code": c, "select_date": pd.Timestamp(signal_date),
                          "formula_name": "profile"} for c in codes])


def run_once(selections, start, end):
    eng = BacktestEngine(BT_CFG)
    return eng.run(selections=selections, start_time=start, end_time=end,
                   stop_config=STOP_CFG)


def main() -> None:
    n_stocks = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    start = sys.argv[2] if len(sys.argv) > 2 else "2026-06-15"
    end = sys.argv[3] if len(sys.argv) > 3 else "2026-08-01"
    signal_date = "2026-07-15"
    codes = _LIQUID_CODES[:min(n_stocks, len(_LIQUID_CODES))]
    sel = make_selections(codes, signal_date)
    print(f"规模: {len(codes)} 只股, {start}~{end}, period=1m, 信号日 {signal_date}")

    # ── 冷跑 (真实取数, 可能慢) ──
    t0 = time.time()
    res = run_once(sel, start, end)
    dt_cold = time.time() - t0
    n_trades = len(res.trades)
    print(f"\n[冷跑] 取数+建矩阵+循环 总耗时: {dt_cold:.2f}s  (成交 {n_trades} 笔)")

    # ── 热跑 (kline 缓存命中, 只剩建矩阵+循环) ──
    t0 = time.time()
    res2 = run_once(sel, start, end)
    dt_warm = time.time() - t0
    print(f"[热跑] kline 缓存命中 总耗时: {dt_warm:.2f}s  (成交 {len(res2.trades)} 笔)")
    print(f"       → 纯取数(网络) 约占: {max(dt_cold - dt_warm, 0):.2f}s")

    # ── cProfile 热跑, 看 CPU 热点 ──
    prof = cProfile.Profile()
    prof.enable()
    run_once(sel, start, end)
    prof.disable()
    s = io.StringIO()
    ps = pstats.Stats(prof, stream=s).sort_stats("cumtime")
    ps.print_stats(25)
    print("\n── 热点 Top (cumtime, 热跑) ──")
    print(s.getvalue())


if __name__ == "__main__":
    main()
