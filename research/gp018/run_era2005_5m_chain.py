"""GUPIAO_018 2005-2026 真5分钟回测 — 分段链式可续跑版。

每段完成立即落盘 (state.json + 段 equity/trades); 重启自动从断点继续。
用法: MAXBUY=100000 SUFFIX=mb10w python -X utf8 -u research/gp018/run_era2005_5m_chain.py
"""
import gc, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
from backtest.engine import BacktestEngine
from backtest.metrics import MetricsCalculator

STOP = {
    "priority": "stop_first",
    "cost_stop": {"enabled": True, "threshold": -0.12},
    "trailing_stop": {"enabled": True, "activation": 0.035, "drawdown": 0.01},
    "ladder_tp": {"enabled": True, "levels": [
        {"profit": 0.06, "sell_ratio": 0.30},
        {"profit": 0.15, "sell_ratio": 0.30},
    ]},
    "time_stop": {"enabled": True, "max_hold_days": 20},
    "cond_time_stop": {"enabled": False},
    "first_day": {"enabled": False},
    "formula_sell": {"enabled": False},
}

CHUNKS = [
    ("20050101", "20080101"), ("20080101", "20110101"), ("20110101", "20140101"),
    ("20140101", "20170101"), ("20170101", "20200101"), ("20200101", "20230101"),
    ("20230101", "20250101"), ("20250101", "20260801"),
]

MAXBUY = float(os.environ.get("MAXBUY", "20000"))
TAG = "era2005_5m_chain_" + os.environ.get("SUFFIX", "mb2w")
OUT = "research/gp018/results"
STATE = f"{OUT}/{TAG}_state.json"

BT = {
    "initial_capital": 1000000.0,
    "commission": 0.0003,
    "slippage": 0.001,
    "period": "5m",
    "degrade_5m": True,
    "position_sizing": {"max_positions": 999, "min_buy_amount": 2000.0,
                        "max_buy_amount": MAXBUY, "lot_size": 100, "min_lots": 1},
}

# 断点恢复
state = {"capital": BT["initial_capital"], "done": []}
if os.path.exists(STATE):
    state = json.load(open(STATE, encoding="utf-8"))
    print(f"[RESUME] 已完成段 {state['done']}, 资金 {state['capital']:,.0f}", flush=True)

sel_all = pd.read_parquet(f"{OUT}/server_gp018_2005_2026.parquet")
sel_all["select_date"] = pd.to_datetime(sel_all["select_date"])

capital = state["capital"]
for i, (s, e) in enumerate(CHUNKS):
    if i in state["done"]:
        continue
    ds = sel_all["select_date"].dt.strftime("%Y%m%d")
    sel = sel_all[(ds >= s) & (ds < e)]
    print(f"[INFO] 段{i} {s}~{e} 信号 {len(sel)} 初始资金 {capital:,.0f}", flush=True)
    if not len(sel):
        state["done"].append(i)
        continue
    bt = dict(BT, initial_capital=capital)
    t0 = time.time()
    result = BacktestEngine(bt).run(selections=sel, start_time=s, end_time=e, stop_config=STOP)
    eq = result.get("equity_curve")
    tr = result.get("trades")
    if hasattr(eq, "iloc") and len(eq):
        final_eq = float(eq["equity"].iloc[-1])
        m = result.get("metrics") or {}
        print(f"[DONE] 段{i} {s}~{e} elapsed={time.time()-t0:.0f}s 末权益 {final_eq:,.0f} "
              f"段累计 {m.get('cumulative_return',0)*100:.2f}% dd={m.get('max_drawdown',0)*100:.2f}% "
              f"trades={m.get('total_trades')}", flush=True)
        eq.to_parquet(f"{OUT}/{TAG}_eq_{i}.parquet")
        if hasattr(tr, "columns") and len(tr):
            tr.to_parquet(f"{OUT}/{TAG}_tr_{i}.parquet")
        capital = final_eq
        state["capital"] = capital
        state["done"].append(i)
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    del result
    gc.collect()

# 合并
eq_parts, tr_parts = [], []
for i in state["done"]:
    p = f"{OUT}/{TAG}_eq_{i}.parquet"
    if os.path.exists(p):
        eq_parts.append(pd.read_parquet(p))
    p2 = f"{OUT}/{TAG}_tr_{i}.parquet"
    if os.path.exists(p2):
        tr_parts.append(pd.read_parquet(p2))
if not eq_parts:
    print("[FAIL] 无已完成段", flush=True)
    sys.exit(1)
import glob as _g
for f in sorted(_g.glob(f"research/gp018/results/{TAG}_chunk*_equity.parquet")):
    _e = pd.read_parquet(f)
    if not any(_e["date"].equals(p["date"]) for p in eq_parts):
        eq_parts.append(_e)
for f in sorted(_g.glob(f"research/gp018/results/{TAG}_chunk*_trades.parquet")):
    tr_parts.append(pd.read_parquet(f))
eq_all = pd.concat(eq_parts).drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
tr_all = pd.concat(tr_parts, ignore_index=True) if tr_parts else pd.DataFrame()
tr_all = tr_all.drop_duplicates() if len(tr_all) else tr_all
m_all = MetricsCalculator().compute_all(eq_all, tr_all, initial_capital=BT["initial_capital"])
print(f"[FINAL] {TAG} 完成段数={len(state['done'])}/8 cum={m_all.get('cumulative_return',0)*100:.2f}% "
      f"ann={m_all.get('annualized_return',0)*100:.2f}% dd={m_all.get('max_drawdown',0)*100:.2f}% "
      f"sharpe={m_all.get('sharpe_ratio',0):.2f} trades={m_all.get('total_trades')} "
      f"wr={m_all.get('win_rate',0)*100:.1f}% pf={m_all.get('profit_factor',0):.2f}", flush=True)
with open(f"{OUT}/{TAG}.json", "w", encoding="utf-8") as f:
    json.dump({"chunks": CHUNKS, "stop": STOP, "maxbuy": MAXBUY,
               "done_chunks": state["done"], "metrics": m_all}, f, ensure_ascii=False, default=str, indent=1)
eq_all.to_parquet(f"{OUT}/{TAG}_equity.parquet")
if len(tr_all):
    tr_all.to_parquet(f"{OUT}/{TAG}_trades.parquet")
print("[SAVED]", flush=True)
