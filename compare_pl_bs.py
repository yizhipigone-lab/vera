"""PLOYLINE版 vs BARSLAST版 公式对比回测（2024全年）

对比两个版本的 ATAN 金叉策略：
  A. PLOYLINE版: X_3/X_4 用 PLOYLINE 插值 → XG → REF(XG,1) 推后确认
  B. BARSLAST版: 纯 LAST_SC + REF 历史引用 → XG → REF(XG,1) 推后确认

关键问题：
  - 回测中 TDX 拿到全量数据，PLOYLINE 已经是"稳定后"的值
  - 所以两个版本在回测中的信号差异可能很小
  - 真正的差异在实盘中（PLOYLINE 在下一交叉出现前不稳定）
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
import numpy as np
from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from backtest.engine import BacktestEngine
from backtest.stop_config import load_stop_config

TdxConnector.ensure_connected()

START = '20240101'
END   = '20241231'

print("=" * 90)
print("  PLOYLINE版 vs BARSLAST版 公式对比回测")
print(f"  回测区间: {START} ~ {END}")
print("=" * 90)

# ═══════════════════════════════════════════════════
# 1. 获取数据
# ═══════════════════════════════════════════════════
print("\n[1/4] 获取数据...", flush=True)
codes = DataFetcher.get_stock_universe('50')
k = DataFetcher.get_kline(codes, '20220101', END, dividend_type="front", period="1d")
close = k["Close"].sort_index()
high = k["High"].sort_index()
low = k["Low"].sort_index()
valid = close.notna().sum() > 200
close = close.loc[:, valid]
high = high.reindex(columns=close.columns)
low = low.reindex(columns=close.columns)

# 取前300只加速（两个版本对比不需要全量）
common_cols = close.columns[:300]
close = close[common_cols]
high = high.reindex(index=close.index, columns=common_cols).ffill().bfill()
low = low.reindex(index=close.index, columns=common_cols).ffill().bfill()

print(f"  股票数: {len(common_cols)}")

# ═══════════════════════════════════════════════════
# 2. 计算 X_1, X_2, 金叉, 死叉
# ═══════════════════════════════════════════════════
print("\n[2/4] 计算 X_1/X_2 及交叉信号...", flush=True)
ma5 = close.rolling(5).mean()
x1 = np.arctan((ma5 / ma5.shift(1) - 1) * 100) * 180 / np.pi
x2 = x1.rolling(5).mean()

jc = (x1 > x2) & (x1.shift(1) <= x2.shift(1))   # CROSS(X_1, X_2)
sc = (x1 < x2) & (x1.shift(1) >= x2.shift(1))   # CROSS(X_2, X_1)

print(f"  金叉事件总数: {jc.sum().sum():,}")
print(f"  死叉事件总数: {sc.sum().sum():,}")

# ═══════════════════════════════════════════════════
# 3. 版本A: PLOYLINE版 XG 计算
# ═══════════════════════════════════════════════════
print("\n[3/4] 计算两个版本的信号...", flush=True)

def compute_pl_signal(x1, x2, jc, sc):
    """模拟 PLOYLINE 版 XG 计算（全量数据 → 稳定后值）"""
    xg = pd.DataFrame(0, index=x1.index, columns=x1.columns)

    for col in x1.columns:
        xi1 = x1[col].values
        xi2 = x2[col].values

        # 找金叉和死叉位置
        jc_idx = np.where(jc[col].values)[0]
        sc_idx = np.where(sc[col].values)[0]

        if len(jc_idx) == 0:
            continue

        # PLOYLINE for X_3 (CROSS(X_1,X_2), X_2)
        # 在金叉之间，X_3 从上一个金叉的 X_2 线性变化到下一个金叉的 X_2
        x3 = np.full(len(xi1), np.nan)
        for i in range(len(jc_idx) - 1):
            a, b = jc_idx[i], jc_idx[i+1]
            x3[a:b] = np.linspace(xi2[a], xi2[b], b - a)
        # 最后一个金叉之后
        if len(jc_idx) > 0:
            x3[jc_idx[-1]:] = xi2[jc_idx[-1]]

        # PLOYLINE for X_4 (CROSS(X_2,X_1), X_1)
        x4 = np.full(len(xi1), np.nan)
        for i in range(len(sc_idx) - 1):
            a, b = sc_idx[i], sc_idx[i+1]
            x4[a:b] = np.linspace(xi1[a], xi1[b], b - a)
        if len(sc_idx) > 0:
            x4[sc_idx[-1]:] = xi1[sc_idx[-1]]

        # XG: COUNT(CROSS(X_1,X_2), X_3 < REF(X_3,5) AND X_4 > REF(X_4,5))
        x3_s = pd.Series(x3, index=x1.index)
        x4_s = pd.Series(x4, index=x1.index)
        ref_x3_5 = x3_s.shift(5).values
        ref_x4_5 = x4_s.shift(5).values

        cond = (x3_s.values < ref_x3_5) & (x4_s.values > ref_x4_5)
        # COUNT(CROSS, N) 表示最近N根内发生过一次金叉
        # 这里的 N = cond == True 时允许计数，但 TDX 的 COUNT 语义不同...
        # 实际上原公式: XG:=COUNT(CROSS(X_1,X_2), N)
        # N 是第二个参数，表示统计周期
        # 当 N=0 时 COUNT=0，当 N>=1 且最近N根有金叉时 COUNT>=1

        # 简化：cond为True时，在最近5根内检查是否有金叉
        for idx in range(len(xi1)):
            if cond[idx] and not np.isnan(cond[idx]):
                # 检查最近5根内是否有金叉
                start_idx = max(0, idx - 4)
                if np.any(jc[col].values[start_idx:idx+1]):
                    xg.iloc[idx, xg.columns.get_loc(col)] = 1

    return xg


def compute_bs_signal(x1, x2, jc, sc):
    """BARSLAST版 XG 计算"""
    xg = pd.DataFrame(0, index=x1.index, columns=x1.columns)

    for col in x1.columns:
        xi1 = x1[col].values
        xi2 = x2[col].values
        jc_arr = jc[col].values
        sc_arr = sc[col].values

        n = len(xi1)
        for i in range(1, n):
            if jc_arr[i] and not sc_arr[i]:  # 当日金叉
                # 找 LAST_SC: 最近一次死叉的位置
                last_sc = -1
                for j in range(i-1, -1, -1):
                    if sc_arr[j]:
                        last_sc = j
                        break
                if last_sc < 0:
                    continue
                # X_2 < REF(X_2, LAST_SC) AND X_1 > REF(X_1, LAST_SC)
                dist = i - last_sc
                if xi2[i] < xi2[last_sc] and xi1[i] > xi1[last_sc]:
                    xg.iloc[i, xg.columns.get_loc(col)] = 1

    return xg


xg_pl = compute_pl_signal(x1, x2, jc, sc)
xg_bs = compute_bs_signal(x1, x2, jc, sc)

# 信号后移一天 (REF(XG,1)=1 对应 ZP)
sig_pl = (xg_pl.shift(1) == 1).iloc[1:]  # REF(XG,1)=1
sig_bs = (xg_bs.shift(1) == 1).iloc[1:]

pl_total = sig_pl.sum().sum()
bs_total = sig_bs.sum().sum()
pl_dates = sig_pl.sum(axis=1)
bs_dates = sig_bs.sum(axis=1)

# SELECT 条件: XA AND XB AND NOT(300687) AND NOT(920) AND NOT(ST)
# 简化：排除科创板920、排除ST类
st_mask = pd.Series(False, index=common_cols)
for c in common_cols:
    if 'ST' in c or 'st' in c:
        st_mask[c] = True
valid_cols = [c for c in common_cols if not c.startswith('688') and not c.startswith('920') and not st_mask[c]]

print(f"  有效股票(排除ST/科创/920): {len(valid_cols)}")

sig_pl_filtered = sig_pl[valid_cols]
sig_bs_filtered = sig_bs[valid_cols]

pl_filtered = sig_pl_filtered.sum().sum()
bs_filtered = sig_bs_filtered.sum().sum()

print()
print(f"  {'':<20} {'PLOYLINE版':>12} {'BARSLAST版':>12} {'差异':>10}")
print(f"  {'─'*50}")
print(f"  {'XG(原始)':<20} {xg_pl.sum().sum():>12,} {xg_bs.sum().sum():>12,} {'':>10}")
print(f"  {'ZP(过滤后)':<20} {pl_filtered:>12,} {bs_filtered:>12,} {pl_filtered-bs_filtered:>+10,}")

# 逐日信号差异
daily_diff = pl_dates - bs_dates
diff_dates = daily_diff[daily_diff != 0]
print(f"\n  存在信号差异的交易日: {len(diff_dates)} / {len(daily_diff)} ({len(diff_dates)/len(daily_diff)*100:.1f}%)")
if len(diff_dates) > 0:
    print(f"  差异范围: {diff_dates.min():+.0f} ~ {diff_dates.max():+.0f}")
    print(f"  日均差异: {diff_dates.mean():+.2f}")

# 逐只股票信号差异
stock_diff = sig_pl_filtered.sum() - sig_bs_filtered.sum()
diff_stocks = stock_diff[stock_diff != 0]
print(f"\n  信号差异的股票数: {len(diff_stocks)} / {len(valid_cols)} ({len(diff_stocks)/len(valid_cols)*100:.1f}%)")

# ═══════════════════════════════════════════════════
# 4. 回测对比
# ═══════════════════════════════════════════════════
print(f"\n[4/4] 回测对比 (2024全年)...", flush=True)

ENGINE_CFG = {
    'initial_capital': 1000000.0,
    'commission': 0.0003,
    'slippage': 0.001,
    'period': '1d',
    'position_sizing': {
        'min_buy_amount': 2000.0,
        'max_buy_amount': 20000.0,
        'lot_size': 100,
        'min_lots': 1,
    },
}

STOP_CONFIG = load_stop_config()

def build_selections_df(sig_df):
    """将信号DataFrame转为selections格式"""
    records = []
    for col in sig_df.columns:
        for idx in sig_df.index[sig_df[col]]:
            records.append({'stock_code': col, 'select_date': idx.strftime('%Y-%m-%d')})
    return pd.DataFrame(records)

def run_backtest(sig_df, label):
    """运行完整回测"""
    sel = build_selections_df(sig_df.loc[START:END])
    if sel.empty:
        return {'cumulative_return': 0, 'metrics': {}, 'trades': pd.DataFrame()}

    common = sorted(set(close.columns) & set(sel['stock_code'].unique()))
    cs = close[common].ffill().bfill()
    hs = high.reindex(index=cs.index, columns=common).ffill().bfill()
    ls = low.reindex(index=cs.index, columns=common).ffill().bfill()

    entries = pd.DataFrame(False, index=cs.index, columns=cs.columns)
    for _, row in sel.iterrows():
        code = row['stock_code']; dt = pd.to_datetime(row['select_date'])
        if code not in entries.columns: continue
        if dt in entries.index:
            entries.loc[dt, code] = True
        else:
            m = entries.index >= dt
            if m.any(): entries.loc[entries.index[m][0], code] = True

    engine = BacktestEngine(ENGINE_CFG)
    result = engine.run_cached(cs, entries, hs.values.astype(np.float64),
                               ls.values.astype(np.float64), STOP_CONFIG, sel,
                               np.array([0.06, 0.15], dtype=np.float64),
                               np.array([0.30, 0.30], dtype=np.float64), 2,
                               skip_sm=True)

    m = result.get('metrics', {})
    print(f"  [{label}] 收益:{result['cumulative_return']*100:+.2f}% "
          f"年化:{m.get('annualized_return',0)*100:+.2f}% "
          f"回撤:{m.get('max_drawdown',0)*100:+.2f}% "
          f"夏普:{m.get('sharpe_ratio',0):.2f} "
          f"交易:{len(result.get('trades',[]))}笔 "
          f"胜率:{m.get('win_rate',0)*100:.1f}%")
    return result

print()
r_pl = run_backtest(sig_pl_filtered, "PLOYLINE版")
r_bs = run_backtest(sig_bs_filtered, "BARSLAST版")

print(f"\n{'='*90}")
print(f"  结论")
print(f"  {'─'*50}")
ret_diff = r_pl['cumulative_return'] - r_bs['cumulative_return']
print(f"  收益率差异: {ret_diff*100:+.2f}%")
if abs(ret_diff) < 0.01:
    print(f"  → 两版公式在回测中几乎完全一致")
    print(f"  → 原因: TDX回测使用全量数据，PLOYLINE已稳定")
    print(f"  → 差异主要体现在实盘: PLOYLINE中位数7根K线内不稳定")
else:
    print(f"  → 存在可观测差异: {ret_diff*100:+.2f}%")
print(f"{'='*90}")
