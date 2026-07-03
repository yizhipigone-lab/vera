"""
GUPIAO_012 深度分析 (2020.1.1 ~ 2026.6.25)

产出 (桌面 GUPIAO012/):
  - GUPIAO012_analysis.md         主报告
  - GUPIAO012_signals_monthly.csv 月度信号
  - GUPIAO012_pnl_monthly.csv     月度盈亏
  - GUPIAO012_correlation.json    指数相关性
  - GUPIAO012_trades_2020_2026.csv 全量交易

执行: python analyze_gupiao012.py
"""
import sys
import os
import re
import json
import time
import warnings
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

# Windows GBK stdout: 强制 UTF-8
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

warnings.filterwarnings('ignore')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np

from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from core.formula_runner import FormulaRunner
from backtest.engine import BacktestEngine
from backtest.stop_config import load_stop_config

# === 配置 ===
FORMULA_NAME = 'GUPIAO_012'
START = '20200101'
END = '20260625'
UNIVERSE_TYPE = '50'
MAX_SIGNALS = 100000  # 6.5 年放宽

# 3 个指数: 上证 / 中证A500 / 中证500
INDEX_CODES = {
    '999999.SH': '上证指数',
    '000510.SH': '中证A500',
    '000905.SH': '中证500',
}

ENGINE_CFG = {
    'initial_capital': 1_000_000.0,
    'commission': 0.0003,
    'slippage': 0.001,
    'period': '1d',
    'position_sizing': {
        'min_buy_amount': 2_000.0,
        'max_buy_amount': 20_000.0,
        'lot_size': 100,
        'min_lots': 1,
    },
}
STOP_CONFIG = load_stop_config()

DESKTOP_DIR = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop" / "GUPIAO012"
DESKTOP_DIR.mkdir(parents=True, exist_ok=True)

PROJECT_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output', 'gupiao012_analysis.log')
os.makedirs(os.path.dirname(PROJECT_LOG), exist_ok=True)


# ===================== 数据获取 =====================

def fetch_universe_klines(start, end):
    """拉沪深 A 股 K 线，返回 (C, H, L, O, V, univ)"""
    TdxConnector.ensure_connected()
    codes = DataFetcher.get_stock_universe(UNIVERSE_TYPE)
    k = DataFetcher.get_kline(codes, start, end, dividend_type="front", period="1d")
    C = k['Close'].sort_index()
    H = k['High'].sort_index()
    L = k['Low'].sort_index()
    O = k['Open'].sort_index()
    V = k.get('Volume', pd.DataFrame()).sort_index()
    valid = C.notna().sum() > 100
    for d in [C, H, L, O, V]:
        d.drop(columns=[c for c in d.columns if not valid[c]], inplace=True)
    univ = [c for c in C.columns if 'ST' not in c and '*ST' not in c]
    return C, H, L, O, V, univ


def fetch_index_klines_parallel(codes, start, end):
    """并行拉多个指数 K 线，返回 {code: DataFrame(index=date, cols=OHLCV)}"""
    def _one(code):
        return code, DataFetcher.get_kline_single(code, start, end, dividend_type='none', period='1d')
    with ThreadPoolExecutor(max_workers=len(codes)) as ex:
        results = dict(ex.map(_one, codes))
    return results


def run_formula_selection(formula_name, start, end):
    """调 TDX 选股"""
    return FormulaRunner.run_stock_selection_with_dates(
        formula_name=formula_name, formula_arg='',
        stock_list=None, start_time=start, end_time=end,
        stock_period='1d', dividend_type=1,
    )


def run_backtest(sel_df, C, H, L, O, V, univ):
    """对单公式跑回测，返回 trades_df + 关键指标"""
    sig_bt = sel_df.copy()
    sig_bt = sig_bt[(sig_bt['select_date'] >= pd.to_datetime(START)) &
                    (sig_bt['select_date'] <= pd.to_datetime(END))]
    common = sorted(set(univ) & set(sig_bt['stock_code'].unique()))
    cs = C[common].ffill().bfill()
    hs = H.reindex(index=cs.index, columns=common).ffill().bfill()
    ls = L.reindex(index=cs.index, columns=common).ffill().bfill()

    entries = pd.DataFrame(False, index=cs.index, columns=cs.columns)
    for _, row in sig_bt.iterrows():
        sc, dt = row['stock_code'], row['select_date']
        if sc not in entries.columns:
            continue
        if dt in entries.index:
            entries.loc[dt, sc] = True
        else:
            m = entries.index >= dt
            if m.any():
                entries.loc[entries.index[m][0], sc] = True

    engine = BacktestEngine(ENGINE_CFG)
    bp = np.array([0.06, 0.15], dtype=np.float64)
    br = np.array([0.30, 0.30], dtype=np.float64)
    brs = engine.run_cached(cs, entries, hs.values.astype(np.float64),
                            ls.values.astype(np.float64), STOP_CONFIG, sig_bt, bp, br, 2, skip_sm=True)
    trades_df = brs.get('trades', pd.DataFrame())
    metrics = brs['metrics']
    cumret = brs['cumulative_return']

    # 重建 equity_curve (由 trades 的 pnl 累加)
    initial = ENGINE_CFG['initial_capital']
    eq_dates = pd.date_range(start=START, end=END, freq='D')
    equity_curve = pd.Series(initial, index=eq_dates, dtype=float, name='equity')
    if not trades_df.empty:
        for _, t in trades_df.iterrows():
            ed = pd.to_datetime(t['entry_date'])
            xd = pd.to_datetime(t['exit_date'])
            if ed in equity_curve.index:
                equity_curve.loc[ed:] += t['pnl']
    return trades_df, metrics, cumret, equity_curve


# ===================== 分析函数 =====================

def compute_signals_monthly(sel_df):
    """月度信号统计"""
    df = sel_df.copy()
    df['ym'] = df['select_date'].dt.to_period('M').astype(str)
    grp = df.groupby('ym').agg(
        signal_count=('stock_code', 'count'),
        distinct_stocks=('stock_code', 'nunique'),
    )
    grp['signal_to_stock_ratio'] = grp['signal_count'] / grp['distinct_stocks']
    grp['ym_dt'] = pd.to_datetime(grp.index + '-01')
    grp['year'] = grp['ym_dt'].dt.year
    return grp


def compute_signals_avg(sel_df, trading_days):
    """信号平均度（分散度）核心统计"""
    total = len(sel_df)
    distinct_stocks = sel_df['stock_code'].nunique()
    avg_per_stock = total / max(distinct_stocks, 1)
    avg_per_day = total / max(trading_days, 1)
    avg_per_month = total / 78  # 2020.1~2026.6 = 78 个月

    # 月度分布
    monthly_counts = sel_df.groupby(sel_df['select_date'].dt.to_period('M').astype(str)).size()
    monthly_cv = monthly_counts.std() / max(monthly_counts.mean(), 1)

    # TOP20 信号最多股票
    top20 = sel_df['stock_code'].value_counts().head(20).reset_index()
    top20.columns = ['stock_code', 'signal_count']
    top20['signal_pct'] = top20['signal_count'] / total * 100

    # 板块分布
    def classify_board(code):
        if code.startswith('688'):
            return '科创板'
        elif code.startswith('300') or code.startswith('301'):
            return '创业板'
        elif code.startswith('60'):
            return '上证主板'
        elif code.startswith('00') or code.startswith('001') or code.startswith('002') or code.startswith('003'):
            return '深证主板'
        return '其他'

    board_dist = pd.Series([classify_board(c) for c in sel_df['stock_code']]).value_counts()
    board_pct = (board_dist / board_dist.sum() * 100).round(2)

    return {
        'total_signals': total,
        'distinct_stocks': distinct_stocks,
        'avg_signals_per_stock': round(avg_per_stock, 2),
        'avg_signals_per_day': round(avg_per_day, 2),
        'avg_signals_per_month': round(avg_per_month, 1),
        'monthly_cv': round(monthly_cv, 2),
        'monthly_min': int(monthly_counts.min()),
        'monthly_max': int(monthly_counts.max()),
        'monthly_median': int(monthly_counts.median()),
        'empty_months': int((monthly_counts == 0).sum()) if (monthly_counts == 0).any() else 0,
        'top20': top20,
        'board_dist': board_dist,
        'board_pct': board_pct,
        'top20_concentration': top20['signal_count'].sum() / total * 100,
    }


def compute_pnl_monthly(trades_df, by='entry_date', equity_curve=None):
    """按 entry_date 或 exit_date 计算月度盈亏"""
    if trades_df.empty:
        return pd.DataFrame()
    df = trades_df.copy()
    df['ym'] = pd.to_datetime(df[by]).dt.to_period('M').astype(str)
    grp = df.groupby('ym').agg(
        trade_count=('profit_pct', 'count'),
        win_count=('profit_pct', lambda s: (s > 0).sum()),
        loss_count=('profit_pct', lambda s: (s <= 0).sum()),
        gross_profit=('pnl', lambda s: s[df.loc[s.index, 'profit_pct'] > 0].sum()),
        gross_loss=('pnl', lambda s: s[df.loc[s.index, 'profit_pct'] <= 0].sum()),
        avg_pnl=('pnl', 'mean'),
        max_win=('pnl', 'max'),
        max_loss=('pnl', 'min'),
        avg_profit_pct=('profit_pct', 'mean'),
    )
    grp['win_rate'] = grp['win_count'] / grp['trade_count']
    grp['net_pnl'] = grp['gross_profit'] + grp['gross_loss']
    # 月度收益率 = 月净盈亏 / (月初权益)
    if equity_curve is not None:
        eq_monthly = equity_curve.resample('ME').last()
        rets = []
        for ym in grp.index:
            try:
                # ym 是 '2020-01' 格式, 月初日期
                month_start = pd.to_datetime(ym + '-01')
                # 找该月最后一个权益值 (即月初权益 = 上月末权益)
                prev_month_end = month_start - pd.Timedelta(days=1)
                if prev_month_end in eq_monthly.index:
                    eq_start = float(eq_monthly[prev_month_end])
                else:
                    eq_start = float(ENGINE_CFG['initial_capital'])
                ret = grp.loc[ym, 'net_pnl'] / max(eq_start, 1)
                rets.append(float(ret))
            except Exception:
                rets.append(np.nan)
        grp['ret_pct'] = rets
    return grp


def analyze_exit_reasons(trades_df):
    """退出原因分布"""
    if trades_df.empty:
        return {}
    counts = trades_df['exit_reason'].value_counts()
    pct = (counts / counts.sum() * 100).round(2)

    detail = {}
    for reason, n in counts.items():
        sub = trades_df[trades_df['exit_reason'] == reason]
        detail[reason] = {
            'count': int(n),
            'pct': float(pct[reason]),
            'avg_pnl': round(float(sub['pnl'].mean()), 2),
            'total_pnl': round(float(sub['pnl'].sum()), 2),
            'win_rate': round(float((sub['profit_pct'] > 0).mean() * 100), 2),
        }

    # 阶梯止盈细分
    ladder = trades_df[trades_df['exit_reason'] == '阶梯止盈']
    ladder_6 = (ladder['profit_pct'].between(0.058, 0.065)).sum()
    ladder_15 = (ladder['profit_pct'].between(0.14, 0.16)).sum()
    detail['阶梯止盈_6%档'] = int(ladder_6)
    detail['阶梯止盈_15%档'] = int(ladder_15)
    detail['阶梯止盈_其他档'] = int(len(ladder) - ladder_6 - ladder_15)
    return detail


def compute_correlation(monthly_pnl_df, equity_curve, index_dfs):
    """3 指数与策略月度相关性"""
    # 策略月度收益率 (基于权益曲线)
    eq_monthly = equity_curve.resample('ME').last()
    strat_ret = eq_monthly.pct_change().dropna()

    result = {}
    for code, idx_df in index_dfs.items():
        if idx_df is None or idx_df.empty:
            result[code] = {'error': 'no_data'}
            continue
        close_col = 'close' if 'close' in idx_df.columns else idx_df.columns[0]
        idx_close = idx_df[close_col]
        idx_ret = idx_close.resample('ME').last().pct_change().dropna()

        common = pd.concat([strat_ret.rename('strat'), idx_ret.rename('idx')], axis=1, join='inner').dropna()
        if len(common) < 6:
            result[code] = {'error': f'too_few_months ({len(common)})'}
            continue

        s = common['strat'].values
        i = common['idx'].values

        # Pearson
        try:
            from scipy.stats import pearsonr
            corr, p_value = pearsonr(s, i)
        except Exception:
            corr = float(np.corrcoef(s, i)[0, 1])
            p_value = float('nan')

        # Beta
        cov_si = float(np.cov(s, i, ddof=1)[0, 1])
        var_i = float(np.var(i, ddof=1))
        beta = cov_si / var_i if var_i > 1e-12 else float('nan')

        # 跑赢/跑输
        diff = s - i
        out_n = int((diff > 0).sum())
        under_n = int((diff < 0).sum())
        tie_n = int((diff == 0).sum())

        # 大盘涨月/跌月胜率
        up_mask = i > 0
        down_mask = i < 0
        up_winrate = float((s[up_mask] > 0).mean()) if up_mask.any() else float('nan')
        down_winrate = float((s[down_mask] > 0).mean()) if down_mask.any() else float('nan')

        # 上行/下行捕获率
        up_capture = float(s[up_mask].mean() / i[up_mask].mean()) if up_mask.any() and i[up_mask].mean() != 0 else float('nan')
        down_capture = float(s[down_mask].mean() / i[down_mask].mean()) if down_mask.any() and i[down_mask].mean() != 0 else float('nan')

        # 平均超额
        avg_excess = float((s - i).mean())

        result[code] = {
            'name': INDEX_CODES[code],
            'n_months': len(common),
            'correlation': round(corr, 4),
            'p_value': round(p_value, 4) if not np.isnan(p_value) else None,
            'beta': round(beta, 4) if not np.isnan(beta) else None,
            'outperform_months': out_n,
            'underperform_months': under_n,
            'tie_months': tie_n,
            'up_month_strat_winrate': round(up_winrate, 4) if not np.isnan(up_winrate) else None,
            'down_month_strat_winrate': round(down_winrate, 4) if not np.isnan(down_winrate) else None,
            'up_capture': round(up_capture, 4) if not np.isnan(up_capture) else None,
            'down_capture': round(down_capture, 4) if not np.isnan(down_capture) else None,
            'avg_excess_return': round(avg_excess, 6),
            'avg_strat_return': round(float(s.mean()), 6),
            'avg_idx_return': round(float(i.mean()), 6),
        }
    return result


def analyze_monthly_reasons(pnl_entry, sig_monthly, index_dfs, top_n=5):
    """TOP N 大赚/大亏月 + 大盘背景标签"""
    if pnl_entry.empty:
        return {'top_wins': [], 'top_losses': []}
    sorted_by_pnl = pnl_entry.sort_values('net_pnl', ascending=False)
    top_wins = sorted_by_pnl.head(top_n).copy()
    top_losses = sorted_by_pnl.tail(top_n).copy().iloc[::-1]

    # 加大盘涨跌幅
    sh_idx = index_dfs.get('999999.SH')
    if sh_idx is not None and not sh_idx.empty:
        cc = 'close' if 'close' in sh_idx.columns else sh_idx.columns[0]
        sh_monthly = sh_idx[cc].resample('ME').last().pct_change()

        def _add_idx(pnl_df):
            rets = []
            for ym in pnl_df.index:
                dt = pd.to_datetime(ym + '-01')
                if dt in sh_monthly.index:
                    rets.append(sh_monthly.loc[dt])
                else:
                    rets.append(np.nan)
            pnl_df = pnl_df.copy()
            pnl_df['sh_monthly_ret'] = rets
            return pnl_df

        top_wins = _add_idx(top_wins)
        top_losses = _add_idx(top_losses)

    # 加信号数
    for df_ in [top_wins, top_losses]:
        if 'signal_count' not in df_.columns:
            sigs = []
            for ym in df_.index:
                if ym in sig_monthly.index:
                    sigs.append(sig_monthly.loc[ym, 'signal_count'])
                else:
                    sigs.append(0)
            df_['signal_count'] = sigs

    return {'top_wins': top_wins, 'top_losses': top_losses}


def generate_optimization_suggestions(sig_avg, exit_reasons, correlation, monthly_reasons, pnl_entry):
    """数据驱动的优化建议"""
    suggestions = []

    # 1. 信号集中度
    if sig_avg['top20_concentration'] > 25:
        suggestions.append(
            f"**[信号集中]** TOP20 股票信号占比 {sig_avg['top20_concentration']:.1f}% > 25%，"
            f"建议加入 '月内同行业持仓上限 N 只' 规则（建议 N=2-3），降低单股黑天鹅风险"
        )
    else:
        suggestions.append(
            f"**[信号分散良好]** TOP20 股票信号占比 {sig_avg['top20_concentration']:.1f}%，"
            f"分散度优秀，无需强制去重。建议保持当前公式的选股宽度"
        )

    # 2. 大盘相关性
    sh_corr = correlation.get('999999.SH', {})
    if isinstance(sh_corr, dict) and sh_corr.get('correlation') is not None:
        corr = sh_corr['correlation']
        beta = sh_corr.get('beta')
        down_cap = sh_corr.get('down_capture')
        if corr is not None and abs(corr) > 0.6 and down_cap is not None and abs(down_cap) > 0.7:
            suggestions.append(
                f"**[Beta 暴露高]** 与上证指数相关性 {corr:.2f}、下行捕获率 {down_cap:.2f}，"
                f"策略在大盘下跌时跟随下跌。建议增加大盘 MA60 趋势过滤（MA60 之下空仓）或 IF 期货对冲"
            )
        elif corr is not None and abs(corr) < 0.3:
            suggestions.append(
                f"**[独立 α 强]** 与上证指数相关性 {corr:.2f} < 0.3，"
                f"策略 alpha 独立于大盘，无需 beta 过滤，是真正的'选股 alpha'"
            )

    # 3. 阶梯止盈
    ladder6 = exit_reasons.get('阶梯止盈_6%档', 0)
    ladder15 = exit_reasons.get('阶梯止盈_15%档', 0)
    ladder_total = exit_reasons.get('阶梯止盈', {}).get('count', 0) if isinstance(exit_reasons.get('阶梯止盈'), dict) else 0
    if ladder_total > 0:
        r6 = ladder6 / ladder_total
        r15 = ladder15 / ladder_total
        if r6 < 0.35 and r15 < 0.10:
            suggestions.append(
                f"**[阶梯止盈偏严]** 6%档触发率 {r6*100:.1f}%，15%档触发率 {r15*100:.1f}%，"
                f"两档都没充分发挥。可考虑下移 6%→4% 抓住更多小额获利，或上移 15%→20% 押注大波段"
            )

    # 4. 时间/成本止损
    cost_stop = exit_reasons.get('成本止损', {})
    if isinstance(cost_stop, dict) and cost_stop.get('pct', 0) > 30:
        suggestions.append(
            f"**[成本止损占比高]** {cost_stop['pct']:.1f}% 交易以成本止损 -12% 离场，"
            f"说明公式在弱势期给出大量伪信号。建议叠加 '大盘 MA20 下行 + 信号' 双过滤"
        )

    # 5. 退出原因结构
    if exit_reasons:
        ladder_pct = exit_reasons.get('阶梯止盈', {}).get('pct', 0) if isinstance(exit_reasons.get('阶梯止盈'), dict) else 0
        if ladder_pct > 30:
            suggestions.append(
                f"**[止盈体系健康]** 阶梯止盈占 {ladder_pct:.1f}% 退出，说明趋势捕捉能力较强，"
                f"建议保留当前 6%/15% 双档结构，可加第 3 档（如 25%/30%）抓大牛股"
            )

    # 6. 月度连续亏损
    if not pnl_entry.empty and len(pnl_entry) >= 4:
        # 找连续亏损月
        neg_streak = 0
        max_streak = 0
        for ym in pnl_entry.index:
            if pnl_entry.loc[ym, 'net_pnl'] < 0:
                neg_streak += 1
                max_streak = max(max_streak, neg_streak)
            else:
                neg_streak = 0
        if max_streak >= 4:
            suggestions.append(
                f"**[连续亏损月]** 历史最大连续亏损月数 = {max_streak}，"
                f"建议引入 '连续 3 月亏损 + 当月大盘 MA60 下行 → 暂停信号 1 月' 的硬规则"
            )

    # 7. 信号密度 (新增)
    if sig_avg.get('avg_signals_per_day', 0) > 5:
        suggestions.append(
            f"**[信号密度高]** 平均每个交易日触发 {sig_avg['avg_signals_per_day']:.1f} 个信号 → 月均数百笔交易。"
            f"建议加入 '单只股票 20 日内只买 1 次' 的去重规则，降低换手率与冲击成本"
        )

    # 8. 中证500 高相关性 (新增)
    zz500 = correlation.get('000905.SH', {})
    if isinstance(zz500, dict) and zz500.get('correlation') is not None:
        zz500_corr = zz500['correlation']
        sh_corr_val = sh_corr.get('correlation', 0) or 0
        if zz500_corr > sh_corr_val + 0.1:
            suggestions.append(
                f"**[中小盘暴露偏高]** 与中证500 相关性 {zz500_corr:.3f} 显著高于与上证 {sh_corr_val:.3f}，"
                f"说明策略在中小盘表现更佳。在大盘股行情期（如 2024 红利行情）可能跑输，"
                f"可加 '中证500 月线 MA20 之上' 作为仓位开关"
            )

    # 9. 月度胜率稳健 (新增)
    if not pnl_entry.empty:
        win_months = int((pnl_entry['net_pnl'] > 0).sum())
        total_months = len(pnl_entry)
        win_ratio = win_months / total_months
        if win_ratio > 0.7:
            suggestions.append(
                f"**[月度胜率优秀]** {win_months}/{total_months} 月份盈利 ({win_ratio*100:.0f}%)。"
                f"6.5 年稳健盈利，但建议做样本外验证 (如 2007~2019 年样本) 确认是否过拟合当前周期"
            )

    return suggestions


# ===================== 输出函数 =====================

def fmt_pct(x, d=2):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return '—'
    return f'{x*100:.{d}f}%'


def write_analysis_md(result_path, metrics, cumret, sel_df, sig_monthly, sig_avg,
                      pnl_entry, pnl_exit, exit_reasons, correlation,
                      monthly_reasons, suggestions, index_dfs, equity_curve,
                      elapsed):
    """写主报告 GUPIAO012_analysis.md"""
    L = []
    L.append(f'# GUPIAO_012 深度分析报告 — 2020.1.1 ~ 2026.6.25')
    L.append('')
    L.append(f'- 公式: **{FORMULA_NAME}**')
    L.append(f'- 区间: **{START} ~ {END}** (6.5 年)')
    L.append(f'- 范围: 沪深 A 股 type=50')
    L.append(f'- 信号上限: {MAX_SIGNALS}')
    L.append(f'- 阶梯止盈: 6%:30% / 15%:30%')
    L.append(f'- 成本止损: -12%  /  移动止盈: 激活 8% 回撤 5%  /  时间止损: 20 日')
    L.append(f'- 初始资金: {ENGINE_CFG["initial_capital"]:,.0f}')
    L.append(f'- 报告生成: {time.strftime("%Y-%m-%d %H:%M:%S")}  用时: {elapsed:.0f}s ({elapsed/60:.1f}min)')
    L.append('')

    # 0. 摘要
    L.append('## 0. 摘要 Executive Summary')
    L.append('')
    L.append('| 指标 | 2020~2026 (本次) | 2024~2026 (上一批) |')
    L.append('|------|----------------:|-------------------:|')
    L.append(f'| 区间年数 | 6.5 | 2.5 |')
    L.append(f'| 累计收益 | {metrics.get("cumulative_return", 0)*100:+.2f}% | +207.55% |')
    L.append(f'| 年化收益 | {metrics.get("annualized_return", 0)*100:+.2f}% | +57.40% |')
    L.append(f'| 最大回撤 | {metrics.get("max_drawdown", 0)*100:.2f}% | -17.15% |')
    L.append(f'| 夏普比率 | {metrics.get("sharpe_ratio", 0):.2f} | 2.50 |')
    L.append(f'| 胜率 | {metrics.get("win_rate", 0)*100:.1f}% | 82.6% |')
    L.append(f'| 总信号数 | {sig_avg["total_signals"]:,} | 18,322 |')
    L.append(f'| 涉及股票数 | {sig_avg["distinct_stocks"]:,} | (未统计) |')
    L.append(f'| 总交易数 | {metrics.get("total_trades", 0):,} | 6,381 |')
    L.append('')

    # 1. 月度信号统计
    L.append('## 1. 月度信号统计')
    L.append('')
    L.append('### 表 1.1 月度信号数 (按月份展开)')
    L.append('')
    L.append('| 年月 | 信号数 | 涉及股票数 |')
    L.append('|------|------:|----------:|')
    for ym, row in sig_monthly.iterrows():
        L.append(f'| {ym} | {int(row["signal_count"]):,} | {int(row["distinct_stocks"]):,} |')
    L.append('')
    L.append('> 注: TDX 选股 XG 输出对 (stock, date) 唯一, 故"涉及股票数"通常 ≤ 信号数')
    L.append('')

    # 年度汇总
    L.append('### 表 1.2 年度信号分布')
    L.append('')
    L.append('| 年份 | 信号数 | 月均信号 | 月最多 | 月最少 | 月CV |')
    L.append('|-----:|------:|--------:|------:|------:|----:|')
    if not sig_monthly.empty:
        year_grp = sig_monthly.groupby('year')['signal_count'].agg(['sum', 'mean', 'max', 'min'])
        for yr, row in year_grp.iterrows():
            year_data = sig_monthly[sig_monthly['year'] == yr]['signal_count']
            cv = round(year_data.std() / max(year_data.mean(), 1), 2)
            L.append(f'| {yr} | {int(row["sum"]):,} | {row["mean"]:.0f} | {int(row["max"])} | {int(row["min"])} | {cv} |')
    L.append('')

    L.append('**解读**:')
    L.append('')
    L.append(f'- 全期共 **{sig_avg["total_signals"]:,}** 条信号，分布在 **{sig_avg["distinct_stocks"]:,}** 只股票上')
    L.append(f'- 平均每月 **{sig_avg["avg_signals_per_month"]:.0f}** 条，平均每个交易日 **{sig_avg["avg_signals_per_day"]:.1f}** 条')
    L.append(f'- 月度信号数 CV = **{sig_avg["monthly_cv"]}** (越小越均匀)')
    L.append(f'- 月度信号区间: 最多 {sig_avg["monthly_max"]} 条，最少 {sig_avg["monthly_min"]} 条，中位数 {sig_avg["monthly_median"]} 条')
    L.append(f'- 空仓月份数: **{sig_avg["empty_months"]}** 个 (月度信号=0 的月份)')
    L.append('')

    # 2. 月度盈亏
    L.append('## 2. 月度盈亏统计')
    L.append('')
    L.append('### 表 2.1 按 entry_date 月度盈亏 (推荐主指标)')
    L.append('')
    L.append('| 年月 | 交易数 | 胜 | 负 | 胜率 | 净盈亏(元) | 月收益率 |')
    L.append('|------|------:|---:|---:|-----:|----------:|--------:|')
    if not pnl_entry.empty:
        for ym, row in pnl_entry.iterrows():
            ret_str = fmt_pct(row.get('ret_pct', np.nan), 2)
            L.append(f'| {ym} | {int(row["trade_count"])} | {int(row["win_count"])} | {int(row["loss_count"])} | '
                     f'{row["win_rate"]*100:.1f}% | {row["net_pnl"]:+,.0f} | {ret_str} |')
    L.append('')

    L.append('### 表 2.2 按 exit_date 月度盈亏 (cash basis 参考)')
    L.append('')
    L.append('| 年月 | 交易数 | 胜 | 负 | 胜率 | 净盈亏(元) |')
    L.append('|------|------:|---:|---:|-----:|----------:|')
    if not pnl_exit.empty:
        for ym, row in pnl_exit.iterrows():
            L.append(f'| {ym} | {int(row["trade_count"])} | {int(row["win_count"])} | {int(row["loss_count"])} | '
                     f'{row["win_rate"]*100:.1f}% | {row["net_pnl"]:+,.0f} |')
    L.append('')

    # 年度汇总
    L.append('### 表 2.3 年度盈亏汇总 (按 entry_date)')
    L.append('')
    L.append('| 年份 | 交易数 | 胜率 | 净盈亏(元) |')
    L.append('|-----:|------:|-----:|----------:|')
    if not pnl_entry.empty:
        pnl_entry_copy = pnl_entry.copy()
        pnl_entry_copy['year'] = pnl_entry_copy.index.str[:4].astype(int)
        year_pnl = pnl_entry_copy.groupby('year').agg(
            trades=('trade_count', 'sum'),
            wins=('win_count', 'sum'),
            pnl=('net_pnl', 'sum'),
        )
        for yr, row in year_pnl.iterrows():
            wr = row['wins'] / max(row['trades'], 1) * 100
            L.append(f'| {yr} | {int(row["trades"]):,} | {wr:.1f}% | {row["pnl"]:+,.0f} |')
    L.append('')

    # 3. 信号平均度
    L.append('## 3. 信号的平均度 (分散度)')
    L.append('')
    L.append('### 核心统计')
    L.append('')
    L.append('| 指标 | 数值 |')
    L.append('|------|-----:|')
    L.append(f'| 总信号数 | {sig_avg["total_signals"]:,} |')
    L.append(f'| 涉及股票数 (去重) | {sig_avg["distinct_stocks"]:,} |')
    avg_repeat = sig_avg['total_signals'] / sig_avg['distinct_stocks']
    L.append(f'| 单只股票平均被选次数 (信号÷股票) | {avg_repeat:.2f} |')
    L.append(f'| 信号÷交易日 比 (每个交易日平均信号) | {sig_avg["avg_signals_per_day"]:.2f} |')
    L.append(f'| 信号÷月 比 (每月平均信号) | {sig_avg["avg_signals_per_month"]:.1f} |')
    L.append(f'| 月度信号 CV (变异系数, 越小越均匀) | {sig_avg["monthly_cv"]} |')
    L.append(f'| 月度信号中位数 | {sig_avg["monthly_median"]} |')
    L.append(f'| TOP20 股票信号占比 | {sig_avg["top20_concentration"]:.1f}% |')
    L.append('')

    L.append('### TOP 20 信号最多股票')
    L.append('')
    L.append('| 排名 | 股票 | 信号数 | 占比 |')
    L.append('|----:|------|------:|----:|')
    for i, row in sig_avg['top20'].iterrows():
        L.append(f'| {i+1} | {row["stock_code"]} | {row["signal_count"]} | {row["signal_pct"]:.2f}% |')
    L.append('')

    L.append('### 板块分布')
    L.append('')
    L.append('| 板块 | 信号数 | 占比 |')
    L.append('|------|------:|----:|')
    for board, cnt in sig_avg['board_dist'].items():
        pct = sig_avg['board_pct'][board]
        L.append(f'| {board} | {cnt:,} | {pct:.2f}% |')
    L.append('')

    L.append('**解读**:')
    L.append('')
    if sig_avg['top20_concentration'] > 30:
        L.append(f'- TOP20 股票占信号 **{sig_avg["top20_concentration"]:.1f}%**，信号集中度偏高，存在明星股效应')
    elif sig_avg['top20_concentration'] > 15:
        L.append(f'- TOP20 股票占信号 **{sig_avg["top20_concentration"]:.1f}%**，分散度中等')
    else:
        L.append(f'- TOP20 股票占信号 **{sig_avg["top20_concentration"]:.1f}%**，分散度良好')

    # 信号/股票 比 (月内) 通常 = 1 是数据特征 (TDX XG 输出对 stock+date 唯一)
    # 用 月度独立股票数 / 月度信号数 = 1 表示没有同股同月重复触发, 是好特征
    avg_ratio = sig_avg['total_signals'] / sig_avg['distinct_stocks']
    if avg_ratio > 4:
        L.append(f'- 单只股票平均被选中 **{avg_ratio:.1f}** 次，存在反复出现的"明星股"')
    elif avg_ratio > 2:
        L.append(f'- 单只股票平均被选中 **{avg_ratio:.1f}** 次，分布合理')
    else:
        L.append(f'- 单只股票平均被选中 **{avg_ratio:.1f}** 次，分布广泛且分散 (无明星股依赖)')

    # 月度均匀度
    if sig_avg['monthly_cv'] < 0.5:
        L.append(f'- 月度信号分布均匀 (CV={sig_avg["monthly_cv"]}), 无明显月份空白期')
    elif sig_avg['monthly_cv'] < 1.0:
        L.append(f'- 月度信号分布中等波动 (CV={sig_avg["monthly_cv"]}), 部分月份信号集中')
    else:
        L.append(f'- 月度信号分布波动大 (CV={sig_avg["monthly_cv"]}), 存在明显月份集中或空白')

    L.append('')

    # 4. 各月份盈亏原因
    L.append('## 4. 各月份盈亏原因分析')
    L.append('')

    L.append('### 退出原因分布')
    L.append('')
    L.append('| 退出原因 | 笔数 | 占比 | 平均盈亏 | 总盈亏(元) | 胜率 |')
    L.append('|---------|----:|----:|--------:|----------:|----:|')
    for reason, detail in exit_reasons.items():
        if isinstance(detail, dict):
            L.append(f'| {reason} | {detail["count"]:,} | {detail["pct"]:.1f}% | '
                     f'{detail["avg_pnl"]:+.0f} | {detail["total_pnl"]:+,.0f} | {detail["win_rate"]:.1f}% |')
    L.append('')

    L.append('### TOP 5 大赚月')
    L.append('')
    L.append('| 年月 | 净盈亏(元) | 交易数 | 胜率 | 当月信号 | 上证月收益 |')
    L.append('|------|---------:|------:|----:|------:|---------:|')
    if not monthly_reasons['top_wins'].empty:
        for ym, row in monthly_reasons['top_wins'].iterrows():
            sh_ret = row.get('sh_monthly_ret', np.nan)
            sh_str = fmt_pct(sh_ret, 2) if not pd.isna(sh_ret) else '—'
            L.append(f'| {ym} | {row["net_pnl"]:+,.0f} | {int(row["trade_count"])} | {row["win_rate"]*100:.1f}% | '
                     f'{int(row.get("signal_count", 0))} | {sh_str} |')
    L.append('')

    L.append('### TOP 5 大亏月')
    L.append('')
    L.append('| 年月 | 净盈亏(元) | 交易数 | 胜率 | 当月信号 | 上证月收益 |')
    L.append('|------|---------:|------:|----:|------:|---------:|')
    if not monthly_reasons['top_losses'].empty:
        for ym, row in monthly_reasons['top_losses'].iterrows():
            sh_ret = row.get('sh_monthly_ret', np.nan)
            sh_str = fmt_pct(sh_ret, 2) if not pd.isna(sh_ret) else '—'
            L.append(f'| {ym} | {row["net_pnl"]:+,.0f} | {int(row["trade_count"])} | {row["win_rate"]*100:.1f}% | '
                     f'{int(row.get("signal_count", 0))} | {sh_str} |')
    L.append('')

    L.append('**解读**:')
    L.append('')
    if not monthly_reasons['top_wins'].empty:
        top1 = monthly_reasons['top_wins'].iloc[0]
        L.append(f'- 最大盈利月 **{monthly_reasons["top_wins"].index[0]}**，净利 {top1["net_pnl"]:+,.0f} 元')
    if not monthly_reasons['top_losses'].empty:
        bot1 = monthly_reasons['top_losses'].iloc[0]
        L.append(f'- 最大亏损月 **{monthly_reasons["top_losses"].index[0]}**，亏损 {bot1["net_pnl"]:+,.0f} 元')

    ladder_total = exit_reasons.get('阶梯止盈', {}).get('count', 0) if isinstance(exit_reasons.get('阶梯止盈'), dict) else 0
    ladder6 = exit_reasons.get('阶梯止盈_6%档', 0)
    ladder15 = exit_reasons.get('阶梯止盈_15%档', 0)
    if ladder_total > 0:
        L.append(f'- 阶梯止盈共触发 **{ladder_total}** 笔（{exit_reasons["阶梯止盈"]["pct"]:.1f}%），'
                 f'其中 6%档 **{ladder6}** 笔 ({(ladder6/max(ladder_total,1)*100):.1f}%)，'
                 f'15%档 **{ladder15}** 笔 ({(ladder15/max(ladder_total,1)*100):.1f}%)')
    L.append('')

    # 5. 大盘相关性
    L.append('## 5. 与大盘指数的相关性')
    L.append('')
    L.append('### 表 5.1 三指数月度相关性矩阵')
    L.append('')
    L.append('| 指数 | 代码 | 月份数 | Pearson corr | p-value | Beta | 跑赢月 | 跑输月 | 平局月 |')
    L.append('|------|------|----:|----------:|-------:|-----:|------:|------:|-----:|')
    for code, info in correlation.items():
        if 'error' in info:
            L.append(f'| {INDEX_CODES.get(code, code)} | {code} | — | {info["error"]} | — | — | — | — | — |')
            continue
        L.append(f'| {info["name"]} | {code} | {info["n_months"]} | '
                 f'{info["correlation"]:.4f} | {info["p_value"]} | '
                 f'{info["beta"]} | {info["outperform_months"]} | '
                 f'{info["underperform_months"]} | {info["tie_months"]} |')
    L.append('')

    L.append('### 表 5.2 大盘涨跌月胜率 + 捕获率')
    L.append('')
    L.append('| 指数 | 大盘涨月胜率 | 大盘跌月胜率 | 上行捕获率 | 下行捕获率 | 月均超额 |')
    L.append('|------|----------:|----------:|--------:|--------:|------:|')
    for code, info in correlation.items():
        if 'error' in info:
            continue
        L.append(f'| {info["name"]} | '
                 f'{fmt_pct(info["up_month_strat_winrate"], 1)} | '
                 f'{fmt_pct(info["down_month_strat_winrate"], 1)} | '
                 f'{info["up_capture"]} | '
                 f'{info["down_capture"]} | '
                 f'{fmt_pct(info["avg_excess_return"], 2)} |')
    L.append('')

    L.append('**解读**:')
    L.append('')
    sh = correlation.get('999999.SH', {})
    if isinstance(sh, dict) and 'error' not in sh:
        corr = sh['correlation']
        beta = sh['beta']
        down_cap = sh.get('down_capture', 0) or 0
        if abs(corr) < 0.3:
            L.append(f'- 与上证相关性 **{corr:.3f}**，**alpha 独立**于大盘，是真正的选股 alpha')
        elif abs(corr) < 0.6:
            L.append(f'- 与上证相关性 **{corr:.3f}**，与大盘有一定联动但 alpha 仍有显著贡献')
        else:
            L.append(f'- 与上证相关性 **{corr:.3f}** 偏高，与大盘共振明显')
        if abs(down_cap) > 0.7:
            L.append(f'- 下行捕获率 **{down_cap:.2f}**，大盘跌时策略跟随下跌 — beta 暴露较高')
        elif abs(down_cap) < 0.3:
            L.append(f'- 下行捕获率 **{down_cap:.2f}**，大盘跌时策略抗跌明显 — α 防御性强')

    zz500 = correlation.get('000905.SH', {})
    if isinstance(zz500, dict) and 'error' not in zz500:
        corr = zz500['correlation']
        L.append(f'- 与中证500 相关性 **{corr:.3f}** (相比上证 {sh.get("correlation", "—")})，'
                 f'策略对中小盘暴露 {"较高" if corr > sh.get("correlation", 0) + 0.05 else "适中"}')
    L.append('')

    # 6. 优化建议
    L.append('## 6. 优化建议')
    L.append('')
    for i, s in enumerate(suggestions, 1):
        L.append(f'### 建议 {i}')
        L.append('')
        L.append(s)
        L.append('')

    # 7. 附录
    L.append('## 7. 附录 — 公式源码')
    L.append('')
    formula_path = Path(r'E:\NEW_TDX\T0001\export\gs_txt\gs_1_GUPIAO_012.txt')
    if formula_path.exists():
        content = formula_path.read_text(encoding='gbk', errors='ignore')
        L.append('```')
        L.append(content)
        L.append('```')
        L.append('')
    L.append('**关键参数**: P=21, S=8, M1=3 (EMA 通道参数)')
    L.append('')
    L.append('---')
    L.append(f'生成时间: {time.strftime("%Y-%m-%d %H:%M:%S")}')

    try:
        result_path.write_text('\n'.join(L), encoding='utf-8')
        print(f'  [MD] 主报告: {result_path}')
    except OSError as e:
        print(f'  [warn] 主报告写入失败: {e}')


def write_csv_json(sel_df, sig_monthly, pnl_entry, pnl_exit, correlation, trades_df):
    """写 CSV + JSON"""
    # 1. 月度信号 CSV
    sig_csv = DESKTOP_DIR / 'GUPIAO012_signals_monthly.csv'
    df = sig_monthly.drop(columns=['ym_dt', 'year']) if 'ym_dt' in sig_monthly.columns else sig_monthly.copy()
    df.to_csv(sig_csv, encoding='utf-8-sig')
    print(f'  [CSV] 月度信号: {sig_csv}')

    # 2. 月度盈亏 CSV (合并 entry/exit 两口径)
    pnl_csv = DESKTOP_DIR / 'GUPIAO012_pnl_monthly.csv'
    if not pnl_entry.empty:
        e = pnl_entry.add_suffix('_entry').copy()
        e.index.name = 'ym'
        if not pnl_exit.empty:
            x = pnl_exit.add_suffix('_exit').copy()
            x.index.name = 'ym'
            merged = e.join(x, how='outer')
        else:
            merged = e
        merged.to_csv(pnl_csv, encoding='utf-8-sig')
    else:
        pnl_csv.write_text('no data', encoding='utf-8')
    print(f'  [CSV] 月度盈亏: {pnl_csv}')

    # 3. 相关性 JSON
    corr_json = DESKTOP_DIR / 'GUPIAO012_correlation.json'
    with open(corr_json, 'w', encoding='utf-8') as f:
        json.dump({
            'formula': FORMULA_NAME,
            'start': START, 'end': END,
            'indices': correlation,
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f'  [JSON] 指数相关性: {corr_json}')

    # 4. 全量交易 CSV
    trades_csv = DESKTOP_DIR / 'GUPIAO012_trades_2020_2026.csv'
    if not trades_df.empty:
        t = trades_df.copy()
        if 'entry_date' in t.columns:
            t = t.sort_values('entry_date')
        t.to_csv(trades_csv, encoding='utf-8-sig', index=False)
        print(f'  [CSV] 交易明细: {trades_csv} ({len(t):,} 行)')


# ===================== 主函数 =====================

def main():
    t_start = time.time()
    print('=' * 80)
    print('  VERA GUPIAO_012 深度分析 (2020.1.1 ~ 2026.6.25)')
    print(f'  公式: {FORMULA_NAME}  区间: {START} ~ {END}')
    print(f'  输出: {DESKTOP_DIR}')
    print('=' * 80)

    log_f = open(PROJECT_LOG, 'w', encoding='utf-8')
    log_f.write(f'step,elapsed_s,info\n')

    # === 步骤 1: 沪深A股 K线 ===
    t0 = time.time()
    print('\n[1] 拉沪深A股 K线 (20200101~20260625)...', flush=True)
    C, H, L, O, V, univ = fetch_universe_klines(START, END)
    print(f'  股票池: {len(univ)} 只, K线 shape: {C.shape}, 用时 {time.time()-t0:.0f}s')
    log_f.write(f'fetch_universe,{time.time()-t0:.1f},{C.shape}\n')
    log_f.flush()

    # === 步骤 2: 3 个指数 K线 (并行) ===
    t0 = time.time()
    print('\n[2] 并行拉 3 个指数 K线...', flush=True)
    index_dfs = fetch_index_klines_parallel(list(INDEX_CODES.keys()), START, END)
    for code, df in index_dfs.items():
        print(f'  {INDEX_CODES[code]} ({code}): {df.shape}')
    print(f'  用时 {time.time()-t0:.0f}s')
    log_f.write(f'fetch_index,{time.time()-t0:.1f},{len(index_dfs)} indices\n')
    log_f.flush()

    # === 步骤 3: GUPIAO_012 选股 ===
    t0 = time.time()
    print('\n[3] GUPIAO_012 选股 (20200101~20260625)...', flush=True)
    sel_df = run_formula_selection(FORMULA_NAME, START, END)
    if len(sel_df) > MAX_SIGNALS:
        print(f'  [warn] 信号 {len(sel_df)} > MAX_SIGNALS {MAX_SIGNALS}, 截断')
        sel_df = sel_df.head(MAX_SIGNALS)
    print(f'  信号: {len(sel_df):,} 条, {sel_df["stock_code"].nunique():,} 只股票, 用时 {time.time()-t0:.0f}s')
    log_f.write(f'selection,{time.time()-t0:.1f},{len(sel_df)} signals\n')
    log_f.flush()

    if len(sel_df) < 5:
        print('  [warn] 信号太少, 无法回测')
        return

    # === 步骤 4: 回测 ===
    t0 = time.time()
    print('\n[4] 回测...', flush=True)
    trades_df, metrics, cumret, equity_curve = run_backtest(sel_df, C, H, L, O, V, univ)
    print(f'  交易: {len(trades_df):,} 笔, 累计 {cumret*100:+.2f}%, '
          f'年化 {metrics["annualized_return"]*100:+.2f}%, '
          f'回撤 {metrics["max_drawdown"]*100:.2f}%, '
          f'夏普 {metrics["sharpe_ratio"]:.2f}, '
          f'胜率 {metrics["win_rate"]*100:.1f}%, '
          f'用时 {time.time()-t0:.0f}s')
    log_f.write(f'backtest,{time.time()-t0:.1f},{len(trades_df)} trades\n')
    log_f.flush()

    # === 步骤 5-7: 月度统计 ===
    t0 = time.time()
    print('\n[5] 月度信号 + 信号平均度...', flush=True)
    trading_days = len(C.index)
    sig_monthly = compute_signals_monthly(sel_df)
    sig_avg = compute_signals_avg(sel_df, trading_days)
    print(f'  {len(sig_monthly)} 个月, TOP20 占比 {sig_avg["top20_concentration"]:.1f}%')
    log_f.write(f'signals_monthly,{time.time()-t0:.1f},{len(sig_monthly)} months\n')
    log_f.flush()

    t0 = time.time()
    print('\n[6] 月度盈亏统计...', flush=True)
    pnl_entry = compute_pnl_monthly(trades_df, by='entry_date', equity_curve=equity_curve)
    pnl_exit = compute_pnl_monthly(trades_df, by='exit_date', equity_curve=equity_curve)
    print(f'  entry口径 {len(pnl_entry)} 个月, exit口径 {len(pnl_exit)} 个月')
    log_f.write(f'pnl_monthly,{time.time()-t0:.1f}\n')
    log_f.flush()

    t0 = time.time()
    print('\n[7] 退出原因分析...', flush=True)
    exit_reasons = analyze_exit_reasons(trades_df)
    for r, d in exit_reasons.items():
        if isinstance(d, dict):
            print(f'  {r}: {d["count"]} 笔 ({d["pct"]:.1f}%)')
    log_f.write(f'exit_reasons,{time.time()-t0:.1f}\n')
    log_f.flush()

    # === 步骤 8: 大盘相关性 ===
    t0 = time.time()
    print('\n[8] 大盘相关性分析...', flush=True)
    correlation = compute_correlation(pnl_entry, equity_curve, index_dfs)
    for code, info in correlation.items():
        if 'error' not in info:
            print(f'  {info["name"]} ({code}): corr={info["correlation"]:.3f}, beta={info["beta"]:.2f}, '
                  f'跑赢 {info["outperform_months"]}/{info["n_months"]} 月')
    log_f.write(f'correlation,{time.time()-t0:.1f}\n')
    log_f.flush()

    # === 步骤 9: 月度原因标签 ===
    t0 = time.time()
    print('\n[9] 月度原因标签...', flush=True)
    monthly_reasons = analyze_monthly_reasons(pnl_entry, sig_monthly, index_dfs)
    log_f.write(f'monthly_reasons,{time.time()-t0:.1f}\n')
    log_f.flush()

    # === 步骤 10: 优化建议 ===
    t0 = time.time()
    print('\n[10] 优化建议生成...', flush=True)
    suggestions = generate_optimization_suggestions(sig_avg, exit_reasons, correlation, monthly_reasons, pnl_entry)
    print(f'  生成 {len(suggestions)} 条建议')
    log_f.write(f'suggestions,{time.time()-t0:.1f},{len(suggestions)} items\n')
    log_f.flush()

    # === 步骤 11: 写出所有文件 ===
    t0 = time.time()
    print('\n[11] 写入文件...', flush=True)
    md_path = DESKTOP_DIR / 'GUPIAO012_analysis.md'
    write_analysis_md(md_path, metrics, cumret, sel_df, sig_monthly, sig_avg,
                      pnl_entry, pnl_exit, exit_reasons, correlation,
                      monthly_reasons, suggestions, index_dfs, equity_curve,
                      time.time() - t_start)
    write_csv_json(sel_df, sig_monthly, pnl_entry, pnl_exit, correlation, trades_df)
    log_f.write(f'write_outputs,{time.time()-t0:.1f}\n')
    log_f.close()

    elapsed_total = time.time() - t_start
    print(f'\n{"=" * 80}')
    print(f'  完成 | 用时 {elapsed_total:.0f}s ({elapsed_total/60:.1f}min)')
    print(f'  报告目录: {DESKTOP_DIR}')
    print('=' * 80)


if __name__ == '__main__':
    main()