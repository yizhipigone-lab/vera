"""
QUANTQQ 5m 冠军组合 engine.run() 全路径对拍 (2026-07-18)

目的: 扫描驱动 (run_cached 复刻路径) 的最终 Top 组合, 用 engine.run() 官方完整路径
(自选股缓存读信号, 引擎自己取数/过滤/退市处理) 独立复跑, 核对两口径数字一致。

用法:
    python tools/quantqq_5m_parity.py --cost -0.5 --act 0.08 --dd 0.001 --time 60
输出: stdout 最后一行 JSON {status, annret, maxdd, calmar, sharpe, winrate, trades}
"""
import argparse
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost", type=float, required=True)
    ap.add_argument("--act", type=float, required=True)
    ap.add_argument("--dd", type=float, required=True)
    ap.add_argument("--time", type=int, required=True)
    ap.add_argument("--sel", default=os.path.join(
        "output", "quantqq_5m_sweep", "cache", "selections.csv"))
    args = ap.parse_args()

    from backtest.engine import BacktestEngine

    selections = pd.read_csv(args.sel, dtype={"stock_code": str})
    bt_cfg = {
        "initial_capital": 3_000_000.0, "commission": 0.0003, "slippage": 0.001,
        "stamp_tax": 0.0005, "enable_realistic_costs": True, "period": "5m",
        "position_sizing": {"min_buy_amount": 2000.0, "max_buy_amount": 20000.0,
                            "lot_size": 100, "min_lots": 1},
        "use_kline_cache": True,
    }
    stop = {
        "priority": "trailing_first",
        "cost_stop": {"enabled": True, "threshold": args.cost},
        "trailing_stop": {"enabled": True, "activation": args.act, "drawdown": args.dd},
        "ladder_tp": {"enabled": False, "levels": []},
        "time_stop": {"enabled": True, "max_hold_days": args.time},
        "cond_time_stop": {"enabled": False},
        "first_day": {"enabled": False},
    }
    engine = BacktestEngine(bt_cfg)
    res = engine.run(selections=selections, start_time="20240801",
                     end_time="20260717", stop_config=stop)
    m = res["metrics"]
    print(json.dumps({
        "status": "ok",
        "annret": m.get("annualized_return", 0),
        "maxdd": m.get("max_drawdown", 0),
        "calmar": m.get("calmar_ratio", 0),
        "sharpe": m.get("sharpe_ratio", 0),
        "winrate": m.get("win_rate", 0),
        "trades": m.get("total_trades", 0),
    }))


if __name__ == "__main__":
    main()
