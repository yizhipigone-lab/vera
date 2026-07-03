"""
批量回测 E:\\NEW_TDX\\T0001\\export\\gs_txt\\ 下的所有通达信公式。

文件名规则: gs_1_XXX.txt  →  公式名 = XXX (已注册在通达信客户端)

配置:
  - 区间: 2025-06-23 ~ 2026-06-23 (近 1 年)
  - 范围: 沪深 A 股 type=50
  - 信号上限: 50000 (跳过明显垃圾公式)
  - 排序: 按年化收益降序
"""
import sys
import os
import re
import json
import time
import warnings

warnings.filterwarnings('ignore')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np

from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from core.formula_runner import FormulaRunner
from backtest.engine import BacktestEngine
from backtest.stop_config import load_stop_config

# === 配置 ===
GS_TXT_DIR = r'E:\NEW_TDX\T0001\export\gs_txt'
START = '20250623'
END = '20260623'
UNIVERSE_TYPE = '50'
MAX_SIGNALS = 50000  # 超过这个信号数视为不合理, 跳过

# 与 default.yaml 一致的回测参数 (BUG-5 修复后)
ENGINE_CFG = {
    'initial_capital': 1_000_000.0,
    'commission': 0.0003,
    'slippage': 0.001,
    'enable_realistic_costs': True,   # A2 修复: 真实成本 (2026-06-26)
    'stamp_tax': 0.0005,             # A2 修复: 印花税 (2026-06-26)
    'period': '1d',
    'position_sizing': {
        'min_buy_amount': 2_000.0,
        'max_buy_amount': 20_000.0,
        'lot_size': 100,
        'min_lots': 1,
    },
}
STOP_CONFIG = load_stop_config()


def extract_formula_name(filename):
    """gs_1_XXX.txt → XXX"""
    base = filename.replace('.txt', '')
    m = re.match(r'^gs_\d+_(.+)$', base)
    return m.group(1) if m else base


def run_one(formula_name, sel_df, C, H, L, O, V, common_univ):
    """对单个公式跑回测. sel_df 已经过滤好, common_univ 是回测用的股票池"""
    try:
        sig_bt = sel_df.copy()
        # 限制到请求区间 (TDX 已经过滤, 但保险起见再过滤一次)
        sig_bt = sig_bt[(sig_bt['select_date'] >= pd.to_datetime(START)) &
                        (sig_bt['select_date'] <= pd.to_datetime(END))]
        ts = len(sig_bt)
        if ts < 5:
            return {'status': f'too_few_signals ({ts})'}

        common = sorted(set(common_univ) & set(sig_bt['stock_code'].unique()))
        if len(common) < 3:
            return {'status': f'too_few_stocks ({len(common)})'}

        cs = C[common].ffill().bfill()
        hs = H.reindex(index=cs.index, columns=common).ffill().bfill()
        ls = L.reindex(index=cs.index, columns=common).ffill().bfill()

        entries = pd.DataFrame(False, index=cs.index, columns=cs.columns)
        for _, row in sig_bt.iterrows():
            sc, dt = row['stock_code'], row['select_date']
            if sc not in entries.columns:
                continue
            if dt in entries.index:
                entries.loc[dt, sc] = True
            else:
                m = entries.index >= dt
                if m.any():
                    entries.loc[entries.index[m][0], sc] = True

        engine = BacktestEngine(ENGINE_CFG)
        bp = np.array([0.06, 0.15], dtype=np.float64)
        br = np.array([0.30, 0.30], dtype=np.float64)
        brs = engine.run_cached(cs, entries, hs.values.astype(np.float64),
                                ls.values.astype(np.float64), STOP_CONFIG, sig_bt, bp, br, 2, skip_sm=True)
        m = brs['metrics']
        trades = brs.get('trades', pd.DataFrame())
        ladder = trades[trades.get('exit_reason', '') == '阶梯止盈'] if not trades.empty else pd.DataFrame()
        ladder_6 = 0
        ladder_15 = 0
        if not ladder.empty:
            for _, t in ladder.iterrows():
                p = t.get('profit_pct', 0)
                if abs(p - 0.06) < 0.005:
                    ladder_6 += 1
                elif abs(p - 0.15) < 0.01:
                    ladder_15 += 1

        return {
            'status': 'ok',
            'signals': ts,
            'stocks': len(common),
            'trades': len(trades),
            'cumret': brs['cumulative_return'],
            'annret': m['annualized_return'],
            'maxdd': m['max_drawdown'],
            'sharpe': m['sharpe_ratio'],
            'winrate': m['win_rate'],
            'ladder_6_count': ladder_6,
            'ladder_15_count': ladder_15,
        }
    except Exception as e:
        return {'status': f'backtest_error: {type(e).__name__}: {str(e)[:80]}'}


def main():
    t_start = time.time()
    print('=' * 80)
    print('  VERA 批量回测 — gs_txt 全量公式')
    print(f'  区间: {START} ~ {END}  范围: 沪深 A 股 type=50  信号上限: {MAX_SIGNALS}')
    print('=' * 80)

    # 1. 取文件名列表
    files = sorted([f for f in os.listdir(GS_TXT_DIR) if f.startswith('gs_') and f.endswith('.txt')])
    print(f'\n[1] 共 {len(files)} 个 .txt 文件')

    # 2. 连 TDX + 取 K 线 (只取一次, 复用)
    print(f'\n[2] 连接 TDX + 取 K 线...', flush=True)
    TdxConnector.ensure_connected()
    codes = DataFetcher.get_stock_universe(UNIVERSE_TYPE)
    k = DataFetcher.get_kline(codes, START, END, dividend_type="front", period="1d")
    C = k['Close'].sort_index()
    H = k['High'].sort_index()
    L = k['Low'].sort_index()
    O = k['Open'].sort_index()
    V = k.get('Volume', pd.DataFrame()).sort_index()
    valid = C.notna().sum() > 100
    for d in [C, H, L, O, V]:
        d.drop(columns=[c for c in d.columns if not valid[c]], inplace=True)
    univ = [c for c in C.columns if 'ST' not in c and '*ST' not in c]
    print(f'  股票池: {len(univ)} 只, K线 shape: {C.shape}')

    # 3. 逐个公式回测
    print(f'\n[3] 批量回测...', flush=True)
    results = []
    ok_count = 0
    fail_count = 0
    skip_count = 0
    t0 = time.time()
    log_path = 'output/gs_txt_batch_progress.log'
    os.makedirs('output', exist_ok=True)
    log_f = open(log_path, 'w', encoding='utf-8')

    for i, fname in enumerate(files, 1):
        formula = extract_formula_name(fname)
        elapsed = time.time() - t0
        speed = i / max(elapsed, 1) * 60
        eta_min = (len(files) - i) / max(speed, 0.01)
        print(f'\n  [{i}/{len(files)}] {fname} → [{formula}] (~{speed:.1f}/min, ETA {eta_min:.0f}min)', flush=True)

        # 选股
        try:
            sel_df = FormulaRunner.run_stock_selection_with_dates(
                formula_name=formula, formula_arg='',
                stock_list=None, start_time=START, end_time=END,
                stock_period='1d', dividend_type=1,
            )
        except Exception as e:
            print(f'    ERR selection: {type(e).__name__}: {e}', flush=True)
            results.append({
                'file': fname, 'formula': formula,
                'status': f'selection_error: {type(e).__name__}: {str(e)[:60]}',
            })
            fail_count += 1
            log_f.write(f'{i},{formula},{fail_count},{skip_count},{ok_count},selection_error\n')
            log_f.flush()
            continue

        if sel_df is None or len(sel_df) == 0:
            print(f'    X no_signals', flush=True)
            results.append({'file': fname, 'formula': formula, 'status': 'no_signals'})
            fail_count += 1
            log_f.write(f'{i},{formula},{fail_count},{skip_count},{ok_count},no_signals\n')
            log_f.flush()
            continue

        if len(sel_df) > MAX_SIGNALS:
            print(f'    TOOMANY signals ({len(sel_df)})', flush=True)
            results.append({
                'file': fname, 'formula': formula,
                'status': f'too_many_signals ({len(sel_df)})',
                'signals': len(sel_df),
            })
            skip_count += 1
            log_f.write(f'{i},{formula},{fail_count},{skip_count},{ok_count},too_many_signals\n')
            log_f.flush()
            continue

        # 回测
        r = run_one(formula, sel_df, C, H, L, O, V, univ)
        r['file'] = fname
        r['formula'] = formula
        results.append(r)

        if r['status'] == 'ok':
            ok_count += 1
            print(f'    OK {r["signals"]} {r["stocks"]} {r["trades"]} '
                  f'{r["cumret"]*100:+.2f}% {r["annret"]*100:+.2f}% '
                  f'{r["maxdd"]*100:.2f}% {r["winrate"]*100:.1f}%', flush=True)
            log_f.write(f'{i},{formula},{fail_count},{skip_count},{ok_count},ok,'
                        f'{r["signals"]},{r["stocks"]},{r["trades"]},'
                        f'{r["cumret"]:.4f},{r["annret"]:.4f},{r["maxdd"]:.4f},'
                        f'{r["sharpe"]:.2f},{r["winrate"]:.4f}\n')
        else:
            fail_count += 1
            print(f'    X {r["status"]}', flush=True)
            log_f.write(f'{i},{formula},{fail_count},{skip_count},{ok_count},{r["status"][:40]}\n')
        log_f.flush()

    log_f.close()
    elapsed_total = time.time() - t_start
    print(f'\n{"=" * 80}')
    print(f'  完成 | OK={ok_count} FAIL={fail_count} SKIP={skip_count} | 用时 {elapsed_total:.0f}s')
    print('=' * 80)

    # 4. 排序 + 输出
    ok_results = [r for r in results if r['status'] == 'ok']
    ok_results.sort(key=lambda r: r.get('annret') if r.get('annret') is not None else -999,
                    reverse=True)

    # JSON
    out_json = 'output/gs_txt_batch_results.json'
    with open(out_json, 'w', encoding='utf-8') as f:
        json.dump({
            'config': {
                'start': START, 'end': END, 'universe': UNIVERSE_TYPE,
                'max_signals': MAX_SIGNALS,
                'total_files': len(files),
            },
            'summary': {
                'ok': ok_count, 'fail': fail_count, 'skip': skip_count,
                'elapsed_s': round(elapsed_total, 1),
            },
            'top_results': ok_results[:50],
            'all_results': results,
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f'  保存: {out_json}')

    # CSV (只 ok 的, 按年化降序)
    out_csv = 'output/gs_txt_batch_results.csv'
    with open(out_csv, 'w', encoding='utf-8-sig') as f:
        f.write('rank,file,formula,signals,stocks,trades,cumret,annret,maxdd,sharpe,winrate,ladder6,ladder15\n')
        for rank, r in enumerate(ok_results, 1):
            f.write(f'{rank},{r["file"]},{r["formula"]},'
                    f'{r["signals"]},{r["stocks"]},{r["trades"]},'
                    f'{r["cumret"]:.4f},{r["annret"]:.4f},{r["maxdd"]:.4f},'
                    f'{r["sharpe"]:.2f},{r["winrate"]:.4f},'
                    f'{r["ladder_6_count"]},{r["ladder_15_count"]}\n')
    print(f'  保存: {out_csv}')

    # Markdown
    out_md = 'output/gs_txt_batch_results.md'
    with open(out_md, 'w', encoding='utf-8') as f:
        f.write('# gs_txt 全量公式批量回测报告\n\n')
        f.write(f'- **区间**: {START} ~ {END}\n')
        f.write(f'- **范围**: 沪深 A 股 type=50 ({len(univ)} 只)\n')
        f.write(f'- **信号上限**: {MAX_SIGNALS}\n')
        f.write(f'- **初始资金**: {ENGINE_CFG["initial_capital"]:,.0f}\n')
        f.write(f'- **阶梯止盈**: 6%:30% / 15%:30% (BUG-5 修复后)\n')
        f.write(f'- **总公式**: {len(files)}  **成功**: {ok_count}  **失败**: {fail_count}  **跳过**: {skip_count}\n')
        f.write(f'- **总用时**: {elapsed_total:.0f}s ({elapsed_total/60:.1f}min)\n\n')

        f.write('## TOP 50 (按年化收益降序)\n\n')
        f.write('| 排名 | 文件 | 公式 | 信号 | 股票 | 交易 | 累计收益 | 年化 | 最大回撤 | 夏普 | 胜率 | 6%档 | 15%档 |\n')
        f.write('|----:|------|------|----:|----:|----:|---------:|-----:|---------:|-----:|-----:|-----:|------:|\n')
        for rank, r in enumerate(ok_results[:50], 1):
            f.write(f'| {rank} | `{r["file"]}` | {r["formula"]} | {r["signals"]} | {r["stocks"]} | '
                    f'{r["trades"]} | {r["cumret"]*100:+.2f}% | {r["annret"]*100:+.2f}% | '
                    f'{r["maxdd"]*100:.2f}% | {r["sharpe"]:.2f} | {r["winrate"]*100:.1f}% | '
                    f'{r["ladder_6_count"]} | {r["ladder_15_count"]} |\n')

        if fail_count > 0:
            f.write(f'\n## 失败/跳过的公式 ({fail_count + skip_count} 个)\n\n')
            for r in results:
                if r['status'] != 'ok':
                    f.write(f'- `{r["file"]}` → [{r["formula"]}]: {r["status"]}\n')

    print(f'  保存: {out_md}')


if __name__ == '__main__':
    main()
