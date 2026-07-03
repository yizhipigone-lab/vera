"""测试首日未达标规则"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd, numpy as np
from core.connector import TdxConnector
from backtest.engine import _simulate_core_v3
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

# Test: WITHOUT first_day
ea1, rt1 = _simulate_core_v3(
    close.values.astype(np.float64), entries.values,
    1000000.0, 0.0003, 5000.0, 50000.0, 100, 1,
    True, -0.06, True, 0.05, 0.03,
    True, np.array([0.03], dtype=np.float64), np.array([0.30], dtype=np.float64), 1,
    True, 10, False, 7, 0.01,
    first_day_enabled=False, first_day_target=0.03, high_np=None,
)

# Test: WITH first_day (target=3%)
if high_df is not None:
    high_np = high_df.values.astype(np.float64)
else:
    high_np = None

ea2, rt2 = _simulate_core_v3(
    close.values.astype(np.float64), entries.values,
    1000000.0, 0.0003, 5000.0, 50000.0, 100, 1,
    True, -0.06, True, 0.05, 0.03,
    True, np.array([0.03], dtype=np.float64), np.array([0.30], dtype=np.float64), 1,
    True, 10, False, 7, 0.01,
    first_day_enabled=True, first_day_target=0.03, high_np=high_np,
)

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
