"""止盈止损优化 v2 — 聚焦多层阶梯 + 宽止损，目标 20%+"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.connector import TdxConnector
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
print(f"选股: {len(selections)} 条")

# ===== 核心搜索网格 — 聚焦多层阶梯 =====
LADDER_GRID = [
    ([{'profit': 0.03, 'sell_ratio': 0.50}, {'profit': 0.08, 'sell_ratio': 1.0}], "3%→50%,8%→100%"),
    ([{'profit': 0.03, 'sell_ratio': 0.30}, {'profit': 0.06, 'sell_ratio': 1.0}], "3%→30%,6%→100%"),
    ([{'profit': 0.02, 'sell_ratio': 0.40}, {'profit': 0.06, 'sell_ratio': 1.0}], "2%→40%,6%→100%"),
    ([{'profit': 0.02, 'sell_ratio': 0.33}, {'profit': 0.05, 'sell_ratio': 0.50}, {'profit': 0.10, 'sell_ratio': 1.0}], "2%→33%,5%→50%,10%→100%"),
    ([{'profit': 0.03, 'sell_ratio': 0.33}, {'profit': 0.06, 'sell_ratio': 0.50}, {'profit': 0.10, 'sell_ratio': 1.0}], "3%→33%,6%→50%,10%→100%"),
]

COST_GRID = [-0.08, -0.10]

TRAILING_GRID = [
    (0.05, 0.03, "5%/3%"),
    (0.08, 0.04, "8%/4%"),
]

TIME_GRID = [15, 20]

COND_TIME_GRID = [
    (7, 0.02, "7d/2%"),
]

results = []
total = len(LADDER_GRID) * len(COST_GRID) * len(TRAILING_GRID) * len(TIME_GRID) * len(COND_TIME_GRID)
print(f"总组合数: {total}")

engine = BacktestEngine(ENGINE_CONFIG)
count = 0
best_so_far = -999

for ladder_lv, ladder_label in LADDER_GRID:
    for cost_threshold in COST_GRID:
        for trail_act, trail_dd, trail_label in TRAILING_GRID:
            for time_days in TIME_GRID:
                for ct_days, ct_profit, ct_label in COND_TIME_GRID:
                    count += 1
                    label = f"L={ladder_label} C={cost_threshold:.0%} T={trail_label} Time={time_days}d CT={ct_label}"

                    stop_config = {
                        'cost_stop': {'enabled': True, 'threshold': cost_threshold},
                        'trailing_stop': {'enabled': True, 'activation': trail_act, 'drawdown': trail_dd},
                        'ladder_tp': {'enabled': True, 'levels': ladder_lv},
                        'time_stop': {'enabled': True, 'max_hold_days': time_days},
                        'cond_time_stop': {'enabled': True, 'days': ct_days, 'profit': ct_profit},
                    }

                    t0 = time.time()
                    result = engine.run(selections, start_time='20260101', end_time='20260528', stop_config=stop_config)
                    elapsed = time.time() - t0

                    m = result['metrics']
                    ret = m['cumulative_return']
                    annual = m['annualized_return']
                    dd = m['max_drawdown']
                    wr = m['win_rate']
                    n_trades = len(result['trades'])

                    results.append({
                        'cum_ret': ret, 'annual': annual, 'max_dd': dd, 'win_rate': wr,
                        'trades': n_trades, 'label': label, 'time': elapsed,
                    })

                    marker = " *** NEW BEST!" if ret > best_so_far else ""
                    if ret > best_so_far:
                        best_so_far = ret
                    print(f"[{count}/{total}] {ret*100:+.2f}% | {label} | {elapsed:.0f}s{marker}")

# 排序
results.sort(key=lambda x: x['cum_ret'], reverse=True)

print("\n" + "="*100)
print("ALL RESULTS (按累计收益排序)")
print("="*100)
for i, r in enumerate(results):
    print(f"{i+1:2d}. 累计{r['cum_ret']*100:+.2f}% 年化{r['annual']*100:+.1f}% 回撤{r['max_dd']*100:+.1f}% 胜率{r['win_rate']*100:.0f}% 交易{r['trades']} | {r['label']}")

with open('output/optimize_v2_results.json', 'w', encoding='utf-8') as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print("\n结果已保存")
