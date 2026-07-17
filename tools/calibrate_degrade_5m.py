"""偏差校准实验 (计划书 2026-07-18 Phase C, 一次性验证, 不进 pytest)。

拿 5m 数据完整的股-天, 人为抹掉再降级, 双跑对账:
  A 基线: 真实完整 5m (degrade_5m 关)
  B 人为降级: 抹掉指定股-天的 5m bar → degrade_5m 开 → 1d 填充
对比每笔匹配交易 (stock_code + entry_date) 的入场价/出场价/退出原因/盈亏,
验证"执行价多为派生价 → 近无损"的假设, 并量化 close 价策略 (时间止损等) 偏差。

用法 (需 TDX + 本地 K 线缓存):
  python tools/calibrate_degrade_5m.py --start 20260501 --end 20260620 \
      --codes 600001.SH,000001.SZ --degrade-days 3
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from unittest.mock import patch

from backtest.engine import BacktestEngine
from core.data_fetcher import DataFetcher


def _strip_stock_days(kline: dict, days: list, codes: list) -> dict:
    """抹掉指定股-天的 5m bar (置 NaN 后 dropna 行只对全缺行生效,
    所以直接对指定 (行,列) 置 NaN; engine 侧该日有效 bar<48 → 触发降级)。"""
    out = {}
    day_set = {pd.Timestamp(d).normalize() for d in days}
    for f, df in kline.items():
        df = df.copy()
        row_mask = df.index.normalize().isin(day_set)
        for c in codes:
            if c in df.columns:
                df.loc[row_mask, c] = np.nan
        out[f] = df
    return out


def _run_with_kline(kline, mask, selections, stop_config, degrade):
    eng = BacktestEngine({'period': '5m', 'degrade_5m': degrade, 'initial_capital': 100000.0})
    # 只 patch 窗口拉取 + 交易日历; 1d 拉取走真实 KlineCache (降级需要)
    with patch.object(DataFetcher, 'get_kline_windowed',
                      return_value=(kline, mask)), \
         patch.object(DataFetcher, 'get_trading_days',
                      return_value=sorted(kline['Close'].index.normalize().unique())):
        return eng.run(selections=selections, stop_config=stop_config)


def main() -> int:
    ap = argparse.ArgumentParser(description="5m 降级偏差校准实验")
    ap.add_argument('--start', required=True)
    ap.add_argument('--end', required=True)
    ap.add_argument('--codes', required=True, help='逗号分隔, 需 5m 数据完整')
    ap.add_argument('--degrade-days', type=int, default=3, help='每只股人为抹掉的天数')
    ap.add_argument('--seed', type=int, default=42)
    args = ap.parse_args()

    codes = [c.strip() for c in args.codes.split(',') if c.strip()]
    print(f"[校准] 拉取 {len(codes)} 只股 5m 窗口数据 {args.start}~{args.end} ...")

    # 每天一个信号 (让窗口覆盖全区间), 用真实窗口拉取
    selections = pd.DataFrame([
        {'select_date': pd.Timestamp(args.start), 'stock_code': c} for c in codes])
    kline, mask = DataFetcher.get_kline_windowed(
        selections, period='5m', window_trading_days=45,
        dividend_type='front', fill_data=False)
    if not kline or 'Close' not in kline:
        print("[校准] 5m 数据为空, 终止")
        return 1

    close = kline['Close']
    # 预检: 降级依赖 1d 数据 (KlineCache), 拉不到就明确退出, 不跑"假降级"
    try:
        probe = DataFetcher.get_kline(
            codes, args.start, args.end, period='1d',
            dividend_type='front', fill_data=False, use_cache=True)
        if not probe or 'Close' not in probe:
            raise RuntimeError("1d 返回为空")
    except Exception as e:
        print(f"[校准] 1d 数据预检失败 ({e})\n"
              "       降级依赖 1d K线缓存 — 请登录通达信客户端后重试, "
              "或先跑 1d 缓存预热。")
        return 2
    # 校验所选股票 5m 完整度, 挑出可人为降级的日子 (有效 bar==48 的天)
    rng = np.random.default_rng(args.seed)
    per_code_days = {}
    for c in codes:
        if c not in close.columns:
            print(f"[校准] {c} 不在 5m 数据列, 跳过")
            continue
        col = close[c]
        valid = col.groupby(col.index.normalize()).count()
        full_days = valid[valid == 48].index.tolist()
        if len(full_days) < args.degrade_days + 2:
            print(f"[校准] {c} 完整 5m 天不足 ({len(full_days)}), 跳过")
            continue
        pick = rng.choice(full_days, size=args.degrade_days, replace=False).tolist()
        per_code_days[c] = pick
    if not per_code_days:
        print("[校准] 没有可校准的股票")
        return 1
    all_days = sorted({d for ds in per_code_days.values() for d in ds})
    print(f"[校准] 人为降级股-天: "
          + ", ".join(f"{c}×{len(ds)}" for c, ds in per_code_days.items()))

    stop_config = {
        'cost_stop': {'enabled': True, 'threshold': -0.12},
        'trailing_stop': {'enabled': True, 'activation': 0.035, 'drawdown': 0.01},
        'ladder_tp': {'enabled': True, 'levels': [
            {'profit': 0.06, 'sell_ratio': 0.3}, {'profit': 0.15, 'sell_ratio': 0.3}]},
        'time_stop': {'enabled': True, 'max_hold_days': 20},
    }
    # 信号日 = 每个被抹掉的天 (让交易正好落在降级日入场)
    sel_rows = [{'select_date': d, 'stock_code': c}
                for c, ds in per_code_days.items() for d in ds]
    selections = pd.DataFrame(sel_rows)

    print("[校准] A 基线 (真实 5m)...")
    res_a = _run_with_kline(kline, mask, selections, stop_config, degrade=False)
    print("[校准] B 人为降级 (1d 填充)...")
    kline_b = _strip_stock_days(kline, all_days, list(per_code_days))
    mask_b = mask.reindex(index=kline_b['Close'].index, columns=kline_b['Close'].columns,
                          fill_value=False).fillna(False).astype(bool)
    res_b = _run_with_kline(kline_b, mask_b, selections, stop_config, degrade=True)

    ta, tb = res_a.trades, res_b.trades
    ma = res_a.metrics or {}
    mb = res_b.metrics or {}
    print(f"[校准] 基线交易 {len(ta)} 笔, 降级交易 {len(tb)} 笔")

    key = ['stock_code', 'entry_date']
    merged = ta.merge(tb, on=key, suffixes=('_a', '_b')) if not ta.empty and not tb.empty \
        else pd.DataFrame()
    report = {
        'codes': list(per_code_days),
        'degrade_days': {c: [str(d.date()) for d in ds] for c, ds in per_code_days.items()},
        'trades_baseline': int(len(ta)),
        'trades_degraded': int(len(tb)),
        'matched': int(len(merged)),
        'metrics_baseline': {k: ma.get(k) for k in ('cumulative_return', 'sharpe_ratio', 'max_drawdown', 'win_rate')},
        'metrics_degraded': {k: mb.get(k) for k in ('cumulative_return', 'sharpe_ratio', 'max_drawdown', 'win_rate')},
    }
    if not merged.empty:
        merged['entry_same'] = np.isclose(merged['entry_price_a'], merged['entry_price_b'])
        merged['exit_diff'] = merged['exit_price_b'] - merged['exit_price_a']
        merged['exit_diff_pct'] = merged['exit_diff'] / merged['entry_price_a']
        by_reason = merged.groupby('exit_reason_a').agg(
            n=('exit_diff', 'size'),
            entry_same=('entry_same', 'sum'),
            exit_diff_abs_max=('exit_diff', lambda s: float(s.abs().max())),
            pnl_diff=('pnl_b', lambda s: 0.0),  # 占位, 下面算
        )
        pnl_diff = (merged['pnl_b'] - merged['pnl_a']).groupby(merged['exit_reason_a']).sum()
        by_reason['pnl_diff_sum'] = pnl_diff
        print("\n[校准] 按退出原因对比 (基线 vs 降级):")
        print(by_reason.to_string())
        report['by_reason'] = by_reason.reset_index().to_dict(orient='records')
        report['total_pnl_diff'] = float((merged['pnl_b'] - merged['pnl_a']).sum())
        print(f"\n[校准] 匹配交易 {len(merged)} 笔, 入场价一致 "
              f"{int(merged['entry_same'].sum())} 笔, 总盈亏差 "
              f"{report['total_pnl_diff']:+.2f} 元")
    degr = res_b.get('degradation')
    if degr:
        report['degradation'] = degr
        print(f"[校准] 降级报告: 降级 {degr['degraded_trades']}/{degr['total_trades']} 笔, "
              f"影响金额 [{degr['impact_amount']['pessimistic']:+.0f}, "
              f"{degr['impact_amount']['optimistic']:+.0f}]")

    out = _PROJECT_ROOT / 'output' / 'degrade_calibration.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                   encoding='utf-8')
    print(f"[校准] 报告落盘: {out}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
