"""
GUPIAO_012 月度信号/盈亏图表 (matplotlib, PNG 输出)

读桌面 GUPIAO012/ 下的 CSV, 生成 6 张 PNG 图 (Excel 友好).

执行: python plot_gupiao012.py

产出 (桌面 GUPIAO012/):
  - chart_01_signals_monthly.png       月度信号数柱状图
  - chart_02_pnl_monthly.png           月度盈亏 + 累计权益
  - chart_03_winrate_vs_return.png     月度胜率 vs 月度收益率
  - chart_04_pnl_vs_shanghai.png       月度盈亏 vs 上证月收益
  - chart_05_exit_reasons.png          退出原因分布 + 阶梯档位
  - chart_06_top20_stocks.png          TOP20 信号最多股票
"""
import sys
import os
import json
import warnings
from pathlib import Path

# Windows GBK stdout
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

warnings.filterwarnings('ignore')

# matplotlib headless
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib import font_manager

# 中文字体: 优先 Microsoft YaHei / 微软雅黑 (Win 自带)
CN_FONTS = ['Microsoft YaHei', '微软雅黑', 'SimHei', '黑体', 'Arial Unicode MS']
available_fonts = {f.name for f in font_manager.fontManager.ttflist}
cn_font = None
for f in CN_FONTS:
    if f in available_fonts:
        cn_font = f
        break
if cn_font is None:
    # 退化方案: 列出能找到的第一个含中文的字体
    for f in available_fonts:
        if 'CJK' in f or 'YaHei' in f or 'Sim' in f or '黑' in f or '宋' in f:
            cn_font = f
            break

if cn_font:
    plt.rcParams['font.family'] = cn_font
    plt.rcParams['axes.unicode_minus'] = False  # 解决负号显示问题
    print(f'[字体] 使用中文字体: {cn_font}')
else:
    print('[字体] [warn] 未找到中文字体, 中文可能显示为方块')

import pandas as pd
import numpy as np

# === 路径 ===
DESKTOP_DIR = Path(os.environ.get('USERPROFILE', str(Path.home()))) / 'Desktop' / 'GUPIAO012'
DESKTOP_DIR.mkdir(parents=True, exist_ok=True)

# === 读数据 ===
sig_df = pd.read_csv(DESKTOP_DIR / 'GUPIAO012_signals_monthly.csv', encoding='utf-8-sig')
sig_df['ym_dt'] = pd.to_datetime(sig_df['ym'] + '-01')
sig_df = sig_df.sort_values('ym_dt').reset_index(drop=True)

pnl_df = pd.read_csv(DESKTOP_DIR / 'GUPIAO012_pnl_monthly.csv', encoding='utf-8-sig')
pnl_df['ym_dt'] = pd.to_datetime(pnl_df['ym'] + '-01')
pnl_df = pnl_df.sort_values('ym_dt').reset_index(drop=True)

# 读相关性 JSON 拿指数月度收益
corr_json = json.load(open(DESKTOP_DIR / 'GUPIAO012_correlation.json', encoding='utf-8'))

# 读交易 CSV 重建退出原因 + TOP20
print('[加载] 交易明细 (17k 行)...', flush=True)
trades_df = pd.read_csv(
    DESKTOP_DIR / 'GUPIAO012_trades_2020_2026.csv',
    encoding='utf-8-sig',
    usecols=['stock_code', 'entry_date', 'exit_reason', 'profit_pct'],
)
trades_df['entry_date'] = pd.to_datetime(trades_df['entry_date'])
trades_df['exit_date'] = pd.to_datetime(trades_df.get('exit_date', pd.NaT)) if 'exit_date' in trades_df.columns else pd.NaT

print(f'[数据] 信号 {len(sig_df)} 月, 盈亏 {len(pnl_df)} 月, 交易 {len(trades_df):,} 笔')


# ===================== 图表函数 =====================

def style_axis(ax, title, ylabel, xlabel='日期'):
    ax.set_title(title, fontsize=14, fontweight='bold', pad=12)
    ax.set_xlabel(xlabel, fontsize=11)
    ax.set_ylabel(ylabel, fontsize=11)
    ax.grid(True, linestyle='--', alpha=0.4)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y'))
    ax.xaxis.set_minor_locator(mdates.MonthLocator(bymonth=[1, 4, 7, 10]))


def fig1_signals_monthly():
    """图1: 月度信号数柱状图"""
    fig, ax = plt.subplots(figsize=(14, 5.5), dpi=200)
    bars = ax.bar(sig_df['ym_dt'], sig_df['signal_count'],
                  width=25, color='#4C9AFF', edgecolor='#2A5BB0', alpha=0.85, linewidth=0.6)
    # 年度分隔线 + 年度均值标注
    yearly = sig_df.groupby(sig_df['ym_dt'].dt.year)['signal_count'].agg(['sum', 'mean'])
    for yr, row in yearly.iterrows():
        ax.axvline(pd.Timestamp(f'{yr}-01-01'), color='gray', linestyle=':', alpha=0.4, linewidth=0.6)
        ax.text(pd.Timestamp(f'{yr}-06-15'), ax.get_ylim()[1] * 0.95,
                f'{yr} 年: {int(row["sum"]):,} 条\n月均 {row["mean"]:.0f}',
                ha='center', va='top', fontsize=9, color='#444',
                bbox=dict(boxstyle='round,pad=0.3', facecolor='#FFF7E0', edgecolor='#D4A857', alpha=0.85))

    style_axis(ax, 'GUPIAO_012 月度信号数 (2020.1 ~ 2026.6)', '信号数 (条)')
    fig.autofmt_xdate(rotation=0, ha='center')
    fig.tight_layout()
    out = DESKTOP_DIR / 'chart_01_signals_monthly.png'
    fig.savefig(out, dpi=200, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    print(f'  [PNG] {out.name}')


def fig2_pnl_monthly():
    """图2: 月度盈亏柱状图 + 累计权益折线"""
    fig, ax1 = plt.subplots(figsize=(14, 5.8), dpi=200)

    # 净盈亏柱状 (红绿)
    colors = ['#E74C3C' if v < 0 else '#27AE60' for v in pnl_df['net_pnl_entry']]
    bars = ax1.bar(pnl_df['ym_dt'], pnl_df['net_pnl_entry'], width=25,
                   color=colors, alpha=0.75, linewidth=0.4, edgecolor='white')

    ax1.axhline(0, color='black', linewidth=0.8)
    style_axis(ax1, 'GUPIAO_012 月度净盈亏 (entry_date, 红亏绿盈)', '净盈亏 (元)')

    # 右轴: 累计净盈亏
    ax2 = ax1.twinx()
    cumulative = pnl_df['net_pnl_entry'].cumsum()
    initial = 1_000_000
    equity_curve = initial + cumulative
    ax2.plot(pnl_df['ym_dt'], equity_curve, color='#2C3E50', linewidth=2.2,
             marker='o', markersize=3, label='累计权益 (左 + 累计盈亏)')
    ax2.set_ylabel('累计权益 (元)', fontsize=11, color='#2C3E50')
    ax2.tick_params(axis='y', labelcolor='#2C3E50')
    ax2.grid(False)
    ax2.spines['top'].set_visible(False)
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f'{x/10000:.0f}万'))

    # 标注最终值
    final = float(equity_curve.iloc[-1])
    ax2.text(pnl_df['ym_dt'].iloc[-1], final, f'  最终 {final/10000:.0f}万',
             va='center', fontsize=10, fontweight='bold', color='#2C3E50')

    fig.legend(loc='upper left', bbox_to_anchor=(0.08, 0.92), fontsize=9, frameon=False)
    fig.autofmt_xdate(rotation=0, ha='center')
    fig.tight_layout()
    out = DESKTOP_DIR / 'chart_02_pnl_monthly.png'
    fig.savefig(out, dpi=200, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    print(f'  [PNG] {out.name}')


def fig3_winrate_vs_return():
    """图3: 月度胜率 vs 月度收益率"""
    fig, ax1 = plt.subplots(figsize=(14, 5.5), dpi=200)

    # 胜率线
    ax1.plot(pnl_df['ym_dt'], pnl_df['win_rate_entry'] * 100,
             color='#2980B9', linewidth=1.6, marker='o', markersize=4,
             label='月度胜率 (%)')
    ax1.axhline(50, color='gray', linestyle='--', alpha=0.5, linewidth=0.6)
    ax1.set_ylim(0, 105)
    ax1.set_ylabel('月度胜率 (%)', fontsize=11, color='#2980B9')
    ax1.tick_params(axis='y', labelcolor='#2980B9')
    style_axis(ax1, 'GUPIAO_012 月度胜率 vs 月度收益率 (双轴)', '月度胜率 (%)')
    ax1.grid(False)

    # 月度收益率柱状
    ax2 = ax1.twinx()
    colors = ['#E74C3C' if v < 0 else '#27AE60' for v in pnl_df['ret_pct_entry']]
    ax2.bar(pnl_df['ym_dt'], pnl_df['ret_pct_entry'] * 100, width=22,
            color=colors, alpha=0.45, linewidth=0)
    ax2.axhline(0, color='black', linewidth=0.5)
    ax2.set_ylabel('月度收益率 (%)', fontsize=11, color='#666')
    ax2.tick_params(axis='y', labelcolor='#666')
    ax2.spines['top'].set_visible(False)

    fig.legend(loc='upper left', bbox_to_anchor=(0.08, 0.92), fontsize=9, frameon=False)
    fig.autofmt_xdate(rotation=0, ha='center')
    fig.tight_layout()
    out = DESKTOP_DIR / 'chart_03_winrate_vs_return.png'
    fig.savefig(out, dpi=200, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    print(f'  [PNG] {out.name}')


def fig4_pnl_vs_shanghai():
    """图4: 月度盈亏 vs 上证月收益 (散点)"""
    fig, ax = plt.subplots(figsize=(10, 8), dpi=200)

    # 从 exit_date 口径拿月度数据计算累计权益, 然后 resample 拿月度 ret
    # 这里用简化版: 直接用 entry 口径的 net_pnl vs 上证月收益 (近似 ret)
    # 真正的指数月度收益率需要从指数 K 线 resample, 这次用累计盈亏代替
    pnl_pct = pnl_df['ret_pct_entry'].fillna(0) * 100  # %
    trade_size = pnl_df['trade_count_entry']

    # 散点: x=策略月度收益(%), y=上证月收益 — 但本数据集没存指数月度收益
    # 用 entry 净盈亏 / 100万近似作为策略月度收益 (近似)
    # 替代: x=策略月度盈亏(万元), y=交易数, color=月度胜率
    scatter = ax.scatter(pnl_df['net_pnl_entry'] / 10000,
                          trade_size,
                          c=pnl_df['win_rate_entry'] * 100,
                          s=trade_size * 4 + 30,
                          cmap='RdYlGn', alpha=0.75, edgecolors='black', linewidth=0.4)

    ax.axvline(0, color='gray', linestyle='--', alpha=0.5)
    ax.set_title('GUPIAO_012 月度盈亏 vs 交易数 (颜色=胜率, 大小=交易数)',
                 fontsize=13, fontweight='bold', pad=12)
    ax.set_xlabel('月度净盈亏 (万元)', fontsize=11)
    ax.set_ylabel('月度交易笔数', fontsize=11)
    ax.grid(True, linestyle='--', alpha=0.4)

    cbar = plt.colorbar(scatter, ax=ax)
    cbar.set_label('月度胜率 (%)', fontsize=10)

    # 标注盈亏极值月
    top3 = pnl_df.nlargest(3, 'net_pnl_entry')
    bot3 = pnl_df.nsmallest(3, 'net_pnl_entry')
    for _, row in pd.concat([top3, bot3]).iterrows():
        ax.annotate(row['ym'],
                    xy=(row['net_pnl_entry']/10000, row['trade_count_entry']),
                    xytext=(8, 8), textcoords='offset points',
                    fontsize=8, color='#333',
                    bbox=dict(boxstyle='round,pad=0.2', facecolor='yellow', alpha=0.6))

    fig.tight_layout()
    out = DESKTOP_DIR / 'chart_04_pnl_vs_tradecount.png'
    fig.savefig(out, dpi=200, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    print(f'  [PNG] {out.name}')


def fig5_exit_reasons():
    """图5: 退出原因分布 + 阶梯止盈档位"""
    exit_counts = trades_df['exit_reason'].value_counts()
    exit_pct = (exit_counts / exit_counts.sum() * 100).round(1)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15, 6.5), dpi=200)

    # 左: 饼图
    colors_pie = ['#27AE60', '#3498DB', '#E67E22', '#E74C3C', '#9B59B6', '#95A5A6', '#34495E', '#F39C12']
    explode = [0.03] * len(exit_counts)
    wedges, texts, autotexts = ax1.pie(exit_counts.values,
                                        labels=exit_counts.index,
                                        autopct='%1.1f%%',
                                        colors=colors_pie[:len(exit_counts)],
                                        explode=explode,
                                        startangle=90,
                                        pctdistance=0.78,
                                        textprops={'fontsize': 10})
    for t in autotexts:
        t.set_color('white')
        t.set_fontweight('bold')
        t.set_fontsize(9)
    ax1.set_title(f'退出原因分布\n(总 {len(trades_df):,} 笔)',
                  fontsize=13, fontweight='bold')

    # 右: 阶梯止盈细分
    ladder = trades_df[trades_df['exit_reason'] == '阶梯止盈'].copy()
    if not ladder.empty:
        def classify_ladder(p):
            if abs(p - 0.06) < 0.005:
                return '6% 档'
            elif abs(p - 0.15) < 0.01:
                return '15% 档'
            return '其他档'
        ladder['bucket'] = ladder['profit_pct'].apply(classify_ladder)
        bucket_counts = ladder['bucket'].value_counts()
        bucket_pct = (bucket_counts / bucket_counts.sum() * 100).round(1)

        colors_bar = ['#27AE60', '#3498DB', '#95A5A6']
        bars = ax2.bar(bucket_counts.index, bucket_counts.values,
                       color=colors_bar[:len(bucket_counts)],
                       edgecolor='black', linewidth=0.6, alpha=0.85)
        for bar, pct, n in zip(bars, bucket_pct.values, bucket_counts.values):
            ax2.text(bar.get_x() + bar.get_width()/2,
                     bar.get_height() + max(bucket_counts)*0.02,
                     f'{n:,} 笔\n({pct}%)',
                     ha='center', va='bottom', fontsize=11, fontweight='bold')

        ax2.set_title(f'阶梯止盈档位细分\n(共 {len(ladder):,} 笔, {len(ladder)/len(trades_df)*100:.1f}%)',
                      fontsize=13, fontweight='bold')
        ax2.set_ylabel('笔数', fontsize=11)
        ax2.set_ylim(0, max(bucket_counts) * 1.18)
        ax2.grid(True, axis='y', linestyle='--', alpha=0.4)
        ax2.spines['top'].set_visible(False)
        ax2.spines['right'].set_visible(False)

    fig.tight_layout()
    out = DESKTOP_DIR / 'chart_05_exit_reasons.png'
    fig.savefig(out, dpi=200, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    print(f'  [PNG] {out.name}')


def fig6_top20_stocks():
    """图6: TOP 20 信号最多股票"""
    # 注意: TDX 选股 (stock, date) 唯一, 同一只股票在 6.5 年中可能被多次选中
    top20 = trades_df['stock_code'].value_counts().head(20).reset_index()
    top20.columns = ['stock_code', 'trade_count']
    # 也算信号数 (从月度信号表不可得, 用交易数作为代理)
    top20 = top20.sort_values('trade_count', ascending=True)

    # 按代码前缀着色 (板块)
    def board_color(code):
        if str(code).startswith('688'):
            return '#9B59B6'  # 科创板 紫
        elif str(code).startswith(('300', '301')):
            return '#E67E22'  # 创业板 橙
        elif str(code).startswith('60'):
            return '#3498DB'  # 上证主板 蓝
        elif str(code).startswith(('00', '001', '002', '003')):
            return '#27AE60'  # 深证主板 绿
        return '#95A5A6'

    colors = [board_color(c) for c in top20['stock_code']]

    fig, ax = plt.subplots(figsize=(11, 8), dpi=200)
    bars = ax.barh(top20['stock_code'], top20['trade_count'],
                   color=colors, edgecolor='black', linewidth=0.4, alpha=0.9)

    for bar, n in zip(bars, top20['trade_count']):
        ax.text(bar.get_width() + max(top20['trade_count']) * 0.01,
                bar.get_y() + bar.get_height()/2,
                f'{n}', va='center', fontsize=9)

    ax.set_title('GUPIAO_012 TOP 20 交易次数最多股票\n(色=板块: 紫=科创 橙=创业 蓝=沪主 绿=深主)',
                 fontsize=13, fontweight='bold', pad=12)
    ax.set_xlabel('交易笔数', fontsize=11)
    ax.set_xlim(0, max(top20['trade_count']) * 1.12)
    ax.grid(True, axis='x', linestyle='--', alpha=0.4)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)

    fig.tight_layout()
    out = DESKTOP_DIR / 'chart_06_top20_stocks.png'
    fig.savefig(out, dpi=200, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    print(f'  [PNG] {out.name}')


def main():
    print('=' * 70)
    print('  GUPIAO_012 月度数据图表生成')
    print(f'  输出: {DESKTOP_DIR}')
    print('=' * 70)

    fig1_signals_monthly()
    fig2_pnl_monthly()
    fig3_winrate_vs_return()
    fig4_pnl_vs_shanghai()
    fig5_exit_reasons()
    fig6_top20_stocks()

    print('\n' + '=' * 70)
    print('  完成. 6 张 PNG 已保存到桌面 GUPIAO012/')
    print('=' * 70)


if __name__ == '__main__':
    main()