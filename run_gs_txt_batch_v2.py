"""
gs_txt 公式批量回测 (先生 2026-07-03 定调版, v4)

设计:
    - 选股: 系统 StockSelector.run()
    - 回测: 系统 BacktestEngine.run()
    - 每个公式用 subprocess 独立进程 (90s 超时, 先生定)
    - 主进程: 列文件 → 并行调子脚本 → 收集指标 → 增量 MD 报告 (每 3 个输出一次)

用法:
    python run_gs_txt_batch_v2.py [--debug N] [--parallel N]
"""
import argparse
import sys
import os
import re
import json
import time
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# === 配置 (先生 2026-07-03 定) ===
GS_TXT_DIR = r'E:\NEW_TDX\T0001\export\gs_txt'
START = '20260101'
END = '20261231'
REPORT_INTERVAL = 3      # 每 N 个输出一次增量 MD

EXCLUDE_PREFIX = 'GUPIAO'
EXCLUDE_HEADER = '15,2'

TIMEOUT = 150  # 先生定: 150s (子进程启动15s + 选股40-60s + 回测20-35s + 余量)
DEFAULT_PARALLEL = 3  # 先生定: 每 3 个并行


def parse_header(fname):
    try:
        with open(os.path.join(GS_TXT_DIR, fname), 'rb') as f:
            data = f.read()
        lines = data.split(b'\n')
        title = lines[0].decode('gbk', errors='replace').strip()
        header = lines[1].decode('ascii', errors='replace').strip() if len(lines) > 1 else ''
        return title, header
    except Exception:
        return '?', ''


def should_skip(fname, header):
    if EXCLUDE_PREFIX in fname:
        return 'GUPIAO'
    if header.startswith(EXCLUDE_HEADER):
        return f'header={EXCLUDE_HEADER}'
    return None


def list_target_files():
    files = []
    for f in sorted(os.listdir(GS_TXT_DIR)):
        if not (f.startswith('gs_') and f.endswith('.txt')):
            continue
        title, header = parse_header(f)
        if should_skip(f, header) is None:
            files.append((f, title, header))
    return files


def extract_formula_name(filename):
    base = filename.replace('.txt', '')
    m = re.match(r'^gs_\d+_(.+)$', base)
    return m.group(1) if m else base


def run_one(formula, start, end, timeout=TIMEOUT):
    """subprocess 调 _gs_run_one.py"""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_gs_run_one.py')
    try:
        r = subprocess.run(
            [sys.executable, '-X', 'utf8', script, formula, start, end],
            capture_output=True, text=True, timeout=timeout, encoding='utf-8',
        )
    except subprocess.TimeoutExpired:
        return {'status': 'selection_timeout'}

    out = r.stdout if r.stdout else ''
    json_line = None
    for line in out.split('\n'):
        line = line.strip()
        if line.startswith('{') and '"status"' in line:
            json_line = line
            break
    if not json_line:
        return {'status': 'error', 'msg': f'no json: {(r.stderr or "")[:80]}'}
    try:
        return json.loads(json_line)
    except json.JSONDecodeError:
        return {'status': 'error', 'msg': f'bad json: {json_line[:100]}'}


def write_md(ok_results, all_results, n_total, elapsed, suffix=''):
    """生成增量 MD 报告"""
    out_md = f'output/gs_batch{suffix}.md'
    ok_count = len(ok_results)
    fail_count = len([r for r in all_results if r['status'] not in ('ok', 'too_many_signals')])
    skip_count = len([r for r in all_results if r['status'] == 'too_many_signals'])
    timeout_count = len([r for r in all_results if r['status'] == 'selection_timeout'])

    ok_sorted = sorted(ok_results,
                       key=lambda r: r.get('annret') if r.get('annret') is not None else -999,
                       reverse=True)

    with open(out_md, 'w', encoding='utf-8') as f:
        f.write('# gs_txt 公式批量回测 (v4 — 系统能力 + 并行)\n\n')
        f.write(f'- **区间**: {START} ~ {END}\n')
        f.write(f'- **排除**: GUPIAO + 文件头 15,2\n')
        f.write(f'- **信号上限**: 50000\n')
        f.write(f'- **超时**: {TIMEOUT}s\n')
        f.write(f'- **总公式**: {n_total}  **已测**: {len(all_results)}  **成功**: {ok_count}  '
                f'**失败**: {fail_count}  **跳过(信号超限)**: {skip_count}  **超时**: {timeout_count}\n')
        f.write(f'- **用时**: {elapsed:.0f}s ({elapsed/60:.1f}min)\n\n')

        f.write('## 排名 (按年化收益降序)\n\n')
        f.write('| 排名 | 文件 | 标题 | 文件头 | 信号 | 股票 | 交易 | 累计 | 年化 | 回撤 | 夏普 | 胜率 | 6%档 | 15%档 |\n')
        f.write('|----:|------|------|------|----:|----:|----:|-----:|-----:|-----:|-----:|-----:|-----:|------:|\n')
        for rank, r in enumerate(ok_sorted, 1):
            f.write(f'| {rank} | `{r["file"]}` | {r["title"]} | {r["header"]} '
                    f'| {r.get("signals","")} | {r.get("stocks","")} | {r.get("trades","")} '
                    f'| {r.get("cumret",0)*100:+.2f}% | {r.get("annret",0)*100:+.2f}% '
                    f'| {r.get("maxdd",0)*100:.2f}% | {r.get("sharpe",0):.2f} | {r.get("winrate",0)*100:.1f}% '
                    f'| {r.get("ladder6",0)} | {r.get("ladder15",0)} |\n')

        failed = [r for r in all_results if r['status'] != 'ok']
        if failed:
            f.write(f'\n## 失败/跳过/超时 ({len(failed)} 个)\n\n')
            for r in failed:
                f.write(f'- `{r["file"]}` → [{r["formula"]}] (header={r["header"]}): {r["status"]}\n')

    return out_md


def main():
    ap = argparse.ArgumentParser(description='gs_txt 公式批量回测 (v4 — 并行 + 增量报告)')
    ap.add_argument('--debug', type=int, default=0, help='只跑前 N 个 (循环模式)')
    ap.add_argument('--parallel', type=int, default=DEFAULT_PARALLEL, help=f'并行数 (默认 {DEFAULT_PARALLEL})')
    args = ap.parse_args()

    t_start = time.time()
    print('=' * 80)
    print('  VERA 批量回测 — gs_txt (v4 — 并行 + 增量报告)')
    print(f'  区间: {START} ~ {END}  超时: {TIMEOUT}s  并行: {args.parallel}')
    print('=' * 80)

    # 1. 列文件
    print('\n[1] 枚举待跑文件...', flush=True)
    all_files = list_target_files()
    print(f'  共 {len(all_files)} 个', flush=True)

    if args.debug > 0:
        debug_path = os.path.join('output', 'debug_10_picks.json')
        with open(debug_path, encoding='utf-8') as f:
            debug_picks = json.load(f)[:args.debug]
        all_files = [
            (f, *parse_header(f)) for f in debug_picks
            if os.path.exists(os.path.join(GS_TXT_DIR, f))
        ]
        print(f'  DEBUG 模式: 只跑 {len(all_files)} 个', flush=True)

    # 2. 串行跑 (TDX PYPlugins 不支持并发, 并行会导致全部 timeout)
    n_total = len(all_files)
    n_done = 0
    all_results = []
    ok_results = []
    os.makedirs('output', exist_ok=True)
    progress_log = 'output/gs_txt_batch_progress.log'
    suffix = f'_debug{args.debug}' if args.debug > 0 else '_full'
    report_interval = REPORT_INTERVAL  # 每 N 个输出一次

    print(f'\n[2] 串行回测 (TDX 不支持并发)...', flush=True)

    for i, (fname, title, header) in enumerate(all_files, 1):
        formula = extract_formula_name(fname)
        elapsed_total = time.time() - t_start
        speed = i / max(elapsed_total, 1) * 60 if elapsed_total > 0 else 0
        eta_min = (n_total - i) / max(speed, 0.01) if speed > 0 else 999
        print(f'\n  [{i}/{n_total}] {fname} → [{formula}] '
              f'(header={header}, ~{speed:.1f}/min, ETA {eta_min:.0f}min)', flush=True)

        r = run_one(formula, START, END)
        r['file'] = fname
        r['formula'] = formula
        r['header'] = header
        r['title'] = title
        all_results.append(r)
        n_done += 1

        if r['status'] == 'ok':
            ok_results.append(r)
            print(f'    OK {r["signals"]}sig {r["stocks"]}stk {r["trades"]}trd '
                  f'{r["cumret"]*100:+.2f}% ann={r["annret"]*100:+.2f}% '
                  f'dd={r["maxdd"]*100:.2f}% wr={r["winrate"]*100:.1f}%', flush=True)
        elif r['status'] == 'too_many_signals':
            print(f'    TOOMANY {r["signals"]}sig', flush=True)
        elif r['status'] == 'selection_timeout':
            print(f'    TIMEOUT (>90s)', flush=True)
        elif r['status'] == 'no_signals':
            print(f'    X no_signals', flush=True)
        else:
            print(f'    X {r["status"]}: {r.get("msg","")}', flush=True)

        # 每 N 个更新一次进度日志 + 增量 MD
        if i % report_interval == 0 or i == n_total:
            elapsed = time.time() - t_start
            with open(progress_log, 'w', encoding='utf-8') as log_f:
                for j, rr in enumerate(all_results, 1):
                    if rr['status'] == 'ok':
                        log_f.write(f'{j},{rr["formula"]},{rr["header"]},ok,{rr["signals"]},'
                                    f'{rr["stocks"]},{rr["trades"]},{rr["cumret"]:.4f},'
                                    f'{rr["annret"]:.4f},{rr["maxdd"]:.4f},{rr["sharpe"]:.2f},'
                                    f'{rr["winrate"]:.4f}\n')
                    else:
                        log_f.write(f'{j},{rr["formula"]},{rr["header"]},{rr["status"]}\n')

            with open(f'output/gs_batch{suffix}.json', 'w', encoding='utf-8') as f:
                json.dump({
                    'config': {'start': START, 'end': END, 'timeout': TIMEOUT, 'total': n_total},
                    'summary': {'done': len(all_results), 'ok': len(ok_results),
                                'elapsed_s': round(elapsed, 1)},
                    'all_results': all_results,
                }, f, ensure_ascii=False, indent=2, default=str)

            out_md = write_md(ok_results, all_results, n_total, elapsed, suffix)
            print(f'    [增量报告] {out_md}', flush=True)

    # 全部完成后最终报告
    elapsed_total = time.time() - t_start
    ok_count = len(ok_results)
    fail_count = len([r for r in all_results if r['status'] not in ('ok', 'too_many_signals')])
    skip_count = len([r for r in all_results if r['status'] == 'too_many_signals'])
    timeout_count = len([r for r in all_results if r['status'] == 'selection_timeout'])

    suffix = f'_debug{args.debug}' if args.debug > 0 else '_full'
    out_md = write_md(ok_results, all_results, n_total, elapsed_total, suffix)
    print(f'\n{"=" * 80}')
    print(f'  全部完成 | OK={ok_count} FAIL={fail_count} SKIP={skip_count} TIMEOUT={timeout_count}')
    print(f'  用时 {elapsed_total:.0f}s ({elapsed_total/60:.1f}min)')
    print(f'  报告: {out_md}')
    print('=' * 80)

    # 最终 CSV
    ok_sorted = sorted(ok_results,
                       key=lambda r: r.get('annret') if r.get('annret') is not None else -999,
                       reverse=True)
    out_csv = f'output/gs_batch{suffix}.csv'
    with open(out_csv, 'w', encoding='utf-8-sig') as f:
        f.write('rank,file,header,formula,title,signals,stocks,trades,cumret,annret,maxdd,sharpe,winrate,ladder6,ladder15\n')
        for rank, r in enumerate(ok_sorted, 1):
            f.write(f'{rank},"{r["file"]}",{r["header"]},"{r["formula"]}","{r["title"]}",'
                    f'{r.get("signals","")},{r.get("stocks","")},{r.get("trades","")},'
                    f'{r.get("cumret",0):.4f},{r.get("annret",0):.4f},{r.get("maxdd",0):.4f},'
                    f'{r.get("sharpe",0):.2f},{r.get("winrate",0):.4f},'
                    f'{r.get("ladder6",0)},{r.get("ladder15",0)}\n')
    print(f'  CSV: {out_csv}')


if __name__ == '__main__':
    main()
