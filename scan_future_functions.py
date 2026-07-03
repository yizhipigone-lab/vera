"""
扫描 gs_txt 全部公式的未来函数/绘图函数/动态函数
输出: output/gs_future_scan.md + .csv
"""
import os, re, csv, json
from collections import Counter

DIR = r'E:\NEW_TDX\T0001\export\gs_txt'

# 未来函数 (TDX 官方 + 常见)
FUTURE_KW = ['ZIG','PEAK','TROUGH','PEAKBARS','TROUGHBARS','FLATZIG','FLATZIGA',
             'ZIGA','PEAKA','TROUGHA','FFT','BACKSET','REFX','REFXV','TROUGHBARS',
             'PEAKBARS','LAST','HHV','LLV']
# 真正的未来函数 (会污染信号) — HHV/LLV 不是未来函数, 移除
FUTURE_KW = ['ZIG','PEAK','TROUGH','PEAKBARS','TROUGHBARS','FLATZIG','FLATZIGA',
             'ZIGA','PEAKA','TROUGHA','FFT','BACKSET','REFX','REFXV']

# 绘图函数
DRAW_KW = ['DRAWICON','DRAWTEXT','DRAWBMP','DRAWLINE','DRAWKLINE','DRAWBAND',
           'DRAWNUMBER','DRAWRECTREL','STICKLINE','DRAWSL','DRAWGBK','PLOYLINE',
           'PARTLINE','VERTLINE','DRAWTEXT_FIX','DRAWNUMBER_FIX','DRAWNULL','NDRAW']

# 动态行情函数 (盘中会变, 回测不准)
DYNAMIC_KW = ['DCLOSE','DVOL','DOPEN','DHIGH','DLOW','DYNAINFO']

# 其他可疑 (基本面/代码过滤, 不一定是未来但影响可复现性)
OTHER_KW = ['CAPITAL','FINANCE','NAMELIKE','CODELIKE']


def scan_file(fname):
    try:
        with open(os.path.join(DIR, fname), 'rb') as f:
            text = f.read().decode('gbk', errors='replace')
        body = text.split('Source Code:', 1)[-1] if 'Source Code:' in text else text
        body = re.sub(r'\{[^}]*\}', '', body)  # 去注释
        # 第1行标题 + 第2行 header
        lines_raw = text.split('\n')
        title = lines_raw[0].strip() if lines_raw else ''
        header = lines_raw[1].strip() if len(lines_raw) > 1 else ''

        hits = {'future': [], 'draw': [], 'dynamic': [], 'other': []}
        for kw in FUTURE_KW:
            if re.search(r'\b' + kw + r'\b', body):
                hits['future'].append(kw)
        for kw in DRAW_KW:
            if re.search(r'\b' + kw + r'\b', body):
                hits['draw'].append(kw)
        for kw in DYNAMIC_KW:
            if re.search(r'\b' + kw + r'\b', body):
                hits['dynamic'].append(kw)
        for kw in OTHER_KW:
            if re.search(r'\b' + kw + r'\b', body):
                hits['other'].append(kw)
        return title, header, hits
    except Exception as e:
        return '?', '?', {'future': [], 'draw': [], 'dynamic': [], 'other': [], 'err': str(e)}


def main():
    files = sorted([f for f in os.listdir(DIR)
                    if f.startswith('gs_') and f.endswith('.txt') and 'GUPIAO' not in f])
    rows = []
    for fn in files:
        title, header, hits = scan_file(fn)
        is_clean = (not hits['future'] and not hits['draw']
                    and not hits['dynamic'] and not hits.get('err'))
        rows.append({
            'file': fn, 'title': title, 'header': header,
            'future': ','.join(hits['future']),
            'draw': ','.join(hits['draw']),
            'dynamic': ','.join(hits['dynamic']),
            'other': ','.join(hits['other']),
            'clean': '是' if is_clean else '否',
        })

    # 统计
    n_clean = sum(1 for r in rows if r['clean'] == '是')
    n_future = sum(1 for r in rows if r['future'])
    n_draw = sum(1 for r in rows if r['draw'])
    n_dyn = sum(1 for r in rows if r['dynamic'])
    print(f'总 {len(rows)} 个')
    print(f'  全干净: {n_clean}')
    print(f'  有未来函数: {n_future}')
    print(f'  有绘图函数: {n_draw}')
    print(f'  有动态函数: {n_dyn}')

    # CSV
    with open('output/gs_future_scan.csv', 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['file','title','header','clean','future','draw','dynamic','other'])
        w.writeheader()
        for r in rows: w.writerow(r)

    # MD
    with open('output/gs_future_scan.md', 'w', encoding='utf-8') as f:
        f.write('# gs_txt 公式未来函数扫描报告\n\n')
        f.write(f'- **总数**: {len(rows)}\n')
        f.write(f'- **全干净**: {n_clean}\n')
        f.write(f'- **有未来函数**: {n_future} (回测不可信)\n')
        f.write(f'- **有绘图函数**: {n_draw} (需检查是否污染信号)\n')
        f.write(f'- **有动态函数**: {n_dyn} (DCLOSE/DVOL/DYNAINFO, 回测不准)\n\n')
        f.write('## 有未来函数的公式 (回测成绩不可信)\n\n| 文件 | 标题 | 未来函数 |\n|---|---|---|\n')
        for r in rows:
            if r['future']:
                f.write(f'| `{r["file"]}` | {r["title"]} | {r["future"]} |\n')
        f.write('\n## 有绘图函数的公式 (需人工确认是否污染信号)\n\n| 文件 | 标题 | 绘图函数 |\n|---|---|---|\n')
        for r in rows:
            if r['draw'] and not r['future']:
                f.write(f'| `{r["file"]}` | {r["title"]} | {r["draw"]} |\n')
        f.write('\n## 有动态函数的公式 (DCLOSE/DVOL/DYNAINFO)\n\n| 文件 | 标题 | 动态函数 |\n|---|---|---|\n')
        for r in rows:
            if r['dynamic']:
                f.write(f'| `{r["file"]}` | {r["title"]} | {r["dynamic"]} |\n')
    print('保存: output/gs_future_scan.md + .csv')


if __name__ == '__main__':
    main()
