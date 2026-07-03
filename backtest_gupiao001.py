"""
GUPIAO_001~078 批量回测 + 阶段报告 + 汇总报告

严格基于 run_gs_txt_batch.py 模板改造:
  - 区间: 2024-01-01 ~ 2026-06-25  (2.5 年)
  - 范围: 沪深 A 股 type=50
  - 信号上限: 50000
  - 报告: 桌面 GUPIAO001/ (Markdown)

执行: python backtest_gupiao001.py
"""
import sys
import os
import re
import json
import time
import warnings
from pathlib import Path

# Windows GBK stdout: 强制 UTF-8 输出避免 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

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
START = '20240101'
END = '20260625'
UNIVERSE_TYPE = '50'
MAX_SIGNALS = 50000

# 与 default.yaml 一致的回测参数 (BUG-5 修复后)
ENGINE_CFG = {
    'initial_capital': 1_000_000.0,
    'commission': 0.0003,
    'slippage': 0.001,
    'period': '1d',
    'position_sizing': {
        'min_buy_amount': 2_000.0,
        'max_buy_amount': 20_000.0,
        'lot_size': 100,
        'min_lots': 1,
    },
}
STOP_CONFIG = load_stop_config()

# 桌面输出目录
DESKTOP_DIR = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop" / "GUPIAO001"
DESKTOP_DIR.mkdir(parents=True, exist_ok=True)

# 工程内日志
PROJECT_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output', 'gupiao001_progress.log')
os.makedirs(os.path.dirname(PROJECT_LOG), exist_ok=True)

STAGE_SIZE = 20  # 每 20 个公式一个阶段


def extract_formula_name(filename):
    """gs_1_GUPIAO_001.txt → GUPIAO_001"""
    base = filename.replace('.txt', '')
    m = re.match(r'^gs_\d+_(.+)$', base)
    return m.group(1) if m else base


def run_one(formula_name, sel_df, C, H, L, O, V, common_univ):
    """对单个公式跑回测. sel_df 已经过滤好, common_univ 是回测用的股票池"""
    try:
        sig_bt = sel_df.copy()
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


def fmt_status(r):
    """格式化单行状态输出"""
    if r.get('status') == 'ok':
        return (f'信号 {r["signals"]} 选股 {r["stocks"]} 交易 {r["trades"]} '
                f'累计 {r["cumret"]*100:+.2f}% 年化 {r["annret"]*100:+.2f}% '
                f'回撤 {r["maxdd"]*100:.2f}% 胜率 {r["winrate"]*100:.1f}%')
    return r.get('status', 'unknown')


def write_stage_report(stage_no, stage_results, total_so_far, total_all):
    """生成阶段报告 Markdown"""
    md_path = DESKTOP_DIR / f'GUPIAO001_stage_{stage_no}.md'
    ok_results = sorted(
        [r for r in stage_results if r.get('status') == 'ok'],
        key=lambda r: r.get('annret') if r.get('annret') is not None else -999,
        reverse=True,
    )
    fail_results = [r for r in stage_results if r.get('status') != 'ok']
    ok_n = len(ok_results)
    fail_n = len(fail_results)

    first_idx = total_so_far - len(stage_results) + 1
    last_idx = total_so_far

    lines = []
    lines.append(f'# GUPIAO 阶段 {stage_no} 报告 — 公式 {first_idx:03d}~{last_idx:03d}')
    lines.append('')
    lines.append(f'- 区间: {START} ~ {END}  (2.5 年)')
    lines.append(f'- 范围: 沪深 A 股 type=50')
    lines.append(f'- 信号上限: {MAX_SIGNALS}')
    lines.append(f'- 阶梯止盈: 6%:30% / 15%:30%  (BUG-5 修复后)')
    lines.append(f'- 成本止损: -12%  /  移动止盈: 激活 8% 回撤 5%  /  时间止损: 20 日')
    lines.append(f'- 本阶段公式数: {len(stage_results)}  (成功 {ok_n} / 失败 {fail_n})')
    lines.append(f'- 进度: {total_so_far}/{total_all}')
    lines.append('')

    lines.append('## 本阶段 TOP (按年化收益降序)')
    lines.append('')
    lines.append('| 排名 | 文件 | 公式 | 信号 | 股票 | 交易 | 累计收益 | 年化 | 最大回撤 | 夏普 | 胜率 | 6%档 | 15%档 |')
    lines.append('|----:|------|------|----:|----:|----:|---------:|-----:|---------:|-----:|-----:|-----:|------:|')
    if ok_results:
        for rank, r in enumerate(ok_results, 1):
            lines.append(
                f'| {rank} | `{r["file"]}` | {r["formula"]} | {r["signals"]} | {r["stocks"]} | '
                f'{r["trades"]} | {r["cumret"]*100:+.2f}% | {r["annret"]*100:+.2f}% | '
                f'{r["maxdd"]*100:.2f}% | {r["sharpe"]:.2f} | {r["winrate"]*100:.1f}% | '
                f'{r["ladder_6_count"]} | {r["ladder_15_count"]} |'
            )
    else:
        lines.append('| — | — | — | — | — | — | — | — | — | — | — | — | — |')

    if fail_results:
        lines.append('')
        lines.append(f'## 本阶段 失败/跳过 ({fail_n} 个)')
        lines.append('')
        for r in fail_results:
            lines.append(f'- `{r["file"]}` → [{r["formula"]}]: {r["status"]}')

    lines.append('')
    lines.append('---')
    lines.append(f'生成时间: {time.strftime("%Y-%m-%d %H:%M:%S")}')

    try:
        md_path.write_text('\n'.join(lines), encoding='utf-8')
        print(f'    [MD] 阶段报告: {md_path}')
    except OSError as e:
        print(f'    [warn] 阶段报告写入失败: {e}')


def write_summary_report(all_results, total_n, elapsed_total):
    """生成汇总报告 Markdown"""
    md_path = DESKTOP_DIR / 'GUPIAO001_summary.md'
    ok_results = sorted(
        [r for r in all_results if r.get('status') == 'ok'],
        key=lambda r: r.get('annret') if r.get('annret') is not None else -999,
        reverse=True,
    )
    fail_results = [r for r in all_results if r.get('status') != 'ok']
    ok_n = len(ok_results)
    fail_n = len(fail_results)

    lines = []
    lines.append('# GUPIAO_001~078 批量回测汇总报告')
    lines.append('')
    lines.append(f'- 区间: **{START} ~ {END}**  (2.5 年)')
    lines.append(f'- 范围: 沪深 A 股 type=50')
    lines.append(f'- 信号上限: {MAX_SIGNALS}')
    lines.append(f'- 初始资金: {ENGINE_CFG["initial_capital"]:,.0f}')
    lines.append(f'- 阶梯止盈: 6%:30% / 15%:30%')
    lines.append(f'- 成本止损: -12%  /  移动止盈: 激活 8% 回撤 5%  /  时间止损: 20 日')
    lines.append(f'- 总公式数: **{total_n}**')
    lines.append(f'- 成功 / 失败+跳过: **{ok_n} / {fail_n}**')
    lines.append(f'- 总用时: {elapsed_total:.0f}s ({elapsed_total/60:.1f}min)')
    lines.append('')

    lines.append('## 状态总览 (全 78 个)')
    lines.append('')
    lines.append('| # | 文件 | 公式 | 状态 | 信号 | 交易 | 年化 | 最大回撤 | 胜率 |')
    lines.append('|---:|------|------|------|----:|----:|-----:|---------:|-----:|')
    for idx, r in enumerate(all_results, 1):
        if r.get('status') == 'ok':
            lines.append(
                f'| {idx} | `{r["file"]}` | {r["formula"]} | ok | '
                f'{r["signals"]} | {r["trades"]} | '
                f'{r["annret"]*100:+.2f}% | {r["maxdd"]*100:.2f}% | {r["winrate"]*100:.1f}% |'
            )
        else:
            short = r.get('status', '')[:30]
            lines.append(f'| {idx} | `{r["file"]}` | {r["formula"]} | {short} | — | — | — | — | — |')

    lines.append('')
    lines.append('## TOP 20 公式 (按年化收益降序)')
    lines.append('')
    lines.append('| 排名 | 文件 | 公式 | 信号 | 股票 | 交易 | 累计收益 | 年化 | 最大回撤 | 夏普 | 胜率 | 6%档 | 15%档 |')
    lines.append('|----:|------|------|----:|----:|----:|---------:|-----:|---------:|-----:|-----:|-----:|------:|')
    if ok_results:
        for rank, r in enumerate(ok_results[:20], 1):
            lines.append(
                f'| {rank} | `{r["file"]}` | {r["formula"]} | {r["signals"]} | {r["stocks"]} | '
                f'{r["trades"]} | {r["cumret"]*100:+.2f}% | {r["annret"]*100:+.2f}% | '
                f'{r["maxdd"]*100:.2f}% | {r["sharpe"]:.2f} | {r["winrate"]*100:.1f}% | '
                f'{r["ladder_6_count"]} | {r["ladder_15_count"]} |'
            )
    else:
        lines.append('| — | — | — | — | — | — | — | — | — | — | — | — | — |')

    if fail_results:
        lines.append('')
        lines.append(f'## 失败/跳过全量清单 ({fail_n} 个)')
        lines.append('')
        for r in fail_results:
            lines.append(f'- `{r["file"]}` → [{r["formula"]}]: {r["status"]}')

    lines.append('')
    lines.append('## 阶段报告索引')
    lines.append('')
    stage_count = (total_n + STAGE_SIZE - 1) // STAGE_SIZE
    for s in range(1, stage_count + 1):
        start_idx = (s - 1) * STAGE_SIZE + 1
        end_idx = min(s * STAGE_SIZE, total_n)
        lines.append(f'- [GUPIAO001_stage_{s}.md](GUPIAO001_stage_{s}.md) 公式 {start_idx:03d}~{end_idx:03d}')

    lines.append('')
    lines.append('## 注解')
    lines.append('')
    lines.append('- **信号**: TDX 公式在区间内返回 XG=1 的 (stock, date) 组合数')
    lines.append('- **交易**: 触发实际买入并完成平仓的交易笔数')
    lines.append('- **累计/年化**: BacktestEngine 计算的复利收益')
    lines.append('- **阶梯止盈档位**: 6% 档触发卖出 30%, 15% 档触发卖出 30%')
    lines.append('')
    lines.append('---')
    lines.append(f'生成时间: {time.strftime("%Y-%m-%d %H:%M:%S")}')

    try:
        md_path.write_text('\n'.join(lines), encoding='utf-8')
        print(f'  [MD] 汇总报告: {md_path}')
    except OSError as e:
        print(f'  [warn] 汇总报告写入失败: {e}')


def write_csv_and_json(all_results, total_n):
    """全量 CSV + JSON (桌面 + 工程双备份)"""
    # CSV (BOM, Excel 友好)
    csv_path = DESKTOP_DIR / 'GUPIAO001_all.csv'
    ok_results = sorted(
        [r for r in all_results if r.get('status') == 'ok'],
        key=lambda r: r.get('annret') if r.get('annret') is not None else -999,
        reverse=True,
    )
    with open(csv_path, 'w', encoding='utf-8-sig') as f:
        f.write('rank,file,formula,status,signals,stocks,trades,cumret,annret,maxdd,sharpe,winrate,ladder6,ladder15\n')
        for rank, r in enumerate(ok_results, 1):
            f.write(f'{rank},{r["file"]},{r["formula"]},ok,'
                    f'{r["signals"]},{r["stocks"]},{r["trades"]},'
                    f'{r["cumret"]:.4f},{r["annret"]:.4f},{r["maxdd"]:.4f},'
                    f'{r["sharpe"]:.2f},{r["winrate"]:.4f},'
                    f'{r["ladder_6_count"]},{r["ladder_15_count"]}\n')
        for r in all_results:
            if r.get('status') != 'ok':
                f.write(f',{r["file"]},{r["formula"]},{r["status"]},,,,,,,,,,\n')
    print(f'  [MD] 全量 CSV: {csv_path}')

    # JSON (含全部结果)
    json_path = DESKTOP_DIR / 'GUPIAO001_all.json'
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump({
            'config': {
                'start': START, 'end': END, 'universe': UNIVERSE_TYPE,
                'max_signals': MAX_SIGNALS, 'total_files': total_n,
            },
            'all_results': all_results,
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f'  [MD] 全量 JSON: {json_path}')


def main():
    t_start = time.time()
    print('=' * 80)
    print('  VERA GUPIAO_001~078 批量回测')
    print(f'  区间: {START} ~ {END}  范围: 沪深 A 股 type=50  信号上限: {MAX_SIGNALS}')
    print(f'  输出: {DESKTOP_DIR}')
    print('=' * 80)

    # 1. 取文件名列表 (仅 GUPIAO)
    files = sorted([
        f for f in os.listdir(GS_TXT_DIR)
        if f.startswith('gs_1_GUPIAO_') and f.endswith('.txt')
    ])
    print(f'\n[1] 共 {len(files)} 个 GUPIAO 公式')

    if len(files) == 0:
        print('  [warn] 未找到任何 GUPIAO 文件，退出')
        return

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
    log_f = open(PROJECT_LOG, 'w', encoding='utf-8')
    log_f.write(f'idx,formula,fail_count,skip_count,ok_count,status,signals,stocks,trades,cumret,annret,maxdd,sharpe,winrate\n')

    current_stage_results = []  # 只放当前阶段的公式
    current_stage_no = 0

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
            print(f'    [X] selection_error: {type(e).__name__}: {e}', flush=True)
            r = {
                'file': fname, 'formula': formula,
                'status': f'selection_error: {type(e).__name__}: {str(e)[:60]}',
            }
            results.append(r)
            current_stage_results.append(r)
            fail_count += 1
            log_f.write(f'{i},{formula},{fail_count},{skip_count},{ok_count},selection_error,,,,,,,\n')
            log_f.flush()

            # 检查阶段报告
            _check_stage_report(i, len(files), current_stage_results, results)
            continue

        if sel_df is None or len(sel_df) == 0:
            print(f'    [X] no_signals', flush=True)
            r = {'file': fname, 'formula': formula, 'status': 'no_signals'}
            results.append(r)
            current_stage_results.append(r)
            fail_count += 1
            log_f.write(f'{i},{formula},{fail_count},{skip_count},{ok_count},no_signals,,,,,,,\n')
            log_f.flush()

            _check_stage_report(i, len(files), current_stage_results, results)
            continue

        if len(sel_df) > MAX_SIGNALS:
            print(f'    [skip] too_many_signals ({len(sel_df)})', flush=True)
            r = {
                'file': fname, 'formula': formula,
                'status': f'too_many_signals ({len(sel_df)})',
                'signals': len(sel_df),
            }
            results.append(r)
            current_stage_results.append(r)
            skip_count += 1
            log_f.write(f'{i},{formula},{fail_count},{skip_count},{ok_count},too_many_signals,,,,,,,\n')
            log_f.flush()

            _check_stage_report(i, len(files), current_stage_results, results)
            continue

        # 回测
        r = run_one(formula, sel_df, C, H, L, O, V, univ)
        r['file'] = fname
        r['formula'] = formula
        results.append(r)
        current_stage_results.append(r)

        if r['status'] == 'ok':
            ok_count += 1
            print(f'    [OK] {fmt_status(r)}', flush=True)
            log_f.write(f'{i},{formula},{fail_count},{skip_count},{ok_count},ok,'
                        f'{r["signals"]},{r["stocks"]},{r["trades"]},'
                        f'{r["cumret"]:.4f},{r["annret"]:.4f},{r["maxdd"]:.4f},'
                        f'{r["sharpe"]:.2f},{r["winrate"]:.4f}\n')
        else:
            fail_count += 1
            print(f'    [X] {r["status"]}', flush=True)
            log_f.write(f'{i},{formula},{fail_count},{skip_count},{ok_count},{r["status"][:40]},,,,,,\n')
        log_f.flush()

        # 阶段报告检查
        _check_stage_report(i, len(files), current_stage_results, results)

    log_f.close()
    elapsed_total = time.time() - t_start
    print(f'\n{"=" * 80}')
    print(f'  完成 | OK={ok_count} FAIL={fail_count} SKIP={skip_count} | 用时 {elapsed_total:.0f}s')
    print('=' * 80)

    # 4. 全量 CSV + JSON
    write_csv_and_json(results, len(files))

    # 5. 汇总报告
    write_summary_report(results, len(files), elapsed_total)

    print(f'\n  [DIR] 所有报告输出至: {DESKTOP_DIR}')


def _check_stage_report(i, total_n, current_stage_results, all_results):
    """每 20 个或最后一个时生成阶段报告；写入后清空当前阶段缓冲"""
    if i % STAGE_SIZE == 0 or i == total_n:
        stage_no = (i + STAGE_SIZE - 1) // STAGE_SIZE
        write_stage_report(stage_no, list(current_stage_results), i, total_n)
        current_stage_results.clear()  # 清空缓冲，下个阶段只装自己的公式


if __name__ == '__main__':
    main()