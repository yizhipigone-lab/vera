"""测试首日未达标规则"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd, numpy as np
from core.connector import TdxConnector
from backtest.engine import BacktestEngine
from core.data_fetcher import DataFetcher
from utils.logger import get_logger
logger = get_logger(__name__)

TdxConnector.initialize()
from tqcenter import tq

# 用UPN 2024Q1获取信号
codes = [s['Code'] for s in tq.get_stock_list('50', list_type=1)[:300] if isinstance(s, dict)]
codes = [c for c in codes if not ('ST' in c or c.startswith('688'))][:200]

# Get close prices
close = DataFetcher.get_close_price(codes, "20240301", "20240328", dividend_type="front")
close = close.ffill().bfill()

# Get high prices
kline = DataFetcher.get_kline(codes, "20240301", "20240328", dividend_type="front")
high_df = kline["High"] if kline and "High" in kline else None
if high_df is not None:
    high_df = high_df.ffill().bfill()

# Get UPN signals
result = tq.formula_process_mul_xg(
    formula_name='UPN', formula_arg='3',
    return_count=0, return_date=True,
    stock_list=codes, stock_period='1d',
    start_time='20240301', end_time='20240328',
    count=0, dividend_type=1,
)

# Build entries
entries = pd.DataFrame(False, index=close.index, columns=close.columns)
for code, val in result.items():
    if code == 'ErrorId' or not val or not isinstance(val, dict):
        continue
    if code not in entries.columns:
        continue
    for entry_list in val.values():
        if not isinstance(entry_list, list):
            continue
        for entry in entry_list:
            if not isinstance(entry, dict):
                continue
            if str(entry.get('Value', '')) != '1':
                continue
            d = str(entry.get('Date', ''))
            if '20240301' <= d <= '20240328':
                try:
                    dt = pd.to_datetime(d, format='%Y%m%d')
                    if dt in entries.index:
                        entries.loc[dt, code] = True
                except Exception as e:
                    logger.warning("Date parse failed: %s", e)

# Align
common = sorted(set(close.columns) & set(entries.columns))
close = close[common]
entries = entries[common]
if high_df is not None:
    high_df = high_df.reindex(index=close.index, columns=common).ffill().bfill()

print(f"Data: {close.shape}, entries: {entries.sum().sum()} signals")

# 候选 A 阶段 1.5: 收编到 run_cached (锁 _simulate_core_v3 私有; filter_limit_up=False 复现直调口径)
_eng = BacktestEngine({
    "initial_capital": 1_000_000.0,
    "commission": 0.0003,
    "enable_realistic_costs": False,
    "period": "1d",
    "position_sizing": {"min_buy_amount": 5000.0, "max_buy_amount": 50000.0, "lot_size": 100, "min_lots": 1},
})
_sc = {
    "priority": "stop_first",
    "cost_stop": {"enabled": True, "threshold": -0.06},
    "trailing_stop": {"enabled": True, "activation": 0.05, "drawdown": 0.03},
    "ladder_tp": {"enabled": True, "levels": [{"profit": 0.03, "sell_ratio": 0.30}]},
    "time_stop": {"enabled": True, "max_hold_days": 10},
    "cond_time_stop": {"enabled": False, "days": 7, "profit": 0.01},
    "first_day": {"enabled": False, "target": 0.03},
}
_lp = np.array([0.03], dtype=np.float64)
_lr = np.array([0.30], dtype=np.float64)

# Test: WITHOUT first_day
_res1 = _eng.run_cached(close, entries, None, None, _sc, None, _lp, _lr, 1,
                       skip_sm=True, filter_limit_up=False, return_raw=True)
ea1, rt1 = _res1["raw_equity"], _res1["raw_trades"]

# Test: WITH first_day (target=3%)
high_np = high_df.values.astype(np.float64) if high_df is not None else None
_sc["first_day"]["enabled"] = True
_res2 = _eng.run_cached(close, entries, high_np, None, _sc, None, _lp, _lr, 1,
                       skip_sm=True, filter_limit_up=False, return_raw=True)
ea2, rt2 = _res2["raw_equity"], _res2["raw_trades"]

# Analyze
def analyze(rt, label):
    reasons = {}
    day1_count = 0
    for row in rt:
        r = int(row[8])
        reasons[r] = reasons.get(r, 0) + 1
        if r == 10:
            day1_count += 1
    cr = (ea2[-1] - 1000000) / 1000000 if label == 'WITH' else (ea1[-1] - 1000000) / 1000000
    print(f"\n[{label}] Trades={len(rt)} Return={cr:.2%}")
    print(f"  Reasons: {reasons}")
    # Show sample day-1 trades
    if day1_count > 0:
        samples = [row for row in rt if int(row[8]) == 10][:5]
        for row in samples:
            ci, ei, xi = int(row[0]), int(row[1]), int(row[2])
            code = common[ci] if ci < len(common) else '?'
            ep, xp = row[3], row[4]
            pp = row[7]
            print(f"  {code}: entry_bar={ei} exit_bar={xi} ep={ep:.2f} xp={xp:.2f} ret={pp*100:.2f}%")

analyze(rt1, "WITHOUT first_day")
analyze(rt2, "WITH first_day (3%)")

TdxConnector.close()
