# -*- coding: utf-8 -*-
"""自动寻优第 29 轮策略 (self-contained, 可独立运行): RSI超卖回升
生成时间: 2026-08-11 08:41
因子: rsi_rebound  入场参数: {"n": 14, "low_thr": 30}
出场: 硬止损=-0.06 移动止盈激活=0.035/回撤=0.01 (条件单语义) 时间=12天 阶梯=[(0.05, 0.5), (0.1, 0.5)]
仓位: 最多持 10 只, 单票 5000~30000元
手续费/滑点: 0.0003 / 0.001
区间: 20190101 ~ 20260731  股票池: hs300 (type=23, 剔除ST)
绩效: 年化=7.48% 回撤=-22.62% 夏普=0.51 交易=17736 胜率=65.5%
口径: 日线 / 止损优先 / 信号日T收盘买入 / 移动止盈条件单语义
"""
import os, sys
# standalone 脚本若在 research/auto_optimizer_runs/ 下, 上溯三层到 VERA 根
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "..")))

import numpy as np
import pandas as pd
from backtest.engine import BacktestEngine
from core.data_fetcher import DataFetcher
from selection.selector import StockSelector

START, END = "20190101", "20260731"
BT_CFG = {
    "initial_capital": 1000000.0,
    "commission": 0.0003,
    "slippage": 0.001,
    "period": "1d",
    "degrade_5m": True,
    "matrix_cache": True,
    "position_sizing": {
        "max_positions": 10,
        "min_buy_amount": 5000,
        "max_buy_amount": 30000,
        "lot_size": 100,
        "min_lots": 1
    }
}
STOP_CFG = {
    "priority": "stop_first",
    "cost_stop": {
        "enabled": True,
        "threshold": -0.06
    },
    "trailing_stop": {
        "enabled": True,
        "activation": 0.035,
        "drawdown": 0.01,
        "confirm": "real"
    },
    "ladder_tp": {
        "enabled": True,
        "levels": [
            {
                "profit": 0.05,
                "sell_ratio": 0.5
            },
            {
                "profit": 0.1,
                "sell_ratio": 0.5
            }
        ]
    },
    "time_stop": {
        "enabled": True,
        "max_hold_days": 12
    },
    "cond_time_stop": {
        "enabled": False
    },
    "first_day": {
        "enabled": False
    },
    "formula_sell": {
        "enabled": False
    },
    "capabilities": {
        "formula_exit": True,
        "gap_protection": True,
        "delisting": True
    }
}

def f_rsi_rebound(close, high=None, low=None, vol=None, n=14, low_thr=30, **_):
    """RSI 超卖回升: 昨日 RSI < low_thr 且 今日回升。"""
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(n).mean()
    loss = (-delta.clip(upper=0)).rolling(n).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return (rsi.shift(1) < low_thr) & (rsi > low_thr)


def signals_to_selections(signal_df: pd.DataFrame, formula_name: str) -> pd.DataFrame:
    """bool 信号 DataFrame -> BacktestEngine 可吃的 selections[stock_code, select_date, formula_name]。"""
    stacked = signal_df.stack()
    fired = stacked[stacked]
    if fired.empty:
        return pd.DataFrame(columns=["stock_code", "select_date", "formula_name"])
    s = pd.DataFrame(fired.index.tolist(), columns=["select_date", "stock_code"])
    s["formula_name"] = formula_name
    s["select_date"] = pd.to_datetime(s["select_date"]).dt.strftime("%Y%m%d")
    return s[["stock_code", "select_date", "formula_name"]]



def main():
    sel = StockSelector({"formula_name": "_placeholder",
                          "universe": {"type": "23", "exclude_st": True},
                          "period": "1d", "dividend_type": 1})
    codes = sel.resolve_universe()
    print(f"[INFO] 股票池 {len(codes)} 只")
    kl = DataFetcher.get_kline(codes, START, END, period="1d", dividend_type="front", use_cache=True)
    close = kl["Close"]; high = kl.get("High"); low = kl.get("Low"); vol = kl.get("Volume")
    sig = f_rsi_rebound(close, high=high, low=low, vol=vol, **{"n": 14, "low_thr": 30})
    selections = signals_to_selections(sig, "RSI超卖回升")
    print(f"[INFO] 信号 {len(selections)} 条, 覆盖 {selections['stock_code'].nunique()} 只")
    res = BacktestEngine(BT_CFG).run(selections=selections, start_time=START, end_time=END, stop_config=STOP_CFG)
    m = res.get("metrics") or {}
    print("=" * 60)
    print(f"年化={m.get('annualized_return',0)*100:.2f}%  回撤={m.get('max_drawdown',0)*100:.2f}%  "
          f"夏普={m.get('sharpe_ratio',0):.2f}  交易={m.get('total_trades')}  胜率={m.get('win_rate',0)*100:.1f}%")
    print("=" * 60)


if __name__ == "__main__":
    main()
