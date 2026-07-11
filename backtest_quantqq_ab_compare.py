"""QUANTQQ 回测 A/B 对比: stop_first (旧 trailing 语义) vs trailing_first (新 trailing 语义)

同区间同信号, 只改 priority 配置.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
import numpy as np
import json
import logging
from datetime import datetime
from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from core.formula_runner import FormulaRunner
from backtest.engine import BacktestEngine
from backtest.stop_config import load_stop_config

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

TdxConnector.ensure_connected()
START = '20260101'
END   = datetime.now().strftime('%Y%m%d')
FORMULA = 'QUANTQQ'

logger.info('=' * 80)
logger.info(f'  QUANTQQ A/B 对比 — {START[:4]}.{START[4:6]}.{START[6:]} ~ {END[:4]}.{END[4:6]}.{END[6:]}')
logger.info('=' * 80)

# 调 TDX 跑 QUANTQQ 选股 (只跑一次, 两组配置共用)
logger.info(f'\n调 TDX 跑 {FORMULA} 选股...')
sel_df = FormulaRunner.run_stock_selection_with_dates(
    formula_name=FORMULA, formula_arg='',
    stock_list=None,
    start_time=START, end_time=END,
    stock_period='1d', dividend_type=1,
)
logger.info(f'信号: {len(sel_df):,}')

ENGINE_CFG = {
    'initial_capital': 1000000.0,
    'commission': 0.0003,
    'slippage': 0.001,
    'period': '1d',
    'position_sizing': {
        'min_buy_amount': 2000.0,
        'max_buy_amount': 20000.0,
        'lot_size': 100,
        'min_lots': 1,
    },
}

# 2. 加载基础配置, 改 priority 跑两组
base_cfg = load_stop_config()

def run_with_priority(priority, trailing_activation=0.035, trailing_drawdown=0.01):
    """A/B 对比: priority 控制顺序, trailing_activation/drawdown 控制 trailing 参数
    priority=trailing_first: 用新 trailing 语义 (Low 触及回撤线, 回撤线价执行)
    priority=stop_first: 用旧 trailing 语义 (Close 回撤, Close 执行) — 但参数仍是新参数
    """
    cfg = {**base_cfg, 'priority': priority}
    cfg['trailing_stop'] = {**cfg['trailing_stop'], 'activation': trailing_activation, 'drawdown': trailing_drawdown}
    engine = BacktestEngine(ENGINE_CFG)
    return engine.run(
        selections=sel_df,
        start_time=START,
        end_time=END,
        stop_config=cfg,
    )

# A: 旧 trailing 参数 (8%/5%) + stop_first 优先级 (原 v3.3 默认)
# B: 新 trailing 参数 (3.5%/1%) + trailing_first 优先级 (本次新默认)
logger.info("\n[A] 旧 trailing 参数 (8%/5%) + stop_first...")
result_a = run_with_priority('stop_first', 0.08, 0.05)

logger.info("\n[B] 新 trailing 参数 (3.5%/1%) + trailing_first...")
result_b = run_with_priority('trailing_first', 0.035, 0.01)

# 3. 对比报告
def metrics(result):
    m = result['metrics']
    trades = result['trades']
    return {
        'cum_ret': m.get('cumulative_return', 0) * 100,
        'ann_ret': m.get('annualized_return', 0) * 100,
        'max_dd': m.get('max_drawdown', 0) * 100,
        'sharpe': m.get('sharpe_ratio', 0),
        'win_rate': m.get('win_rate', 0) * 100,
        'plr': m.get('profit_loss_ratio', 0),
        'n_trades': len(trades),
    }

a = metrics(result_a)
b = metrics(result_b)

logger.info("\n" + "=" * 100)
logger.info(f"  A/B 对比报告 (2026.1.1 ~ {END[:4]}.{END[4:6]}.{END[6:]}, 区间大跌行情)")
logger.info("=" * 100)

logger.info(f"\n{'指标':<14}  {'A: stop_first':>18}  {'B: trailing_first':>20}  {'差异(B-A)':>14}")
logger.info("-" * 80)
logger.info(f"{'累计收益%':<14}  {a['cum_ret']:>17.2f}%  {b['cum_ret']:>19.2f}%  {b['cum_ret']-a['cum_ret']:>+13.2f}%")
logger.info(f"{'年化收益%':<14}  {a['ann_ret']:>17.2f}%  {b['ann_ret']:>19.2f}%  {b['ann_ret']-a['ann_ret']:>+13.2f}%")
logger.info(f"{'最大回撤%':<14}  {a['max_dd']:>17.2f}%  {b['max_dd']:>19.2f}%  {b['max_dd']-a['max_dd']:>+13.2f}%")
logger.info(f"{'夏普比率':<14}  {a['sharpe']:>18.2f}  {b['sharpe']:>20.2f}  {b['sharpe']-a['sharpe']:>+14.2f}")
logger.info(f"{'胜率%':<14}  {a['win_rate']:>17.2f}%  {b['win_rate']:>19.2f}%  {b['win_rate']-a['win_rate']:>+13.2f}%")
logger.info(f"{'盈亏比':<14}  {a['plr']:>18.2f}  {b['plr']:>20.2f}  {b['plr']-a['plr']:>+14.2f}")
logger.info(f"{'交易笔数':<14}  {a['n_trades']:>18d}  {b['n_trades']:>20d}  {b['n_trades']-a['n_trades']:>+14d}")

# 退出原因对比
def reason_dist(result, label):
    trades = result['trades']
    if trades.empty or 'exit_reason' not in trades.columns:
        logger.info(f"\n[{label}] 退出原因: 无数据")
        return
    logger.info(f"\n[{label}] 退出原因分布:")
    rc = trades['exit_reason'].value_counts()
    for reason, cnt in rc.items():
        pct = cnt / len(trades) * 100
        logger.info(f"  {reason:30s}  {cnt:4d}  ({pct:.1f}%)")

reason_dist(result_a, "A: stop_first")
reason_dist(result_b, "B: trailing_first")

logger.info("\n" + "=" * 100)
logger.info("  结论: 本次区间 (2026上半年, 大跌) trailing_first 的优势可能不明显")
logger.info("        大涨行情才更能体现 trailing 锁利优势 — 建议同时跑 2024-2025 大涨行情对比")
logger.info("=" * 100)

# 保存结果
out_dir = 'output/results/ab_compare'
os.makedirs(out_dir, exist_ok=True)
ts = datetime.now().strftime('%Y%m%d_%H%M%S')
out_path = f'{out_dir}/quantqq_ab_compare_{ts}.json'
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump({
        'meta': {'period': f'{START}~{END}', 'engine_semantics': 'v3.5-trailing-first-20260706'},
        'A_stop_first': result_a,
        'B_trailing_first': result_b,
        'A_metrics': a,
        'B_metrics': b,
    }, f, ensure_ascii=False, default=str)
logger.info(f"\n对比结果已保存: {out_path}")