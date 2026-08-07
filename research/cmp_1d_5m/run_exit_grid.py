# -*- coding: utf-8 -*-
"""出场结构网格驱动 (tdx-strategy-research §4): 只调止盈止损, 信号算一次复用。

用法: python -X utf8 research/cmp_1d_5m/run_exit_grid.py <start> <end> <tag>
变体表在 VARIANTS 里 (归因结论 → 小幅微调, 改阈值不改结构)。
所有变体统一 trailing confirm (诚实语义), 1D/5M 同语义可终审。
"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
from backtest.engine import BacktestEngine
from selection.selector import StockSelector

BT_CFG = {
    "initial_capital": 1000000.0,
    "commission": 0.0003,
    "slippage": 0.001,
    "period": os.environ.get("PERIOD", "1d"),
    "degrade_5m": True,
    "position_sizing": {"max_positions": 999, "min_buy_amount": 2000.0,
                        "max_buy_amount": 20000.0, "lot_size": 100, "min_lots": 1},
}

CONFIRM = os.environ.get("CONFIRM", "close")  # low | close

# 用户当前基准出场 (2026-08-04 拍板): -12% / 6%+15%卖30% / 移动6%激活回撤1% / 12天
def _stop(cost=-0.12, act=0.06, dd=0.01, ladder=((0.06, 0.3), (0.15, 0.3)),
          mhd=12, cond=None, confirm=CONFIRM):
    return {
        "priority": "stop_first",
        "cost_stop": {"enabled": True, "threshold": cost},
        "trailing_stop": {"enabled": True, "activation": act, "drawdown": dd,
                          "confirm": confirm},
        "ladder_tp": {"enabled": bool(ladder), "levels": [
            {"profit": p, "sell_ratio": r} for p, r in ladder]},
        "time_stop": {"enabled": True, "max_hold_days": mhd},
        "cond_time_stop": ({"enabled": True, "days": cond[0], "profit": cond[1]}
                           if cond else {"enabled": False}),
        "first_day": {"enabled": False},
        "formula_sell": {"enabled": False},
    }

# ── 变体表 (2026-08-04 归因: 成本止损-226万 + 9-12天持仓-84万 → 两个止血方向) ──
# 注: cond_time_stop 语义是"N天后反弹到profit才走"(条件止盈), 不是"N天不涨就走",
#     不符合本次止血目标, 不纳入网格。
VARIANTS = {
    "BASE_dd1":      _stop(),                       # 基准
    "TIME7":         _stop(mhd=7),                  # 时间止损 12→7天
    "TIME8":         _stop(mhd=8),                  # 时间止损 12→8天
    "COST08":        _stop(cost=-0.08),             # 硬止损 -12→-8%
    "COST10":        _stop(cost=-0.10),             # 硬止损 -12→-10%
    "DD2":           _stop(dd=0.02),                # 回撤 1→2% (诚实语义重测)
    "TIME8_COST10":  _stop(mhd=8, cost=-0.10),      # 双止血组合(松)
    "TIME7_COST08":  _stop(mhd=7, cost=-0.08),      # 双止血组合(紧)
}


def main():
    start, end, tag = sys.argv[1], sys.argv[2], sys.argv[3]
    names = sys.argv[4:] or list(VARIANTS)
    sel_cfg = {"formula_name": "GUPIAO_018", "formula_arg": "",
               "universe": {"type": "50", "exclude_st": True},
               "period": "1d", "dividend_type": 1}
    t0 = time.time()
    selections = StockSelector(sel_cfg).run(start_time=start, end_time=end)
    n = 0 if selections is None else len(selections)
    print(f"[INFO] 信号={n} 股票={0 if not n else selections['stock_code'].nunique()} "
          f"取信号耗时={time.time()-t0:.0f}s period={BT_CFG['period']} confirm={CONFIRM}",
          flush=True)
    if not n:
        return
    outdir = "research/cmp_1d_5m/grid"
    os.makedirs(outdir, exist_ok=True)
    sel_path = f"{outdir}/sel_{tag}.parquet"
    if not os.path.exists(sel_path):
        selections.to_parquet(sel_path)

    rows = []
    for name in names:
        stop = VARIANTS[name]
        engine = BacktestEngine(BT_CFG)
        t0 = time.time()
        try:
            result = engine.run(selections=selections, start_time=start,
                                end_time=end, stop_config=stop)
        except Exception as e:
            print(f"[FAIL] {name}: {type(e).__name__} {str(e)[:100]}", flush=True)
            continue
        m = result.get("metrics") or {}
        rows.append({"variant": name, "range": f"{start}~{end}", **{
            k: m.get(k) for k in ("cumulative_return", "annualized_return",
                                  "max_drawdown", "sharpe_ratio", "total_trades",
                                  "win_rate", "profit_factor", "avg_hold_days")}})
        print(f"[DONE] {name:16s} cum={m.get('cumulative_return',0)*100:+.2f}% "
              f"ann={m.get('annualized_return',0)*100:+.2f}% "
              f"dd={m.get('max_drawdown',0)*100:.2f}% sharpe={m.get('sharpe_ratio',0):.2f} "
              f"trades={m.get('total_trades')} wr={m.get('win_rate',0)*100:.1f}% "
              f"pf={m.get('profit_factor',0):.2f} ({time.time()-t0:.0f}s)", flush=True)
        tr = result.get("trades")
        if hasattr(tr, "to_parquet"):
            tr.to_parquet(f"{outdir}/trades_{tag}_{name}.parquet")
    df = pd.DataFrame(rows)
    df.to_json(f"{outdir}/summary_{tag}.json", orient="records", force_ascii=False, indent=1)
    print("[ALL DONE]", flush=True)


if __name__ == "__main__":
    main()
