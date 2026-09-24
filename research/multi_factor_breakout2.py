# -*- coding: utf-8 -*-
"""multi_factor_breakout2.py
=============================
中证500 + MA金叉10/30 + 个股层面信号质量过滤 → 冲年化 > 18.88%。

breakout1 教训 (大盘择时失败, 2026-08-11):
  - MA250 大盘择时: 砍 42.7% 信号 + 熊市全空仓 → 年化 16.99%→14.27% (降!), 回撤-21%→-10%
  - 根因: 全市场开关, 躲过刀子也躲过肉 (错过 15 疯牛 + V 型反弹初段)
  - 0/7 达标, 大盘择时这条路死。

换方向: 个股层面信号质量过滤 (不空仓)
  - 不择大盘的时, 对每只金叉票加质量门槛
  - 趋势确认: 个股收盘 > MA60/MA120 (砍假金叉)
  - 量能确认: 成交量 > 5日均量 × 1.3 (砍无量金叉)
  - 每天都有强势票入选, 不全市场空仓

用法: python -X utf8 research/multi_factor_breakout2.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import pandas as pd

from backtest.engine import BacktestEngine
from core.data_fetcher import DataFetcher
from selection.selector import StockSelector
from utils.logger import get_logger
from auto_strategy_optimizer import (
    FACTORS, signals_to_selections, make_bt_cfg, make_stop,
    PERIOD, MAX_DD, CAPITAL,
)

logger = get_logger(__name__)

LONG_START, LONG_END = "20060101", "20260731"
TARGET_ANN = 0.1888
UTYPE = "24"
MAX_POS, MIN_AMT, MAX_AMT, COOLDOWN = 5, 10000, 100000, 20

SEGMENTS = [
    ("全区间", "20060101", "20260731"),
    ("段1(07牛08灾)", "20060101", "20121231"),
    ("段2(15牛灾)", "20130101", "20191231"),
    ("段3(21核心资产)", "20200101", "20260731"),
]


def build_bt_cfg() -> dict:
    cfg = make_bt_cfg(MAX_POS, MIN_AMT, MAX_AMT)
    cfg["sell_cooldown_days"] = COOLDOWN
    return cfg


def run_bt(selections, bt_cfg, stop, start, end, tag):
    if selections is None or len(selections) == 0:
        logger.info("[%-26s] 信号空, 跳过", tag); return None
    res = BacktestEngine(bt_cfg).run(selections=selections, start_time=start,
                                      end_time=end, stop_config=stop)
    m = res.get("metrics") or {}
    ann = float(m.get("annualized_return", 0) or 0)
    dd = float(m.get("max_drawdown", 1) or 1)
    sharpe = float(m.get("sharpe_ratio", 0) or 0)
    trades = int(m.get("total_trades", 0) or 0)
    wr = float(m.get("win_rate", 0) or 0)
    ok = ann >= TARGET_ANN
    mark = "[年化达标>18.88%]" if ok else ""
    logger.info("[%-26s] %s~%s | 年化=%6.2f%% 回撤=%6.2f%% 夏普=%5.2f 交易=%-6d 胜率=%5.1f%% %s",
                tag, start, end, ann * 100, dd * 100, sharpe, trades, wr * 100, mark)
    return res


def yearly_returns(equity_curve) -> pd.DataFrame:
    if equity_curve is None or len(equity_curve) == 0:
        return pd.DataFrame()
    ec = equity_curve.copy()
    ec["date"] = pd.to_datetime(ec["date"])
    ec["year"] = ec["date"].dt.year
    year_end = ec.groupby("year")["equity"].last()
    prev = year_end.shift(1).fillna(CAPITAL)
    ret = (year_end / prev - 1)
    return pd.DataFrame({"年末权益": year_end.round(0),
                          "年度收益": (ret * 100).round(2).astype(str) + "%"})


def main():
    print("=" * 98)
    print(f"  中证500 + MA金叉10/30 + 个股信号质量过滤 | {LONG_START}~{LONG_END} | 目标年化>{TARGET_ANN:.2%}")
    print("=" * 98)

    sel = StockSelector({"formula_name": "_", "universe": {"type": UTYPE, "exclude_st": True},
                         "period": PERIOD, "dividend_type": 1})
    codes = sel.resolve_universe()
    logger.info("股票池 中证500 -> %d 只", len(codes))
    kl = DataFetcher.get_kline(codes, LONG_START, LONG_END, period=PERIOD,
                               dividend_type="front", use_cache=True)
    if not kl or "Close" not in kl:
        logger.error("取数失败"); return
    close = kl["Close"]; high = kl.get("High"); low = kl.get("Low"); vol = kl.get("Volume")
    logger.info("OHLCV: %d 股 × %d 日", close.shape[1], close.shape[0])

    # 基础信号: MA金叉10/30
    sig_cross = FACTORS["ma_cross"](close, high=high, low=low, vol=vol, fast=10, slow=30)

    # 个股层面过滤条件 (同 shape bool DataFrame)
    ma60 = close.rolling(60).mean()
    ma120 = close.rolling(120).mean()
    vol_ma5 = vol.rolling(5).mean()
    mom20 = close.pct_change(20)

    trend60 = close > ma60           # 中期趋势确认
    trend120 = close > ma120         # 长期趋势确认
    vol_up = vol > vol_ma5 * 1.3     # 量能确认 (放量)
    mom_pos = mom20 > 0              # 动量为正

    # 组合信号
    VARIANTS = [
        ("A.无过滤", sig_cross),
        ("B.金叉+MA60趋势", sig_cross & trend60),
        ("C.金叉+MA120趋势", sig_cross & trend120),
        ("D.金叉+量能1.3", sig_cross & vol_up),
        ("E.金叉+MA60+量能", sig_cross & trend60 & vol_up),
        ("F.金叉+MA60+动量+", sig_cross & trend60 & mom_pos),
        ("G.金叉+MA60+量能+动量", sig_cross & trend60 & vol_up & mom_pos),
    ]

    bt_cfg = build_bt_cfg()
    stop_base20 = make_stop(-0.06, 0.035, 0.01, 20, None)   # 基准延时20d (夏普最优)
    stop_lock20 = make_stop(-0.08, 0.04, 0.01, 20, None)    # 慢松快锁延时20d (年化最高)

    rows = []
    best_name, best_ann, best_stop_name = None, -1, ""
    for vname, vsig in VARIANTS:
        vsel = signals_to_selections(vsig, vname)
        n_sig = len(vsel)
        for st_name, stop in (("基准延时20d", stop_base20), ("慢松快锁延时20d", stop_lock20)):
            if n_sig == 0:
                continue
            res = BacktestEngine(bt_cfg).run(selections=vsel, start_time=LONG_START,
                                              end_time=LONG_END, stop_config=stop)
            m = res.get("metrics") or {}
            ann = float(m.get("annualized_return", 0) or 0)
            dd = float(m.get("max_drawdown", 1) or 1)
            sharpe = float(m.get("sharpe_ratio", 0) or 0)
            trades = int(m.get("total_trades", 0) or 0)
            wr = float(m.get("win_rate", 0) or 0)
            ok_ann = ann >= TARGET_ANN
            ok_dd = abs(dd) <= MAX_DD
            mark = "[双达标]" if (ok_ann and ok_dd) else ("[年化达标]" if ok_ann else "")
            combo = f"{vname}|{st_name}"
            logger.info("%-44s | 信号=%-6d 年化=%6.2f%% 回撤=%6.2f%% 夏普=%5.2f 交易=%-6d 胜率=%5.1f%% %s",
                        combo, n_sig, ann * 100, dd * 100, sharpe, trades, wr * 100, mark)
            rows.append({"组合": combo, "信号": n_sig, "年化": f"{ann*100:.2f}%",
                         "回撤": f"{dd*100:.2f}%", "夏普": f"{sharpe:.2f}",
                         "交易": trades, "胜率": f"{wr*100:.1f}%",
                         "年化达标": "✓" if ok_ann else "", "双达标": "✓" if (ok_ann and ok_dd) else ""})
            if ann > best_ann:
                best_ann, best_name, best_stop_name = ann, vname, st_name

    print("\n" + "=" * 98)
    print("  个股质量过滤结果 (按年化降序)")
    print("=" * 98)
    if rows:
        df = pd.DataFrame(rows)
        df["_s"] = df["年化"].str.rstrip("%").astype(float)
        df = df.sort_values("_s", ascending=False).drop(columns="_s")
        print(df.to_string(index=False))
        n_ann = (df["年化达标"] == "✓").sum()
        n_both = (df["双达标"] == "✓").sum()
        print(f"\n年化达标(>18.88%): {n_ann}/{len(df)}   双达标: {n_both}/{len(df)}")
        if best_name:
            print(f"最优: {best_name} | {best_stop_name} -> 年化 {best_ann*100:.2f}%")

    # 最优组合分段验证
    if best_name:
        print("\n" + "=" * 98)
        print(f"  最优组合分段验证: {best_name} | {best_stop_name}")
        print("=" * 98)
        for tag, s, e in SEGMENTS:
            vsel = signals_to_selections(
                dict((v[0], v[1]) for v in VARIANTS)[best_name], best_name)
            stop = stop_lock20 if "慢松" in best_stop_name else stop_base20
            run_bt(vsel, bt_cfg, stop, s, e, f"{best_name}-{tag}")
    print("=" * 98)
    logger.info("个股质量过滤突破完成")


if __name__ == "__main__":
    TURN_START = time.time()
    try:
        main()
    finally:
        logger.info("总耗时 %.0fs", time.time() - TURN_START)
