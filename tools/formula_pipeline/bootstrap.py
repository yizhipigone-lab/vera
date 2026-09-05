# -*- coding: utf-8 -*-
"""流水线数据预备 (2026-08-26, 全新编写)。

run 启动时执行一次 (TDX 在线时拉取并缓存; 离线时用本地缓存):
  1. 沪深 A 股代码+名称 → cache/stocks.json (ST 过滤/北交所排除)
  2. 基准指数日线 → cache/bench_<name>.parquet

缓存目录: data/formula_pipeline_cache/ (与旧流水线缓存隔离)
"""
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

CACHE_DIR = Path(r"E:\1target\VERA\data\formula_pipeline_cache")
KLINE_1D = Path(r"E:\1target\VERA\data\kline_cache\1d")

# 基准指数 (与淘汰标准对照): 主要基准沪深300
BENCH_INDEXES = {"hs300": "hs300", "zz500": "zz500", "zz1000": "zz1000"}


def shsz_from_local() -> list:
    """本地日线缓存中的沪深 A 股代码 (无名称 fallback)。"""
    out = []
    for p in KLINE_1D.glob("*.parquet"):
        stem = p.stem
        if "." not in stem:
            continue
        num, mkt = stem.split(".", 1)
        if mkt == "SH" and num.startswith(("60", "68")):
            out.append(stem)
        elif mkt == "SZ" and num.startswith(("00", "30")):
            out.append(stem)
    return sorted(out)


def load_stocks(refresh=False) -> dict:
    """沪深 A 股 {code: name}。优先本地缓存; --refresh 或无缓存时 TDX 在线拉。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fp = CACHE_DIR / "stocks.json"
    if fp.exists() and not refresh:
        return json.loads(fp.read_text(encoding="utf-8"))
    stocks = {}
    try:
        from core.data_fetcher import DataFetcher
        raw = DataFetcher._ensure_ready() or True
        tq = DataFetcher._connector().tq()
        resp = tq.get_stock_list("50", list_type=1)  # 50 = 沪深A股
        for s in resp:
            if isinstance(s, dict) and s.get("Code") and s.get("Name"):
                stocks[str(s["Code"]).strip()] = str(s["Name"]).strip()
        if stocks:
            fp.write_text(json.dumps(stocks, ensure_ascii=False, indent=1),
                          encoding="utf-8")
            return stocks
    except Exception as e:
        print(f"[prep] TDX 在线拉取失败 ({e}); 使用本地缓存/裸代码模式")
    # fallback: 本地代码, 无名称
    codes = shsz_from_local()
    return {c: "" for c in codes}


def load_benchmark(refresh=False) -> pd.DataFrame:
    """沪深300 指数日线 (date, close)。无缓存且 TDX 不可用 → 空表 (调用方处理)。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fp = CACHE_DIR / "bench_hs300.parquet"
    if fp.exists() and not refresh:
        return pd.read_parquet(fp)
    try:
        from core.data_fetcher import DataFetcher
        df = DataFetcher.get_index_data("hs300", start_time="20040101",
                                        end_time="20261231", period="1d")
        if df is not None and len(df):
            out = pd.DataFrame({"date": pd.to_datetime(df.index),
                                "close": df["close"].values})
            out.to_parquet(fp, index=False)
            return out
    except Exception as e:
        print(f"[prep] TDX 指数拉取失败 ({e})")
    return pd.DataFrame()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    st = load_stocks(args.refresh)
    bm = load_benchmark(args.refresh)
    n_st = sum(1 for v in st.values() if "ST" in v.upper()) if st else 0
    print(f"[prep] 沪深A股 {len(st)} 只 (含ST {n_st}); "
          f"基准行数 {len(bm)} "
          f"({bm['date'].min():%Y-%m-%d} ~ {bm['date'].max():%Y-%m-%d})"
          if len(bm) else "[prep] 基准缺失!")
