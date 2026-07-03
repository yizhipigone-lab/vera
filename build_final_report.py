"""
最终汇总报告 (先生 2026-07-03)

应用先生指示:
  - 跳过有未来/绘图/动态函数的公式
  - 整合 第1次跑 + 第2次重跑 的结果 (重跑覆盖)
  - 输出最终排名 MD/CSV
"""
import csv, re, json
from collections import Counter

# 1. 读 scan
scan = {}
with open('output/gs_future_scan.csv', encoding='utf-8-sig') as f:
    for r in csv.DictReader(f):
        if 'GUPIAO' in r['file']: continue
        if r['header'].startswith('15,2'): continue
        scan[r['file']] = r

def has_dirty(s):
    return bool(s.get('future') or s.get('draw') or s.get('dynamic'))

# 2. 读第1次跑结果
results1 = {}
with open('output/gs_txt_batch_progress.log', encoding='utf-8') as f:
    for line in f:
        m = re.match(r'^\d+,(.+?),([0-9]+,[0-9]+),(ok|too_many_signals|no_signals|selection_timeout|error)(.*)$',
                     line.strip())
        if not m: continue
        formula, header, st, rest = m.group(1), m.group(2), m.group(3), m.group(4)
        fn = f'gs_1_{formula}.txt'
        rec = {'file': fn, 'formula': formula, 'header': header, 'status': st}
        if st == 'ok':
            parts = [p for p in rest.strip(',').split(',') if p]
            if len(parts) >= 7:
                try:
                    rec.update({
                        'signals': int(parts[0]), 'stocks': int(parts[1]), 'trades': int(parts[2]),
                        'cumret': float(parts[3]), 'annret': float(parts[4]),
                        'maxdd': float(parts[5]), 'sharpe': float(parts[6]),
                        'winrate': float(parts[7]) if len(parts) > 7 else 0,
                    })
                except ValueError:
                    continue
        results1[fn] = rec

# 3. 读第2次重跑结果 (覆盖)
results2 = {}
with open('output/gs_rerun_progress.log', encoding='utf-8') as f:
    for line in f:
        m = re.match(r'^(.+?),([0-9]+,[0-9]+),(ok|too_many_signals|no_signals|selection_timeout|error)(.*)$',
                     line.strip())
        if not m: continue
        formula, header, st, rest = m.group(1), m.group(2), m.group(3), m.group(4)
        fn = f'gs_1_{formula}.txt'
        rec = {'file': fn, 'formula': formula, 'header': header, 'status': st}
        if st == 'ok':
            parts = [p for p in rest.strip(',').split(',') if p]
            if len(parts) >= 7:
                try:
                    rec.update({
                        'signals': int(parts[0]), 'stocks': int(parts[1]), 'trades': int(parts[2]),
                        'cumret': float(parts[3]), 'annret': float(parts[4]),
                        'maxdd': float(parts[5]), 'sharpe': float(parts[6]),
                        'winrate': float(parts[7]) if len(parts) > 7 else 0,
                    })
                except ValueError:
                    continue
        results2[fn] = rec

# 4. 合并 (重跑覆盖第1次)
all_results = {**results1, **results2}

# 5. 应用过滤: 剔除有问题函数
ok_clean = []  # 干净公式的成功回测
dirty_ok = []  # 有问题公式的成功回测（参考，不入排名）
for fn, r in all_results.items():
    s = scan.get(fn, {})
    if has_dirty(s):
        if r['status'] == 'ok':
            r['dirty_reason'] = f"future={s['future']} draw={s['draw']} dyn={s['dynamic']}"
            dirty_ok.append(r)
        continue
    if r['status'] == 'ok':
        ok_clean.append(r)

# 6. 排序
ok_clean.sort(key=lambda r: r.get('annret', -999), reverse=True)

# 7. 输出
out_md = 'output/gs_final_clean.md'
with open(out_md, 'w', encoding='utf-8') as f:
    f.write('# gs_txt 公式批量回测 — 最终排名 (先生 2026-07-03)\n\n')
    f.write('## 应用规则\n')
    f.write('- 区间: 2026.01.01 ~ 2026.12.31\n')
    f.write('- 排除: GUPIAO 前缀 + 文件头 15,2\n')
    f.write('- **剔除标准 (先生定)**: 公式源代码含 未来函数(ZIG/BACKSET等) / 绘图函数(DRAWLINE等) / 动态函数(DCLOSE/DVOL/DYNAINFO) — 回测结果不可信\n')
    f.write('- 信号上限: 50000\n')
    f.write('- 超时: 150 秒\n\n')
    f.write('## 汇总\n')
    f.write(f'- 321 个总公式 → 剔除 104 个有问题 → **217 个干净公式**\n')
    f.write(f'- 217 个干净公式中: 成功回测 **{len(ok_clean)}** / 无信号 {sum(1 for fn,r in all_results.items() if not has_dirty(scan.get(fn,{})) and r["status"]=="no_signals")} / 信号超限 {sum(1 for fn,r in all_results.items() if not has_dirty(scan.get(fn,{})) and r["status"]=="too_many_signals")}\n\n')

    f.write('## 排名 (按年化收益降序, 仅含干净公式)\n\n')
    f.write('| 排名 | 文件 | 标题 | 文件头 | 信号 | 股票 | 交易 | 累计 | 年化 | 回撤 | 夏普 | 胜率 |\n')
    f.write('|----:|------|------|------|----:|----:|----:|-----:|-----:|-----:|-----:|-----:|\n')
    for rank, r in enumerate(ok_clean, 1):
        f.write(f'| {rank} | `{r["file"]}` | {scan.get(r["file"],{}).get("title","")} | {r["header"]} '
                f'| {r.get("signals","")} | {r.get("stocks","")} | {r.get("trades","")} '
                f'| {r.get("cumret",0)*100:+.2f}% | {r.get("annret",0)*100:+.2f}% '
                f'| {r.get("maxdd",0)*100:.2f}% | {r.get("sharpe",0):.2f} | {r.get("winrate",0)*100:.1f}% |\n')

    f.write(f'\n## 被剔除的公式 (104 个, 回测成绩不参考)\n\n')
    f.write('| 文件 | 标题 | 文件头 | 未来函数 | 绘图函数 | 动态函数 |\n')
    f.write('|---|---|---|---|---|---|\n')
    dirty_all = [scan[fn] for fn in scan if has_dirty(scan[fn])]
    dirty_all.sort(key=lambda s: s['file'])
    for s in dirty_all:
        f.write(f'| `{s["file"]}` | {s["title"]} | {s["header"]} '
                f'| {s["future"] or "-"} | {s["draw"] or "-"} | {s["dynamic"] or "-"} |\n')

print(f'干净公式 OK 数: {len(ok_clean)}')
print(f'有问题公式 OK 数: {len(dirty_ok)}')
print(f'最终报告: {out_md}')

# JSON
with open('output/gs_final_clean.json', 'w', encoding='utf-8') as f:
    json.dump({
        'config': {'interval': '20260101~20261231', 'rule': 'skip future/draw/dynamic functions'},
        'clean_ok_count': len(ok_clean),
        'top_results': ok_clean[:30],
    }, f, ensure_ascii=False, indent=2, default=str)

# CSV
out_csv = 'output/gs_final_clean.csv'
with open(out_csv, 'w', encoding='utf-8-sig') as f:
    w = csv.DictWriter(f, fieldnames=['rank','file','header','title','signals','stocks','trades','cumret','annret','maxdd','sharpe','winrate'])
    w.writeheader()
    for rank, r in enumerate(ok_clean, 1):
        w.writerow({
            'rank': rank, 'file': r['file'], 'header': r['header'],
            'title': scan.get(r['file'],{}).get('title',''),
            'signals': r.get('signals',''), 'stocks': r.get('stocks',''), 'trades': r.get('trades',''),
            'cumret': f'{r.get("cumret",0):.4f}',
            'annret': f'{r.get("annret",0):.4f}',
            'maxdd': f'{r.get("maxdd",0):.4f}',
            'sharpe': f'{r.get("sharpe",0):.2f}',
            'winrate': f'{r.get("winrate",0):.4f}',
        })
print(f'CSV: {out_csv}')
