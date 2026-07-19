# -*- coding: utf-8 -*-
"""避雷阈值信号源锦标赛 — 4 个便宜信号源 + 2 个固定对照,竞争"何时收紧避雷阈值"。

计划书: docs/plan/2026-07-19_避雷阈值信号锦标赛_计划书.md
评估链路与 tools/combo_filter_test.py 完全同口径(评分一次 → 逐日阈值过滤 → 同 stop_config 回测),
唯一区别:阈值按 select_date 逐日切换(宽松 -2 / 严格 0),由信号源决定。

用法:
    python tools/filter_signal_tournament.py --strategy-yaml config/strategy_QUANTQQ.yaml --tag 20250719_20260718
    python tools/filter_signal_tournament.py --strategy-yaml config/strategy_QUANTQQ.yaml --tag 20250719_20260718 --skip-breadth
    python tools/filter_signal_tournament.py --strategy-yaml config/strategy_QUANTQQ.yaml --tag 20250719_20260718 --skip-backtest  # 只生成信号序列+诊断
"""
import sys
import os
import argparse
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent

from utils.config_loader import ConfigLoader
from backtest.stop_config import load_stop_config
from factor_score import score_selections
from combo_filter_test import run_backtest

# ═══════════════════════════════════════════════════════════════
# 信号源(纯函数, trailing-only, True=严格日)
# 每个函数只依赖 ≤t 的数据 — 因果性由 tests/test_filter_signals.py 强制
# ═══════════════════════════════════════════════════════════════

def signal_ma200(close: pd.Series, ma: int = 200) -> pd.Series:
    """C1 指数趋势: 收盘 < MA200 → 严格。趋势类软肋: 崩盘初期慢, 逼空不误报。"""
    return close < close.rolling(ma).mean()


def signal_drawdown(close: pd.Series, lookback: int = 250, thresh: float = 0.15) -> pd.Series:
    """C2 指数回撤: 距 250 日高点回撤 > thresh → 严格。"""
    dd = 1.0 - close / close.rolling(lookback).max()
    return dd > thresh


def signal_volatility(close: pd.Series, win: int = 20, pct: float = 0.8, base: int = 252) -> pd.Series:
    """C3 波动率分位: 20 日已实现波动率 > 过去 252 日 80 分位 → 严格。
    软肋(预注册): 波动率不分方向, 逼空同样触发严格(踏空风险)。"""
    rv = close.pct_change().rolling(win).std()
    thr = rv.rolling(base).quantile(pct)
    return rv > thr


def signal_breadth(closes: pd.DataFrame, ma: int = 20, thresh: float = 0.2) -> pd.Series:
    """C4 市场宽度: 站上 MA20 个股占比 < thresh → 严格。
    closes: index=date, columns=stock_code。NaN(未上市/缺数据)自动剔除出占比。"""
    above = closes > closes.rolling(ma).mean()
    return above.mean(axis=1) < thresh


# ═══════════════════════════════════════════════════════════════
# 诊断与退化守卫(计划书 §4)
# ═══════════════════════════════════════════════════════════════

RATIO_LO, RATIO_HI = 0.05, 0.60   # 严格日占比超出 [5%, 60%] → 退化(≈固定阈值)
MAX_SWITCHES = 24                  # 年切换 >24 次 → 抖动


def arm_diagnostics(strict_daily: pd.Series, select_dates: pd.DatetimeIndex) -> dict:
    """strict_daily: 日级布尔序列(index=交易日); select_dates: 选股信号日期。
    返回 严格日占比(按选股日计) + 切换次数(按日级序列, 窗口内)。"""
    strict_sel = strict_daily.reindex(select_dates).fillna(False)
    ratio = float(strict_sel.mean()) if len(strict_sel) else 0.0
    switches = int(strict_daily.astype(int).diff().abs().sum()) if len(strict_daily) > 1 else 0
    degenerate = not (RATIO_LO <= ratio <= RATIO_HI) or switches > MAX_SWITCHES
    return {"strict_ratio": ratio, "switches": switches, "degenerate": degenerate}


def judge(arm: dict, base: dict) -> str:
    """计划书 §4 预注册过线条件(vs C0a 固定宽松)。base=C0a 的 metrics。"""
    if arm["degenerate"]:
        return "DEGENERATE"
    if base["calmar"] is None or arm["calmar"] is None:
        return "N/A"
    dd_gain = abs(base["maxdd"]) - abs(arm["maxdd"])      # 回撤改善(maxdd 符号口径无关)
    ann_loss = arm["annret"] - base["annret"]             # 年化损失(负=更好)
    calmar_ok = arm["calmar"] >= base["calmar"] - 0.1
    if calmar_ok and dd_gain >= 0.02 and ann_loss >= -0.01:
        return "PASS"
    return "FAIL"


# ═══════════════════════════════════════════════════════════════
# 数据装载
# ═══════════════════════════════════════════════════════════════

def load_hs300(start: str, end: str, warmup_days: int = 700) -> pd.Series:
    """沪深300 收盘序列(TDX, 步骤0已验证 2020-01 至今 1584 行)。"""
    from core.connector import TdxConnector
    from core.data_fetcher import DataFetcher
    wstart = (pd.Timestamp(start) - pd.Timedelta(days=warmup_days)).strftime("%Y%m%d")
    TdxConnector.initialize()
    try:
        df = DataFetcher.get_index_data("hs300", wstart, end, dividend_type="none", period="1d")
    finally:
        TdxConnector.close()
    if df is None or df.empty:
        raise RuntimeError("沪深300 指数数据为空")
    close = df["close"].copy()
    close.index = pd.to_datetime(close.index)
    print(f"[INFO] 沪深300: {len(close)} 行 {close.index[0].date()} → {close.index[-1].date()}")
    return close


def load_market_closes(start: str, cache_path: Path) -> pd.DataFrame:
    """全市场 1d 收盘宽表(date × code), 供 C4 宽度。带 parquet 缓存(重跑秒级)。"""
    need_start = pd.Timestamp(start) - pd.Timedelta(days=60)  # MA20 暖机
    if cache_path.exists():
        df = pd.read_parquet(cache_path)
        print(f"[INFO] 宽度缓存命中: {df.shape}")
        return df
    kdir = ROOT / "data" / "kline_cache" / "1d"
    files = sorted(kdir.glob("*.parquet"))
    print(f"[INFO] 读全市场日线 {len(files)} 只(一次性, 约1-2分钟)...")
    series = {}
    t0 = time.time()
    for i, f in enumerate(files):
        try:
            df = pd.read_parquet(f, columns=["date", "close"])
        except Exception:
            continue
        df = df[df["date"] >= need_start]
        if len(df):
            series[f.stem] = pd.Series(df["close"].values, index=pd.to_datetime(df["date"]))
        if (i + 1) % 1000 == 0:
            print(f"  ... {i + 1}/{len(files)} ({time.time() - t0:.0f}s)")
    closes = pd.concat(series, axis=1).sort_index()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    closes.to_parquet(cache_path)
    print(f"[INFO] 全市场收盘宽表: {closes.shape}, 已缓存 {cache_path}")
    return closes


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy-yaml", required=True)
    ap.add_argument("--tag", required=True, help="区间标签 20250719_20260718")
    ap.add_argument("--loose", type=int, default=-2, help="宽松日剔除阈值 total≤此值")
    ap.add_argument("--strict", type=int, default=0, help="严格日剔除阈值 total≤此值")
    ap.add_argument("--skip-breadth", action="store_true", help="跳过 C4 市场宽度(免读全市场日线)")
    ap.add_argument("--skip-backtest", action="store_true", help="只生成信号序列+诊断, 不回测")
    args = ap.parse_args()

    strat = ConfigLoader.load_yaml(args.strategy_yaml)
    bt_cfg = strat.get("backtest", ConfigLoader.load_defaults().get("backtest", {}))
    stop_config = load_stop_config(args.strategy_yaml)
    label = strat["selection"]["formula_name"]
    start, end = args.tag.split("_")

    # 1. 评分一次(与 combo_filter_test 同口径)
    selections = pd.read_parquet(ROOT / "data" / "baseline" / f"{label}_selections_{args.tag}.parquet")
    mf = pd.read_parquet(ROOT / "data" / "factors" / f"moneyflow_{args.tag}.parquet")
    ti = pd.read_parquet(ROOT / "data" / "factors" / f"top_inst_{args.tag}.parquet")
    bt = pd.read_parquet(ROOT / "data" / "factors" / f"block_trade_{args.tag}.parquet")
    print(f"[INFO] {label} 基线信号 {len(selections)} 条, 算三因子评分...")
    scored = score_selections(selections, mf, ti, bt)
    keep_cols = ["stock_code", "select_date", "formula_name"]
    select_dates = pd.DatetimeIndex(pd.to_datetime(scored["select_date"].unique()))

    # 2. 生成各组 严格/宽松 日级序列
    hs300 = load_hs300(start, end)
    arms: dict[str, pd.Series] = {
        "C0a_fixed_loose": pd.Series(False, index=hs300.index),
        "C0b_fixed_strict": pd.Series(True, index=hs300.index),
        "C1_ma200": signal_ma200(hs300),
        "C2_drawdown": signal_drawdown(hs300),
        "C3_volatility": signal_volatility(hs300),
    }
    if not args.skip_breadth:
        closes = load_market_closes(start, ROOT / "output" / "gs_filter" / "_breadth_closes.parquet")
        arms["C4_breadth"] = signal_breadth(closes)

    outdir = ROOT / "output" / "gs_filter"
    outdir.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, strict in arms.items():
        strict = strict.reindex(hs300.index.union(strict.index)).fillna(False).sort_index()
        strict[strict.index > pd.Timestamp(end)] = False  # 窗口外无意义, 统一宽松(不参与评估)
        win = strict[(strict.index >= pd.Timestamp(start)) & (strict.index <= pd.Timestamp(end))]
        diag = arm_diagnostics(win, select_dates)
        pd.DataFrame({"date": win.index, "strict": win.values}).to_parquet(
            outdir / f"signal_series_{name}_{args.tag}.parquet", index=False)
        rows.append({"arm": name, **diag})
        print(f"[信号] {name}: 严格日占比 {diag['strict_ratio']:.1%}, 切换 {diag['switches']} 次"
              f"{' [退化]' if diag['degenerate'] else ''}")

    if args.skip_backtest:
        print("[INFO] --skip-backtest, 信号序列已落盘, 结束")
        return

    # 3. 逐组回测(同 stop_config)
    print("\n[INFO] 逐组回测...")
    results = []
    for row in rows:
        name = row["arm"]
        strict = pd.read_parquet(outdir / f"signal_series_{name}_{args.tag}.parquet")
        thr_map = pd.Series(np.where(strict["strict"], args.strict, args.loose),
                            index=pd.to_datetime(strict["date"]))
        thr_per_row = thr_map.reindex(pd.to_datetime(scored["select_date"])).fillna(args.loose)
        filtered = scored[scored["total_score"].values > thr_per_row.values][keep_cols].copy()
        t0 = time.time()
        r = run_backtest(filtered, bt_cfg, stop_config, start, end, name)
        r.update({"arm": name, "kept": len(filtered), "elapsed_s": int(time.time() - t0), **row})
        results.append(r)
        print(f"  {name}: 剩 {len(filtered)} 条 | 年化 {r['annret']:+.4f} | 回撤 {r['maxdd']:.4f} | "
              f"Calmar {r['calmar']} | ({r['elapsed_s']}s)")

    # 4. 判定(vs C0a)并落盘
    base = next(r for r in results if r["arm"] == "C0a_fixed_loose")
    for r in results:
        r["verdict"] = "BASE" if r["arm"] == "C0a_fixed_loose" else judge(r, base)
    summary = pd.DataFrame(results)[
        ["arm", "kept", "annret", "maxdd", "sharpe", "winrate", "calmar",
         "strict_ratio", "switches", "degenerate", "verdict"]]
    rep = ROOT / "output" / "reports" / f"tournament_{label}_{args.tag}.csv"
    rep.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(rep, index=False, encoding="utf-8-sig")

    print(f"\n=== 锦标赛结果(vs C0a 固定宽松: 年化 {base['annret']:+.4f} 回撤 {base['maxdd']:.4f} "
          f"Calmar {base['calmar']})===")
    print(summary.to_string(index=False))
    print(f"\n[INFO] 明细已存 {rep}")
    print("[判定口径] PASS = Calmar不差于基线0.1 且 回撤改善≥2pp 且 年化损失≤1pp; "
          "DEGENERATE = 严格日占比∉[5%,60%] 或 切换>24 (计划书§4)")


if __name__ == "__main__":
    main()
