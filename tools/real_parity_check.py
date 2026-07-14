"""真实数据对拍: 新兼容壳 _simulate_core_v3 vs 旧 _simulate_core_v3_legacy。

用真实 TDX K 线 + 真实 QUANTQQ 选股信号 + 真实 default.yaml 止损配置,
跑 BacktestEngine.run_cached(return_raw=True), monkeypatch 切换壳/legacy,
字节级对比 raw_equity + raw_trades。3 个优先级各跑一次。

用法: python tools/real_parity_check.py
"""
from __future__ import annotations

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, r"E:\NEW_TDX\PYPlugins\user")

import numpy as np
import pandas as pd

import backtest.engine as eng_mod
from utils.config_loader import ConfigLoader
from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from core.formula_runner import FormulaRunner
from backtest.engine import BacktestEngine


STOCKS = ["601872.SH", "600519.SH", "000858.SZ", "601318.SH", "002475.SZ"]
START, END = "20260101", "20260704"


def build_entries(picks: pd.DataFrame, close: pd.DataFrame) -> pd.DataFrame:
    """从 selections 构建对齐 close 的 entries 信号矩阵 (bool)。"""
    entries = pd.DataFrame(False, index=close.index, columns=close.columns)
    if picks is None or picks.empty:
        return entries
    # selections 含 select_date + 股票代码列
    date_col = "select_date" if "select_date" in picks.columns else picks.columns[0]
    code_col = None
    for c in ("stock_code", "code", "stock"):
        if c in picks.columns:
            code_col = c
            break
    if code_col is None:
        return entries
    for _, row in picks.iterrows():
        d = pd.Timestamp(row[date_col]).normalize()
        code = row[code_col]
        if code in close.columns and d in close.index:
            entries.loc[d, code] = True
    return entries


def run_once(eng, close, entries, high, low, open_, stop, lp, lr, nl, priority):
    sc = dict(stop)
    sc["priority"] = priority
    res = eng.run_cached(
        close, entries,
        high.values.astype(np.float64), low.values.astype(np.float64),
        sc, picks_dummy(close), lp, lr, nl,
        filter_limit_up=False, return_raw=True,
        open_np=open_.values.astype(np.float64),
    )
    return res["raw_equity"], res["raw_trades"]


def picks_dummy(close):
    """run_cached 需要 selections 参数但核心不用, 给个占位。"""
    return pd.DataFrame(columns=["select_date", "stock_code"])


def main():
    TdxConnector.initialize()
    try:
        defaults = ConfigLoader.load_defaults()
        bt_cfg = defaults.get("backtest", {})
        stop = defaults.get("stop_loss", {})

        # 1. 真实 K 线
        k = DataFetcher.get_kline(STOCKS, start_time=START, end_time=END,
                                  period="1d", fill_data=False)
        close = k["Close"]
        high = k["High"]
        low = k["Low"]
        open_ = k["Open"]
        # 对齐: 丢掉全 NaN 列
        valid = close.columns[close.notna().any()]
        close, high, low, open_ = close[valid], high[valid], low[valid], open_[valid]
        print(f"[数据] {len(valid)} 只股票 × {len(close)} 根 bar: {list(valid)}")

        # 2. 真实 QUANTQQ 信号
        picks = FormulaRunner.run_stock_selection_with_dates(
            formula_name="QUANTQQ", formula_arg="",
            stock_list=list(valid), start_time=START, end_time=END, stock_period="1d",
        )
        entries = build_entries(picks, close)
        n_signals = int(entries.values.sum())
        print(f"[信号] QUANTQQ 命中 {n_signals} 个信号" +
              (f" (picks={len(picks)})" if picks is not None and not picks.empty else ""))
        if n_signals == 0:
            print("[WARN] 无信号, 对拍仍跑(只验现金路径)")

        # 3. ladder 配置
        levels = stop.get("ladder_tp", {}).get("levels", [])
        lv = sorted(levels, key=lambda x: x["profit"])
        if lv:
            lp = np.array([x["profit"] for x in lv], dtype=np.float64)
            lr = np.array([x["sell_ratio"] for x in lv], dtype=np.float64)
        else:
            lp = np.array([0.06, 0.15], dtype=np.float64)
            lr = np.array([0.3, 0.3], dtype=np.float64)
        nl = len(lp)

        eng = BacktestEngine(bt_cfg)

        # 4. 三优先级对拍
        all_pass = True
        for prio in ["stop_first", "ladder_tp_first", "trailing_first"]:
            # 新壳
            eq_new, tr_new = run_once(eng, close, entries, high, low, open_, stop, lp, lr, nl, prio)
            # 切 legacy
            orig = eng_mod._simulate_core_v3
            eng_mod._simulate_core_v3 = eng_mod._simulate_core_v3_legacy
            try:
                eq_old, tr_old = run_once(eng, close, entries, high, low, open_, stop, lp, lr, nl, prio)
            finally:
                eng_mod._simulate_core_v3 = orig
            # 对比
            eq_ok = np.array_equal(eq_old, eq_new)
            tr_ok = (tr_old.shape == tr_new.shape and
                     (tr_old.shape[0] == 0 or np.array_equal(tr_old, tr_new)))
            status = "PASS" if (eq_ok and tr_ok) else "FAIL"
            if not (eq_ok and tr_ok):
                all_pass = False
            print(f"[{prio}] {status}  equity={eq_ok}({eq_old.shape})  "
                  f"trades={tr_ok}({tr_old.shape[0]} vs {tr_new.shape[0]})")
            if not eq_ok:
                d = np.where(eq_old != eq_new)[0]
                print(f"        equity 首个发散 bar idx={d[0] if len(d) else 'NA'}")
            if not tr_ok and tr_old.shape == tr_new.shape and tr_old.shape[0]:
                dr = np.where(np.any(tr_old != tr_new, axis=1))[0]
                dcols = np.where(np.any(tr_old != tr_new, axis=0))[0]
                print(f"        trades 发散行={dr[:5]} 发散列={dcols}")
                r = dr[0]
                c = dcols[0]
                print(f"        row{r} col{c}: old={tr_old[r, c]!r}  new={tr_new[r, c]!r}")
                print(f"                     old.hex={float(tr_old[r, c]).hex()}")
                print(f"                     new.hex={float(tr_new[r, c]).hex()}")
                print(f"        full old: {tr_old[r]}")
                print(f"        full new: {tr_new[r]}")

        print("\n" + ("=" * 50))
        print("[结论] 真实数据对拍:", "全 PASS -- 新壳与 legacy 字节级一致" if all_pass
              else "有 FAIL -- 见上方细节")
        return 0 if all_pass else 1
    finally:
        TdxConnector.close()


if __name__ == "__main__":
    sys.exit(main())
