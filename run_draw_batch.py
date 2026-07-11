"""含绘图函数(不含 POLYLINE)公式批量回测 (2026-07-08)

从 _gs_scan_out/draw_no_polyline_picks.json 读 489 个公式,
逐个 subprocess 调 _gs_run_one.py, 串行跑(TDX 不支持并发),
输出 output/gs_batch_draw.{md,csv,json}。

用法:
    python run_draw_batch.py --debug 3      # 先跑 3 个验证
    python run_draw_batch.py                # 跑全量 489 个
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# === 配置 ===
PICKS_PATH = Path('_gs_scan_out/draw_no_polyline_picks.json')
START = '20260101'
END = '20260708'           # 今天 (2026-07-08), 用户要"26.1.1 开始"至今
TIMEOUT = 150
REPORT_INTERVAL = 10       # 每 10 个写一次增量报告
SUFFIX = '_draw'


def extract_formula_name(filename: str) -> str:
    base = filename.replace('.txt', '')
    m = re.match(r'^gs_\d+_(.+)$', base)
    name = m.group(1) if m else base
    return name.strip()


def load_picks() -> list[dict]:
    picks = json.loads(PICKS_PATH.read_text(encoding='utf-8'))
    for p in picks:
        p['formula'] = extract_formula_name(p['file'])
    return picks


def run_one(formula: str, start: str, end: str, timeout: int = TIMEOUT) -> dict:
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_gs_run_one.py')
    try:
        r = subprocess.run(
            [sys.executable, '-X', 'utf8', script, formula, start, end],
            capture_output=True, text=True, timeout=timeout, encoding='utf-8',
        )
    except subprocess.TimeoutExpired:
        return {'status': 'selection_timeout'}

    out = r.stdout or ''
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


def write_md(ok_results, all_results, n_total, elapsed, suffix=SUFFIX):
    out_md = f'output/gs_batch{suffix}.md'
    ok_count = len(ok_results)
    fail_count = len([r for r in all_results
                      if r['status'] not in ('ok', 'too_many_signals')])
    skip_count = len([r for r in all_results if r['status'] == 'too_many_signals'])
    timeout_count = len([r for r in all_results if r['status'] == 'selection_timeout'])
    no_sig_count = len([r for r in all_results if r['status'] == 'no_signals'])

    ok_sorted = sorted(
        ok_results,
        key=lambda r: r.get('annret') if r.get('annret') is not None else -999,
        reverse=True,
    )

    with open(out_md, 'w', encoding='utf-8') as f:
        f.write('# 含绘图函数公式批量回测 (2026-07-08)\n\n')
        f.write(f'- **区间**: {START} ~ {END} (日线)\n')
        f.write(f'- **股票池**: 系统默认 type=50 (沪深A股全市场)\n')
        f.write(f'- **买入**: 信号日收盘价\n')
        f.write(f'- **止损止盈**: 移动止损3.5%/1% + 阶梯6%/15% + 时间20天 + 硬止损-12%\n')
        f.write(f'- **筛选**: 含绘图函数(DRAWICON/STICKLINE/DRAWTEXT等) 且 不含 POLYLINE\n')
        f.write(f'- **总公式**: {n_total}  **已测**: {len(all_results)}  **成功**: {ok_count}  '
                f'**无信号**: {no_sig_count}  **信号超限**: {skip_count}  '
                f'**超时**: {timeout_count}  **失败**: {fail_count}\n')
        f.write(f'- **用时**: {elapsed:.0f}s ({elapsed/60:.1f}min)\n\n')

        f.write('## 排名 (按年化收益降序)\n\n')
        f.write('| 排名 | 文件 | 公式名 | 信号 | 股票 | 交易 | 累计 | 年化 | 回撤 | 夏普 | 胜率 | 6%档 | 15%档 |\n')
        f.write('|----:|------|------|----:|----:|----:|-----:|-----:|-----:|-----:|-----:|----:|----:|\n')
        for rank, r in enumerate(ok_sorted, 1):
            f.write(f'| {rank} | `{r["file"]}` | {r["formula"]} '
                    f'| {r.get("signals","")} | {r.get("stocks","")} | {r.get("trades","")} '
                    f'| {r.get("cumret",0)*100:+.2f}% | {r.get("annret",0)*100:+.2f}% '
                    f'| {r.get("maxdd",0)*100:.2f}% | {r.get("sharpe",0):.2f} '
                    f'| {r.get("winrate",0)*100:.1f}% '
                    f'| {r.get("ladder6",0)} | {r.get("ladder15",0)} |\n')

        failed = [r for r in all_results if r['status'] != 'ok']
        if failed:
            f.write(f'\n## 无信号/超限/超时/失败 ({len(failed)} 个)\n\n')
            for r in failed:
                f.write(f'- `{r["file"]}` → [{r["formula"]}]: {r["status"]}'
                        f'{(" — " + r.get("msg","")) if r.get("msg") else ""}\n')

    return out_md


def main():
    ap = argparse.ArgumentParser(description='含绘图函数公式批量回测')
    ap.add_argument('--debug', type=int, default=0, help='只跑前 N 个 (0=全量)')
    args = ap.parse_args()

    t_start = time.time()
    print('=' * 80)
    print('  VERA 含绘图函数公式批量回测')
    print(f'  区间: {START} ~ {END} (日线)  超时: {TIMEOUT}s')
    print('=' * 80)

    picks = load_picks()
    print(f'\n[1] 载入清单: {len(picks)} 个', flush=True)
    if args.debug > 0:
        picks = picks[:args.debug]
        print(f'  DEBUG 模式: 只跑前 {len(picks)} 个', flush=True)

    n_total = len(picks)
    all_results: list[dict] = []
    ok_results: list[dict] = []
    os.makedirs('output', exist_ok=True)
    progress_log = f'output/gs_batch{SUFFIX}_progress.log'

    print(f'\n[2] 串行回测 (TDX 不支持并发)...', flush=True)
    for i, p in enumerate(picks, 1):
        fname = p['file']
        formula = p['formula']
        elapsed_total = time.time() - t_start
        speed = i / max(elapsed_total, 1) * 60 if elapsed_total > 0 else 0
        eta_min = (n_total - i) / max(speed, 0.01) if speed > 0 else 999
        print(f'\n  [{i}/{n_total}] {fname} → [{formula}] '
              f'(~{speed:.1f}/min, ETA {eta_min:.0f}min)', flush=True)

        r = run_one(formula, START, END)
        r['file'] = fname
        r['formula'] = formula
        all_results.append(r)

        if r['status'] == 'ok':
            ok_results.append(r)
            print(f'    OK {r["signals"]}sig {r["stocks"]}stk {r["trades"]}trd '
                  f'{r["cumret"]*100:+.2f}% ann={r["annret"]*100:+.2f}% '
                  f'dd={r["maxdd"]*100:.2f}% wr={r["winrate"]*100:.1f}%', flush=True)
        elif r['status'] == 'too_many_signals':
            print(f'    TOOMANY {r["signals"]}sig', flush=True)
        elif r['status'] == 'selection_timeout':
            print(f'    TIMEOUT (>{TIMEOUT}s)', flush=True)
        elif r['status'] == 'no_signals':
            print(f'    — no_signals', flush=True)
        else:
            print(f'    X {r["status"]}: {r.get("msg","")}', flush=True)

        if i % REPORT_INTERVAL == 0 or i == n_total:
            elapsed = time.time() - t_start
            with open(progress_log, 'w', encoding='utf-8') as log_f:
                for j, rr in enumerate(all_results, 1):
                    if rr['status'] == 'ok':
                        log_f.write(
                            f'{j},{rr["formula"]},ok,{rr["signals"]},'
                            f'{rr["stocks"]},{rr["trades"]},{rr["cumret"]:.4f},'
                            f'{rr["annret"]:.4f},{rr["maxdd"]:.4f},'
                            f'{rr["sharpe"]:.2f},{rr["winrate"]:.4f}\n')
                    else:
                        log_f.write(f'{j},{rr["formula"]},{rr["status"]}\n')

            with open(f'output/gs_batch{SUFFIX}.json', 'w', encoding='utf-8') as f:
                json.dump({
                    'config': {'start': START, 'end': END,
                               'timeout': TIMEOUT, 'total': n_total},
                    'summary': {'done': len(all_results), 'ok': len(ok_results),
                                'elapsed_s': round(elapsed, 1)},
                    'all_results': all_results,
                }, f, ensure_ascii=False, indent=2, default=str)

            out_md = write_md(ok_results, all_results, n_total, elapsed)
            print(f'    [增量报告] {out_md}', flush=True)

    elapsed_total = time.time() - t_start
    out_md = write_md(ok_results, all_results, n_total, elapsed_total)
    ok_sorted = sorted(ok_results,
                       key=lambda r: r.get('annret') if r.get('annret') is not None else -999,
                       reverse=True)
    out_csv = f'output/gs_batch{SUFFIX}.csv'
    with open(out_csv, 'w', encoding='utf-8-sig') as f:
        f.write('rank,file,formula,signals,stocks,trades,cumret,annret,'
                'maxdd,sharpe,winrate,ladder6,ladder15\n')
        for rank, r in enumerate(ok_sorted, 1):
            f.write(f'{rank},"{r["file"]}","{r["formula"]}",'
                    f'{r.get("signals","")},{r.get("stocks","")},{r.get("trades","")},'
                    f'{r.get("cumret",0):.4f},{r.get("annret",0):.4f},'
                    f'{r.get("maxdd",0):.4f},{r.get("sharpe",0):.2f},'
                    f'{r.get("winrate",0):.4f},{r.get("ladder6",0)},{r.get("ladder15",0)}\n')

    ok_count = len(ok_results)
    print(f'\n{"=" * 80}')
    print(f'  完成 | OK={ok_count}  用时 {elapsed_total:.0f}s ({elapsed_total/60:.1f}min)')
    print(f'  MD:  {out_md}')
    print(f'  CSV: {out_csv}')
    print('=' * 80)


if __name__ == '__main__':
    main()
