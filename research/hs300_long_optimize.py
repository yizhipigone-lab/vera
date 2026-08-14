# -*- coding: utf-8 -*-
"""hs300_long_optimize.py
========================
沪深300 长期寻优 (2006-01-01 ~ 2026-07-31, 约 20.5 年), 门槛年化 > 18.88%。

为何换沪深300 (用户 2026-08-11 决策):
  - 中证A500 20年回测已证不可达 18.88% (28组合天花板17.96%, 段2腰斩-52.85%),
    且前段数据残缺 (成分股大量2006后上市, kline_truncated 严重)。
  - 沪深300 成分股多为 2005 前上市老股 (招行/平安/茅台/工行/中石化),
    20年数据齐全, 无前段残缺 → 真正的长期实证。
  - 代价: 7.5年沪深300天花板18.81% (vs 中证A500 30.58%), 20年可能更低 (alpha衰减)。

本脚本: 6 因子 × 3 出场 = 18 组合, 全区间年化排序 + 达标标记。
因子: MA金叉5/20, MA金叉10/30, 突破20, 动量20, 放量突破, 低波动突破(lowvol)。
出场: 基准 / 慢松 / 慢松快锁(最稳, 夏普最高)。

用法: python -X utf8 research/hs300_long_optimize.py
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

from core.data_fetcher import DataFetcher
from selection.selector import StockSelector
from utils.logger import get_logger
from auto_strategy_optimizer import (
    FACTORS, signals_to_selections, make_bt_cfg, make_stop,
    PERIOD, MAX_DD,
)

logger = get_logger(__name__)

LONG_START, LONG_END = "20060101", "20260731"
TARGET_ANN = 0.1888
UTYPE = "23"            # 沪深300
POOL_NAME = "沪深300"
RUN_DIR = "research/auto_optimizer_runs"

MAX_POS, MIN_AMT, MAX_AMT, COOLDOWN = 5, 10000, 100000, 20

FACTOR_GRID = [
    ("ma_cross", {"fast": 5, "slow": 20}, "MA金叉5/20"),
    ("ma_cross", {"fast": 10, "slow": 30}, "MA金叉10/30"),
    ("breakout", {"window": 20}, "突破20日"),
    ("momentum", {"window": 20}, "动量20日"),
    ("volume_breakout", {"window": 20, "vol_ma": 5}, "放量突破"),
    ("lowvol_breakout", {"window": 20, "vol_window": 20}, "低波动突破"),
]

# 出场: 基准 / 慢松(年化向) / 慢松快锁(夏普向, 最稳)
EXIT_GRID = [
    (-0.06, 0.035, 0.01, 12, None, "基准(-6/3.5/1/12d)"),
    (-0.08, 0.06, 0.02, 15, None, "慢松(-8/6/2/15d)"),
    (-0.08, 0.04, 0.01, 15, None, "慢松快锁(-8/4/1/15d)"),
]


def build_bt_cfg() -> dict:
    cfg = make_bt_cfg(MAX_POS, MIN_AMT, MAX_AMT)
    cfg["sell_cooldown_days"] = COOLDOWN
    return cfg


def main():
    print("=" * 90)
    print(f"  沪深300 长期寻优 | {LONG_START}~{LONG_END} (约 20.5 年) | 日线 | 尾盘买入口径")
    print(f"  参数: 单票{MAX_AMT//10000}万 + {MAX_POS}仓 + 冷却{COOLDOWN}天 | 门槛: 年化>{TARGET_ANN:.2%} (回撤<={MAX_DD:.0%}作参考)")
    print(f"  沪深300卖点: 成分股2005前上市为主, 20年数据齐全 (真长期实证)")
    print("=" * 90)

    sel = StockSelector({"formula_name": "_", "universe": {"type": UTYPE, "exclude_st": True},
                         "period": PERIOD, "dividend_type": 1})
    codes = sel.resolve_universe()
    logger.info("股票池 %s (type=%s) -> %d 只", POOL_NAME, UTYPE, len(codes))

    t0 = time.time()
    kl = DataFetcher.get_kline(codes, LONG_START, LONG_END, period=PERIOD,
                               dividend_type="front", use_cache=True)
    if not kl or "Close" not in kl:
        logger.error("取数失败, 退出"); return
    close = kl["Close"]; high = kl.get("High"); low = kl.get("Low"); vol = kl.get("Volume")
    logger.info("OHLCV 就绪: %d 股 × %d 日, 取数 %.0fs", close.shape[1], close.shape[0], time.time() - t0)

    bt_cfg = build_bt_cfg()

    rows = []
    t_start = time.time()
    for fkey, fparams, fname in FACTOR_GRID:
        try:
            sig = FACTORS[fkey](close, high=high, low=low, vol=vol, **fparams)
            selections = signals_to_selections(sig, fname)
            n_sig = len(selections)
        except Exception as e:
            logger.warning("[%s] 因子计算失败: %s", fname, e); continue
        if n_sig == 0:
            logger.warning("[%s] 信号为空, 跳过", fname); continue

        for cost, tact, tdd, tdays, ladder, ename in EXIT_GRID:
            tag = f"{fname} | {ename}"
            try:
                stop = make_stop(cost, tact, tdd, tdays, ladder)
                from backtest.engine import BacktestEngine
                res = BacktestEngine(bt_cfg).run(selections=selections,
                                                  start_time=LONG_START, end_time=LONG_END,
                                                  stop_config=stop)
                m = res.get("metrics") or {}
                ann = float(m.get("annualized_return", 0) or 0)
                dd = float(m.get("max_drawdown", 1) or 1)
                sharpe = float(m.get("sharpe_ratio", 0) or 0)
                trades = int(m.get("total_trades", 0) or 0)
                wr = float(m.get("win_rate", 0) or 0)
                ok_ann = ann >= TARGET_ANN and trades > 0
                ok_dd = abs(dd) <= MAX_DD
                mark = "[双达标]" if (ok_ann and ok_dd) else ("[年化达标]" if ok_ann else "")
                logger.info("%-40s | 信号=%-7d 年化=%6.2f%% 回撤=%6.2f%% 夏普=%5.2f 交易=%-6d 胜率=%5.1f%% %s",
                            tag, n_sig, ann * 100, dd * 100, sharpe, trades, wr * 100, mark)
                rows.append({"因子": fname, "出场": ename, "信号": n_sig,
                             "年化": f"{ann*100:.2f}%", "回撤": f"{dd*100:.2f}%",
                             "夏普": f"{sharpe:.2f}", "交易": trades,
                             "胜率": f"{wr*100:.1f}%",
                             "年化达标": "✓" if ok_ann else "",
                             "双达标": "✓" if (ok_ann and ok_dd) else ""})
            except Exception as e:
                logger.warning("[%s] 回测异常: %s", tag, e)

    print("\n" + "=" * 90)
    print("  沪深300 长期寻优结果 (按年化降序)")
    print("=" * 90)
    if rows:
        df = pd.DataFrame(rows)
        df["_sort"] = df["年化"].str.rstrip("%").astype(float)
        df = df.sort_values("_sort", ascending=False).drop(columns="_sort")
        print(df.to_string(index=False))
        os.makedirs(RUN_DIR, exist_ok=True)
        out = os.path.join(RUN_DIR, "hs300_long_results.csv")
        df.to_csv(out, index=False, encoding="utf-8-sig")
        n_ann = (df["年化达标"] == "✓").sum()
        n_both = (df["双达标"] == "✓").sum()
        best = df.iloc[0]
        print(f"\n年化达标(>18.88%): {n_ann} / {len(df)}   双达标(年化+回撤<=30%): {n_both} / {len(df)}")
        print(f"最优: {best['因子']} + {best['出场']} -> 年化{best['年化']} 回撤{best['回撤']} 夏普{best['夏普']}")
    print("=" * 90)
    logger.info("沪深300长期寻优完成, 总耗时 %.0fs", time.time() - t_start)


if __name__ == "__main__":
    TURN_START = time.time()
    try:
        main()
    finally:
        logger.info("总耗时 %.0fs", time.time() - TURN_START)
