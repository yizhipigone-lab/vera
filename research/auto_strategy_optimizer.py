# -*- coding: utf-8 -*-
"""
auto_strategy_optimizer.py
==========================
纯 Python 因子 + VERA 引擎, 自动迭代寻优 A 股策略 (2019-01-01 ~ 2026-07-31)。

口径 (用户拍板, 不改):
  - 日线 (period=1d)
  - 止损优先 (priority=stop_first)
  - 移动止盈条件单语义 (trailing_stop.confirm="real"):
        本 bar 创峰值 → 不判定 (不拿新线审判本 bar 旧低点)
        跳空开盘已在线下 → 按开盘价成交 (集合竞价拿不到线价)
        盘中 Low 触线 → 按线价成交 (隔夜挂好的条件单, 实盘可达)
  - 信号日 T 收盘价买入 (引擎 _build_entry_signals 铁律)
  - 股票池/手续费/滑点/仓位 = 自动寻优边界

达标门槛 (同时满足, 否则换因子/参数/出场继续 loop):
  年化收益率 >= 25%  且  历史最大回撤 <= 30%

用法:
  python -X utf8 research/auto_strategy_optimizer.py --smoke        # 冒烟 (沪深300, 1组合, 验证框架)
  python -X utf8 research/auto_strategy_optimizer.py --pool hs300    # 沪深300 寻优
  python -X utf8 research/auto_strategy_optimizer.py --pool alla     # 全A 寻优 (慢, 每组合 1-3 分钟)
  python -X utf8 research/auto_strategy_optimizer.py --pool alla --max-iters 300 --seed 42

输出:
  - 每轮: research/auto_optimizer_runs/run_NNN_<factor>.py  (该策略完整可独立运行代码)
  - 每轮: research/auto_optimizer_runs/results.csv           (累计指标表)
  - 达标: research/auto_optimizer_runs/champion.py + .json   (达标策略)
"""
from __future__ import annotations

import argparse
import csv
import inspect
import json
import os
import random
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from backtest.engine import BacktestEngine
from core.data_fetcher import DataFetcher
from selection.selector import StockSelector
from utils.logger import get_logger

logger = get_logger(__name__)

# ============================ 全局配置 ============================
START, END = "20190101", "20260731"
PERIOD = "1d"
CAPITAL = 1_000_000.0
COMMISSION = 0.0003          # 万3 (双边)
SLIPPAGE = 0.001             # 千1
TARGET_ANN = 0.25            # 年化 >= 25%
MAX_DD = 0.30                # 最大回撤 <= 30%
RUN_DIR = "research/auto_optimizer_runs"
POOL_MAP = {"alla": "50", "hs300": "23", "zz500": "24", "czb": "51", "kcb": "52"}


# ============================ BT_CFG / stop_config 工厂 ============================
def make_bt_cfg(max_positions: int, min_amt: float, max_amt: float) -> dict:
    """回测引擎配置: 资金/手续费/滑点/仓位。"""
    return {
        "initial_capital": CAPITAL,
        "commission": COMMISSION,
        "slippage": SLIPPAGE,
        "period": PERIOD,
        "degrade_5m": True,
        "matrix_cache": True,  # 复用 prep 矩阵缓存: 同因子同持仓期组合取数只算1次 (用户指示: 充分使用本地K线缓存)
        "position_sizing": {
            "max_positions": max_positions,
            "min_buy_amount": min_amt,
            "max_buy_amount": max_amt,
            "lot_size": 100,
            "min_lots": 1,
        },
    }


def make_stop(cost_stop=-0.06, trail_act=0.035, trail_dd=0.01,
              time_days=12, ladder=None) -> dict:
    """止损优先 + 移动止盈条件单语义 (confirm=real)。

    ladder: [(profit, sell_ratio), ...] 或 None。
    """
    lv = [{"profit": float(p), "sell_ratio": float(r)} for p, r in ladder] if ladder else []
    return {
        "priority": "stop_first",
        "cost_stop": {"enabled": True, "threshold": float(cost_stop)},
        "trailing_stop": {
            "enabled": True,
            "activation": float(trail_act),
            "drawdown": float(trail_dd),
            "confirm": "real",  # 条件单语义 (用户铁律)
        },
        "ladder_tp": {"enabled": bool(lv), "levels": lv},
        "time_stop": {"enabled": True, "max_hold_days": int(time_days)},
        "cond_time_stop": {"enabled": False},
        "first_day": {"enabled": False},
        "formula_sell": {"enabled": False},
        "capabilities": {"formula_exit": True, "gap_protection": True, "delisting": True},
    }


# ============================ 数据层 ============================
def load_universe(pool: str) -> list:
    utype = POOL_MAP[pool]
    sel = StockSelector({
        "formula_name": "_placeholder",  # 仅取池, 不选股; resolve_universe 不依赖此字段
        "universe": {"type": utype, "exclude_st": True},
        "period": PERIOD,
        "dividend_type": 1,
    })
    codes = sel.resolve_universe()
    logger.info("股票池 %s (type=%s) -> %d 只", pool, utype, len(codes))
    return codes


def load_ohlc(codes: list):
    t0 = time.time()
    kl = DataFetcher.get_kline(
        codes, START, END, period=PERIOD,
        dividend_type="front", use_cache=True,
    )
    if not kl or "Close" not in kl:
        raise RuntimeError("DataFetcher.get_kline 拉取日线失败, 检查 TDX 连接")
    close = kl["Close"]
    high = kl.get("High")
    low = kl.get("Low")
    vol = kl.get("Volume")
    logger.info("OHLCV 就绪: %d 股 × %d 日, 取数耗时 %.0fs",
                close.shape[1], close.shape[0], time.time() - t0)
    return close, high, low, vol


# ============================ 因子库 (纯 pandas, 返回 bool 信号 DataFrame) ============================
# 签名统一: (close, high=None, low=None, vol=None, **params) -> DataFrame[bool]
# 信号 True = 当日触发买入 (信号日 T 收盘买入, 由引擎执行)
def f_ma_cross(close, high=None, low=None, vol=None, fast=5, slow=20, **_):
    """均线金叉: 短均线上穿长均线 (经典趋势跟随)。"""
    maf = close.rolling(fast).mean()
    mas = close.rolling(slow).mean()
    return (maf > mas) & (maf.shift(1) <= mas.shift(1))


def f_breakout(close, high=None, low=None, vol=None, n=20, **_):
    """突破: 收盘价 > 昨日 N 日最高 (创 N 日新高)。"""
    hh = close.rolling(n).max().shift(1)
    return close > hh


def f_volume_breakout(close, high=None, low=None, vol=None, n=20, k=2.0, **_):
    """放量突破: 创 N 日新高 且 量 > N 日均量 × k。"""
    if vol is None:
        return pd.DataFrame(False, index=close.index, columns=close.columns)
    hh = close.rolling(n).max().shift(1)
    vma = vol.rolling(n).mean().shift(1)
    return (close > hh) & (vol > vma * k)


def f_momentum(close, high=None, low=None, vol=None, n=20, thr=0.08, **_):
    """动量: N 日涨幅 > thr (横截面动量)。"""
    return close.pct_change(n) > thr


def f_reversal(close, high=None, low=None, vol=None, n=10, drop=-0.08, **_):
    """超跌反弹: 前 N 日跌幅 < drop 且 当日收阳企稳。"""
    ret = close.pct_change(n)
    return (ret.shift(1) < drop) & (close > close.shift(1))


def f_rsi_rebound(close, high=None, low=None, vol=None, n=14, low_thr=30, **_):
    """RSI 超卖回升: 昨日 RSI < low_thr 且 今日回升。"""
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(n).mean()
    loss = (-delta.clip(upper=0)).rolling(n).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return (rsi.shift(1) < low_thr) & (rsi > low_thr)


def f_lowvol_breakout(close, high=None, low=None, vol=None, n=20, q=0.3, **_):
    """低波突破: 日收益波动率处于横截面低分位 且 创 N 日新高。"""
    ret = close.pct_change()
    vola = ret.rolling(n).std()
    qtl = vola.quantile(q, axis=1)
    low_vol = vola.lt(qtl, axis=0)
    hh = close.rolling(n).max().shift(1)
    return low_vol & (close > hh)


def f_double_ma_pullback(close, high=None, low=None, vol=None, fast=10, slow=60, **_):
    """趋势回踩: 收盘 > 长均线 (多头排列) 且 回踩短均线 (昨收<短均线, 今收>短均线)。"""
    maf = close.rolling(fast).mean()
    mas = close.rolling(slow).mean()
    return (close > mas) & (mas > mas.shift(1)) & (close.shift(1) < maf.shift(1)) & (close > maf)


# ---- 多因子共振 (信号精简, 胜率更高, 冲 25% 年化门槛) ----
def f_ma_cross_trend(close, high=None, low=None, vol=None, fast=5, slow=20, trend=60, **_):
    """金叉+大趋势: 短均线上穿长均线 且 收盘>长期均线 (顺势金叉, 过滤逆势噪音)。"""
    maf = close.rolling(fast).mean()
    mas = close.rolling(slow).mean()
    mat = close.rolling(trend).mean()
    cross = (maf > mas) & (maf.shift(1) <= mas.shift(1))
    return cross & (close > mat)


def f_ma_cross_volume(close, high=None, low=None, vol=None, fast=5, slow=20, k=1.5, **_):
    """金叉+放量确认: 短均线上穿长均线 且 当日量>short日均量×k (放量金叉更可信)。"""
    if vol is None:
        return pd.DataFrame(False, index=close.index, columns=close.columns)
    maf = close.rolling(fast).mean()
    mas = close.rolling(slow).mean()
    cross = (maf > mas) & (maf.shift(1) <= mas.shift(1))
    vma = vol.rolling(fast).mean().shift(1)
    return cross & (vol > vma * k)


def f_trend_strong_breakout(close, high=None, low=None, vol=None, n=20, trend=60, k=1.3, **_):
    """强趋势突破: 创N日新高 + 收盘>长期均线 + 放量 (过滤熊市假突破)。"""
    if vol is None:
        return pd.DataFrame(False, index=close.index, columns=close.columns)
    hh = close.rolling(n).max().shift(1)
    mat = close.rolling(trend).mean()
    vma = vol.rolling(n).mean().shift(1)
    return (close > hh) & (close > mat) & (vol > vma * k)


def f_pullback_volume(close, high=None, low=None, vol=None, fast=10, slow=60, k=1.3, **_):
    """回踩放量: 多头排列下回踩短均线企稳 + 放量 (趋势中继加仓点)。"""
    if vol is None:
        return pd.DataFrame(False, index=close.index, columns=close.columns)
    maf = close.rolling(fast).mean()
    mas = close.rolling(slow).mean()
    pullback = (close > mas) & (mas > mas.shift(1)) & (close.shift(1) < maf.shift(1)) & (close > maf)
    vma = vol.rolling(fast).mean().shift(1)
    return pullback & (vol > vma * k)


def f_multi_confluence(close, high=None, low=None, vol=None, fast=5, slow=20, trend=60,
                       k=1.3, rsi_n=14, rsi_hi=70, **_):
    """多因子共振: 金叉 + 收盘>长期均线 + 放量 + RSI不超买 (最精信号, 高胜率)。"""
    if vol is None:
        return pd.DataFrame(False, index=close.index, columns=close.columns)
    maf = close.rolling(fast).mean()
    mas = close.rolling(slow).mean()
    mat = close.rolling(trend).mean()
    cross = (maf > mas) & (maf.shift(1) <= mas.shift(1))
    vma = vol.rolling(fast).mean().shift(1)
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(rsi_n).mean()
    loss = (-delta.clip(upper=0)).rolling(rsi_n).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return cross & (close > mat) & (vol > vma * k) & (rsi < rsi_hi)


FACTORS = {
    "ma_cross": f_ma_cross,
    "breakout": f_breakout,
    "volume_breakout": f_volume_breakout,
    "momentum": f_momentum,
    "reversal": f_reversal,
    "rsi_rebound": f_rsi_rebound,
    "lowvol_breakout": f_lowvol_breakout,
    "double_ma_pullback": f_double_ma_pullback,
    "ma_cross_trend": f_ma_cross_trend,
    "ma_cross_volume": f_ma_cross_volume,
    "trend_strong_breakout": f_trend_strong_breakout,
    "pullback_volume": f_pullback_volume,
    "multi_confluence": f_multi_confluence,
}


# ============================ 信号 → selections ============================
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


# ============================ 回测 + 判定 ============================
def run_backtest(selections: pd.DataFrame, bt_cfg: dict, stop_cfg: dict) -> dict:
    if selections.empty:
        return {"annualized_return": 0.0, "max_drawdown": 1.0, "total_trades": 0,
                "sharpe_ratio": 0.0, "win_rate": 0.0, "profit_factor": 0.0,
                "cumulative_return": 0.0, "avg_hold_days": 0.0}
    eng = BacktestEngine(bt_cfg)
    res = eng.run(selections=selections, start_time=START, end_time=END, stop_config=stop_cfg)
    return res.get("metrics") or {}


def judge(m: dict) -> bool:
    # max_drawdown 实测存负数 (-0.1781), 用 abs 防方向错 (原 <=0.30 会把 -0.40 也判达标)
    return (m.get("annualized_return", 0) >= TARGET_ANN
            and abs(m.get("max_drawdown", 0)) <= MAX_DD
            and m.get("total_trades", 0) > 0)


# ============================ 完整策略代码渲染 (每轮产出可独立运行脚本) ============================
_SIGNALS_SRC = inspect.getsource(signals_to_selections)


def _py_literal(d: dict) -> str:
    """dict -> 合法 Python 字面量字符串 (json.dumps 的 true/false/null 转 True/False/None)。

    bt_cfg/stop_cfg 的字符串值 (priority/confirm/period) 均不含 true|false|null 子串,
    替换安全。否则生成的 standalone 脚本会因 true/false 不是 Python 字面量而 NameError。
    """
    return (json.dumps(d, ensure_ascii=False, indent=4)
            .replace("true", "True").replace("false", "False").replace("null", "None"))


def render_full_code(run_id: int, strat: dict, pool: str) -> str:
    """把当轮策略渲染成完整、可独立 python xxx.py 运行的脚本。"""
    factor_fn = FACTORS[strat["factor"]]
    factor_name = factor_fn.__name__
    factor_src = inspect.getsource(factor_fn)
    bt_cfg_str = _py_literal(make_bt_cfg(strat["max_positions"], strat["min_amt"], strat["max_amt"]))
    stop_cfg_str = _py_literal(
        make_stop(strat["cost_stop"], strat["trail_act"], strat["trail_dd"],
                  strat["time_days"], strat["ladder"]))
    params_str = json.dumps(strat["params"], ensure_ascii=False)
    utype = POOL_MAP[pool]
    ann = strat.get("ann", 0.0)
    dd = strat.get("dd", 1.0)
    return f'''# -*- coding: utf-8 -*-
"""自动寻优第 {run_id} 轮策略 (self-contained, 可独立运行): {strat['name']}
生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}
因子: {strat['factor']}  入场参数: {params_str}
出场: 硬止损={strat['cost_stop']} 移动止盈激活={strat['trail_act']}/回撤={strat['trail_dd']} (条件单语义) 时间={strat['time_days']}天 阶梯={strat['ladder']}
仓位: 最多持 {strat['max_positions']} 只, 单票 {strat['min_amt']}~{strat['max_amt']}元
手续费/滑点: {COMMISSION} / {SLIPPAGE}
区间: {START} ~ {END}  股票池: {pool} (type={utype}, 剔除ST)
绩效: 年化={ann:.2%} 回撤={dd:.2%} 夏普={strat.get('sharpe',0):.2f} 交易={strat.get('trades',0)} 胜率={strat.get('wr',0):.1%}
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

START, END = "{START}", "{END}"
BT_CFG = {bt_cfg_str}
STOP_CFG = {stop_cfg_str}

{factor_src}

{_SIGNALS_SRC}


def main():
    sel = StockSelector({{"formula_name": "_placeholder",
                          "universe": {{"type": "{utype}", "exclude_st": True}},
                          "period": "1d", "dividend_type": 1}})
    codes = sel.resolve_universe()
    print(f"[INFO] 股票池 {{len(codes)}} 只")
    kl = DataFetcher.get_kline(codes, START, END, period="1d", dividend_type="front", use_cache=True)
    close = kl["Close"]; high = kl.get("High"); low = kl.get("Low"); vol = kl.get("Volume")
    sig = {factor_name}(close, high=high, low=low, vol=vol, **{params_str})
    selections = signals_to_selections(sig, "{strat['name']}")
    print(f"[INFO] 信号 {{len(selections)}} 条, 覆盖 {{selections['stock_code'].nunique()}} 只")
    res = BacktestEngine(BT_CFG).run(selections=selections, start_time=START, end_time=END, stop_config=STOP_CFG)
    m = res.get("metrics") or {{}}
    print("=" * 60)
    print(f"年化={{m.get('annualized_return',0)*100:.2f}}%  回撤={{m.get('max_drawdown',0)*100:.2f}}%  "
          f"夏普={{m.get('sharpe_ratio',0):.2f}}  交易={{m.get('total_trades')}}  胜率={{m.get('win_rate',0)*100:.1f}}%")
    print("=" * 60)


if __name__ == "__main__":
    main()
'''


# ============================ 寻优空间 ============================
def build_search_space(smoke: bool = False) -> list:
    """预设空间 (因子 × 出场 × 仓位 全叉积)。"""
    if smoke:
        return [{
            "name": "MA金叉(冒烟)", "factor": "ma_cross", "params": {"fast": 5, "slow": 20},
            "cost_stop": -0.06, "trail_act": 0.035, "trail_dd": 0.01, "time_days": 12, "ladder": None,
            "max_positions": 10, "min_amt": 5000, "max_amt": 30000,
        }]
    factors = [
        # 单因子 (沪深300第一轮已证: 趋势/均线类强, 抄底类中等)
        ("MA金叉5/20", "ma_cross", {"fast": 5, "slow": 20}),
        ("MA金叉10/30", "ma_cross", {"fast": 10, "slow": 30}),
        ("趋势回踩10/60", "double_ma_pullback", {"fast": 10, "slow": 60}),
        ("RSI超卖回升", "rsi_rebound", {"n": 14, "low_thr": 30}),
        ("超跌反弹", "reversal", {"n": 10, "drop": -0.08}),
        # 多因子共振 (信号精简, 胜率更高, 冲 25%)
        ("金叉+趋势60", "ma_cross_trend", {"fast": 5, "slow": 20, "trend": 60}),
        ("金叉+放量", "ma_cross_volume", {"fast": 5, "slow": 20, "k": 1.5}),
        ("强趋势突破", "trend_strong_breakout", {"n": 20, "trend": 60, "k": 1.3}),
        ("回踩+放量", "pullback_volume", {"fast": 10, "slow": 60, "k": 1.3}),
        ("多因子共振", "multi_confluence", {"fast": 5, "slow": 20, "trend": 60, "k": 1.3, "rsi_n": 14, "rsi_hi": 70}),
    ]
    exits = [
        dict(cost_stop=-0.06, trail_act=0.035, trail_dd=0.01, time_days=12, ladder=None),
        dict(cost_stop=-0.08, trail_act=0.05, trail_dd=0.015, time_days=15, ladder=None),
        dict(cost_stop=-0.06, trail_act=0.035, trail_dd=0.01, time_days=12,
             ladder=[(0.05, 0.5), (0.10, 0.5)]),
        dict(cost_stop=-0.10, trail_act=0.08, trail_dd=0.03, time_days=20, ladder=None),
    ]
    positions = [
        dict(max_positions=10, min_amt=5000, max_amt=30000),
        dict(max_positions=20, min_amt=3000, max_amt=15000),
    ]
    space = []
    for (nm, fac, params), ex, pos in _product(factors, exits, positions):
        s = {"name": nm, "factor": fac, "params": params}
        s.update(ex)
        s.update(pos)
        space.append(s)
    return space


def _product(*its):
    import itertools
    return itertools.product(*its)


# 随机采样空间 (预设穷尽后继续 loop 用, 满足"纯 loop 到达标")
FACTOR_SAMPLE = {
    "ma_cross": lambda: {"fast": random.choice([5, 10, 20]), "slow": random.choice([20, 30, 60])},
    "breakout": lambda: {"n": random.choice([10, 20, 40, 60])},
    "volume_breakout": lambda: {"n": random.choice([10, 20]), "k": random.choice([1.5, 2.0, 3.0])},
    "momentum": lambda: {"n": random.choice([5, 10, 20, 60]), "thr": random.choice([0.03, 0.05, 0.08, 0.15])},
    "reversal": lambda: {"n": random.choice([5, 10]), "drop": random.choice([-0.05, -0.08, -0.12])},
    "rsi_rebound": lambda: {"n": random.choice([7, 14]), "low_thr": random.choice([20, 25, 30])},
    "lowvol_breakout": lambda: {"n": random.choice([20, 40]), "q": random.choice([0.2, 0.3, 0.4])},
    "double_ma_pullback": lambda: {"fast": random.choice([5, 10]), "slow": random.choice([30, 60, 120])},
}
POS_SAMPLE = [
    dict(max_positions=5, min_amt=10000, max_amt=50000),
    dict(max_positions=10, min_amt=5000, max_amt=30000),
    dict(max_positions=15, min_amt=3000, max_amt=20000),
    dict(max_positions=20, min_amt=2000, max_amt=15000),
    dict(max_positions=30, min_amt=2000, max_amt=10000),
]


def sample_strategy(idx: int) -> dict:
    fac_name = random.choice(list(FACTOR_SAMPLE.keys()))
    params = FACTOR_SAMPLE[fac_name]()
    cost_stop = random.choice([-0.05, -0.06, -0.08, -0.10])
    trail_act = random.choice([0.03, 0.035, 0.05, 0.08])
    trail_dd = random.choice([0.01, 0.015, 0.02, 0.03])
    time_days = random.choice([10, 12, 15, 20])
    ladder = random.choice([None, [(0.05, 0.5), (0.10, 0.5)], [(0.06, 0.3), (0.15, 0.3)]])
    pos = random.choice(POS_SAMPLE)
    return {
        "name": f"{fac_name}#{idx}", "factor": fac_name, "params": params,
        "cost_stop": cost_stop, "trail_act": trail_act, "trail_dd": trail_dd,
        "time_days": time_days, "ladder": ladder, **pos,
    }


# ============================ CSV 记录 ============================
CSV_FIELDS = ["run_id", "name", "factor", "params", "cost_stop", "trail_act", "trail_dd",
              "time_days", "ladder", "max_positions", "min_amt", "max_amt",
              "n_sig", "ann", "dd", "sharpe", "trades", "wr", "ok", "elapsed"]


def append_csv(path: str, row: dict):
    exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in CSV_FIELDS})


# ============================ 单轮执行 ============================
def evaluate(i: int, strat: dict, close, high, low, vol, pool: str, csv_path: str,
            best: dict, sig_cache: dict) -> dict | None:
    """跑一轮: 算因子(同因子同参数缓存复用) -> 信号 -> 回测 -> 判定 -> 渲染完整代码 -> 记录。"""
    t0 = time.time()
    sig_key = (strat["factor"], json.dumps(strat["params"], sort_keys=True, ensure_ascii=False))
    sig = sig_cache.get(sig_key)
    if sig is None:
        try:
            sig = FACTORS[strat["factor"]](close, high=high, low=low, vol=vol, **strat["params"])
        except Exception as e:
            logger.warning("[%d] 因子 %s 计算异常: %s", i, strat["factor"], e)
            return None
        sig_cache[sig_key] = sig
    sel = signals_to_selections(sig, strat["name"])
    n_sig = len(sel)
    bt_cfg = make_bt_cfg(strat["max_positions"], strat["min_amt"], strat["max_amt"])
    stop = make_stop(strat["cost_stop"], strat["trail_act"], strat["trail_dd"],
                     strat["time_days"], strat["ladder"])
    try:
        m = run_backtest(sel, bt_cfg, stop)
    except Exception as e:
        logger.warning("[%d] 回测异常 %s: %s", i, strat["name"], e)
        return None
    ann = float(m.get("annualized_return", 0) or 0)
    dd = float(m.get("max_drawdown", 1) or 1)
    sharpe = float(m.get("sharpe_ratio", 0) or 0)
    trades = int(m.get("total_trades", 0) or 0)
    wr = float(m.get("win_rate", 0) or 0)
    ok = judge(m)
    elapsed = time.time() - t0
    strat_full = {**strat, "ann": ann, "dd": dd, "sharpe": sharpe, "trades": trades,
                  "wr": wr, "n_sig": n_sig}
    logger.info("[%d] %-18s | 信号=%-6d 年化=%6.2f%% 回撤=%6.2f%% 夏普=%5.2f 交易=%-5d 胜率=%5.1f%% %s 耗时=%.0fs",
                i, strat["name"][:18], n_sig, ann * 100, dd * 100, sharpe, trades, wr * 100,
                "[达标]" if ok else "", elapsed)
    # 每轮输出完整可运行代码
    code = render_full_code(i, strat_full, pool)
    safe_factor = strat["factor"].replace("/", "_")
    with open(f"{RUN_DIR}/run_{i:03d}_{safe_factor}.py", "w", encoding="utf-8") as f:
        f.write(code)
    append_csv(csv_path, {
        "run_id": i, "name": strat["name"], "factor": strat["factor"],
        "params": json.dumps(strat["params"], ensure_ascii=False),
        "cost_stop": strat["cost_stop"], "trail_act": strat["trail_act"],
        "trail_dd": strat["trail_dd"], "time_days": strat["time_days"],
        "ladder": strat["ladder"], "max_positions": strat["max_positions"],
        "min_amt": strat["min_amt"], "max_amt": strat["max_amt"],
        "n_sig": n_sig, "ann": f"{ann:.4f}", "dd": f"{dd:.4f}",
        "sharpe": f"{sharpe:.3f}", "trades": trades, "wr": f"{wr:.4f}",
        "ok": int(ok), "elapsed": f"{elapsed:.0f}",
    })
    if ann > best.get("ann", float("-inf")):
        best.clear()
        best.update(strat_full)
        best["code"] = code
    if ok:
        return strat_full
    return None


# ============================ main ============================
def main():
    ap = argparse.ArgumentParser(description="纯Python因子 + VERA引擎 自动寻优")
    ap.add_argument("--pool", default="hs300", choices=list(POOL_MAP.keys()))
    ap.add_argument("--smoke", action="store_true", help="冒烟: 仅 1 组合验证框架")
    ap.add_argument("--max-iters", type=int, default=200,
                    help="预设空间穷尽后, 随机采样的最大轮数上限 (防无限 loop 烧机)")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    random.seed(args.seed)
    os.makedirs(RUN_DIR, exist_ok=True)
    csv_path = f"{RUN_DIR}/results.csv"
    if os.path.exists(csv_path):
        os.remove(csv_path)

    print("=" * 70)
    print(f"  VERA 自动策略寻优 | 区间 {START}~{END} | 池={args.pool} | 口径=日线/止损优先/条件单语义")
    print(f"  达标门槛: 年化 >= {TARGET_ANN:.0%}  且  最大回撤 <= {MAX_DD:.0%}")
    print(f"  手续费={COMMISSION} 滑点={SLIPPAGE} 资金={CAPITAL:,.0f}")
    print("=" * 70)

    codes = load_universe(args.pool)
    close, high, low, vol = load_ohlc(codes)

    space = build_search_space(smoke=args.smoke)
    logger.info("寻优空间: 预设 %d 组合 (穷尽后随机采样最多 %d 轮)", len(space), args.max_iters)

    best: dict = {}
    sig_cache: dict = {}
    # 阶段 1: 预设空间
    for i, strat in enumerate(space, 1):
        champ = evaluate(i, strat, close, high, low, vol, args.pool, csv_path, best, sig_cache)
        if champ:
            _save_champion(champ, args.pool)
            return
    # 阶段 2: 随机采样继续 loop (纯 loop 到达标, 有 max-iters 上界保护)
    if args.smoke:
        _report_no_champion(best, args.pool)
        return
    base = len(space)
    for j in range(args.max_iters):
        strat = sample_strategy(base + j + 1)
        champ = evaluate(base + j + 1, strat, close, high, low, vol, args.pool, csv_path, best, sig_cache)
        if champ:
            _save_champion(champ, args.pool)
            return
    _report_no_champion(best, args.pool)


def _save_champion(champ: dict, pool: str):
    with open(f"{RUN_DIR}/champion.py", "w", encoding="utf-8") as f:
        f.write(champ["code"])
    meta = {k: v for k, v in champ.items() if k != "code"}
    with open(f"{RUN_DIR}/champion.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2, default=str)
    logger.info("=" * 70)
    logger.info("  达标! champion 已保存: %s/champion.py", RUN_DIR)
    logger.info("  %s | 年化=%.2f%% 回撤=%.2f%% 夏普=%.2f 交易=%d",
                champ["name"], champ["ann"] * 100, champ["dd"] * 100,
                champ["sharpe"], champ["trades"])
    logger.info("=" * 70)


def _report_no_champion(best: dict, pool: str):
    logger.info("=" * 70)
    logger.info("  寻优空间穷尽未达标 (口径=日线, 已含条件单语义移动止盈, 无乐观偏差)")
    logger.info("  当前最优: %s", best.get("name", "(无)"))
    if best:
        logger.info("    年化=%.2f%%  回撤=%.2f%%  夏普=%.2f  交易=%d",
                    best.get("ann", 0) * 100, best.get("dd", 0) * 100,
                    best.get("sharpe", 0), best.get("trades", 0))
        logger.info("  诚实基线: 25%%年化+30%%回撤在 %s 池 × 日线真实口径下未触及;", pool)
        logger.info("           这与 VERA 实测一致 (公开策略真实口径多年化为个位数~10%)。")
        logger.info("  可选: 换更大池(--pool alla) / 提高 max-iters / 放宽门槛 / 接受相对最优。")
    logger.info("=" * 70)


if __name__ == "__main__":
    TURN_START = time.time()
    try:
        main()
    finally:
        logger.info("总耗时 %.0fs", time.time() - TURN_START)
