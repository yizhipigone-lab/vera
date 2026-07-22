"""
1d 分年份回测 (2026-07-21)

每公式用最优参数(从 sweep 取年化最高), 分 4 年段独立回测 (资金每段重置300万):
  2023H2 (08-12) / 2024 / 2025 / 2026H1 (01-07)
输出每年 年化/回撤/交易/胜率/Sharpe + 3年汇总对比.

用法: python tools/gs_1d_yearly.py
"""
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from quantqq_5m_sweep import COARSE, combo_stop_config  # noqa: E402
from backtest.engine import BacktestEngine  # noqa: E402

YEARS = [("2023H2", "20230801", "20231231"),
         ("2024", "20240101", "20241231"),
         ("2025", "20250101", "20251231"),
         ("2026H1", "20260101", "20260717")]
LADDER = dict(COARSE["ladder"])  # name -> (name, levels)

BT_CFG = {"initial_capital": 3_000_000.0, "commission": 0.0003, "slippage": 0.001,
          "stamp_tax": 0.0005, "enable_realistic_costs": True, "period": "1d",
          "position_sizing": {"min_buy_amount": 2000.0, "max_buy_amount": 20000.0,
                              "lot_size": 100, "min_lots": 1},
          "use_kline_cache": True}


def best_params(formula):
    df = pd.read_csv(f"output/gs_1d_sweep/{formula}/sweep_1d.csv")
    for c in ["annret", "cost", "act", "dd", "time_days", "cond_days",
              "cond_profit", "calmar", "maxdd", "trades"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[df["annret"].notna()]
    return df.loc[df["annret"].idxmax()]


def run_year(sel, c_dict, start, end):
    engine = BacktestEngine(BT_CFG)
    res = engine.run(selections=sel, start_time=start, end_time=end,
                     stop_config=combo_stop_config(c_dict, "trailing_first"))
    return res["metrics"]


def main():
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
    except Exception:
        pass

    formulas = json.load(open("output/gs_filter/candidates_12pct.json",
                              encoding="utf-8"))
    rows = []
    for formula in formulas:
        sel_path = f"output/gs_1d_sweep/{formula}/selections.csv"
        if not os.path.exists(sel_path):
            print(f"[{formula}] 无 selections, 跳过")
            continue
        sel = pd.read_csv(sel_path, dtype={"stock_code": str})
        bp = best_params(formula)
        c_dict = {"cost": bp["cost"], "act": bp["act"], "dd": bp["dd"],
                  "ladder": bp["ladder"], "levels": LADDER[bp["ladder"]][1],
                  "time_days": bp["time_days"], "cond_days": bp["cond_days"],
                  "cond_profit": bp["cond_profit"]}
        print(f"\n=== {formula} | 最优参数: 硬止损{abs(bp['cost'])*100:.0f}% "
              f"激活{bp['act']*100:.1f}% 回撤{bp['dd']*100:.1f}% "
              f"{bp['ladder']} 时间{int(bp['time_days'])}天 ===", flush=True)
        for yn, ys, ye in YEARS:
            m = run_year(sel, c_dict, ys, ye)
            ann = m["annualized_return"] * 100
            dd = m["max_drawdown"] * 100
            print(f"  {yn}: 年化{ann:>7.1f}% 回撤{dd:>6.1f}% 交易{m['total_trades']:>6} "
                  f"胜率{m['win_rate']*100:>4.0f}% Sharpe{m.get('sharpe_ratio',0):>5.2f} "
                  f"Calmar{m.get('calmar_ratio',0):>5.1f}", flush=True)
            rows.append({"formula": formula, "year": yn,
                         "annret": m["annualized_return"],
                         "maxdd": m["max_drawdown"],
                         "trades": m["total_trades"],
                         "winrate": m["win_rate"],
                         "sharpe": m.get("sharpe_ratio", 0),
                         "calmar": m.get("calmar_ratio", 0),
                         "best_cost": bp["cost"], "best_act": bp["act"]})
    out = pd.DataFrame(rows)
    out.to_csv("output/gs_1d_sweep/yearly_breakdown.csv", index=False,
               encoding="utf-8")
    print(f"\n[OUT] output/gs_1d_sweep/yearly_breakdown.csv ({len(rows)} 行)")


if __name__ == "__main__":
    main()
