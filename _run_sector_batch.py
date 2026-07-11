"""
128 行业板块批量回测 — 先跑前 10 个看效果
公式: QUANTQQ, 时间: 2026.1.1 ~ 2026.7.4
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np
from datetime import datetime

from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from core.formula_runner import FormulaRunner
from backtest.engine import BacktestEngine
from utils.config_loader import ConfigLoader

# ── 配置 ──
START, END = "20260101", "20260704"
FORMULA_NAME, FORMULA_ARG = "QUANTQQ", ""
TOP_N = None  # None = 全部 128 个

# 止损止盈配置 (从当前 default.yaml 读)
defaults = ConfigLoader.load_defaults()
STOP_CONFIG = defaults.get("stop_loss", {})
BT_CONFIG = defaults.get("backtest", {})
print(f"止损配置: {STOP_CONFIG.keys()}")
print(f"时间: {START} ~ {END}")
print()

# ── 连接 TDX ──
TdxConnector.initialize()
print("[1/4] 拉 128 个板块列表...")
sectors = DataFetcher.get_sector_list()
print(f"  共 {len(sectors)} 个板块, 全部跑")
if TOP_N:
    sectors = sectors[:TOP_N]

results = []

for idx, sec in enumerate(sectors):
    code, name = sec["code"], sec["name"]
    print(f"\n[2/4] [{idx+1}/{TOP_N}] {code} {name} — 拉成份股...")
    try:
        members = DataFetcher.get_sector_stocks(code)
    except Exception as e:
        print(f"  跳过: {e}")
        continue
    if not members:
        print(f"  板块 {name} 成份股为空, 跳过")
        continue
    print(f"  成份股: {len(members)} 只")

    # 选股
    print(f"[3/4] 选股 [{FORMULA_NAME}]...")
    t0 = datetime.now()
    try:
        picks = FormulaRunner.run_stock_selection_with_dates(
            formula_name=FORMULA_NAME, formula_arg=FORMULA_ARG,
            stock_list=members, start_time=START, end_time=END,
            stock_period="1d",
        )
    except Exception as e:
        print(f"  选股失败: {e}")
        continue
    if picks.empty:
        print(f"  选股结果为空, 跳过")
        results.append({"name": name, "code": code, "signals": 0, "cumulative_return": 0, "result": "无信号"})
        continue
    print(f"  选股 {len(picks)} 条, 耗时 {(datetime.now()-t0).total_seconds():.1f}s")

    # 回测
    print(f"[4/4] 回测...")
    t0 = datetime.now()
    engine = BacktestEngine(BT_CONFIG)
    bt = engine.run(selections=picks, start_time=START, end_time=END, stop_config=STOP_CONFIG)
    metrics = bt.get("metrics", {})
    cum_ret = metrics.get("cumulative_return", 0)
    n_trades = bt.get("trade_count", 0)
    print(f"  累计收益: {cum_ret:.2%}, {n_trades} 笔交易, 耗时 {(datetime.now()-t0).total_seconds():.1f}s")
    results.append({"name": name, "code": code, "signals": len(picks), "cumulative_return": cum_ret, "trades": n_trades})

TdxConnector.close()

# ── 排名 ──
print(f"\n{'='*60}")
print(f"排名 (按累计收益) — {FORMULA_NAME}, {START}~{END}")
print(f"{'='*60}")
results.sort(key=lambda x: x["cumulative_return"], reverse=True)
for i, r in enumerate(results):
    ret_str = f"{r['cumulative_return']:.2%}" if isinstance(r['cumulative_return'], (int, float)) else str(r['cumulative_return'])
    print(f"{i+1:>3}. {r['name']:<12} {r['code']:<14} 信号={r['signals']} 笔 累计收益={ret_str:>10}")

# 保存结果
with open("output/_sector_top10_ranking.json", "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print(f"\n结果已保存: output/_sector_top10_ranking.json")
