"""QUANTQQ 回测 — 2026.1.1 至今 (调 TDX 跑 QUANTQQ 公式选股, 不是 Python 海龟)

2026-07-06: 用户纠正 — QUANTQQ 是 TDX 公式名, 不是 Python 海龟突破.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
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

logger.info('=' * 100)
logger.info(f'  VERA QUANTQQ 回测 — {START[:4]}.{START[4:6]}.{START[6:]} ~ {END[:4]}.{END[4:6]}.{END[6:]}')
logger.info(f'  Engine v3.5 (trailing_first + 新 trailing 语义, trailing 3.5%/1%)')
logger.info('=' * 100)

# 1. 加载配置
STOP_CONFIG = load_stop_config()
priority = STOP_CONFIG.get('priority', 'stop_first')
logger.info(f'\n[配置] priority={priority}')
logger.info(f'       trailing_stop: {STOP_CONFIG.get("trailing_stop")}')
logger.info(f'       ladder_tp: {STOP_CONFIG.get("ladder_tp", {}).get("levels")}')
logger.info(f'       cost_stop: {STOP_CONFIG.get("cost_stop")}')

# 2. 调 TDX 跑 QUANTQQ 选股 (不自己算海龟)
logger.info(f'\n[1/4] 调 TDX 跑 {FORMULA} 选股...')
sel_df = FormulaRunner.run_stock_selection_with_dates(
    formula_name=FORMULA, formula_arg='',
    stock_list=None,  # None = 用 TDX 默认股票池
    start_time=START, end_time=END,
    stock_period='1d', dividend_type=1,
)
logger.info(f'      信号: {len(sel_df):,} 笔, 股票: {sel_df["stock_code"].nunique():,} 只')

# 3. 回测
logger.info(f'\n[2/4] 运行回测...')

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

engine = BacktestEngine(ENGINE_CFG)
result = engine.run(
    selections=sel_df,
    start_time=START,
    end_time=END,
    stop_config=STOP_CONFIG,
)

# 4. 输出报告
trades_df = result['trades']
equity_curve = result['equity_curve']
metrics = result['metrics']

logger.info('\n' + '=' * 100)
logger.info('  报告')
logger.info('=' * 100)
logger.info(f'\n【核心指标】')
logger.info(f'  区间: {START[:4]}.{START[4:6]}.{START[6:]} ~ {END[:4]}.{END[4:6]}.{END[6:]}')
logger.info(f'  公式: {FORMULA} (TDX)')
logger.info(f'  信号: {len(sel_df):,} 笔 (覆盖 {sel_df["stock_code"].nunique():,} 只股票)')
logger.info(f'  交易: {len(trades_df)} 笔')
logger.info(f'  累计收益: {metrics.get("cumulative_return", 0)*100:.2f}%')
logger.info(f'  年化收益: {metrics.get("annualized_return", 0)*100:.2f}%')
logger.info(f'  最大回撤: {metrics.get("max_drawdown", 0)*100:.2f}%')
logger.info(f'  夏普比率: {metrics.get("sharpe_ratio", 0):.2f}')
logger.info(f'  胜率: {metrics.get("win_rate", 0)*100:.2f}%')
logger.info(f'  盈亏比: {metrics.get("profit_loss_ratio", 0):.2f}')

# 退出原因统计
if not trades_df.empty and 'exit_reason' in trades_df.columns:
    logger.info(f'\n【退出原因分布】')
    for reason, cnt in trades_df['exit_reason'].value_counts().head(10).items():
        pct = cnt / len(trades_df) * 100
        logger.info(f'  {reason:20s}  {cnt:4d}  ({pct:.1f}%)')

# 月度收益
if not equity_curve.empty:
    logger.info(f'\n【月度收益】')
    eq = equity_curve.copy()
    eq['date'] = pd.to_datetime(eq['date'])
    eq = eq.set_index('date')
    monthly_eq = eq['equity'].resample('ME').last()
    monthly_ret = monthly_eq.pct_change().fillna(0)
    for dt, ret in monthly_ret.items():
        bar = '█' * max(0, int(ret * 50)) if ret > 0 else '▓' * max(0, int(-ret * 50))
        sign = '+' if ret >= 0 else ' '
        logger.info(f'  {dt.strftime("%Y-%m")}  {sign}{ret*100:6.2f}%  {bar}')

# TOP20 信号股票
logger.info(f'\n【TOP20 信号股票 (按信号次数)】')
top_codes = sel_df['stock_code'].value_counts().head(20)
for i, (code, cnt) in enumerate(top_codes.items(), 1):
    logger.info(f'  {i:2d}. {code}  {cnt} 次')

logger.info('\n' + '=' * 100)
logger.info(f'  完成 — QUANTQQ 真实信号 (TDX 公式) + 新引擎能力')
logger.info('=' * 100)

# 5. 保存结果
out_dir = 'output/results'
os.makedirs(out_dir, exist_ok=True)
ts = datetime.now().strftime('%Y%m%d_%H%M%S')
result_path = f'{out_dir}/quantqq_2026_{priority}_{ts}.json'

# trades_df 转 list-of-dict 保存 (避免 pandas repr 序列化)
trades_records = trades_df.to_dict('records') if not trades_df.empty else []

with open(result_path, 'w', encoding='utf-8') as f:
    json.dump({
        'meta': {
            'formula': FORMULA,
            'engine_version': 'signal-day-close',
            'entry_price_basis': 'close_on_signal_day',
            'trailing_semantics': 'intraday_low_trail_line',
            'priority': priority,
            'period': f'{START}~{END}',
            'signal_count': len(sel_df),
            'trade_count': len(trades_df),
            'cumulative_return': metrics.get('cumulative_return', 0),
            'engine_semantics': 'v3.5-trailing-first-20260706',
            'note': '修复: QUANTQQ 用 TDX 公式选股 (不是 Python 海龟)',
        },
        'data': {
            **result,
            'trades': trades_records,  # 替换为 list-of-dict
        },
    }, f, ensure_ascii=False, default=str)
logger.info(f'\n结果已保存: {result_path}')