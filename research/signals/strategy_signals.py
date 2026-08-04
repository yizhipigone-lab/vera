"""策略变体信号生成器: 因果基础信号 + 特征增强 + 过滤/排序。

特征全部仅用 T 日及以前数据 (严格因果)。
输出: 长表 (stock_code, select_date, 特征列...), 供变体筛选与引擎回测。
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from research.signals.cyw_signal import compute_signals, EXCLUDE_CODES


def _features(df: pd.DataFrame, sig: pd.Series) -> pd.DataFrame:
    """对信号 bar 提取因果特征。"""
    c, o, h, l, v = (df[x].astype(float) for x in ["close", "open", "high", "low", "volume"])
    rng = (h - l).replace(0, np.nan)
    out = pd.DataFrame(index=df.index)
    out["vol_ratio"] = v / v.rolling(5, min_periods=1).mean()          # 量比(对5日均量)
    out["body"] = (c - o) / rng                                        # 实体强度 [-1,1]
    out["close_pos"] = (c - l) / rng                                   # 收盘在当日振幅位置
    ma20 = c.rolling(20, min_periods=1).mean()
    ma60 = c.rolling(60, min_periods=1).mean()
    out["above_ma20"] = (c > ma20).astype(float)
    out["above_ma60"] = (c > ma60).astype(float)
    out["ma60_slope"] = (ma60 / ma60.shift(5) - 1).fillna(0)           # 60日线5日斜率
    hhv55 = h.rolling(55, min_periods=1).max()
    llv55 = l.rolling(55, min_periods=1).min()
    out["pos55"] = ((c - llv55) / (hhv55 - llv55).replace(0, np.nan)).clip(0, 1)
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    out["atr14_pct"] = tr.rolling(14, min_periods=1).mean() / c        # ATR/收盘
    out["ret1"] = c / c.shift(1) - 1
    out["score"] = (out["close_pos"].clip(0, 1) * out["vol_ratio"].clip(0, 3)
                    * (1 + out["body"].clip(-1, 1)))                   # 反转质量分
    return out[sig]


def scan_features(kline_dir: str, start: str = "", end: str = "") -> pd.DataFrame:
    records = []
    t0 = time.time()
    files = sorted(Path(kline_dir).glob("*.parquet"))
    for k, f in enumerate(files):
        code = f.stem
        if code.split(".")[0] in EXCLUDE_CODES:
            continue
        try:
            df = pd.read_parquet(f)
        except Exception:
            continue
        if len(df) < 60:
            continue
        df = df.sort_values("date").reset_index(drop=True)
        sig = compute_signals(df, mode="causal")
        if not sig.any():
            continue
        feat = _features(df, sig)
        feat["stock_code"] = code
        feat["select_date"] = df.loc[sig, "date"].to_numpy()
        records.append(feat.reset_index(drop=True))
        if (k + 1) % 1000 == 0:
            print(f"  ..{k+1}/{len(files)} elapsed={time.time()-t0:.0f}s", flush=True)
    out = pd.concat(records, ignore_index=True) if records else pd.DataFrame()
    if len(out):
        out["select_date"] = pd.to_datetime(out["select_date"])
        ds = out["select_date"].dt.strftime("%Y%m%d")
        m = pd.Series(True, index=out.index)
        if start:
            m &= ds >= start
        if end:
            m &= ds <= end
        out = out[m].sort_values(["select_date", "stock_code"]).reset_index(drop=True)
    print(f"[DONE] features={len(out)} stocks={out['stock_code'].nunique() if len(out) else 0} "
          f"elapsed={time.time()-t0:.0f}s", flush=True)
    return out


if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else ""
    end = sys.argv[2] if len(sys.argv) > 2 else ""
    outp = sys.argv[3] if len(sys.argv) > 3 else "research/signals/causal_features.parquet"
    df = scan_features("data/kline_cache/1d", start=start, end=end)
    df.to_parquet(outp)
    print(f"saved -> {outp}")
