# -*- coding: utf-8 -*-
"""
pond_optimizer.py
=================
动态择板 loop 寻优 (2026-08-11, 接 auto_strategy_optimizer 第三轮未达标后的换轨)。

背景: 日线单池赛道 189 组合穷尽, 天花板 18.81% (MA金叉5/20, hs300)。
      CLAUDE.md「战略方向」轴间实验判定: 板轴(主板/创业/科创/北交)是默认最优轴,
      10 公式中 6 个以板轴最优, 全部板轴 > 随机选板。本脚本把动态择板套到因子框架。

口径 (继承 auto_strategy_optimizer, 不改):
  - 日线 / 止损优先 / 移动止盈条件单语义 (confirm=real) / 信号日T收盘买入
  - 达标门槛: 年化 >= 25% 且 最大回撤 <= 30%

核心设计 (复用 tools/pond_axis_experiment.py 的择板三件套):
  1. 全A池 (type=5 含北交所, 剔ST) + 4 板池 (主板=全A剔除创业/科创, 创业/科创/北交按 type)
  2. 4 板动量指数 (shanghai/chuangyeban/kechuang50/899050.BJ)
  3. 每 rebal 交易日, 按 mom 日动量选板 TopK (因果: 用调仓日前一收盘)
  4. 因子信号在全A算一次 (sig_cache), 择板只过滤信号 → 不同择板组合不重复算因子
  5. matrix_cache=True 复用 prep 矩阵; use_kline_cache 复用 parquet (用户指示: 充分用本地K线缓存)

用法:
  python -X utf8 research/pond_optimizer.py --smoke        # 冒烟 (1 组合)
  python -X utf8 research/pond_optimizer.py                 # 全量 60 组合
  python -X utf8 research/pond_optimizer.py --max-iters 200 # 穷尽后随机采样继续 loop
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

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import numpy as np
import pandas as pd

from backtest.engine import BacktestEngine
from core.data_fetcher import DataFetcher
from selection.selector import StockSelector
from utils.logger import get_logger

# 复用 auto_strategy_optimizer 的因子库 + 工厂 (不破坏现有, import 复用)
from auto_strategy_optimizer import (
    FACTORS, signals_to_selections, run_backtest, judge,
    make_bt_cfg, make_stop, _py_literal,
    START, END, PERIOD, CAPITAL, COMMISSION, SLIPPAGE, TARGET_ANN, MAX_DD,
)

logger = get_logger(__name__)

RUN_DIR = "research/pond_optimizer_runs"
PAD_DAYS = 90  # 板动量 warmup 前推天数 (够 60 日动量)

# 板轴动量指数 (复用 pond_axis_experiment.BOARD_INDEX)
BOARD_INDEX = {
    "主板": "shanghai",
    "创业板": "chuangyeban",
    "科创板": "kechuang50",
    "北交所": "899050.BJ",
}


# ============================ 择板层 ============================
def build_board_pools(u_set: set) -> dict:
    """构造 4 板股票池 (∩ 全A池 u_set)。主板 = 全A剔除创业/科创 (北交所已在 type=5 但不在 hs_a)。

    与 pond_axis_experiment line 157-160 同口径。
    """
    uni = {lt: set(DataFetcher.get_stock_universe(lt)) for lt in ("50", "51", "52", "53")}
    pools = {
        "主板": (uni["50"] - uni["51"] - uni["52"]) & u_set,
        "创业板": uni["51"] & u_set,
        "科创板": uni["52"] & u_set,
        "北交所": uni["53"] & u_set,
    }
    for k, v in pools.items():
        logger.info("板池 %s: %d 只", k, len(v))
    return pools


def get_index_close(name_or_code: str, start: str, end: str) -> pd.Series:
    """指数收盘价序列 (失败返回空)。复用 pond_axis_experiment.get_index_close。"""
    try:
        df = DataFetcher.get_index_data(name_or_code, start, end, dividend_type="none")
        if df is None or len(df) == 0:
            return pd.Series(dtype=float)
        col = "Close" if "Close" in df.columns else "close"
        return df[col]
    except Exception as e:
        logger.warning("指数 %s 拉取失败: %s", name_or_code, e)
        return pd.Series(dtype=float)


def momentum_rank(closes: dict, date: pd.Timestamp, mom: int) -> list:
    """各板在 date 前一收盘的 mom 日动量排名 (降序返回板名)。复用 pond_axis。"""
    scores = {}
    for name, s in closes.items():
        if len(s) == 0:
            continue
        pos = s.index.searchsorted(date) - 1
        if pos < mom:
            continue
        scores[name] = s.iloc[pos] / s.iloc[pos - mom] - 1.0
    return sorted(scores, key=scores.get, reverse=True)


# ============================ 择板策略 ============================
# 每个策略预算出 rebal_dates + picks_per_win (动态) 或 pond_codes (静态)
POND_STRATEGIES = [
    {"name": "动态板Top1(5/20)", "mode": "dynamic", "rebal": 5, "mom": 20, "topk": 1},
    {"name": "动态板Top1(20/60)", "mode": "dynamic", "rebal": 20, "mom": 60, "topk": 1},
    {"name": "动态板Top2(5/20)", "mode": "dynamic", "rebal": 5, "mom": 20, "topk": 2},
    {"name": "静态主板", "mode": "static", "board": "主板"},
    {"name": "静态创业板", "mode": "static", "board": "创业板"},
    {"name": "全A不择板", "mode": "all"},
]


def precompute_pond(strategy: dict, closes: dict, win_days: list, pools: dict) -> dict:
    """把择板策略预算成可复用的过滤参数。

    返回:
      dynamic: {"rebal_dates": [...], "picks_per_win": [[板名...], ...]}
      static:  {"pond_codes": set(code)}
      all:     {} (不过滤)
    """
    if strategy["mode"] == "all":
        return {"mode": "all"}
    if strategy["mode"] == "static":
        return {"mode": "static", "pond_codes": set(pools.get(strategy["board"], set()))}
    # dynamic
    rebal_dates = win_days[::strategy["rebal"]]
    picks = []
    for r in rebal_dates:
        rank = momentum_rank(closes, r, strategy["mom"])
        picks.append(rank[: strategy["topk"]])
    return {"mode": "dynamic", "rebal_dates": rebal_dates, "picks_per_win": picks}


def _window_of(date, rebal_dates) -> int:
    i = int(np.searchsorted(np.array(rebal_dates, dtype="datetime64[ns]"),
                            np.datetime64(date), side="right")) - 1
    return max(i, 0)


def filter_selections_by_pond(sel: pd.DataFrame, pond_cfg: dict, pools: dict) -> pd.DataFrame:
    """selections × 择板参数 → 过滤后 selections (只保留当日目标板的信号)。

    复用 pond_axis_experiment.filter_by_picks 的窗口映射逻辑。
    """
    if sel.empty or pond_cfg["mode"] == "all":
        return sel
    sel = sel.copy()
    sel["select_date"] = pd.to_datetime(sel["select_date"])
    codes = sel["stock_code"].values
    if pond_cfg["mode"] == "static":
        ps = pond_cfg["pond_codes"]
        keep = np.array([c in ps for c in codes], dtype=bool)
        out = sel[keep]
    else:  # dynamic
        rebal_dates = pond_cfg["rebal_dates"]
        picks_per_win = pond_cfg["picks_per_win"]
        widx = np.array([_window_of(d, rebal_dates) for d in sel["select_date"]])
        keep = np.zeros(len(sel), dtype=bool)
        for wi in range(len(rebal_dates)):
            picks = picks_per_win[wi]
            pond_set = set().union(*(pools.get(p, set()) for p in picks)) if picks else set()
            if not pond_set:
                continue
            mask = widx == wi
            keep[mask] = np.array([c in pond_set for c in codes[mask]], dtype=bool)
        out = sel[keep]
    out["select_date"] = pd.to_datetime(out["select_date"]).dt.strftime("%Y%m%d")
    return out


# ============================ 寻优空间 ============================
def build_search_space(quick: bool = False) -> list:
    """因子 × 择板 × 出场 全叉积。

    因子: 5 强单因子 (多因子共振第三轮已证废: 过滤太狠错过大牛股)。
    择板: 6 策略 (动态板Top1周/月, 动态板Top2, 静态主板, 静态创业, 全A)。
    出场: 2 (时间止损12天; 阶梯5/50+10/50)。
    仓位: 单票10万+5仓 (对齐 pond_axis_experiment, 防 全A 信号挤兑)。

    quick=True: 仅 MA金叉5/20 × 6 择板 × 1 出场 = 6 组合 (诊断择板效果用)。
    """
    if quick:
        factors = [("MA金叉5/20", "ma_cross", {"fast": 5, "slow": 20})]
    else:
        factors = [
            ("MA金叉5/20", "ma_cross", {"fast": 5, "slow": 20}),
            ("MA金叉10/30", "ma_cross", {"fast": 10, "slow": 30}),
            ("趋势回踩10/60", "double_ma_pullback", {"fast": 10, "slow": 60}),
            ("RSI超卖回升", "rsi_rebound", {"n": 14, "low_thr": 30}),
            ("超跌反弹", "reversal", {"n": 10, "drop": -0.08}),
        ]
    exits = [dict(cost_stop=-0.06, trail_act=0.035, trail_dd=0.01, time_days=12, ladder=None)]
    if not quick:
        exits.append(dict(cost_stop=-0.06, trail_act=0.035, trail_dd=0.01, time_days=12,
                          ladder=[(0.05, 0.5), (0.10, 0.5)]))
    positions = [dict(max_positions=5, min_amt=10000, max_amt=100000)]  # 对齐 pond_axis: 单票10万+5仓
    space = []
    gi = 0
    import itertools
    for (fac_nm, fac, params), pond, ex, pos in itertools.product(factors, POND_STRATEGIES, exits, positions):
        gi += 1
        s = {"name": f"{fac_nm}@{pond['name']}", "factor": fac, "params": params,
             "pond": pond, "gi": gi}
        s.update(ex)
        s.update(pos)
        space.append(s)
    return space


# ============================ CSV ============================
CSV_FIELDS = ["gi", "name", "factor", "params", "pond", "cost_stop", "trail_act", "trail_dd",
              "time_days", "ladder", "max_positions", "n_sig", "ann", "dd", "sharpe",
              "trades", "wr", "ok", "elapsed"]


def append_csv(path: str, row: dict):
    exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in CSV_FIELDS})


# ============================ 完整策略代码渲染 (达标交付 standalone) ============================
def render_full_code_pond(strat: dict) -> str:
    """渲染带动态择板的完整 standalone 脚本 (达标后交付, 可独立 python xxx.py 运行)。"""
    factor_fn = FACTORS[strat["factor"]]
    factor_name = factor_fn.__name__
    factor_src = inspect.getsource(factor_fn)
    signals_src = inspect.getsource(signals_to_selections)
    bt_cfg_str = _py_literal(make_bt_cfg(strat["max_positions"], strat["min_amt"], strat["max_amt"]))
    stop_cfg_str = _py_literal(
        make_stop(strat["cost_stop"], strat["trail_act"], strat["trail_dd"],
                  strat["time_days"], strat["ladder"]))
    params_str = json.dumps(strat["params"], ensure_ascii=False)
    pond = strat["pond"]
    pond_str = json.dumps(pond, ensure_ascii=False)
    ann = strat.get("ann", 0.0)
    dd = strat.get("dd", 1.0)
    return f'''# -*- coding: utf-8 -*-
"""动态择板策略 (self-contained, 可独立运行): {strat['name']}
生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}
因子: {strat['factor']}  入场参数: {params_str}
择板: {pond_str}
出场: 硬止损={strat['cost_stop']} 移动止盈激活={strat['trail_act']}/回撤={strat['trail_dd']} (条件单语义) 时间={strat['time_days']}天 阶梯={strat['ladder']}
仓位: 最多持 {strat['max_positions']} 只, 单票 {strat['min_amt']}~{strat['max_amt']}元
手续费/滑点: {COMMISSION} / {SLIPPAGE}
区间: {START} ~ {END}  股票池: 全A (type=5 含北交所, 剔ST)
绩效: 年化={ann:.2%} 回撤={dd:.2%} 夏普={strat.get('sharpe',0):.2f} 交易={strat.get('trades',0)} 胜率={strat.get('wr',0):.1%}
口径: 日线 / 止损优先 / 信号日T收盘买入 / 移动止盈条件单语义 / 动态择板(板轴动量TopK)
"""
import os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "..")))

import numpy as np
import pandas as pd
from backtest.engine import BacktestEngine
from core.data_fetcher import DataFetcher
from selection.selector import StockSelector

START, END = "{START}", "{END}"
PAD_DAYS = 90
BOARD_INDEX = {{"主板": "shanghai", "创业板": "chuangyeban", "科创板": "kechuang50", "北交所": "899050.BJ"}}
POND = {pond_str}
BT_CFG = {bt_cfg_str}
STOP_CFG = {stop_cfg_str}

{factor_src}

{signals_src}


def get_index_close(name, start, end):
    df = DataFetcher.get_index_data(name, start, end, dividend_type="none")
    col = "Close" if (df is not None and "Close" in df.columns) else "close"
    return df[col] if (df is not None and len(df)) else pd.Series(dtype=float)


def momentum_rank(closes, date, mom):
    scores = {{}}
    for n, s in closes.items():
        if len(s) == 0: continue
        pos = s.index.searchsorted(date) - 1
        if pos < mom: continue
        scores[n] = s.iloc[pos] / s.iloc[pos - mom] - 1.0
    return sorted(scores, key=scores.get, reverse=True)


def main():
    padded = (pd.Timestamp(START) - pd.Timedelta(days=PAD_DAYS)).strftime("%Y%m%d")
    stocks = StockSelector({{"formula_name": "_", "universe": {{"type": "5", "exclude_st": True}}}}).resolve_universe()
    u_set = set(stocks)
    print(f"[INFO] 全A池 {{len(stocks)}} 只")
    uni = {{lt: set(DataFetcher.get_stock_universe(lt)) for lt in ("50", "51", "52", "53")}}
    pools = {{"主板": (uni["50"] - uni["51"] - uni["52"]) & u_set,
             "创业板": uni["51"] & u_set, "科创板": uni["52"] & u_set, "北交所": uni["53"] & u_set}}
    closes = {{n: get_index_close(idx, padded, END) for n, idx in BOARD_INDEX.items()}}
    calendar = pd.DatetimeIndex(pd.to_datetime(DataFetcher.get_trading_dates("SH", start_time=padded, end_time=END)))
    win_days = [d for d in calendar if pd.Timestamp(START) <= d <= pd.Timestamp(END)]
    kl = DataFetcher.get_kline(stocks, START, END, period="1d", dividend_type="front", use_cache=True)
    close = kl["Close"]; high = kl.get("High"); low = kl.get("Low"); vol = kl.get("Volume")
    sig = {factor_name}(close, high=high, low=low, vol=vol, **{params_str})
    sel = signals_to_selections(sig, "{strat['name']}")
    print(f"[INFO] 全A信号 {{len(sel)}} 条")
    # 择板过滤
    if POND["mode"] == "static":
        ps = pools.get(POND["board"], set())
        sel = sel[sel["stock_code"].isin(ps)]
    elif POND["mode"] == "dynamic":
        rebal_dates = win_days[::POND["rebal"]]
        sel["select_date"] = pd.to_datetime(sel["select_date"])
        widx = np.array([max(int(np.searchsorted(np.array(rebal_dates, dtype="datetime64[ns]"), np.datetime64(d), side="right")) - 1, 0) for d in sel["select_date"]])
        keep = np.zeros(len(sel), dtype=bool)
        codes = sel["stock_code"].values
        for wi, r in enumerate(rebal_dates):
            rank = momentum_rank(closes, r, POND["mom"])
            pond_set = set().union(*(pools.get(p, set()) for p in rank[:POND["topk"]])) if rank else set()
            if not pond_set: continue
            m = widx == wi
            keep[m] = np.array([c in pond_set for c in codes[m]])
        sel = sel[keep]
        sel["select_date"] = sel["select_date"].dt.strftime("%Y%m%d")
    print(f"[INFO] 择板后信号 {{len(sel)}} 条, 覆盖 {{sel['stock_code'].nunique()}} 只")
    res = BacktestEngine(BT_CFG).run(selections=sel, start_time=START, end_time=END, stop_config=STOP_CFG)
    m = res.get("metrics") or {{}}
    print("=" * 60)
    print(f"年化={{m.get('annualized_return',0)*100:.2f}}%  回撤={{m.get('max_drawdown',0)*100:.2f}}%  "
          f"夏普={{m.get('sharpe_ratio',0):.2f}}  交易={{m.get('total_trades')}}  胜率={{m.get('win_rate',0)*100:.1f}}%")
    print("=" * 60)


if __name__ == "__main__":
    main()
'''


# ============================ 单轮执行 ============================
def evaluate(strat: dict, close, high, low, vol, sig_cache: dict, pond_cfg: dict,
             pools: dict, csv_path: str, best: dict) -> dict | None:
    """跑一轮: 算因子(缓存) -> 全量信号 -> 择板过滤 -> 回测 -> 判定 -> 记录。"""
    t0 = time.time()
    sig_key = (strat["factor"], json.dumps(strat["params"], sort_keys=True, ensure_ascii=False))
    sig = sig_cache.get(sig_key)
    if sig is None:
        try:
            sig = FACTORS[strat["factor"]](close, high=high, low=low, vol=vol, **strat["params"])
        except Exception as e:
            logger.warning("[g%d] 因子 %s 异常: %s", strat["gi"], strat["factor"], e)
            return None
        sig_cache[sig_key] = sig
    full_sel = signals_to_selections(sig, strat["name"])
    sel = filter_selections_by_pond(full_sel, pond_cfg, pools)
    n_sig = len(sel)
    bt_cfg = make_bt_cfg(strat["max_positions"], strat["min_amt"], strat["max_amt"])
    bt_cfg["sell_cooldown_days"] = 20  # 防信号挤兑: 全A信号量大, 加冷却降换手 (对齐 pond_axis_experiment)
    stop = make_stop(strat["cost_stop"], strat["trail_act"], strat["trail_dd"],
                     strat["time_days"], strat["ladder"])
    try:
        m = run_backtest(sel, bt_cfg, stop)
    except Exception as e:
        logger.warning("[g%d] 回测异常 %s: %s", strat["gi"], strat["name"], e)
        return None
    ann = float(m.get("annualized_return", 0) or 0)
    dd = float(m.get("max_drawdown", 1) or 1)
    sharpe = float(m.get("sharpe_ratio", 0) or 0)
    trades = int(m.get("total_trades", 0) or 0)
    wr = float(m.get("win_rate", 0) or 0)
    ok = judge(m)
    elapsed = time.time() - t0
    strat_full = {**strat, "ann": ann, "dd": dd, "sharpe": sharpe, "trades": trades, "wr": wr, "n_sig": n_sig}
    logger.info("[g%d] %-26s | 信号=%-6d 年化=%6.2f%% 回撤=%6.2f%% 夏普=%5.2f 交易=%-5d 胜率=%5.1f%% %s 耗时=%.0fs",
                strat["gi"], strat["name"][:26], n_sig, ann * 100, dd * 100, sharpe, trades, wr * 100,
                "[达标]" if ok else "", elapsed)
    append_csv(csv_path, {
        "gi": strat["gi"], "name": strat["name"], "factor": strat["factor"],
        "params": json.dumps(strat["params"], ensure_ascii=False),
        "pond": strat["pond"]["name"], "cost_stop": strat["cost_stop"],
        "trail_act": strat["trail_act"], "trail_dd": strat["trail_dd"],
        "time_days": strat["time_days"], "ladder": strat["ladder"],
        "max_positions": strat["max_positions"], "n_sig": n_sig,
        "ann": f"{ann:.4f}", "dd": f"{dd:.4f}", "sharpe": f"{sharpe:.3f}",
        "trades": trades, "wr": f"{wr:.4f}", "ok": int(ok), "elapsed": f"{elapsed:.0f}",
    })
    if ann > best.get("ann", float("-inf")):
        best.clear()
        best.update(strat_full)
    if ok:
        strat_full["code"] = render_full_code_pond(strat_full)
        return strat_full
    return None


# ============================ main ============================
def main():
    ap = argparse.ArgumentParser(description="动态择板 loop 寻优 (板轴 = CLAUDE.md 默认最优轴)")
    ap.add_argument("--smoke", action="store_true", help="冒烟: 仅 1 组合验证框架")
    ap.add_argument("--quick", action="store_true", help="快速对照: MA金叉×6择板×1出场=6组合, 诊断择板效果")
    ap.add_argument("--max-iters", type=int, default=200, help="穷尽后随机采样上限")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    random.seed(args.seed)
    os.makedirs(RUN_DIR, exist_ok=True)
    csv_path = f"{RUN_DIR}/results.csv"
    if os.path.exists(csv_path):
        os.remove(csv_path)

    print("=" * 70)
    print(f"  VERA 动态择板寻优 | 区间 {START}~{END} | 池=全A(type=5) | 口径=日线/止损优先/条件单语义")
    print(f"  达标门槛: 年化 >= {TARGET_ANN:.0%}  且  最大回撤 <= {MAX_DD:.0%}")
    print(f"  手续费={COMMISSION} 滑点={SLIPPAGE} 资金={CAPITAL:,.0f}")
    print("=" * 70)

    # 1. 全A池 + 板池 + 板指数 + K线 (一次拉齐, 充分用本地缓存)
    stocks = StockSelector({"formula_name": "_", "universe": {"type": "5", "exclude_st": True},
                            "period": PERIOD, "dividend_type": 1}).resolve_universe()
    u_set = set(stocks)
    logger.info("全A池(type=5 含北交所 剔ST): %d 只", len(u_set))
    pools = build_board_pools(u_set)

    padded_start = (pd.Timestamp(START) - pd.Timedelta(days=PAD_DAYS)).strftime("%Y%m%d")
    logger.info("拉动量指数 (padded_start=%s)...", padded_start)
    closes = {n: get_index_close(idx, padded_start, END) for n, idx in BOARD_INDEX.items()}
    n_ok = sum(1 for v in closes.values() if len(v) > 0)
    logger.info("板指数就绪 %d/%d", n_ok, len(closes))

    calendar = pd.DatetimeIndex(pd.to_datetime(
        DataFetcher.get_trading_dates("SH", start_time=padded_start, end_time=END)))
    win_days = [d for d in calendar if pd.Timestamp(START) <= d <= pd.Timestamp(END)]
    logger.info("交易日历: %d 个交易日 (区间内)", len(win_days))

    # 2. 全A K线 (一次, parquet 缓存)
    t0 = time.time()
    kl = DataFetcher.get_kline(stocks, START, END, period=PERIOD, dividend_type="front", use_cache=True)
    if not kl or "Close" not in kl:
        raise RuntimeError("get_kline 拉取日线失败, 检查 TDX 连接")
    close = kl["Close"]; high = kl.get("High"); low = kl.get("Low"); vol = kl.get("Volume")
    logger.info("OHLCV 就绪: %d 股 × %d 日, 取数耗时 %.0fs", close.shape[1], close.shape[0], time.time() - t0)

    # 3. 预算各择板策略的过滤参数 (一次)
    pond_configs = {}
    for ps in POND_STRATEGIES:
        pond_configs[ps["name"]] = precompute_pond(ps, closes, win_days, pools)
        if ps["mode"] == "dynamic":
            n_picks = sum(1 for p in pond_configs[ps["name"]]["picks_per_win"] if p)
            logger.info("择板 %s 预算完成: %d 个调仓窗口有目标板", ps["name"], n_picks)

    # 4. 寻优空间
    if args.smoke:
        space = [{
            "name": "MA金叉5/20@动态板Top1(5/20)(冒烟)", "factor": "ma_cross",
            "params": {"fast": 5, "slow": 20}, "pond": POND_STRATEGIES[0], "gi": 1,
            "cost_stop": -0.06, "trail_act": 0.035, "trail_dd": 0.01, "time_days": 12, "ladder": None,
            "max_positions": 5, "min_amt": 10000, "max_amt": 100000,
        }]
    else:
        space = build_search_space(quick=args.quick)
    logger.info("寻优空间: %d 组合 (因子×择板×出场)", len(space))

    # 5. loop
    best: dict = {}
    sig_cache: dict = {}
    for strat in space:
        pond_cfg = pond_configs[strat["pond"]["name"]]
        champ = evaluate(strat, close, high, low, vol, sig_cache, pond_cfg, pools, csv_path, best)
        if champ:
            _save_champion(champ)
            return
    _report_no_champion(best)


def _save_champion(champ: dict):
    safe = champ["name"].replace("/", "_").replace("(", "_").replace(")", "_").replace("@", "_")
    with open(f"{RUN_DIR}/champion_{safe}.py", "w", encoding="utf-8") as f:
        f.write(champ["code"])
    meta = {k: v for k, v in champ.items() if k != "code"}
    with open(f"{RUN_DIR}/champion.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2, default=str)
    logger.info("=" * 70)
    logger.info("  达标! champion 已保存: %s/champion_%s.py", RUN_DIR, safe)
    logger.info("  %s | 年化=%.2f%% 回撤=%.2f%% 夏普=%.2f 交易=%d",
                champ["name"], champ["ann"] * 100, champ["dd"] * 100, champ["sharpe"], champ["trades"])
    logger.info("=" * 70)


def _report_no_champion(best: dict):
    logger.info("=" * 70)
    logger.info("  动态择板寻优空间穷尽未达标 (口径=日线, 板轴择塘, 条件单语义, 无乐观偏差)")
    logger.info("  当前最优: %s", best.get("name", "(无)"))
    if best:
        logger.info("    年化=%.2f%%  回撤=%.2f%%  夏普=%.2f  交易=%d",
                    best.get("ann", 0) * 100, best.get("dd", 0) * 100,
                    best.get("sharpe", 0), best.get("trades", 0))
        logger.info("  对比: 日线单池赛道天花板 18.81%% (hs300 MA金叉5/20); 动态择板最优见上。")
    logger.info("=" * 70)


if __name__ == "__main__":
    TURN_START = time.time()
    try:
        main()
    finally:
        logger.info("总耗时 %.0fs", time.time() - TURN_START)
