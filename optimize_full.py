"""全量止盈止损优化 v3 — 预取数据，极速批量回测"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from core.formula_runner import FormulaRunner
from backtest.engine import BacktestEngine

TdxConnector.ensure_connected()

ENGINE_CONFIG = {
    'initial_capital': 200000.0, 'commission': 0.0003, 'slippage': 0.001, 'period': '5m',
    'position_sizing': {'min_buy_amount': 2000.0, 'max_buy_amount': 10000.0, 'lot_size': 100, 'min_lots': 1},
}

print("选股中...")
selections = FormulaRunner.run_stock_selection_with_dates(
    formula_name='QUANTQQ', formula_arg='', start_time='20260101', end_time='20260528', stock_period='1d',
)
print(f"选股: {len(selections)} 条", flush=True)
codes = selections["stock_code"].unique().tolist()

# ===== 预取K线数据（只做一次）=====
print("预取K线数据...")
t0 = time.time()
kline = DataFetcher.get_kline(codes, '20260101', '20260528', dividend_type="front", period="5m")
close = kline["Close"].sort_index()
high_df = kline["High"].sort_index() if "High" in kline else None
low_df = kline["Low"].sort_index() if "Low" in kline else None

# 构建entry signals
engine = BacktestEngine(ENGINE_CONFIG)
entries = engine._build_entry_signals(selections, close)

# 对齐
cols = sorted(close.columns.intersection(entries.columns))
close = close[cols].ffill().bfill()
entries = entries.reindex(index=close.index, columns=cols, fill_value=False)

high_np_full = None
low_np_full = None
if high_df is not None:
    ch_cols = sorted(set(cols) & set(high_df.columns))
    high_df = high_df.reindex(index=close.index, columns=ch_cols).ffill().bfill()
    fcols = sorted(set(cols) & set(high_df.columns))
    close = close[fcols]
    entries = entries[fcols]
    high_np_full = high_df[fcols].values.astype(np.float64)
    cols = fcols
if low_df is not None:
    cl_cols = sorted(set(cols) & set(low_df.columns))
    low_df = low_df.reindex(index=close.index, columns=cl_cols).ffill().bfill()
    fcols2 = sorted(set(cols) & set(low_df.columns))
    close = close[fcols2]
    entries = entries[fcols2]
    if high_np_full is not None:
        high_np_full = high_df[fcols2].values.astype(np.float64)
    low_np_full = low_df[fcols2].values.astype(np.float64)

print(f"数据准备完成: {close.shape} | 耗时 {time.time()-t0:.0f}s")

# ===== 网格搜索 =====
LADDER_GRID = [
    ([{'profit': 0.03, 'sell_ratio': 1.0}], "3%→100%"),
    ([{'profit': 0.04, 'sell_ratio': 1.0}], "4%→100%"),
    ([{'profit': 0.05, 'sell_ratio': 1.0}], "5%→100%"),
    ([{'profit': 0.06, 'sell_ratio': 1.0}], "6%→100%"),
    ([{'profit': 0.01, 'sell_ratio': 0.30}, {'profit': 0.03, 'sell_ratio': 1.0}], "1%→30%,3%→100%"),
    ([{'profit': 0.01, 'sell_ratio': 0.30}, {'profit': 0.05, 'sell_ratio': 1.0}], "1%→30%,5%→100%"),
    ([{'profit': 0.02, 'sell_ratio': 0.30}, {'profit': 0.05, 'sell_ratio': 1.0}], "2%→30%,5%→100%"),
    ([{'profit': 0.02, 'sell_ratio': 0.50}, {'profit': 0.06, 'sell_ratio': 1.0}], "2%→50%,6%→100%"),
    ([{'profit': 0.02, 'sell_ratio': 0.40}, {'profit': 0.06, 'sell_ratio': 1.0}], "2%→40%,6%→100%"),
    ([{'profit': 0.03, 'sell_ratio': 0.30}, {'profit': 0.06, 'sell_ratio': 1.0}], "3%→30%,6%→100%"),
    ([{'profit': 0.03, 'sell_ratio': 0.50}, {'profit': 0.08, 'sell_ratio': 1.0}], "3%→50%,8%→100%"),
    ([{'profit': 0.02, 'sell_ratio': 0.30}, {'profit': 0.05, 'sell_ratio': 0.50}, {'profit': 0.08, 'sell_ratio': 1.0}], "2%→30%,5%→50%,8%→100%"),
    ([{'profit': 0.02, 'sell_ratio': 0.33}, {'profit': 0.05, 'sell_ratio': 0.50}, {'profit': 0.10, 'sell_ratio': 1.0}], "2%→33%,5%→50%,10%→100%"),
    ([{'profit': 0.03, 'sell_ratio': 0.33}, {'profit': 0.06, 'sell_ratio': 0.50}, {'profit': 0.10, 'sell_ratio': 1.0}], "3%→33%,6%→50%,10%→100%"),
]

COST_GRID = [-0.08, -0.10, -0.12]

TRAILING_GRID = [
    (0.03, 0.02, "3%/2%"),
    (0.05, 0.03, "5%/3%"),
    (0.08, 0.04, "8%/4%"),
]

TIME_GRID = [15, 20]

COND_TIME_GRID = [
    (7, 0.02, "7d/2%"),
]

results = []
total = len(LADDER_GRID) * len(COST_GRID) * len(TRAILING_GRID) * len(TIME_GRID) * len(COND_TIME_GRID)
print(f"总组合数: {total} | 预取已完成，预计 {total*0.2:.0f} 秒")

count = 0
best_so_far = -999
start_all = time.time()

for ladder_lv, ladder_label in LADDER_GRID:
    for cost_threshold in COST_GRID:
        for trail_act, trail_dd, trail_label in TRAILING_GRID:
            for time_days in TIME_GRID:
                for ct_days, ct_profit, ct_label in COND_TIME_GRID:
                    count += 1
                    label = f"L={ladder_label} C={cost_threshold:.0%} T={trail_label} Time={time_days}d CT={ct_label}"

                    # 准备阶梯数组
                    lv = sorted(ladder_lv, key=lambda x: x.get("profit", 0))
                    ladder_profits = np.array([lv[i]["profit"] for i in range(len(lv))], dtype=np.float64)
                    ladder_ratios = np.array([lv[i]["sell_ratio"] for i in range(len(lv))], dtype=np.float64)

                    stop_config = {
                        'cost_stop': {'enabled': True, 'threshold': cost_threshold},
                        'trailing_stop': {'enabled': True, 'activation': trail_act, 'drawdown': trail_dd},
                        'ladder_tp': {'enabled': True, 'levels': lv},
                        'time_stop': {'enabled': True, 'max_hold_days': time_days},
                        'cond_time_stop': {'enabled': True, 'days': ct_days, 'profit': ct_profit},
                    }

                    t0 = time.time()
                    result = engine.run_cached(
                        close, entries, high_np_full, low_np_full,
                        stop_config, selections, ladder_profits, ladder_ratios, len(lv),
                        skip_sm=True,
                    )
                    elapsed = time.time() - t0

                    ret = result['cumulative_return']
                    m = result['metrics']

                    results.append({
                        'cum_ret': ret, 'annual': m['annualized_return'], 'max_dd': m['max_drawdown'],
                        'win_rate': m['win_rate'], 'trades': len(result['trades']),
                        'label': label, 'ladder': ladder_label, 'cost': cost_threshold,
                        'trailing': trail_label, 'time_days': time_days, 'cond_time': ct_label,
                    })

                    marker = "  *** NEW BEST!" if ret > best_so_far else ""
                    if ret > best_so_far:
                        best_so_far = ret

                    if count % 10 == 0:
                        elapsed_total = time.time() - start_all
                        eta = (elapsed_total / count) * (total - count)
                        print(f"[{count}/{total}] best={best_so_far*100:+.2f}% | {ret*100:+.2f}% | {label} | ETA {eta:.0f}s{marker}")

# 排序
results.sort(key=lambda x: x['cum_ret'], reverse=True)

print("\n" + "="*130)
print(f"TOP 20 (共{total}组合)")
print("="*130)
for i, r in enumerate(results[:20]):
    print(f"{i+1:2d}. {r['cum_ret']*100:+.2f}% 年{r['annual']*100:+.1f}% 回{r['max_dd']*100:+.1f}% 胜{r['win_rate']*100:.0f}% {r['trades']}笔 | {r['label']}")

with open('output/optimize_full_results.json', 'w', encoding='utf-8') as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

print(f"\n总耗时: {(time.time()-start_all):.0f}秒")
