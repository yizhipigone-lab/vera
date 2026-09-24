"""research/quantqq_sweep/run_continuous.py — 连续区间冠军参数 × 仓位模式对照 (2026-08-15)

回答用户两问:
1. 连续口径 (2015-01-01~2026-06-30 单曲线, 100 万复利起跑);
2. 单票固定 5 万 vs 总资产 2% 动态 (max_position_pct=0.02) vs 实盘现行 2 万固定。

矩阵: output/quantqq_5m_sweep_2010/continuous/cache (prep_continuous.py 直填)。
产出: research/quantqq_sweep/continuous_results.csv + continuous_equity.csv
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

OUT_DIR = Path(__file__).resolve().parent
CHAMPION = {"cost": -0.08, "act": 0.035, "dd": 0.005, "ladder": "off",
            "levels": [], "time_days": 12, "cond_days": 0, "cond_profit": 0.0}
SIZINGS = {
    "fixed5w": {"max_buy_amount": 50_000.0, "max_position_pct": 1.0},
    "pct2": {"max_buy_amount": 1e12, "max_position_pct": 0.02},
    "fixed2w": {"max_buy_amount": 20_000.0, "max_position_pct": 1.0},
}
FILTERS = ("none", "zz1000_ma20", "cyb50_ma200")


def main() -> int:
    os.environ["SWEEP_TAG"] = "continuous"
    import tools.quantqq_5m_sweep_2010 as sweep
    importlib.reload(sweep)
    meta, mats = sweep._load_cache()
    selections = pd.read_csv(
        Path(sweep._cache_dir(sweep.WINDOW_TD)) / "selections.csv",
        dtype={"stock_code": str})

    # 过滤器: 中证1000>MA20 + 创业板50>MA200 (2026-08-15 用户指定)
    idx = pd.read_csv(OUT_DIR / "index_daily.csv",
                      dtype={"code": str}, parse_dates=["date"])
    z = idx[idx["code"] == "zz1000"].set_index("date")["close"].sort_index()
    c50 = idx[idx["code"] == "cyb50"].set_index("date")["close"].sort_index()
    regimes = {
        "none": None,
        "zz1000_ma20": (z > z.rolling(20).mean()).fillna(False),
        "cyb50_ma200": (c50 > c50.rolling(200).mean()).fillna(False),
    }

    entries = mats["entries_df"]
    bar_dates = entries.index.normalize()
    allows = {k: (None if v is None else
                  np.asarray(bar_dates.map(lambda d: bool(v.get(d, False)))))
              for k, v in regimes.items()}

    from backtest.engine import BacktestEngine
    from backtest.prepared import PreparedMatrix
    rows, curves = [], []
    for sname, sz in SIZINGS.items():
        engine = BacktestEngine({
            "initial_capital": sweep.CAPITAL, "commission": 0.0003,
            "slippage": 0.001, "stamp_tax": 0.0005,
            "enable_realistic_costs": True, "period": "5m",
            "position_sizing": {"min_buy_amount": 2000.0,
                                "max_buy_amount": sz["max_buy_amount"],
                                "max_position_pct": sz["max_position_pct"],
                                "lot_size": 100, "min_lots": 1},
        })
        for fname in FILTERS:
            masked = entries if fname == "none" else entries.copy()
            if fname != "none":
                masked[~allows[fname]] = False
            prepared = PreparedMatrix(
                close=mats["close_df"], entries=masked,
                high_np=mats["high_np"], low_np=mats["low_np"],
                open_np=mats["open_np"], tradable_np=mats["tradable_np"],
                last_tradable_idx=mats["last_tradable_idx"])
            res = engine.run_cached(
                prepared, sweep.combo_stop_config(CHAMPION),
                np.array([], dtype=np.float64), np.array([], dtype=np.float64),
                0, filter_limit_up=False)
            m = res["metrics"]
            rows.append({"sizing": sname, "filter": fname,
                         "cumret": m.get("cumulative_return", 0),
                         "annret": m.get("annualized_return", 0),
                         "maxdd": m.get("max_drawdown", 0),
                         "calmar": m.get("calmar_ratio", 0),
                         "sharpe": m.get("sharpe_ratio", 0),
                         "winrate": m.get("win_rate", 0),
                         "trades": m.get("total_trades", 0)})
            eq = res["equity_curve"]
            dcol = next(c for c in eq.columns if "date" in c.lower())
            vcol = next(c for c in eq.columns
                        if any(k in c.lower()
                               for k in ("equity", "asset", "value")))
            daily = eq.assign(day=pd.to_datetime(eq[dcol]).dt.strftime("%Y-%m-%d"))
            daily = daily.groupby("day")[vcol].last().reset_index()
            for d, v in zip(daily["day"], daily[vcol]):
                curves.append({"sizing": sname, "filter": fname,
                               "date": d, "equity": float(v)})
            print(f"[{sname} × {fname}] 年化 {m.get('annualized_return', 0):.1%} "
                  f"回撤 {m.get('max_drawdown', 0):.1%}", flush=True)

    pd.DataFrame(rows).to_csv(OUT_DIR / "continuous_results.csv", index=False)
    pd.DataFrame(curves).to_csv(OUT_DIR / "continuous_equity.csv", index=False)
    print("saved continuous_results.csv / continuous_equity.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
