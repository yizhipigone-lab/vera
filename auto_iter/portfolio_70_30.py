# -*- coding: utf-8 -*-
"""组合回测: 70% 稳健腿(iter_828) + 30% 进攻腿(iter_1092), 2019-01 ~ 2026-07。

设计 (2026-08-10 研究报告 §8.4 提出的折中方案, 本脚本把它做实):
  - 两腿共用同一股票池/同一费用口径(万三佣金+千一滑点+印花税), 条件单语义不受影响
    (两腿移动止盈均未启用);
  - 资金分配通过 bt_cfg 落地: 稳健腿本金 70 万/单笔上限 1.4 万,
    进攻腿本金 30 万/单笔上限 6 千;
  - 组合权益 = 两腿权益逐日相加 (100 万总本金), 绩效从组合权益曲线重算。

用法: python -X utf8 auto_iter/portfolio_70_30.py
"""
import sys, os, copy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
from auto_iter.common import (enforce_offline, build_pool_candidates, load_panel,
                              filter_pool, shrink_panel, seed_stock_info,
                              build_signals, run_backtest)
from auto_iter import auto_strategy_loop as m

# ═══ 腿A (70%): iter_828 稳健型 — 7日暴跌23.9% + 贴23日低点 + RSI(9)≤15
#     年化 18.8% / 回撤 -12.4% / 胜率 82% / PF 8.4
LEG_A = {
    "side": "left",
    "factors": [{"name": "ret_drop", "n": 7, "x": 0.2389},
                {"name": "bias_low", "n": 23, "x": 0.0123},
                {"name": "rsi_low", "n": 9, "th": 15.0}],
    "stop": {"priority": "trailing_first",
             "cost_stop": {"enabled": True, "threshold": -0.25},
             "trailing_stop": {"enabled": False, "activation": 0.0698,
                               "drawdown": 0.0387, "confirm": "real"},
             "ladder_tp": {"enabled": True, "levels": [
                 {"profit": 0.0712, "sell_ratio": 0.2161},
                 {"profit": 0.1775, "sell_ratio": 0.1923},
                 {"profit": 0.3099, "sell_ratio": 0.2673}]},
             "time_stop": {"enabled": True, "max_hold_days": 41},
             "cond_time_stop": {"enabled": False}, "first_day": {"enabled": False},
             "formula_sell": {"enabled": False}},
    "capital": 700_000.0, "ticket": 14_000.0,   # 70% 资金, 单笔同比例缩
}

# ═══ 腿B (30%): iter_1092 进攻型 — 低于55日线20% + 贴26日低点 + RSI(8)≤19
#     年化 27.1% / 回撤 -30.3% / 胜率 36% / PF 1.83
LEG_B = {
    "side": "left",
    "factors": [{"name": "bias_ma", "n": 55, "x": 0.2},
                {"name": "bias_low", "n": 26, "x": 0.0117},
                {"name": "rsi_low", "n": 8, "th": 19.0121}],
    "stop": {"priority": "trailing_first",
             "cost_stop": {"enabled": True, "threshold": -0.25},
             "trailing_stop": {"enabled": False, "activation": 0.0964,
                               "drawdown": 0.0363, "confirm": "real"},
             "ladder_tp": {"enabled": True, "levels": [
                 {"profit": 0.0714, "sell_ratio": 0.1683},
                 {"profit": 0.1315, "sell_ratio": 0.1619},
                 {"profit": 0.3335, "sell_ratio": 0.2161}]},
             "time_stop": {"enabled": True, "max_hold_days": 35},
             "cond_time_stop": {"enabled": False}, "first_day": {"enabled": False},
             "formula_sell": {"enabled": False}},
    "capital": 300_000.0, "ticket": 6_000.0,    # 30% 资金
}


def run_leg(panel, leg) -> dict:
    """单腿回测: 资金/单笔按配比缩放, 返回 equity 曲线与 metrics。"""
    entries = build_signals(panel, leg["side"], leg["factors"])
    bt = copy.deepcopy(m.DEFAULT_BT_CFG)
    bt["initial_capital"] = leg["capital"]
    bt["position_sizing"] = dict(bt["position_sizing"],
                                 max_buy_amount=leg["ticket"])
    res = run_backtest(panel, entries, leg["stop"], bt)
    eq = res["equity_curve"].set_index("date")["equity"]
    n_sig = int(entries.loc[m.BT_START:m.BT_END].sum().sum())
    return {"equity": eq, "metrics": res["metrics"], "n_signals": n_sig,
            "trades": res.get("trades")}


def perf_from_equity(eq: pd.Series, capital: float) -> dict:
    """从权益曲线(元)重算组合级绩效, 口径对齐引擎 (252 交易日/年)。"""
    eq = eq.dropna()
    rets = eq.pct_change().dropna()
    n = len(eq)
    cum = float(eq.iloc[-1] / eq.iloc[0] - 1)
    ann = float((eq.iloc[-1] / eq.iloc[0]) ** (252 / max(n, 1)) - 1)
    peak = eq.cummax()
    dd = eq / peak - 1
    sharpe = float(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0.0
    return {"累计收益": cum, "年化收益": ann, "最大回撤": float(dd.min()),
            "夏普": sharpe, "卡玛": ann / abs(float(dd.min())) if dd.min() < 0 else 0.0,
            "期末权益": float(eq.iloc[-1]), "本金": capital}


def yearly(eq: pd.Series) -> dict:
    eq = eq.copy()
    eq.index = pd.to_datetime(eq.index)
    return {str(y): float(g.iloc[-1] / g.iloc[0] - 1)
            for y, g in eq.groupby(eq.index.year) if len(g) > 1}


def dd_window(eq: pd.Series) -> dict:
    eq = eq.copy()
    eq.index = pd.to_datetime(eq.index)
    dd = eq / eq.cummax() - 1
    trough = dd.idxmin()
    start = eq.loc[:trough].idxmax()
    rec = eq.loc[trough:][eq.loc[trough:] >= eq.loc[start]]
    return {"峰": str(start.date()), "谷": str(trough.date()),
            "深度": float(dd.min()),
            "修复": str(rec.index[0].date()) if len(rec) else "未修复"}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    enforce_offline()
    args = m.parse_args([])
    pool_params = {"amount_quantile": args.amount_quantile,
                   "min_price": 1.0, "max_stocks": args.pool_size}
    candidates = build_pool_candidates()
    panel = load_panel(candidates)
    codes = filter_pool(panel, **pool_params)
    panel = shrink_panel(panel, codes)
    seed_stock_info(codes)
    print(f"[初始化] 股票池 {len(codes)} 只", flush=True)

    a = run_leg(panel, LEG_A)
    print(f"腿A(稳健70%): 信号 {a['n_signals']} 年化 {a['metrics']['annualized_return']:+.1%} "
          f"回撤 {a['metrics']['max_drawdown']:+.1%} 交易 {a['metrics']['total_trades']} 笔", flush=True)
    b = run_leg(panel, LEG_B)
    print(f"腿B(进攻30%): 信号 {b['n_signals']} 年化 {b['metrics']['annualized_return']:+.1%} "
          f"回撤 {b['metrics']['max_drawdown']:+.1%} 交易 {b['metrics']['total_trades']} 笔", flush=True)

    combo = (a["equity"] + b["equity"]).dropna()
    p = perf_from_equity(combo, 1_000_000.0)
    w = dd_window(combo)

    print("\n════════ 组合 (70%稳健 + 30%进攻, 总本金 100 万) ════════")
    print(f"期末权益: {p['期末权益']:,.0f} 元  (累计 {p['累计收益']:+.1%})")
    print(f"年化收益: {p['年化收益']:+.2%}   最大回撤: {p['最大回撤']:+.2%}")
    print(f"夏普: {p['夏普']:.2f}   卡玛: {p['卡玛']:.2f}")
    print(f"最大回撤区间: {w['峰']} 顶 → {w['谷']} 底 ({w['深度']:+.1%}), {w['修复']}修复")
    print("\n逐年收益:")
    for y, r in yearly(combo).items():
        print(f"  {y}: {r:+.1%}")

    # 对照表
    print("\n════════ 对照 (年化 / 回撤 / 卡玛) ════════")
    ma, mb = a["metrics"], b["metrics"]
    print(f"腿A单跑(100%): {ma['annualized_return']:+.1%} / {ma['max_drawdown']:+.1%} / "
          f"{ma['calmar_ratio']:.2f}")
    print(f"腿B单跑(100%): {mb['annualized_return']:+.1%} / {mb['max_drawdown']:+.1%} / "
          f"{mb['calmar_ratio']:.2f}")
    print(f"组合 70/30  : {p['年化收益']:+.1%} / {p['最大回撤']:+.1%} / {p['卡玛']:.2f}")

    # 权重扫描 (近似): 把两腿权益按本金归一后按新权重线性混合。
    # 注意是估算 — 真实改权重应同步缩放单笔金额重跑, 单笔大小对成交有轻微影响
    # (腿B 30%资金实测年化 24.2% vs 100%资金 27.1%)。用于找方向, 不定稿。
    print("\n════════ 权重扫描 (估算, 精确值需按新权重重跑) ════════")
    cap_a, cap_b = LEG_A["capital"], LEG_B["capital"]
    for w in (0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90):
        mix = (a["equity"] / cap_a * w * 1_000_000
               + b["equity"] / cap_b * (1 - w) * 1_000_000).dropna()
        pw = perf_from_equity(mix, 1_000_000.0)
        flag = " ← 回撤≤15%且年化最高" if pw["最大回撤"] >= -0.15 else ""
        print(f"稳健 {w:.0%} / 进攻 {1-w:.0%}: 年化 {pw['年化收益']:+.1%} "
              f"回撤 {pw['最大回撤']:+.1%} 卡玛 {pw['卡玛']:.2f}{flag}")


if __name__ == "__main__":
    main()
