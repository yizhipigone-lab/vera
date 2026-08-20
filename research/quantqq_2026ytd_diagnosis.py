"""research/quantqq_2026ytd_diagnosis.py — QUANTQQ 2026 年内低盈利归因 (2026-08-20)

用户两问: 最近两次回测 (2026-01-01~2026-08-10, QUANTQQ, open_t1) 为何盈利低?
是不是系统设计 (open_t1) 有问题?

方法: 控制变量 4 组合 (1d, 2026-01-01~2026-08-10):
  A. 用户止损参数 + open_t1  (复算用户 17:07 那次, 验证复现)
  B. 用户止损参数 + close_t  (隔离: 同一参数下口径差异)
  C. yaml 冠军参数 + open_t1 (隔离: 同一口径下参数差异)
  D. yaml 冠军参数 + close_t  (隔离: 纯时段效应, 对比 2025-08 起 +38%)
另附: 两时段指数环境对比 (2025-08~年末 vs 2026 年初~08-10)。

用法: python research/quantqq_2026ytd_diagnosis.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE_CFG = "config/strategy_QUANTQQ.yaml"
START, END = "20260101", "20260810"
OUT = Path("output/quantqq_2026ytd_diagnosis")

# 用户 Web 回测的止损参数 (从 stop_config_summary 还原):
# 成本止损 -6% / 移动止盈 3.5%激活+1.5%回撤 条件单语义(real) / 时间止损 10天
USER_STOP = {
    "priority": "trailing_first",
    "cost_stop": {"enabled": True, "threshold": -0.06},
    "trailing_stop": {"enabled": True, "activation": 0.035, "drawdown": 0.015,
                      "confirm": "real"},
    "ladder_tp": {"enabled": False, "levels": []},
    "time_stop": {"enabled": True, "max_hold_days": 10},
    "cond_time_stop": {"enabled": False, "days": 7, "profit": 0.015},
    "first_day": {"enabled": False},
}


def index_env():
    print("=== 指数环境 ===", flush=True)
    for code, name in [("000001.SH", "上证指数"), ("399001.SZ", "深证成指"),
                       ("399006.SZ", "创业板指")]:
        try:
            df = pd.read_parquet(f"data/kline_cache/1d/{code}.parquet",
                                 columns=["date", "close"])
            df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
            s = df.set_index("date")["close"].sort_index()

            def ret(a, b):
                sa = s[s.index >= a].iloc[0]
                sb = s[s.index <= b].iloc[-1]
                return (sb / sa - 1) * 100

            print(f"{name}: 2025-08~2025末 {ret('2025-08-01', '2025-12-31'):+.1f}%"
                  f"  |  2026年初~08-10 {ret('2026-01-01', '2026-08-10'):+.1f}%",
                  flush=True)
        except Exception as e:
            print(f"{code} 读取失败: {e}", flush=True)


def run_one(tag: str, stop_override, mode: str) -> dict:
    from pipeline.pipeline import Pipeline
    cfg = yaml.safe_load(open(BASE_CFG, encoding="utf-8"))
    cfg["time_range"]["start"] = START
    cfg["time_range"]["end"] = END
    cfg["backtest"]["entry_price_mode"] = mode
    cfg["backtest"]["period"] = "1d"
    if stop_override is not None:
        cfg["stop_loss"] = stop_override
    tmp = OUT / f"_tmp_{tag}.yaml"
    tmp.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    print(f"\n[{tag}] 开始 {START}~{END}", flush=True)
    res = Pipeline(str(tmp)).run(close_on_finish=False)
    bt = res["backtest"]
    m = dict(bt.get("metrics", {}) or {})
    row = {"tag": tag, "mode": mode,
           "cumret": m.get("cumulative_return", 0),
           "annret": m.get("annualized_return", 0),
           "maxdd": m.get("max_drawdown", 0),
           "sharpe": m.get("sharpe_ratio", 0),
           "calmar": m.get("calmar_ratio", 0),
           "winrate": m.get("win_rate", 0),
           "trades": m.get("total_trades", 0)}
    info = bt.get("entry_mode_info")
    if info:
        row["t1_reject"] = info["n_oneline_limit_up"]
    print(f"[{tag}] 累计 {row['cumret']:+.2%} 夏普 {row['sharpe']:.2f} "
          f"胜率 {row['winrate']:.1%} 交易 {row['trades']}", flush=True)
    return row


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    index_env()
    rows = [
        run_one("A_用户参数_open_t1", USER_STOP, "open_t1"),
        run_one("B_用户参数_close_t", USER_STOP, "close_t"),
        run_one("C_冠军参数_open_t1", None, "open_t1"),
        run_one("D_冠军参数_close_t", None, "close_t"),
    ]
    df = pd.DataFrame(rows)
    df.to_csv(OUT / "diagnosis.csv", index=False)
    print(f"\n=== 归因对照 ===\n{df.to_string(index=False)}", flush=True)
    print(f"\n保存: {OUT}/diagnosis.csv", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
