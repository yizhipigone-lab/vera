"""先生汇总 gs_txt 干净公式真实状态"""
import csv, re
from collections import Counter

clean = []
with open('output/gs_future_scan.csv', encoding='utf-8-sig') as f:
    for r in csv.DictReader(f):
        if 'GUPIAO' in r['file']: continue
        if r['header'].startswith('15,2'): continue
        if not (r.get('future') or r.get('draw') or r.get('dynamic')):
            clean.append(r['file'])

final = {}
for line in open('output/gs_txt_batch_progress.log', encoding='utf-8'):
    m = re.match(r'^\d+,(.+?),([0-9]+,[0-9]+),([a-z_]+)(,|$)', line.strip())
    if m:
        fn = 'gs_1_' + m.group(1) + '.txt'
        if fn in clean:
            final[fn] = m.group(3)
for line in open('output/gs_rerun_progress.log', encoding='utf-8'):
    m = re.match(r'^(.+?),([0-9]+,[0-9]+),([a-z_]+)(,|$)', line.strip())
    if m:
        fn = 'gs_1_' + m.group(1) + '.txt'
        if fn in clean:
            final[fn] = m.group(3)

cnt = Counter(final.values())
print(f'干净公式 217 个全部跑过, 状态汇总:')
for k, v in cnt.most_common():
    print(f'  {v:4d}  {k}')
print(f'  ---  ---')
print(f'  {sum(cnt.values()):4d}  合计')

err_samples = [fn for fn, st in final.items() if st in ('error', 'selection_timeout')][:10]
print('\nerror/timeout 样例:')
for fn in err_samples:
    with open('E:/NEW_TDX/T0001/export/gs_txt/' + fn, 'rb') as f:
        title = f.readline().decode('gbk', errors='replace').strip()
    print(f'  {fn:35s} [{title}]')

# 保存真实汇总 MD
with open('output/gs_final_clean_v2.md', 'w', encoding='utf-8') as f:
    f.write('# gs_txt 干净公式真实汇总 (217 全部跑过)\n\n')
    f.write('## 汇总\n\n')
    f.write('| 状态 | 数量 | 含义 |\n|---|---:|---|\n')
    states = [
        ('ok', '成功跑出回测 (可信)'),
        ('no_signals', '无信号 (TDX 选不到)'),
        ('too_many_signals', '信号 > 50000 (刷信号公式)'),
        ('error', '跑选股/回测失败 (子进程 error)'),
        ('selection_timeout', '超时 (150s 不够)'),
    ]
    for st, desc in states:
        f.write(f'| {st} | {cnt.get(st, 0)} | {desc} |\n')
    f.write(f'| **合计** | **{sum(cnt.values())}** | |\n')
    f.write('\n## 错误/超时公式样例 (重跑可能就成功, 也可能本来就跑不出)\n\n')
    f.write('| 文件 | 标题 |\n|---|---|\n')
    for fn in err_samples:
        try:
            title = open('E:/NEW_TDX/T0001/export/gs_txt/' + fn, 'rb').readline().decode('gbk', errors='replace').strip()
        except:
            title = '?'
        f.write(f'| `{fn}` | {title} |\n')
print('\n生成: output/gs_final_clean_v2.md')
