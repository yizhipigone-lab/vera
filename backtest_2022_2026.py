"""2022-2026 全周期策略搜索 v2 — 严格择时 + 重仓 + 宽止损，目标年化25%"""
import sys, os, itertools, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd, numpy as np
from core.connector import TdxConnector
from utils.logger import get_logger
from backtest.engine import _simulate_core_v3

logger = get_logger(__name__)

def _ma(df, w):
    return df.rolling(w, min_periods=w).mean()

def align_all(*dfs):
    cols = dfs[0].columns; idx = dfs[0].index
    for d in dfs[1:]:
        cols = cols.intersection(d.columns); idx = idx.intersection(d.index)
    return tuple(d.loc[idx, cols] for d in dfs)

# ═══════════════════════════════════════════════════════════════
# 核心策略: 趋势跟踪 + 严格大盘择时 + 回调买入
# ═══════════════════════════════════════════════════════════════

def trend_breakout(close, open_df, high, low, vol, idx_close,
                   vol_mul=1.5, strength_pct=0.02, pullback=False,
                   market_filter="strict"):  # strict / loose / none
    """
    趋势突破策略。market_filter:
      strict = 指数>60MA 且 60MA>120MA (牛市)
      loose  = 指数>60MA (可操作)
      none   = 无过滤
    """
    close, open_df, high, low, vol = align_all(close, open_df, high, low, vol)

    # 个股条件
    ma5, ma10, ma20, ma60 = _ma(close,5), _ma(close,10), _ma(close,20), _ma(close,60)
    cond = (ma5 > ma10) & (ma10 > ma20) & (ma20 > ma60)  # 多头排列
    cond &= (close - ma60) / ma60 * 100 < 35  # 不追高
    cond &= vol / vol.shift(1) > vol_mul
    cond &= close / close.shift(1) > (1 + strength_pct)
    cond &= close > open_df
    if pullback:
        cond &= close < ma5  # 回踩5日线

    # 大盘择时
    if market_filter != "none" and idx_close is not None:
        idx = idx_close.reindex(close.index).ffill()
        idx_ma60 = idx.rolling(60, min_periods=60).mean()
        idx_ma120 = idx.rolling(120, min_periods=120).mean()
        if market_filter == "strict":
            cond = cond.assign(**{"__mf__": (idx > idx_ma60) & (idx_ma60 > idx_ma120)})
            cond = cond.where(cond["__mf__"], False)
            cond = cond.drop(columns="__mf__")
        else:  # loose
            cond = cond.assign(**{"__mf__": idx > idx_ma60})
            cond = cond.where(cond["__mf__"], False)
            cond = cond.drop(columns="__mf__")

    return cond.fillna(False).shift(1).fillna(False).infer_objects(copy=False)


def multi_factor_v2(close, open_df, high, low, vol, idx_close,
                    vol_mul=1.8, body_pct=0.02, market_filter="strict"):
    """多因子v2: 均线粘合后的放量突破 + MACD + 波动收窄."""
    close, open_df, high, low, vol = align_all(close, open_df, high, low, vol)

    ma5, ma10, ma20, ma60 = _ma(close,5), _ma(close,10), _ma(close,20), _ma(close,60)
    # 均线粘合或刚发散
    stickiness = (ma5 - ma20).abs() / ma20 * 100 < 10
    cond = stickiness & (close > ma10)
    # 放量
    cond &= vol > _ma(vol, 5) * vol_mul
    # 实体
    body = (close - open_df) / open_df.replace(0, np.nan)
    cond &= body > body_pct
    cond &= close > open_df
    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26; dea = dif.ewm(span=9, adjust=False).mean()
    cond &= dif > dea

    # 大盘择时
    if market_filter != "none" and idx_close is not None:
        idx = idx_close.reindex(close.index).ffill()
        idx_ma60 = idx.rolling(60, min_periods=60).mean()
        idx_ma120 = idx.rolling(120, min_periods=120).mean()
        if market_filter == "strict":
            cond = cond.assign(**{"__mf__": (idx > idx_ma60) & (idx_ma60 > idx_ma120)})
            cond = cond.where(cond["__mf__"], False).drop(columns="__mf__")
        else:
            cond = cond.assign(**{"__mf__": idx > idx_ma60})
            cond = cond.where(cond["__mf__"], False).drop(columns="__mf__")

    return cond.fillna(False).shift(1).fillna(False).infer_objects(copy=False)


def high_momentum(close, open_df, high, low, vol, idx_close,
                  vol_mul=1.8, lookback=20, market_filter="strict"):
    """高动量: 创N日新高 + 放量 + 均线支撑."""
    close, open_df, high, low, vol = align_all(close, open_df, high, low, vol)

    ma20, ma60 = _ma(close,20), _ma(close,60)
    # 创N日新高
    cond = close == close.rolling(lookback).max()
    cond &= close > ma20
    # 放量
    cond &= vol > _ma(vol, 5) * vol_mul
    cond &= close > open_df
    # 不追太高的
    cond &= (close - ma60) / ma60 * 100 < 50

    if market_filter != "none" and idx_close is not None:
        idx = idx_close.reindex(close.index).ffill()
        idx_ma60 = idx.rolling(60, min_periods=60).mean()
        idx_ma120 = idx.rolling(120, min_periods=120).mean()
        if market_filter == "strict":
            cond = cond.assign(**{"__mf__": (idx > idx_ma60) & (idx_ma60 > idx_ma120)})
            cond = cond.where(cond["__mf__"], False).drop(columns="__mf__")
        else:
            cond = cond.assign(**{"__mf__": idx > idx_ma60})
            cond = cond.where(cond["__mf__"], False).drop(columns="__mf__")

    return cond.fillna(False).shift(1).fillna(False).infer_objects(copy=False)


# ═══════════════════════════════════════════════════════════════

def get_index_close():
    from tqcenter import tq
    try:
        r = tq.get_market_data(field_list=[], stock_list=['999999.SH'],
            start_time='20220101', end_time='20260430',
            period='1d', dividend_type='none', count=-1)
        if r and 'Close' in r:
            return r['Close'].iloc[:, 0]
    except Exception as e:
        logger.warning("get_stock_close failed: %s", e)
    return None


def run_one(close, entries, cost_th, trail_act, trail_dd, lp_v, lr_v, time_d, cap, max_pct):
    common = sorted(set(close.columns) & set(entries.columns))
    c = close[common].ffill().bfill()
    e = entries.reindex(index=c.index, columns=common, fill_value=False)
    ns = int(e.sum().sum())
    if ns < 5: return None
    lp = np.array(lp_v, dtype=np.float64); lr = np.array(lr_v, dtype=np.float64)
    te = time_d < 900
    ea, rt = _simulate_core_v3(
        c.values.astype(np.float64), e.values,
        float(cap), 0.0003, 5000.0, float(cap * max_pct), 100, 1,
        True, cost_th, True, trail_act, trail_dd,
        True, lp, lr, len(lp),
        te, int(time_d) if te else 999, False, 7, 0.01,
    )
    if len(rt) == 0: return None
    cr = (ea[-1] - cap) / cap
    wr = sum(1 for t in rt if t[7] > 0) / len(rt)
    peak = np.maximum.accumulate(ea); mdd = float(np.min((ea - peak) / peak))
    # 年化
    years = len(c) / 244.0
    cagr = (ea[-1] / cap) ** (1 / years) - 1 if years > 0 and cr > -1 else cr
    # 各年收益
    dates = c.index
    yr_ret = {}
    for yr in range(2022, 2027):
        mask = dates.year == yr
        if mask.any():
            yr_equity = ea[mask]
            if len(yr_equity) > 0:
                yr_start = ea[mask][0] if mask.any() else cap
                yr_end = ea[mask][-1] if mask.any() else cap
                yr_ret[str(yr)] = (yr_end / yr_start - 1)
    return {"trades": len(rt), "cum_return": cr, "annual": cagr,
            "win_rate": wr, "max_dd": mdd, "signals": ns, "yearly": yr_ret}


def main():
    logger.info("=" * 60)
    logger.info("2022-2026 v2 — 严格择时 + 重仓 + 宽止损")
    logger.info("=" * 60)

    TdxConnector.initialize()
    from tqcenter import tq

    raw = tq.get_stock_list("50", list_type=1)
    codes = [s["Code"] for s in raw if isinstance(s, dict)]
    codes = [c for c in codes if not (
        'ST' in c or c.startswith('688') or c.startswith('920') or
        c.startswith('430') or c.startswith('873') or
        c.startswith('8') or c.startswith('4')
    ) and c.endswith(('.SH', '.SZ'))]

    cp, op_p, hp, lp, vp = [], [], [], [], []
    for i in range(0, len(codes), 300):
        batch = codes[i:i+300]
        try:
            r = tq.get_market_data(field_list=[], stock_list=batch,
                start_time="20220101", end_time="20260430",
                period="1d", dividend_type="front", count=-1)
            if r and "Close" in r:
                cp.append(r["Close"]); op_p.append(r["Open"])
                hp.append(r["High"]); lp.append(r.get("Low", r["Close"]*0.98))
                vp.append(r["Volume"])
        except Exception as e: logger.warning("get_kline failed: %s", e)
    close = pd.concat(cp, axis=1).ffill().bfill()
    open_df = pd.concat(op_p, axis=1).ffill().bfill()
    high = pd.concat(hp, axis=1).ffill().bfill()
    low = pd.concat(lp, axis=1).ffill().bfill()
    vol = pd.concat(vp, axis=1).ffill().bfill()
    idx_close = get_index_close()
    logger.info(f"Data: {close.shape[0]} bars x {close.shape[1]} stocks, Idx: {len(idx_close) if idx_close is not None else 0}")

    STRATEGIES = [
        ("趋势突破", trend_breakout, {
            "vol_mul": [1.3, 1.5, 1.8], "strength_pct": [0.01, 0.015, 0.02],
            "pullback": [False, True], "market_filter": ["strict", "loose"],
        }),
        ("多因子v2", multi_factor_v2, {
            "vol_mul": [1.5, 1.8, 2.0], "body_pct": [0.01, 0.015, 0.02],
            "market_filter": ["strict", "loose"],
        }),
        ("高动量", high_momentum, {
            "vol_mul": [1.5, 1.8, 2.0], "lookback": [20, 30, 50],
            "market_filter": ["strict", "loose"],
        }),
    ]

    # 止损: [cost_th, trail_act, trail_dd, ladder_profits, ladder_ratios, time_days]
    STOP_GRID = [
        ("宽A", -0.10, 0.08, 0.05, [0.05,0.15,0.30], [0.25,0.35,1.0], 20),
        ("宽B", -0.12, 0.10, 0.06, [0.08,0.20,0.40], [0.25,0.35,1.0], 30),
        ("宽C", -0.15, 0.12, 0.08, [0.10,0.25,0.50], [0.20,0.35,1.0], 999),
    ]

    POS_SIZES = [(1_000_000, 0.15), (1_000_000, 0.20), (2_000_000, 0.15)]

    all_results = []
    max_trials = 10

    for sname, fn, grid in STRATEGIES:
        keys = list(grid.keys()); values = list(grid.values())
        combos = list(itertools.product(*values))
        if len(combos) > max_trials:
            np.random.seed(42)
            idx = np.random.choice(len(combos), max_trials, replace=False)
            combos = [combos[i] for i in idx]

        for combo in combos:
            params = dict(zip(keys, combo))
            try:
                entries = fn(close, open_df, high, low, vol, idx_close, **params)
            except Exception as e:
                continue

            for sn, cost_th, trail_act, trail_dd, lp_v, lr_v, time_d in STOP_GRID:
                for cap, max_pct in POS_SIZES:
                    if max_pct * cap < 50000: continue
                    m = run_one(close, entries, cost_th, trail_act, trail_dd, lp_v, lr_v, time_d, cap, max_pct)
                    if m is None: continue
                    m.update({"strategy": sname, "stop": sn, "params": params, "capital": cap, "max_pct": max_pct})
                    all_results.append(m)
                    flag = "★★★" if m["annual"] >= 0.25 else ("★★" if m["annual"] >= 0.15 else ("★" if m["annual"] >= 0.10 else " "))
                    logger.info(f"  {flag} [{sname}][{sn}] cap={cap/10000:.0f}w/{max_pct*100:.0f}% "
                                f"累计={m['cum_return']:.2%} 年化={m['annual']:.2%} "
                                f"胜率={m['win_rate']:.1%} 回撤={m['max_dd']:.2%} "
                                f"交易={m['trades']} {params}")

    all_results.sort(key=lambda x: x["annual"], reverse=True)

    logger.info(f"\nTOP 20:")
    for i, r in enumerate(all_results[:20]):
        logger.info(f"  #{i+1} [{r['strategy']}][{r['stop']}] cap={r['capital']/10000:.0f}w/{r['max_pct']*100:.0f}% "
                    f"累计={r['cum_return']:.2%} 年化={r['annual']:.2%} "
                    f"胜率={r['win_rate']:.1%} 回撤={r['max_dd']:.2%} "
                    f"交易={r['trades']} {r['params']}")
        if r.get("yearly"):
            logger.info(f"       各年: {r['yearly']}")

    good = [r for r in all_results if r["annual"] >= 0.25]
    if good:
        logger.info(f"\n★★★ {len(good)} 策略达到年化25%+！")
        best = good[0]
        logger.info(f"  BEST: [{best['strategy']}][{best['stop']}] cap={best['capital']/10000:.0f}w/{best['max_pct']*100:.0f}%")
        logger.info(f"  累计={best['cum_return']:.2%} 年化={best['annual']:.2%} 胜率={best['win_rate']:.1%} 回撤={best['max_dd']:.2%}")
        logger.info(f"  各年: {best.get('yearly',{})}")
        logger.info(f"  参数: {best['params']}")
    else:
        best = all_results[0]
        logger.info(f"\n未达25%，最高年化={best['annual']:.2%} 累计={best['cum_return']:.2%}")
        logger.info(f"  [{best['strategy']}][{best['stop']}] 各年: {best.get('yearly',{})}")

    save = [{k: (float(v) if isinstance(v, (np.floating,)) else str(v) if isinstance(v, (pd.Timestamp,)) else v) for k, v in r.items()} for r in all_results]
    with open("results_2022_2026_v2.json", "w") as f:
        json.dump(save, f, ensure_ascii=False, indent=2, default=str)

    TdxConnector.close()


if __name__ == "__main__":
    main()
