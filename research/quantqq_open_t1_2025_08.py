"""research/quantqq_open_t1_2025_08.py — QUANTQQ 日线 open_t1 vs close_t 对照回测 (2026-08-20)

背景: QUANTQQ 尾盘下单遇涨停拒单丢信号; 新增 entry_price_mode=open_t1
(次日开盘价买入, T+1 一字涨停才拒买)。计划书:
docs/plan/2026-08-20_回测次日开盘买入模式_计划书.md

口径: config/strategy_QUANTQQ.yaml 原样 (全A/前复权/100万/单票2万/移动止盈优先
6%+0.3%/硬止损-50%/60天), 仅改 time_range=20250801~20260820 + entry_price_mode。
注意(2026-07-20 口径声明): 1d 终审口径下移动止盈 0.3% 回撤偏乐观,
两口径对比只看**相对差异**, 绝对值不可全信。

用法: python research/quantqq_open_t1_2025_08.py
产出: output/quantqq_open_t1/compare.csv + trades_{mode}.csv + equity_{mode}.csv
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE_CFG = "config/strategy_QUANTQQ.yaml"
START, END = "20250801", "20260820"
MODES = ("open_t1", "close_t")
OUT = Path("output/quantqq_open_t1")

METRIC_KEYS = ["cumulative_return", "annualized_return", "max_drawdown",
               "sharpe_ratio", "calmar_ratio", "win_rate", "total_trades"]


def run_one(mode: str) -> dict:
    from pipeline.pipeline import Pipeline
    cfg = yaml.safe_load(open(BASE_CFG, encoding="utf-8"))
    cfg["time_range"]["start"] = START
    cfg["time_range"]["end"] = END
    cfg["backtest"]["entry_price_mode"] = mode
    tmp = OUT / f"_tmp_{mode}.yaml"
    tmp.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    print(f"\n{'=' * 60}\n[{mode}] 回测开始 {START}~{END}\n{'=' * 60}", flush=True)
    pipe = Pipeline(str(tmp))
    res = pipe.run(close_on_finish=False)
    bt = res["backtest"]
    m = dict(bt.get("metrics", {}) or {})
    row = {"mode": mode, **{k: m.get(k) for k in METRIC_KEYS}}
    info = bt.get("entry_mode_info")
    if info:
        row.update(info)
    trades = bt.get("trades")
    if trades is not None and len(trades):
        trades.to_csv(OUT / f"trades_{mode}.csv", index=False)
    eq = bt.get("equity_curve")
    if eq is not None and len(eq):
        eq.to_csv(OUT / f"equity_{mode}.csv", index=False)
    print(f"[{mode}] 累计 {m.get('cumulative_return', 0):+.2%} "
          f"年化 {m.get('annualized_return', 0):+.2%} "
          f"回撤 {m.get('max_drawdown', 0):.2%} "
          f"夏普 {m.get('sharpe_ratio', 0):.2f} "
          f"Calmar {m.get('calmar_ratio', 0):.2f} "
          f"胜率 {m.get('win_rate', 0):.1%} "
          f"交易 {m.get('total_trades', 0)} 笔", flush=True)
    if info:
        print(f"[{mode}] 信号 {info['n_signals']} → 平移 {info['n_shifted']}, "
              f"一字涨停拒买 {info['n_oneline_limit_up']}, "
              f"无T+1丢弃 {info['n_no_t1_bar']}, "
              f"全天停牌丢弃 {info['n_no_tradable_bar']}", flush=True)
    return row


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = [run_one(m) for m in MODES]
    df = pd.DataFrame(rows)
    df.to_csv(OUT / "compare.csv", index=False)
    print(f"\n=== 对照表 ===\n{df.to_string(index=False)}")
    print(f"\n已保存: {OUT}/compare.csv, trades_*.csv, equity_*.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
