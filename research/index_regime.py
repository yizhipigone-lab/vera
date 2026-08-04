"""上证指数日线 → 市场周期标签 (bull/bear/range) + regime_ok 过滤位。

判定 (只用 T 日及以前数据):
  bull  : C > MA250 且 MA250 的 20 日斜率 > 0
  bear  : C < MA250 且 MA250 的 20 日斜率 < 0
  range : 其他
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from core.data_fetcher import DataFetcher


def build(start="20190101", end="20260801", out="research/signals/index_regime.parquet"):
    df = DataFetcher.get_index_data("shanghai", start, end, dividend_type="none", period="1d")
    if "date" not in df.columns:
        df = df.reset_index().rename(columns={df.index.name or "index": "date"})
        if "date" not in df.columns:
            df = df.rename(columns={df.columns[0]: "date"})
    df = df.sort_values("date").reset_index(drop=True)
    c = df["close"].astype(float)
    ma250 = c.rolling(250, min_periods=60).mean()
    slope = ma250 / ma250.shift(20) - 1
    regime = pd.Series("range", index=df.index)
    regime[(c > ma250) & (slope > 0)] = "bull"
    regime[(c < ma250) & (slope < 0)] = "bear"
    df["regime"] = regime
    df["ret20"] = c / c.shift(20) - 1
    df.to_parquet(out)
    print(df.groupby("regime").size())
    print(f"saved -> {out} ({df['date'].min()} ~ {df['date'].max()})")


if __name__ == "__main__":
    build()
