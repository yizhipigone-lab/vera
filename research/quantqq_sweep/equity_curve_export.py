"""research/quantqq_sweep/equity_curve_export.py — 导出冠军参数±过滤器的权益曲线 (2026-08-15)

为图表 HTML 供数: 6 段 × {无过滤, 中证1000>MA20} 两条策略权益曲线 +
三个指数 (中证1000/沪深300/上证) 同期日线。
产出: research/quantqq_sweep/equity_curves.csv (era, filter, date, equity)
      research/quantqq_sweep/index_daily.csv  (code, date, close; 含 shanghai)
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
ERAS = ["y2015_2016", "y2017_2018", "y2019_2020",
        "y2021_2022", "y2023_2024", "y2025_2026h1"]

CHAMPION = {"cost": -0.08, "act": 0.035, "dd": 0.005, "ladder": "off",
            "levels": [], "time_days": 12, "cond_days": 0, "cond_profit": 0.0}


def ensure_index_daily() -> None:
    """index_daily.csv 补上证 (zz1000/hs300 已由 regime_filter_test 拉好)。"""
    cache = OUT_DIR / "index_daily.csv"
    df = pd.read_csv(cache, dtype={"code": str}, parse_dates=["date"])
    if "shanghai" in set(df["code"]):
        return
    from core.data_fetcher import DataFetcher
    raw = DataFetcher.get_index_data("999999.SH", "20140601", "20260701",
                                     dividend_type="none", period="1d")
    if isinstance(raw, dict):
        s = raw["Close"].iloc[:, 0]
    else:
        s = raw["close"] if "close" in raw.columns else raw["Close"]
    add = pd.DataFrame({"code": "shanghai", "close": s.values},
                       index=pd.to_datetime(s.index))
    out = pd.concat([df, add.reset_index().rename(columns={"index": "date"})])
    out.to_csv(cache, index=False)
    print("index_daily.csv 已补上证:", len(add), "行")


def main() -> int:
    ensure_index_daily()
    idx = pd.read_csv(OUT_DIR / "index_daily.csv",
                      dtype={"code": str}, parse_dates=["date"])
    z = (idx[idx["code"] == "zz1000"].set_index("date")["close"].sort_index())
    regime = (z > z.rolling(20).mean()).fillna(False)

    rows = []
    for era in ERAS:
        os.environ["SWEEP_TAG"] = era
        import tools.quantqq_5m_sweep_2010 as sweep
        importlib.reload(sweep)
        meta, mats = sweep._load_cache()
        from backtest.engine import BacktestEngine
        engine = BacktestEngine({
            "initial_capital": sweep.CAPITAL, "commission": 0.0003,
            "slippage": 0.001, "stamp_tax": 0.0005,
            "enable_realistic_costs": True, "period": "5m",
            "position_sizing": {"min_buy_amount": 2000.0,
                                "max_buy_amount": sweep.MAX_BUY,
                                "lot_size": 100, "min_lots": 1},
        })
        selections = pd.read_csv(
            Path(sweep._cache_dir(sweep.WINDOW_TD)) / "selections.csv",
            dtype={"stock_code": str})
        entries = mats["entries_df"]
        bar_dates = entries.index.normalize()
        allow = np.asarray(bar_dates.map(lambda d: bool(regime.get(d, False))))

        for fname in ("none", "zz1000_ma20"):
            masked = entries if fname == "none" else entries.copy()
            if fname != "none":
                masked[~allow] = False
            res = engine.run_cached(
                mats["close_df"], masked, mats["high_np"], mats["low_np"],
                sweep.combo_stop_config(CHAMPION), selections,
                np.array([], dtype=np.float64), np.array([], dtype=np.float64),
                0, filter_limit_up=False, open_np=mats["open_np"],
                tradable_np=mats["tradable_np"],
                last_tradable_idx=mats["last_tradable_idx"])
            eq = res["equity_curve"]
            # 列名防御: 找日期列与权益列
            dcol = next(c for c in eq.columns if "date" in c.lower())
            vcol = next(c for c in eq.columns
                        if any(k in c.lower() for k in ("equity", "asset", "value")))
            for d, v in zip(eq[dcol], eq[vcol]):
                rows.append({"era": era, "filter": fname,
                             "date": pd.Timestamp(d).strftime("%Y-%m-%d"),
                             "equity": float(v)})
        print(f"[{era}] 权益曲线已导出", flush=True)

    out = pd.DataFrame(rows)
    out.to_csv(OUT_DIR / "equity_curves.csv", index=False)
    print("equity_curves.csv:", len(out), "行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
