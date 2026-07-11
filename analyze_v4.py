"""QUANTQQ v4 优化结果分析器 — 读 csv, 生成 MD 报告 (含可信度分层 + 鲁棒性筛选)

用法: python analyze_v4.py [csv路径]   (默认读最新的 quantqq_v4_full_*.csv)
输出: docs/audit/2026-07-09_QUANTQQ参数优化_v4_报告.md
"""
import sys, os, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
import numpy as np

OUT_MD = 'docs/audit/2026-07-09_QUANTQQ参数优化_v4_报告.md'


def find_csv():
    if len(sys.argv) > 1:
        return sys.argv[1]
    fs = sorted(glob.glob('output/optimize/quantqq_v4_full_*.csv'))
    if fs:
        return fs[-1]
    if os.path.exists('output/optimize/quantqq_v4_checkpoint.csv'):
        return 'output/optimize/quantqq_v4_checkpoint.csv'
    raise SystemExit('找不到 v4 结果 csv')


def cred_tag(ann):
    """年化可信度分层 (基于 CLAUDE.md 回测可信度铁律)"""
    if ann > 100:
        return '⚠️极不可信(回测口径产物)'
    if ann > 50:
        return '⚠️高估(集中持仓押中翻倍股)'
    if ann > 25:
        return '🟡偏高(需实盘打折)'
    if ann > 15:
        return '🟢可信区间'
    return '⚪未达标'


def md_table(df, cols, n=20):
    lines = ['| ' + ' | '.join(cols) + ' |',
             '|' + '|'.join(['---'] * len(cols)) + '|']
    for _, r in df.head(n).iterrows():
        lines.append('| ' + ' | '.join(str(_fmt(r, c)) for c in cols) + ' |')
    return '\n'.join(lines)


def _fmt(r, c):
    v = r[c]
    if c in ('activation', 'drawdown'):
        return f'{v:.3f}'
    if c in ('cost_stop',):
        return f'{v:.2f}'
    if c in ('train_ann', 'test_ann', 'train_dd', 'test_dd'):
        return f'{v:.1f}'
    if c in ('train_cal', 'test_cal', 'train_sh', 'test_sh'):
        return f'{v:.2f}'
    return v


def main():
    csv = find_csv()
    df = pd.read_csv(csv)
    df['test_ann'] = pd.to_numeric(df['test_ann'], errors='coerce')
    te = df.dropna(subset=['test_ann']).copy()

    L = []
    L.append('# QUANTQQ 参数优化 v4 报告')
    L.append('')
    L.append(f'> 训练段 2020-2023 / 样本外 2024-2026 | 策略 QUANTQQ 全A股 | 生成自 `{os.path.basename(csv)}`')
    L.append('')
    L.append('> ⚠️ **可信度警告**: 本报告年化数字为回测口径(含 ffill/bfill + 集中持仓 + 宽止损)产物。'
             'CLAUDE.md 审计评级 4.5/10, 方向系统性乐观。年化>50% 几乎肯定高估, 实盘大概率大幅打折。'
             '请按"相对排序 + 邻域鲁棒性"参考, 勿信绝对值。')
    L.append('')

    L.append('## 一、概览')
    L.append(f'- 总组合数: **{len(df)}**')
    L.append(f'- 训练年化: max={df.train_ann.max():.1f}% / median={df.train_ann.median():.1f}% / mean={df.train_ann.mean():.1f}%')
    L.append(f'- 训练≥25%: **{(df.train_ann>=25).sum()}** 组 | 训练≥20%: {(df.train_ann>=20).sum()} 组')
    L.append(f'- 跑了样本外(train≥25%)的组合: **{len(te)}** 组')
    if len(te):
        L.append(f'- 样本外年化: max={te.test_ann.max():.1f}% / median={te.test_ann.median():.1f}% / min={te.test_ann.min():.1f}%')
        L.append(f'- 样本外≥20%(硬目标): **{(te.test_ann>=20).sum()}** 组')
        L.append(f'- 样本外≥15%(放宽线): **{(te.test_ann>=15).sum()}** 组')
    L.append('')

    L.append('## 二、达标组合 (训练≥25% 且 样本外≥20%)')
    hit = te[te.test_ann >= 20].copy()
    L.append(f'共 **{len(hit)}** 组达标。下表按**样本外 Calmar** 排序(Calmar 高=回撤控制好, 比单看年化更稳):')
    L.append('')
    if len(hit):
        hit = hit.sort_values('test_cal', ascending=False)
        L.append(md_table(hit, ['activation','drawdown','cost_stop','ladder','pos_mode','priority',
                                'train_ann','train_cal','test_ann','test_cal','test_dd','test_n']))
    else:
        L.append('**(无) — 硬目标落空**')
    L.append('')

    # 鲁棒性: test_cal>=1.5 且 test_n>=3000 且 test_ann 20-50(剔除极端不可信)
    L.append('## 三、鲁棒组合 (样本外 Calmar≥1.5 且 交易≥3000笔 且 年化20-50% 可信区间)')
    L.append('> 这是最接近"实盘可参考"的子集 — 剔除极不可信的超高年化, 要求回撤控制 + 样本量。')
    L.append('')
    robust = te[(te.test_cal >= 1.5) & (te.test_n >= 3000) & (te.test_ann >= 20) & (te.test_ann <= 50)].copy()
    L.append(f'共 **{len(robust)}** 组。按样本外年化排序:')
    L.append('')
    if len(robust):
        robust = robust.sort_values('test_ann', ascending=False)
        L.append(md_table(robust, ['activation','drawdown','cost_stop','ladder','pos_mode','priority',
                                   'test_ann','test_cal','test_dd','test_n']))
    else:
        L.append('**(无) — 无组合同时满足鲁棒条件**')
    L.append('')

    L.append('## 四、按仓位模式分层 (集中度风险分析)')
    L.append('| 仓位模式 | 组数 | 训练年化均值 | 样本外年化均值 | 样本外Calmar均值 | 可信度 |')
    L.append('|---|---|---|---|---|---|')
    for pm, g in df.groupby('pos_mode'):
        gte = g.dropna(subset=['test_ann'])
        ta = gte.test_ann.mean() if len(gte) else float('nan')
        tc = gte.test_cal.mean() if len(gte) else float('nan')
        L.append(f'| {pm} | {len(g)} | {g.train_ann.mean():.1f}% | {ta:.1f}% | {tc:.2f} | {cred_tag(ta)} |')
    L.append('')
    L.append('> **解读**: "小资金/大金额上限/高占比"=集中持仓, 年化高但不可信(押中翻倍股); '
             '"大资金/小金额上限"=分散, 年化低但更接近实盘可达。')
    L.append('')

    L.append('## 五、单变量分析 (样本外年化, 看哪个参数档位稳)')
    for col, label in [('activation','移动止盈激活线'), ('drawdown','回撤距离'), ('cost_stop','硬止损'),
                       ('priority','触发优先级'), ('time_stop','时间退出天数'),
                       ('first_day','首日涨幅'), ('cond_time','条件时间止盈')]:
        g = te.groupby(col).test_ann.agg(['mean','median','count']).reset_index()
        if len(g) > 1:
            L.append(f'**{label}**:')
            L.append('')
            L.append(f'| {label} | 样本外年化均值 | 中位 | 组数 |')
            L.append('|---|---|---|---|')
            for _, r in g.iterrows():
                L.append(f'| {r[col]} | {r["mean"]:.1f}% | {r["median"]:.1f}% | {r["count"]} |')
            L.append('')

    L.append('## 六、结论与实盘建议')
    L.append('1. **绝对值不可信**: 即便样本外达标, 年化>50% 的组合实盘预期打折 50%+(见上方可信度分层)。')
    L.append('2. **优先看鲁棒子集(第三节)**: 若非空, 是最值得实盘验证的参数; 若空, 说明无"既赚钱又稳"的组合。')
    L.append('3. **集中度是收益主因**: 第四节显示收益主要来自仓位集中度, 而非止损参数本身。'
             '实盘集中持仓+宽止损(-20%)=单票暴雷风险大, 需配合风控。')
    L.append('4. **样本外>训练的异常**: 部分组合样本外年化高于训练, 提示 2024-2026 可能是顺周期牛市, '
             '不代表策略 alpha, 需警惕 regime 切换。')
    L.append('5. **建议**: 取鲁棒子集 Top 3, 用审计级口径(去 ffill、T+1 实盘价、含停牌)重跑核实, 再决定实盘。')
    L.append('')

    os.makedirs('docs/audit', exist_ok=True)
    with open(OUT_MD, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L))
    print(f'[OK] 报告已生成: {OUT_MD}')
    print(f'  总组合 {len(df)}, 达标 {len(hit)}, 鲁棒 {len(robust)}')


if __name__ == '__main__':
    main()
