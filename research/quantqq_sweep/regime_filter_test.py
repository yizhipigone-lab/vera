"""research/quantqq_sweep/regime_filter_test.py — 市场过滤器( regime filter )验证 (2026-08-15)

背景: 6 段分段寻优显示 QUANTQQ 在 2021-22 (最优 5.6%) 与 2023-24 (最优 -14.6%)
小微盘系统性弱市全灭 —— 出场参数救不了, 需要"大盘弱势时不开仓"的 regime filter。

设计: 复用 6 段已落盘的矩阵缓存 (output/quantqq_5m_sweep_2010/<era>/cache),
只在入场信号上按日期加 regime 掩码 (当日 regime 关闭 → 全天 5m bar 禁入场),
零重取数、零重建矩阵。出场/引擎逻辑不动。

验证组: 冠军参数 (激活3.5%/回撤0.5%/无阶梯/时间12天/止损-8%) × 5 种过滤器
× 6 段。过滤器用指数日线 (TDX 取一次, 缓存 index_daily.csv):
  none            无过滤 (基线)
  zz1000_ma20     中证1000 收盘 > MA20 (短期趋势)
  zz1000_ma60     中证1000 收盘 > MA60 (中期趋势)
  zz1000_ma20up60 中证1000 MA20 > MA60 (均线多头排列)
  hs300_ma60      沪深300 收盘 > MA60 (大盘蓝筹口径对照)

用法: python research/quantqq_sweep/regime_filter_test.py
产出: research/quantqq_sweep/regime_filter_results.csv + 控制台透视表
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
INDEX_CODES = {"zz1000": "000852.SH", "hs300": "000300.SH",
               "cyb50": "399673.SZ"}  # cyb50=创业板50 (399673)

# 冠军参数 (6 段稳健性分析收敛点) + 实盘现行口径对照
CONFIGS = {
    "champion": {"cost": -0.08, "act": 0.035, "dd": 0.005, "ladder": "off",
                 "levels": [], "time_days": 12, "cond_days": 0, "cond_profit": 0.0},
    # 实盘口径 (2026-08-14 last_result 的 stop_config_summary):
    # 成本-6% / 阶梯 5%+15% 卖30% / 移动止盈 6%+1% / 时间 12 天
    "live": {"cost": -0.06, "act": 0.06, "dd": 0.01, "ladder": "m2_5-15_30",
             "levels": [(0.05, 0.30), (0.15, 0.30)], "time_days": 12,
             "cond_days": 0, "cond_profit": 0.0},
}


def load_index_daily() -> pd.DataFrame:
    """指数日线 (code, date, close)。缓存存在但缺指数时只补缺的。"""
    cache = OUT_DIR / "index_daily.csv"
    df = pd.DataFrame()
    if cache.exists():
        df = pd.read_csv(cache, dtype={"code": str}, parse_dates=["date"])
    have = set(df["code"]) if (not df.empty and "code" in df.columns) else set()
    missing = [n for n in INDEX_CODES if n not in have]
    if cache.exists() and not missing:
        return df
    from core.data_fetcher import DataFetcher
    frames = [df] if not df.empty else []
    for name in missing:
        code = INDEX_CODES[name]
        df1 = DataFetcher.get_index_data(code, "20140601", "20260701",
                                         dividend_type="none", period="1d")
        if isinstance(df1, dict):
            k = df1.get("Close")
            s = k.iloc[:, 0] if isinstance(k, pd.DataFrame) else None
        else:  # get_kline_single 返回整合 DataFrame
            s = (df1["close"] if "close" in df1.columns
                 else df1["Close"] if "Close" in df1.columns
                 else df1.iloc[:, 0])
        if s is None:
            raise RuntimeError(f"指数 {name}({code}) 取数为空")
        frames.append(pd.DataFrame({"code": name,
                                    "date": pd.to_datetime(s.index),
                                    "close": s.values}))
    out = (pd.concat(frames, ignore_index=True)[["code", "date", "close"]]
           .drop_duplicates(["code", "date"]))
    out.to_csv(cache, index=False)
    return out


def build_filters(idx: pd.DataFrame) -> dict[str, pd.Series]:
    """过滤器名 → bool Series (index=日期, True=当日允许开仓)。"""
    z = (idx[idx["code"] == "zz1000"].set_index("date")["close"]
         .sort_index())
    h = (idx[idx["code"] == "hs300"].set_index("date")["close"]
         .sort_index())
    filters = {
        "none": pd.Series(True, index=z.index),
        "zz1000_ma20": z > z.rolling(20).mean(),
        "zz1000_ma60": z > z.rolling(60).mean(),
        "zz1000_ma20up60": z.rolling(20).mean() > z.rolling(60).mean(),
        "hs300_ma60": h > h.rolling(60).mean(),
    }
    # 创业板50 MA200 (2026-08-15 用户指定): 年线级慢过滤。指数数据 2014-06 起,
    # MA200 需 200 日预热 → 2015 年前几个月 regime=False (不开仓), 口径已知悉
    c = (idx[idx["code"] == "cyb50"].set_index("date")["close"].sort_index())
    if len(c) > 250:
        filters["cyb50_ma200"] = (c > c.rolling(200).mean()).fillna(False)
    return {k: v.fillna(False) for k, v in filters.items()}


def main() -> int:
    idx = load_index_daily()
    filters = build_filters(idx)
    print(f"指数数据: {idx['date'].min().date()} ~ {idx['date'].max().date()}, "
          f"过滤器: {list(filters)}")

    rows = []
    for era in ERAS:
        os.environ["SWEEP_TAG"] = era
        import tools.quantqq_5m_sweep_2010 as sweep
        importlib.reload(sweep)  # TAG 在模块级, 每段重载
        meta, mats = sweep._load_cache()
        from backtest.engine import BacktestEngine
        from backtest.prepared import PreparedMatrix
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
        bar_dates = entries.index.normalize()  # 5m bar → 当日日期

        for cfg_name, combo in CONFIGS.items():
            for fname, regime in filters.items():
                # regime 掩码: 当日 regime=False → 全天禁入场
                allow = bar_dates.map(lambda d: bool(regime.get(d, False)))
                masked = entries.copy()
                masked[~np.asarray(allow)] = False
                stop = sweep.combo_stop_config(combo)
                prepared = PreparedMatrix(
                    close=mats["close_df"], entries=masked,
                    high_np=mats["high_np"], low_np=mats["low_np"],
                    open_np=mats["open_np"], tradable_np=mats["tradable_np"],
                    last_tradable_idx=mats["last_tradable_idx"])
                res = engine.run_cached(
                    prepared, stop,
                    np.array([], dtype=np.float64),
                    np.array([], dtype=np.float64), 0,
                    filter_limit_up=False,
                )
                m = res["metrics"]
                rows.append({"era": era, "config": cfg_name, "filter": fname,
                             "annret": m.get("annualized_return", 0),
                             "maxdd": m.get("max_drawdown", 0),
                             "calmar": m.get("calmar_ratio", 0),
                             "winrate": m.get("win_rate", 0),
                             "trades": m.get("total_trades", 0)})
        print(f"[{era}] 完成 {len(CONFIGS) * len(filters)} 组", flush=True)

    out = pd.DataFrame(rows)
    out.to_csv(OUT_DIR / "regime_filter_results.csv", index=False)

    for cfg in CONFIGS:
        print(f"\n=== 年化% | 配置={cfg} | 行=过滤器 列=区间 ===")
        piv = (out[out["config"] == cfg]
               .pivot_table(index="filter", columns="era",
                            values="annret") * 100).round(1)
        piv["mean"] = piv.mean(axis=1).round(1)
        print(piv.to_string())
        print(f"\n=== 最大回撤% | 配置={cfg} ===")
        piv2 = (out[out["config"] == cfg]
                .pivot_table(index="filter", columns="era",
                             values="maxdd") * 100).round(1)
        print(piv2.to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
