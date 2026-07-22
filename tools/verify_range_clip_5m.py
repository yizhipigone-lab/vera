"""2026-07-21 区间精确化迭代 — 端到端验证脚本 (需 TDX 连接)。

复用 2026-07-21 23:01 web 运行的选股信号 (output/selections/回测_raw_20260721_225525.csv,
黑马选股1 / 2024-01-01~2025-01-01 / 60229 条), 用新代码重跑 5m 引擎 + 基准对比, 验证:

1. 权益曲线起点 = 2024-01-02 (请求起点, 5m 深度之前走 1d 降级), 终点 = 2024-12-31 (无 +75d 尾巴)
2. 2024 年 1~6 月有成交 (降级保住信号)
3. 期末未平仓按市值统计, open_positions 明细存在, 无边界退市强平
4. 基准曲线覆盖完整区间 (日粒度回退, granularity=1d)

用法: python tools/verify_range_clip_5m.py
"""
import glob
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest.engine import BacktestEngine
from backtest.benchmark import BenchmarkComparator

# web 运行 (last_result.json stop_config_summary) 复刻:
# 成本止损 -40% / 阶梯 盈利1000%卖20% / 移动止盈 激活6%回撤1% / 时间止损 60天
STOP_CONFIG = {
    "priority": "trailing_first",
    "cost_stop": {"enabled": True, "threshold": -0.40},
    "trailing_stop": {"enabled": True, "activation": 0.06, "drawdown": 0.01},
    "ladder_tp": {"enabled": True, "levels": [{"profit": 10.0, "sell_ratio": 0.20}]},
    "time_stop": {"enabled": True, "max_hold_days": 60},
    "cond_time_stop": {"enabled": False},
    "first_day": {"enabled": False},
}

ENGINE_CONFIG = {
    "period": "5m",
    "degrade_5m": True,
    "initial_capital": 1000000.0,
    "commission": 0.0003,
    "slippage": 0.001,
    "use_kline_cache": True,
    "position_sizing": {"min_buy_amount": 2000.0, "max_buy_amount": 20000.0,
                        "lot_size": 100, "min_lots": 1},
}

START, END = "20240101", "20250101"


def main():
    csv = sorted(glob.glob("output/selections/*raw_20260721_225525.csv"))[-1]
    sel = pd.read_csv(csv)
    print(f"信号: {len(sel)} 条, {sel['stock_code'].nunique()} 只, "
          f"{sel['select_date'].min()} ~ {sel['select_date'].max()}")

    eng = BacktestEngine(ENGINE_CONFIG)
    result = eng.run(selections=sel, start_time=START, end_time=END,
                     stop_config=STOP_CONFIG)

    eq = result.equity_curve
    eq_dates = pd.to_datetime(eq["date"])
    trades = result.trades
    checks = []

    # 1. 权益起止 = 请求区间
    first_d, last_d = eq_dates.iloc[0], eq_dates.iloc[-1]
    checks.append(("权益起点=2024-01-02", str(first_d).startswith("2024-01-02"), str(first_d)))
    checks.append(("权益终点=2024-12-31 (无尾巴)", str(last_d).startswith("2024-12-31"), str(last_d)))

    # 2. 1~6 月有成交 (降级保住信号)
    if not trades.empty:
        ent = pd.to_datetime(trades["entry_date"])
        hj = ent[(ent >= "2024-01-01") & (ent < "2024-07-01")]
        checks.append(("2024 上半年有成交 (降级生效)", len(hj) > 0, f"{len(hj)} 笔"))
        monthly = ent.dt.to_period("M").value_counts().sort_index()
        print("\n成交按月分布:")
        print(monthly.to_string())
    else:
        checks.append(("2024 上半年有成交 (降级生效)", False, "0 笔交易"))

    # 3. 期末未平仓
    ops = result.get("open_positions")
    n_ops = len(ops) if ops else 0
    checks.append(("期末未平仓明细存在", n_ops > 0, f"{n_ops} 笔"))
    if ops:
        mv = sum(p["market_value"] for p in ops)
        pnl = sum(p["unrealized_pnl"] for p in ops)
        print(f"\n未平仓 {n_ops} 笔, 市值合计 {mv:,.0f}, 浮动盈亏 {pnl:+,.0f}")
        print("样例:", ops[0])
    if not trades.empty:
        n_delist = int((trades["exit_reason"] == "退市").sum())
        print(f"退市强平 (reason=11) 笔数: {n_delist} (应仅来自区间内真实退市, 非边界)")

    # 4. 基准覆盖完整区间 (日粒度回退)
    cmp = BenchmarkComparator({"period": "5m",
                               "indices": ["shanghai", "hs300", "chuangyeban"]})
    bm = cmp.fetch_and_compare(
        eq, start_time=first_d.strftime("%Y%m%d"), end_time=last_d.strftime("%Y%m%d"))
    for name, comp in bm.items():
        if comp.empty:
            checks.append((f"基准[{name}]非空", False, "empty"))
            continue
        gran = comp.attrs.get("stats", {}).get("granularity", "5m")
        ok = (str(comp.index.min()).startswith("2024-01-02")
              and str(comp.index.max()).startswith("2024-12-31"))
        checks.append((f"基准[{name}]覆盖全区间 (粒度 {gran})", ok,
                       f"{comp.index.min()} ~ {comp.index.max()}, {len(comp)} 点"))

    degr = result.get("degradation")
    if degr:
        print(f"\n降级报告: 降级持仓 {degr['degraded_trades']}/{degr['total_trades']} "
              f"({degr['degraded_pct']:.1%}), 降级股-天 {degr['n_stock_days']}")

    print("\n===== 验证结果 =====")
    ok_all = True
    for name, ok, detail in checks:
        ok_all = ok_all and ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    print("===== 全部通过 =====" if ok_all else "===== 存在失败项 =====")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
