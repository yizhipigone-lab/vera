"""GP1014 归因实验 (2026-07-24): 不改任何生产代码, monkey-patch 运行时生效。

背景: 我们 (40.67%) vs x-tdxqmt (79%) 同配方 GP1014 差异归因。
三个实验共享同一份选股 (23:36 raw CSV 14525 条) + 同一矩阵缓存:
  A. baseline       — 现口径 (盘中 Low 触线 + 涨停过滤), 应复现 8145 笔 / 40.67%
  B. close-confirm  — 仅移动止盈改收盘确认 (peak/激活/回撤/成交全收盘价口径)
  C. no-limit-filter — 仅放开涨停过滤 (_filter_limit_up 恒等)

用法: python tools/attr_gp1014.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

import pandas as pd

from backtest.engine import BacktestEngine
from backtest.loop.strategies.base import TriggerResult
from backtest.loop.strategies.trailing import TrailingStrategy
from core.connector import TdxConnector

SEL_CSV = 'output/selections/回测_raw_20260724_233551.csv'
OUT_DIR = 'output/attr_gp1014'
START, END = '20240101', '20260630'

BT_CFG = {
    'initial_capital': 1000000.0,
    'commission': 0.0003,
    'slippage': 0.001,
    'stamp_tax': 0.0005,
    'enable_realistic_costs': True,
    'period': '1d',
    'degrade_5m': True,
    'position_sizing': {
        'max_positions': 999, 'min_buy_amount': 2000.0,
        'max_buy_amount': 20000.0, 'lot_size': 100, 'min_lots': 1,
    },
    'matrix_cache': True,
}

# 照 23:36 结果 stop_config_summary 复刻
STOP = {
    'priority': 'trailing_first',
    'cost_stop': {'enabled': True, 'threshold': -0.08},
    'trailing_stop': {'enabled': True, 'activation': 0.03, 'drawdown': 0.02},
    'ladder_tp': {'enabled': True, 'levels': [
        {'profit': 1.00, 'sell_ratio': 0.20},
        {'profit': 1.20, 'sell_ratio': 0.20},
    ]},
    'time_stop': {'enabled': True, 'max_hold_days': 12},
    'cond_time_stop': {'enabled': False},
    'first_day': {'enabled': False},
    'formula_sell': {'enabled': False},
}


def _close_confirm_check(self, pos, bar, ctx):
    """收盘确认版移动止盈: 峰值/激活/回撤判定/成交价全部收盘价口径。

    pos/ctx 的 pos_high_px = 持仓期最高收盘价 (loop.py:185-194 每 bar 用 close 更新,
    ctx 构造前已含本 bar)。与生产版差异仅: Low 触线 → Close 确认, 线价成交 → 收盘价成交。
    """
    ep = pos.entry_px
    peak_close = ctx.pos_high_px if ctx.pos_high_px > 0 else ep
    if ep <= 0 or (peak_close - ep) / ep < self.activation:
        return []
    trail_line = peak_close * (1.0 - self.drawdown)
    if bar.close <= trail_line:
        reason = 8 if (trail_line - ep) / ep > 0.0 else 4
        return [TriggerResult(reason=reason, strategy_name=self.name,
                              execution_price=bar.close)]
    return []


def _identity_filter_limit_up(self, entries, close):
    """Variant C: 涨停过滤恒等 (不做任何过滤)。"""
    return entries


def run_once(selections, label, patch=None, stop=None):
    unpatch = []
    if patch in ('close_confirm', 'both'):
        orig = TrailingStrategy.check
        TrailingStrategy.check = _close_confirm_check
        unpatch.append(lambda: setattr(TrailingStrategy, 'check', orig))
    if patch in ('no_limit_filter', 'both'):
        orig = BacktestEngine._filter_limit_up
        BacktestEngine._filter_limit_up = _identity_filter_limit_up
        unpatch.append(lambda: setattr(BacktestEngine, '_filter_limit_up', orig))
    try:
        eng = BacktestEngine(BT_CFG)
        t0 = time.perf_counter()
        r = eng.run(selections=selections, start_time=START, end_time=END,
                    stop_config=stop or STOP)
        dt = time.perf_counter() - t0
    finally:
        for u in unpatch:
            u()
    trades = r['trades']
    m = r['metrics']
    trades.to_csv(f'{OUT_DIR}/{label}_trades.csv', index=False, encoding='utf-8-sig')
    eq = r['equity_curve']
    eq.to_csv(f'{OUT_DIR}/{label}_equity.csv', index=False, encoding='utf-8-sig')
    reasons = trades['exit_reason'].value_counts().to_dict() if len(trades) else {}
    print(f'[{label}] {dt:.1f}s trades={m["total_trades"]} '
          f'cum={m["cumulative_return"]:.4f} win={m["win_rate"]:.4f} '
          f'maxdd={m["max_drawdown"]:.4f}')
    print(f'  reasons={reasons}')
    if len(trades):
        ret = trades['return']
        print(f'  ret: mean={ret.mean():.4f} median={ret.median():.4f} '
              f'p90={ret.quantile(0.9):.4f} max={ret.max():.4f} '
              f'hold_med={trades["hold_days"].median():.0f}d')
    return m


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    sel = pd.read_csv(SEL_CSV, encoding='utf-8-sig')
    print(f'selections: {len(sel)} 条 (GP1014 {START}~{END})')
    TdxConnector.initialize()
    try:
        if len(sys.argv) > 1 and sys.argv[1] == 'sweep':
            # 回撤扫描: dd ∈ {2,3,4,5,6,8}%, 其余全基线
            print(f'{"dd":>6}{"trades":>8}{"cum":>10}{"win":>8}{"maxdd":>9}'
                  f'{"ret_med":>9}{"ret_p90":>9}{"hold":>5}')
            for dd in (0.02, 0.03, 0.04, 0.05, 0.06, 0.08):
                stop_x = {**STOP, 'trailing_stop': {
                    **STOP['trailing_stop'], 'drawdown': dd}}
                eng = BacktestEngine(BT_CFG)
                r = eng.run(selections=sel, start_time=START, end_time=END,
                            stop_config=stop_x)
                m, t = r['metrics'], r['trades']
                print(f'{dd:>6.0%}{m["total_trades"]:>8}'
                      f'{m["cumulative_return"]:>10.4f}{m["win_rate"]:>8.4f}'
                      f'{m["max_drawdown"]:>9.4f}'
                      f'{t["return"].median():>9.4f}'
                      f'{t["return"].quantile(0.9):>9.4f}'
                      f'{t["hold_days"].median():>5.0f}')
            return
        if len(sys.argv) > 1 and sys.argv[1] == 'trail6':
            # 回撤放宽实验: 仅 trailing drawdown 2%→6%, 其余全基线
            stop6 = {**STOP, 'trailing_stop': {
                **STOP['trailing_stop'], 'drawdown': 0.06}}
            m_a = run_once(sel, 'A_baseline')
            m_e = run_once(sel, 'E_trail6pct', stop=stop6)
            print('\n==== 回撤放宽对比 ====')
            print(f'{"":<18}{"trades":>8}{"cum":>10}{"win":>8}')
            for lab, m in [('A dd=2% (基线)', m_a), ('E dd=6%', m_e)]:
                print(f'{lab:<18}{m["total_trades"]:>8}'
                      f'{m["cumulative_return"]:>10.4f}{m["win_rate"]:>8.4f}')
            return
        m_a = run_once(sel, 'A_baseline')
        m_b = run_once(sel, 'B_close_confirm', patch='close_confirm')
        m_c = run_once(sel, 'C_no_limit_filter', patch='no_limit_filter')
        m_d = run_once(sel, 'D_both', patch='both')
    finally:
        TdxConnector.close()
    print('\n==== 归因对比 ====')
    print(f'{"":<18}{"trades":>8}{"cum":>10}{"win":>8}')
    for lab, m in [('A baseline', m_a), ('B close确认', m_b),
                   ('C 放开涨停', m_c), ('D B+C 组合', m_d)]:
        print(f'{lab:<18}{m["total_trades"]:>8}{m["cumulative_return"]:>10.4f}'
              f'{m["win_rate"]:>8.4f}')
    print('\n对照: x-tdxqmt 79% (9710 笔), 我们 23:36 线上结果 40.67% (8145 笔)')
    print(f'B-A (移动止盈语义贡献): {m_b["cumulative_return"] - m_a["cumulative_return"]:+.4f}')
    print(f'C-A (涨停过滤贡献):     {m_c["cumulative_return"] - m_a["cumulative_return"]:+.4f}')
    print(f'D-A (组合):             {m_d["cumulative_return"] - m_a["cumulative_return"]:+.4f}')
    inter = (m_d["cumulative_return"] - m_a["cumulative_return"]) \
        - (m_b["cumulative_return"] - m_a["cumulative_return"]) \
        - (m_c["cumulative_return"] - m_a["cumulative_return"])
    print(f'交互效应 (D-A)-(B-A)-(C-A): {inter:+.4f}')


if __name__ == '__main__':
    main()
