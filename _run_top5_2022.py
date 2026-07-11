"""Top9-13 公式长周期回测 (2022-01-01 至今)

验证含绘图批次里年化合理的 5 个公式, 在 2022-2026 完整周期(含熊市)的表现。
"""
import subprocess
import sys
import json
import time
from pathlib import Path

# Top15 排名 9-13 (来自 output/gs_batch_draw.md)
PICKS = [
    ('gs_0_黑马起步.txt', '黑马起步'),
    ('gs_1_突破预警.txt', '突破预警'),
    ('gs_1_财神.txt', '财神'),
    ('gs_1_暴利出现.txt', '暴利出现'),
    ('gs_0_短线黑马.txt', '短线黑马'),
]
START = '20220101'
END = '20260708'
TIMEOUT = 300  # 长区间选股更慢, 放宽到 300s


def run_one(formula):
    try:
        r = subprocess.run(
            [sys.executable, '-X', 'utf8', '_gs_run_one.py', formula, START, END],
            capture_output=True, text=True, timeout=TIMEOUT, encoding='utf-8',
        )
    except subprocess.TimeoutExpired:
        return {'status': 'selection_timeout'}
    out = r.stdout or ''
    for line in out.split('\n'):
        line = line.strip()
        if line.startswith('{') and '"status"' in line:
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass
    return {'status': 'error', 'msg': f'no json: {(r.stderr or "")[:100]}'}


def main():
    t0 = time.time()
    results = []
    print(f'区间: {START} ~ {END}  公式数: {len(PICKS)}\n')
    for i, (fname, formula) in enumerate(PICKS, 1):
        print(f'[{i}/{len(PICKS)}] {fname} → [{formula}]', flush=True)
        r = run_one(formula)
        r['file'] = fname
        r['formula'] = formula
        results.append(r)
        if r['status'] == 'ok':
            print(f'  OK {r["signals"]}sig {r["stocks"]}stk {r["trades"]}trd '
                  f'累计{r["cumret"]*100:+.2f}% 年化{r["annret"]*100:+.2f}% '
                  f'回撤{r["maxdd"]*100:.2f}% 夏普{r["sharpe"]:.2f} '
                  f'胜率{r["winrate"]*100:.1f}%  6%档{r["ladder6"]} 15%档{r["ladder15"]}',
                  flush=True)
        else:
            print(f'  {r["status"]}: {r.get("msg","")}', flush=True)

    # 写 MD 报告
    elapsed = time.time() - t0
    out_md = Path('output/top5_2022.md')
    ok = [r for r in results if r['status'] == 'ok']
    ok_sorted = sorted(ok, key=lambda r: r.get('annret', -999), reverse=True)
    with out_md.open('w', encoding='utf-8') as f:
        f.write(f'# Top9-13 公式长周期回测 (2022-01-01 ~ {END})\n\n')
        f.write(f'- **区间**: {START} ~ {END} (日线, 含2022熊市+2023震荡+2024-26牛市)\n')
        f.write(f'- **股票池**: 沪深A股全市场\n')
        f.write(f'- **买入**: 信号日收盘价\n')
        f.write(f'- **止损止盈**: 移动3.5%/1% + 阶梯6%/15% + 时间20天 + 硬止损-12%\n')
        f.write(f'- **公式数**: {len(PICKS)}  OK: {len(ok)}  用时: {elapsed:.0f}s\n\n')
        f.write('| 排名 | 文件 | 公式 | 信号 | 股票 | 交易 | 累计 | 年化 | 回撤 | 夏普 | 胜率 | 6%档 | 15%档 |\n')
        f.write('|----:|------|------|----:|----:|----:|-----:|-----:|-----:|-----:|-----:|----:|----:|\n')
        for rank, r in enumerate(ok_sorted, 1):
            f.write(f'| {rank} | `{r["file"]}` | {r["formula"]} '
                    f'| {r.get("signals","")} | {r.get("stocks","")} | {r.get("trades","")} '
                    f'| {r.get("cumret",0)*100:+.2f}% | {r.get("annret",0)*100:+.2f}% '
                    f'| {r.get("maxdd",0)*100:.2f}% | {r.get("sharpe",0):.2f} '
                    f'| {r.get("winrate",0)*100:.1f}% '
                    f'| {r.get("ladder6",0)} | {r.get("ladder15",0)} |\n')
        failed = [r for r in results if r['status'] != 'ok']
        if failed:
            f.write(f'\n## 未成功 ({len(failed)})\n\n')
            for r in failed:
                f.write(f'- `{r["file"]}` → {r["status"]}: {r.get("msg","")}\n')
    print(f'\n报告: {out_md}')


if __name__ == '__main__':
    main()
