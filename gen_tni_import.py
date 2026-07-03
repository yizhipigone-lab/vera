"""生成 TDX .tni 公式导入文件 — 供用户一次性导入后批量回测

TDX导入步骤: 功能→公式系统→公式管理器→导入公式→选择生成的.tni文件
"""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils.logger import get_logger
logger = get_logger(__name__)

gongshi_dir = r'E:\gongshi'
files = sorted([f for f in os.listdir(gongshi_dir) if f.endswith('.md')])

# 生成一个合并的.tni文件 (TDX文本格式,每个公式以[FORMULA]分隔)
tni_path = r'E:\gongshi\all_468_formulas.tni'

count = 0
with open(tni_path, 'w', encoding='gbk') as out:
    for fname in files:
        filepath = os.path.join(gongshi_dir, fname)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            logger.warning("Read file failed: %s", e)
            continue

        # 提取公式名称
        name_match = re.search(r'^#\s*(.+?)(?:之选股指标公式)?\s*$', content, re.MULTILINE)
        title = name_match.group(1).strip() if name_match else fname.replace('.md', '')

        # 提取公式代码
        code_match = re.search(r'\`\`\`\s*\n(.*?)\n\`\`\`', content, re.DOTALL)
        if not code_match:
            continue
        formula_code = code_match.group(1)

        # 使用短名称(避免重复+TDX长度限制)
        short_name = f'GS{count:04d}'
        desc = title[:40]

        out.write(f'{short_name}:XG:{desc}:{formula_code}\n')
        count += 1

print(f'已生成: {tni_path}')
print(f'包含: {count} 个公式')
print(f'文件大小: {os.path.getsize(tni_path)} bytes')
print()
print('导入步骤:')
print('  1. 打开通达信 → 功能 → 公式系统 → 公式管理器')
print('  2. 条件选股公式 → 导入公式')
print('  3. 选择: E:\\gongshi\\all_468_formulas.tni')
print('  4. 全选 → 导入')
print()
print(f'导入后, 公式名称为 GS0000 ~ GS{count-1:04d}')
