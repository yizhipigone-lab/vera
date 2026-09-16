# -*- coding: utf-8 -*-
"""样本外验证(矩阵切分版) — 无需 TDX/重新选股。

原理: 全段矩阵 cache (close/entries/high/low/open/tradable) 已落盘,
按日期切出训练段/验证段, 重算 tradable 与 last_tradable_idx, 直接跑 run_cached。
绕开 TDX 依赖(公式库已清空 → prep 会报"公式不存在")。

用法:
  python tools/formula_farm/oos_segment.py GS0634 train    # 训练段跑 36 组定参
  python tools/formula_farm/oos_segment.py GS0634 valid    # 验证段跑训练段最优
"""
import argparse
import csv
import json
import os
import sys
import time

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

from quantqq_5m_sweep import combo_stop_config  # noqa: E402

CACHE = 'output/gs_5m_sweep'
TRAIN_END = '2025-11-30'
VALID_START = '2025-12-01'
CAPITAL = 3_000_000.0
MAX_BUY = 20_000.0
PRIORITY = 'trailing_first'


def gen_train_grid():
    """训练段定参网格 108 组 (不带先验: act/dd/cost/time 全档, 阶梯止盈固定 off)。

    不直接用 quantqq gen_coarse_combos() = 2592 组(太慢, GS0634 每组合 21s → 15h);
    也不强行限定 act=8%(那是全段精调的事后结论, 会造成信息泄漏)。
    """
    combos = []
    for cost in [-0.08, -0.12, -0.20]:
        for act in [0.02, 0.035, 0.05, 0.08]:
            for dd in [0.005, 0.01, 0.02]:
                for td in [12, 20, 40]:
                    combos.append({'cost': cost, 'act': act, 'dd': dd, 'ladder': 'off',
                                   'levels': [], 'time_days': td, 'cond_days': 0,
                                   'cond_profit': 0.0})
    return combos  # 3×4×3×3 = 108


def load_seg(formula, seg):
    """载入 cache 并按段切分矩阵。seg: train/valid/all"""
    cdir = os.path.join(ROOT, CACHE, formula, 'cache')
    meta = json.load(open(os.path.join(cdir, 'meta.json'), encoding='utf-8'))
    idx = pd.DatetimeIndex(pd.to_datetime(meta['index']))
    cols = meta['columns']
    ld = lambda n, mmap=None: np.load(os.path.join(cdir, n), mmap_mode=mmap)
    close_df = pd.DataFrame(ld('close.npy', 'r'), index=idx, columns=cols)
    entries_df = pd.DataFrame(ld('entries.npy'), index=idx, columns=cols)
    high_np, low_np = ld('high.npy', 'r'), ld('low.npy', 'r')
    open_np = ld('open.npy', 'r')
    tradable_np = ld('tradable.npy')

    if seg == 'train':
        mask = idx <= pd.Timestamp(TRAIN_END)
    elif seg == 'valid':
        mask = idx >= pd.Timestamp(VALID_START)
    else:
        mask = np.ones(len(idx), dtype=bool)
    return (meta, close_df[mask], entries_df[mask], high_np[mask], low_np[mask],
            open_np[mask], tradable_np[mask])


def run_combos(formula, seg, combos, out_fp):
    from backtest.engine import BacktestEngine, recompute_last_tradable_idx
    from backtest.prepared import PreparedMatrix

    # 断点续跑: 已有结果的 key 跳过 (机器频繁重启, 2026-09-10 教训)
    # F3: prev 行必须保留并合并回写出文件, 否则整表覆盖写会冲掉上一轮已完成结果
    done_keys = set()
    prev = None
    if os.path.exists(out_fp):
        try:
            prev = pd.read_csv(out_fp)
            done_keys = set(prev.loc[prev['annret'].notna(), 'key'])
        except Exception:
            prev = None
            done_keys = set()
    if done_keys:
        before = len(combos)
        combos = [c for c in combos
                  if f"c{c['cost']}_a{c['act']}_d{c['dd']}_L{c['ladder']}_t{c['time_days']}" not in done_keys]
        print(f'[{formula}/{seg}] 断点续跑: 已有 {before-len(combos)} 组跳过, 剩 {len(combos)} 组')
    if not combos:
        print(f'[{formula}/{seg}] 全部已完成, 跳过')
        return

    def _write(rows_):
        """合并历史 prev 行后按 key 去重 (keep='last', 本轮结果优先) 再整表写。"""
        out_df = pd.DataFrame(rows_)
        if prev is not None and len(prev):
            out_df = pd.concat([prev, out_df], ignore_index=True)
            out_df = out_df.drop_duplicates(subset=['key'], keep='last')
        os.makedirs(os.path.dirname(out_fp), exist_ok=True)
        out_df.to_csv(out_fp, index=False)

    meta, close_df, entries_df, high_np, low_np, open_np, tradable_np = load_seg(formula, seg)
    lti = recompute_last_tradable_idx(np.asarray(tradable_np, dtype=bool))
    engine = BacktestEngine({
        'initial_capital': CAPITAL, 'commission': 0.0003, 'slippage': 0.001,
        'stamp_tax': 0.0005, 'enable_realistic_costs': True, 'period': '5m',
        'position_sizing': {'min_buy_amount': 2000.0, 'max_buy_amount': MAX_BUY,
                            'lot_size': 100, 'min_lots': 1},
    })
    print(f'[{formula}/{seg}] 矩阵 {close_df.shape}, 区间 {close_df.index[0]} ~ {close_df.index[-1]}')
    rows = []
    t0 = time.time()
    for i, c in enumerate(combos, 1):
        levels = c['levels']
        lp = np.array([p for p, _ in levels], dtype=np.float64)
        lr = np.array([r for _, r in levels], dtype=np.float64)
        try:
            prepared = PreparedMatrix(
                close=close_df, entries=entries_df, high_np=high_np, low_np=low_np,
                open_np=open_np, tradable_np=tradable_np, last_tradable_idx=lti)
            res = engine.run_cached(prepared, combo_stop_config(c, PRIORITY), lp, lr,
                                    len(levels), filter_limit_up=False)
            m = res['metrics']
            rows.append({'key': f"c{c['cost']}_a{c['act']}_d{c['dd']}_L{c['ladder']}_t{c['time_days']}",
                         'annret': m.get('annualized_return', 0), 'maxdd': m.get('max_drawdown', 0),
                         'calmar': m.get('calmar_ratio', 0), 'winrate': m.get('win_rate', 0),
                         'trades': m.get('total_trades', 0)})
        except Exception as e:
            rows.append({'key': f"c{c['cost']}_a{c['act']}_d{c['dd']}_L{c['ladder']}_t{c['time_days']}",
                         'annret': None, 'error': f'{type(e).__name__}: {str(e)[:60]}'})
        if i % 10 == 0:
            print(f'  {i}/{len(combos)} ({time.time()-t0:.0f}s)', flush=True)
            # 增量落盘: 机器可能随时重启, 每 10 组写一次保进度
            _write(rows)
    _write(rows)
    ok = [r for r in rows if r.get('annret') is not None]
    if ok:
        b = max(ok, key=lambda r: r['annret'])
        print(f'[{formula}/{seg}] 最优: ann={b["annret"]*100:.1f}% dd={b["maxdd"]*100:.1f}% '
              f'key={b["key"]}')
    print(f'[{formula}/{seg}] 完成 {len(rows)} 组, 写入 {out_fp}')
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('formula')
    ap.add_argument('seg', choices=['train', 'valid'])
    ap.add_argument('--combos-file', default=None)
    ap.add_argument('--best-from', default=None, help='验证段: 用训练段结果文件里的最优+邻域')
    ap.add_argument('--quick', default=None,
                    help='快捷模式: 直接给定参数 key (如 c-0.2_a0.08_d0.005_Loff_t20) 跑该段')
    args = ap.parse_args()

    out_dir = os.path.join(ROOT, CACHE, f'oos_{args.seg}', args.formula)
    out_fp = os.path.join(out_dir, 'results.csv')

    if args.quick:
        # 快捷: 全段最优参数 + cost/time 邻域直接跑该段
        import re
        m = re.match(r'c(-?[\d.]+)_a([\d.]+)_d([\d.]+)_L(\w+)_t(\d+)', args.quick)
        cost, act, dd, ladder, td = (float(m.group(1)), float(m.group(2)),
                                     float(m.group(3)), m.group(4), int(m.group(5)))
        combos = []
        for c2 in [-0.08, -0.12, -0.20]:
            for t2 in [12, 20, 40]:
                combos.append({'cost': c2, 'act': act, 'dd': dd, 'ladder': ladder,
                               'levels': [], 'time_days': t2, 'cond_days': 0,
                               'cond_profit': 0.0})
        print(f'快捷模式: 基准 {args.quick} + cost/time 邻域 = {len(combos)} 组')
        run_combos(args.formula, args.seg, combos, out_fp)
        return

    if args.seg == 'train':
        combos = gen_train_grid()  # 108 组定参网格(无先验)
    else:
        # 验证段: 用训练段最优参数 + 成本/时间邻域
        src = args.best_from or os.path.join(ROOT, CACHE, 'oos_train', args.formula, 'results.csv')
        df = pd.read_csv(src)
        df = df[df['annret'].notna()].sort_values('annret', ascending=False)
        best = df.iloc[0]
        # 解析 key 重建 combo
        k = best['key']
        import re
        m = re.match(r'c(-?[\d.]+)_a([\d.]+)_d([\d.]+)_L(\w+)_t(\d+)', k)
        cost, act, dd, ladder, td = float(m.group(1)), float(m.group(2)), float(m.group(3)), m.group(4), int(m.group(5))
        combos = []
        for c2 in [-0.08, -0.12, -0.20]:
            for t2 in [12, 20, 40]:
                combos.append({'cost': c2, 'act': act, 'dd': dd, 'ladder': ladder,
                               'levels': [], 'time_days': t2, 'cond_days': 0, 'cond_profit': 0.0})
        print(f'验证 combos: 训练段最优 {k} + cost/time 邻域 = {len(combos)} 组')
    run_combos(args.formula, args.seg, combos, out_fp)


if __name__ == '__main__':
    main()
