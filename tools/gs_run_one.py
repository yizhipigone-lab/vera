"""
gs_txt 单公式回测子脚本 (被 run_gs_txt_batch_v2.py 用 subprocess 调用)

充分调用系统能力:
    - 选股: StockSelector (系统封装, 含 ST/退市过滤)
    - 回测: BacktestEngine.run (系统完整版, 含 K线获取/涨停过滤/退市处理/指标计算)
    - 配置: 读 config/default.yaml (不硬编码)

用法:
    python _gs_run_one.py <formula_name> <start> <end>

输出 (stdout 最后一行 JSON):
    {"status":"ok","signals":N,"stocks":N,"trades":N,"cumret":..,"annret":..,...}
    {"status":"no_signals"}
    {"status":"too_many_signals","signals":N}
    {"status":"error","msg":"..."}
"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.config_loader import ConfigLoader
from selection.selector import StockSelector
from backtest.engine import BacktestEngine
from backtest.stop_config import load_stop_config


def main():
    if len(sys.argv) < 4:
        print(json.dumps({'status': 'error', 'msg': 'usage: formula start end'}))
        return
    formula = sys.argv[1]
    start = sys.argv[2]
    end = sys.argv[3]

    # 读系统默认配置 (default.yaml)
    defaults = ConfigLoader.load_defaults()
    bt_cfg = defaults.get('backtest', {})
    sel_cfg_template = defaults.get('selection', {})

    # 构造选股配置: 用系统的 universe/period/dividend_type, 只换 formula_name
    sel_cfg = {
        'formula_name': formula,
        'formula_arg': '',  # 先生定: formula_arg 空
        'universe': sel_cfg_template.get('universe', {'type': '50', 'exclude_st': True}),
        'period': sel_cfg_template.get('period', '1d'),
        'dividend_type': sel_cfg_template.get('dividend_type', 1),
    }
    stop_config = load_stop_config()
    MAX_SIGNALS = 50000  # 先生定: 信号上限 5 万

    try:
        # 1. 选股 (系统 StockSelector)
        selector = StockSelector(sel_cfg)
        selections = selector.run(start_time=start, end_time=end)

        if selections is None or len(selections) == 0:
            print(json.dumps({'status': 'no_signals'}))
            return

        n_signals = len(selections)
        if n_signals > MAX_SIGNALS:
            print(json.dumps({'status': 'too_many_signals', 'signals': n_signals}))
            return

        # 2. 回测 (系统 BacktestEngine.run — 内部自己取 K 线/涨停过滤/退市处理)
        engine = BacktestEngine(bt_cfg)
        result = engine.run(
            selections=selections,
            start_time=start,
            end_time=end,
            stop_config=stop_config,
        )

        if 'metrics' not in result or not result['metrics']:
            print(json.dumps({'status': 'error', 'msg': 'no metrics'}))
            return

        m = result['metrics']
        trades = result.get('trades', [])
        n_trades = len(trades) if hasattr(trades, '__len__') else 0

        # 阶梯止盈计数 (先生关心的 6%/15% 档)
        ladder_6 = 0
        ladder_15 = 0
        if n_trades > 0 and hasattr(trades, 'iterrows'):
            for _, t in trades.iterrows():
                p = t.get('profit_pct', 0) if hasattr(t, 'get') else 0
                if abs(p - 0.06) < 0.005:
                    ladder_6 += 1
                elif abs(p - 0.15) < 0.01:
                    ladder_15 += 1

        print(json.dumps({
            'status': 'ok',
            'signals': n_signals,
            'stocks': result.get('stock_count', 0),
            'trades': n_trades,
            'cumret': m.get('cumulative_return', 0),
            'annret': m.get('annualized_return', 0),
            'maxdd': m.get('max_drawdown', 0),
            'sharpe': m.get('sharpe_ratio', 0),
            'winrate': m.get('win_rate', 0),
            'ladder6': ladder_6,
            'ladder15': ladder_15,
        }))
    except Exception as e:
        print(json.dumps({'status': 'error', 'msg': f'{type(e).__name__}: {str(e)[:120]}'}))


if __name__ == '__main__':
    main()
