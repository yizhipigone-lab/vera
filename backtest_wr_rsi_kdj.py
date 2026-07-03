"""WR+RSI+KDJ 多指标增强策略 — 2022-2026 回测，目标年化25%"""
import sys, os, itertools, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd, numpy as np
from core.connector import TdxConnector
from utils.logger import get_logger
from backtest.engine import _simulate_core_v3

logger = get_logger(__name__)

def _ma(df, w): return df.rolling(w, min_periods=w).mean()

def calc_wr(high, low, close, n=14):
    """Williams %R: -100(oversold) ~ 0(overbought)"""
    hh = high.rolling(n, min_periods=n).max()
    ll = low.rolling(n, min_periods=n).min()
    wr = (hh - close) / (hh - ll).replace(0, np.nan) * -100
    return wr

def calc_rsi(close, n=14):
    """RSI: 0~100"""
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(n, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).rolling(n, min_periods=n).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def calc_kdj(high, low, close, n=9, m1=3, m2=3):
    """KDJ: 返回 (K, D, J)"""
    llv = low.rolling(n, min_periods=n).min()
    hhv = high.rolling(n, min_periods=n).max()
    rsv = (close - llv) / (hhv - llv).replace(0, np.nan) * 100
    k = rsv.ewm(span=m1, adjust=False).mean()
    d = k.ewm(span=m2, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j

def align_all(*dfs):
    cols = dfs[0].columns; idx = dfs[0].index
    for d in dfs[1:]:
        cols = cols.intersection(d.columns); idx = idx.intersection(d.index)
    return tuple(d.loc[idx, cols] for d in dfs)

def get_index():
    from tqcenter import tq
    try:
        r = tq.get_market_data(field_list=[], stock_list=['999999.SH'],
            start_time='20220101', end_time='20260430',
            period='1d', dividend_type='none', count=-1)
        return r['Close'].iloc[:, 0] if r and 'Close' in r else None
    except Exception as e: logger.warning("get_stock_close failed: %s", e); return None

def enhanced_strategy(close, open_df, high, low, vol, idx_close, params):
    """多指标增强策略."""
    close, open_df, high, low, vol = align_all(close, open_df, high, low, vol)

    # 均线趋势
    ma5, ma10, ma20, ma60 = _ma(close,5), _ma(close,10), _ma(close,20), _ma(close,60)
    cond = (ma5 > ma10) & (ma10 > ma20) & (ma20 > ma60)
    cond &= (close - ma60) / ma60 * 100 < 40

    # 量价
    cond &= vol / _ma(vol, 5) > params.get("vol_mul", 1.5)
    cond &= close / close.shift(1) > (1 + params.get("min_ret", 0.01))
    cond &= close > open_df

    # WR 过滤：超卖区域 (-80到-20，避免极端超卖和超买)
    wr = calc_wr(high, low, close, params.get("wr_period", 14))
    wr_lo, wr_hi = params.get("wr_range", (-80, -20))
    cond &= (wr > wr_lo) & (wr < wr_hi)

    # RSI 过滤：不过热
    rsi = calc_rsi(close, params.get("rsi_period", 14))
    cond &= rsi < params.get("rsi_max", 70)

    # KDJ 金叉
    use_kdj = params.get("use_kdj", True)
    if use_kdj:
        k, d, j = calc_kdj(high, low, close,
                           params.get("kdj_n", 9),
                           params.get("kdj_m1", 3),
                           params.get("kdj_m2", 3))
        cond &= (k > d) & (k.shift(1) <= d.shift(1))  # KDJ金叉

    # 回调买入
    if params.get("pullback", True):
        cond &= close < ma5

    # 大盘择时
    mf = params.get("market_filter", "loose")
    if mf != "none" and idx_close is not None:
        idx = idx_close.reindex(close.index).ffill()
        idx_ma60 = idx.rolling(60, min_periods=60).mean()
        if mf == "strict":
            idx_ma120 = idx.rolling(120, min_periods=120).mean()
            cond = cond.assign(__mf__=(idx > idx_ma60) & (idx_ma60 > idx_ma120))
        else:
            cond = cond.assign(__mf__=idx > idx_ma60)
        cond = cond.where(cond["__mf__"], False).drop(columns="__mf__")

    return cond.fillna(False).shift(1).fillna(False).infer_objects(copy=False)


def run_one(close, entries, stop_params, cap, max_pct):
    common = sorted(set(close.columns) & set(entries.columns))
    c = close[common].ffill().bfill()
    e = entries.reindex(index=c.index, columns=common, fill_value=False)
    ns = int(e.sum().sum())
    if ns < 5: return None

    ct, ta, td, lpv, lrv, tdays = stop_params
    lp = np.array(lpv, dtype=np.float64); lr = np.array(lrv, dtype=np.float64)
    te = tdays < 900
    ea, rt = _simulate_core_v3(
        c.values.astype(np.float64), e.values,
        float(cap), 0.0003, 5000.0, float(cap * max_pct), 100, 1,
        True, ct, True, ta, td,
        True, lp, lr, len(lp),
        te, int(tdays) if te else 999, False, 7, 0.01,
    )
    if len(rt) == 0: return None
    cr = (ea[-1] - cap) / cap
    wr = sum(1 for t in rt if t[7] > 0) / len(rt)
    peak = np.maximum.accumulate(ea); mdd = float(np.min((ea - peak) / peak))
    years = len(c) / 244.0
    cagr = (ea[-1] / cap) ** (1 / years) - 1 if years > 0 and cr > -1 else cr
    # Yearly breakdown
    dates = c.index
    yr_ret = {}
    for yr in range(2022, 2027):
        mask = dates.year == yr
        if mask.sum() > 0:
            yr_e = ea[mask]
            yr_ret[str(yr)] = float((yr_e[-1] / yr_e[0] - 1) if len(yr_e) > 1 else 0)
    return {"trades": len(rt), "cum_return": cr, "annual": cagr,
            "win_rate": wr, "max_dd": mdd, "signals": ns, "yearly": yr_ret}


def main():
    logger.info("=" * 60)
    logger.info("WR+RSI+KDJ 多指标增强策略 2022-2026")
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
    idx_close = get_index()
    logger.info(f"Data: {close.shape[0]} bars x {close.shape[1]} stocks")

    # ── 参数网格 ──
    PARAM_GRID = [
        # (name, params_dict)
        # 组合1: WR超卖+RSI中性+KDJ金叉+回调
        ("WR超卖+KDJ", {
            "vol_mul": [1.3, 1.5, 1.8], "min_ret": [0.01, 0.015],
            "wr_period": [14], "wr_range": [(-80, -30)],
            "rsi_period": [14], "rsi_max": [60],
            "use_kdj": [True], "kdj_n": [9], "kdj_m1": [3], "kdj_m2": [3],
            "pullback": [True], "market_filter": ["loose"],
        }),
        # 组合2: WR中性+RSI不超买+KDJ金叉+突破
        ("WR中性+突破", {
            "vol_mul": [1.5, 1.8, 2.0], "min_ret": [0.015, 0.02],
            "wr_period": [10], "wr_range": [(-50, -10)],
            "rsi_period": [7], "rsi_max": [70],
            "use_kdj": [True], "kdj_n": [5], "kdj_m1": [3], "kdj_m2": [3],
            "pullback": [False], "market_filter": ["loose"],
        }),
        # 组合3: 纯RSI+WR 无KDJ
        ("WR+RSI无KDJ", {
            "vol_mul": [1.3, 1.5], "min_ret": [0.01, 0.015],
            "wr_period": [21], "wr_range": [(-90, -40)],
            "rsi_period": [14], "rsi_max": [55],
            "use_kdj": [False], "kdj_n": [9], "kdj_m1": [3], "kdj_m2": [3],
            "pullback": [True], "market_filter": ["loose"],
        }),
        # 组合4: 全部指标+严格大盘
        ("全指标+严格", {
            "vol_mul": [1.3, 1.5], "min_ret": [0.01, 0.015],
            "wr_period": [14], "wr_range": [(-80, -20)],
            "rsi_period": [14], "rsi_max": [65],
            "use_kdj": [True], "kdj_n": [9], "kdj_m1": [3], "kdj_m2": [3],
            "pullback": [True], "market_filter": ["strict"],
        }),
        # 组合5: KDJ快速+WR极端超卖反弹
        ("KDJ快速+超卖", {
            "vol_mul": [1.3, 1.5], "min_ret": [0.005, 0.01],
            "wr_period": [7], "wr_range": [(-95, -60)],
            "rsi_period": [7], "rsi_max": [50],
            "use_kdj": [True], "kdj_n": [5], "kdj_m1": [2], "kdj_m2": [2],
            "pullback": [True], "market_filter": ["loose"],
        }),
    ]

    # 止损配置
    STOPS = [
        ("宽A", -0.10, 0.08, 0.05, [0.05, 0.15, 0.30], [0.25, 0.35, 1.0], 25),
        ("宽B", -0.12, 0.10, 0.06, [0.08, 0.20, 0.40], [0.25, 0.35, 1.0], 30),
        ("宽C", -0.15, 0.12, 0.08, [0.10, 0.25, 0.50], [0.20, 0.35, 1.0], 999),
    ]
    POS_SIZES = [(1_000_000, 0.20), (1_000_000, 0.25)]

    all_results = []

    for cname, grid in PARAM_GRID:
        keys = list(grid.keys()); values = list(grid.values())
        combos = list(itertools.product(*values))
        if len(combos) > 8:
            np.random.seed(42)
            idx = np.random.choice(len(combos), 8, replace=False)
            combos = [combos[i] for i in idx]

        for combo in combos:
            params = dict(zip(keys, combo))
            try:
                entries = enhanced_strategy(close, open_df, high, low, vol, idx_close, params)
            except Exception as e:
                continue

            for sn, ct, ta, td, lpv, lrv, tdays in STOPS:
                for cap, max_pct in POS_SIZES:
                    m = run_one(close, entries, (ct, ta, td, lpv, lrv, tdays), cap, max_pct)
                    if m is None: continue
                    m.update({"combo": cname, "stop": sn, "params": str(params),
                              "capital": cap, "max_pct": max_pct})
                    all_results.append(m)
                    flag = "★★★" if m["annual"] >= 0.25 else ("★★" if m["annual"] >= 0.18 else ("★" if m["annual"] >= 0.12 else " "))
                    logger.info(f"  {flag} [{cname}][{sn}] cap={cap/10000:.0f}w/{max_pct*100:.0f}% "
                                f"累计={m['cum_return']:.2%} 年化={m['annual']:.2%} "
                                f"胜率={m['win_rate']:.1%} 回撤={m['max_dd']:.2%} "
                                f"交易={m['trades']}")

    all_results.sort(key=lambda x: x["annual"], reverse=True)

    logger.info(f"\n{'='*60}")
    logger.info(f"TOP 15 (按年化)")
    logger.info(f"{'='*60}")
    for i, r in enumerate(all_results[:15]):
        logger.info(f"  #{i+1} [{r['combo']}][{r['stop']}] cap={r['capital']/10000:.0f}w/{r['max_pct']*100:.0f}% "
                    f"累计={r['cum_return']:.2%} 年化={r['annual']:.2%} "
                    f"胜率={r['win_rate']:.1%} 回撤={r['max_dd']:.2%} "
                    f"交易={r['trades']} 各年={r.get('yearly',{})}")
        logger.info(f"       params={r['params']}")

    good = [r for r in all_results if r["annual"] >= 0.25]
    logger.info(f"\n年化>=25%: {len(good)} 个")
    if good:
        for r in good[:5]:
            logger.info(f"  [{r['combo']}][{r['stop']}] 年化={r['annual']:.2%} 累计={r['cum_return']:.2%}")

    TdxConnector.close()


if __name__ == "__main__":
    main()
