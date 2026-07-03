"""
合并 e:/1target/gongshi/tni_output/ 下所有 .tni 文件，
生成 all_468_formulas.tni 供通达信一次性导入。

TDX .tni 格式：每个公式以 [FORMULA] 起头，Name/Type/Desc/Param 各一行，
然后是 TDX 公式代码，最后空行分隔。
"""
import os
import re

GONGSHI_DIR = r'E:\1target\gongshi'
TNI_SUBDIR = os.path.join(GONGSHI_DIR, 'tni_output')
MD_SUBDIR = GONGSHI_DIR
OUT_PATH = os.path.join(GONGSHI_DIR, 'all_468_formulas.tni')

# 读所有 .md 拿到 title 和 url
md_files = sorted([f for f in os.listdir(MD_SUBDIR) if f.endswith('.md')])
md_info = {}  # filename -> (title, url)
for fname in md_files:
    with open(os.path.join(MD_SUBDIR, fname), 'r', encoding='utf-8') as f:
        content = f.read()
    title_m = re.search(r'^#\s*(.+?)(?:之选股指标公式)?\s*$', content, re.MULTILINE)
    title = title_m.group(1).strip() if title_m else fname.replace('.md', '')
    url_m = re.search(r'来源:\s*(\S+)', content)
    url = url_m.group(1) if url_m else ''
    md_info[fname.replace('.md', '')] = (title, url)

# 合并所有 .tni
tni_files = sorted([f for f in os.listdir(TNI_SUBDIR) if f.endswith('.tni')])
print(f'找到 {len(tni_files)} 个 .tni 文件')

count = 0
with open(OUT_PATH, 'w', encoding='gbk') as out:
    out.write('; 合并自 e:/1target/gongshi/tni_output/ 下的所有 .tni 文件\n')
    out.write('; 导入方法：通达信 → 功能 → 公式系统 → 公式管理器 → 条件选股 → 导入公式\n')
    out.write(f'; 公式数量: {len(tni_files)}\n\n')
    for fname in tni_files:
        base = fname.replace('.tni', '')
        title, url = md_info.get(base, (base, ''))
        with open(os.path.join(TNI_SUBDIR, fname), 'r', encoding='utf-8') as f:
            tni_content = f.read()
        # 在 Desc 行加上 url (如果还没有)
        if url and 'Desc=' in tni_content and 'gupang.com' not in tni_content.split('Desc=')[1].split('\n')[0]:
            tni_content = re.sub(
                r'(Desc=)[^\n]*',
                f'\\1{url}',
                tni_content,
                count=1,
            )
        # 把 Name 改成短名 GS0000 等
        short_name = f'GS{count:04d}'
        tni_content = re.sub(r'^Name=.*$', f'Name={short_name}', tni_content, flags=re.MULTILINE, count=1)
        out.write(tni_content)
        out.write('\n')
        count += 1

print(f'已生成: {OUT_PATH}')
print(f'包含: {count} 个公式')
print(f'文件大小: {os.path.getsize(OUT_PATH):,} bytes')
print()
print('导入步骤:')
print('  1. 打开通达信 → 功能 → 公式系统 → 公式管理器')
print('  2. 条件选股 → 导入公式')
print(f'  3. 选择: {OUT_PATH}')
print('  4. 全选 → 导入')
print()
print(f'导入后, 公式名称为 GS0000 ~ GS{count-1:04d}')
print()
print('我们的样本公式对应名:')
samples = [
    'MACD空中加油之选股指标公式',
    'V反转主图之选股指标公式',
    'RSI-WR共振之选股指标公式',
    'RSRS回归斜率之选股指标公式',
    '一跃龙门之选股指标公式',
]
for s in samples:
    # 在 tni_files 里找下标
    tni_name = f'{s}.tni'
    if tni_name in tni_files:
        idx = tni_files.index(tni_name)
        print(f'  {s} → GS{idx:04d}')
