# -*- coding: utf-8 -*-
"""把下载的 .lc5 历史五分钟线导入本地 KlineCache (2026-08-02)。

背景: 本地缓存 data/kline_cache/5m 只覆盖 2024-06-27 起 (TDX 服务器地板),
网盘下载的 lc5 从 2004 年起。本脚本把"缓存起点之前"的历史段导入缓存。

口径 (实测锁定, 与 TDX 前复权一致):
  - OHLC 前复权: 用 tq.get_divid_factors 全历史除权事件,
    每事件系数 r(e) = (前一日raw收盘 - 每股分红) / (1 + 送股比 + 配股比) / 前一日raw收盘
    bar 系数 F(day) = 所有 event_date > day 的 r(e) 连乘, OHLC × F(day)
  - volume 不调整 (TDX 前复权量 = 原始量, 比亚迪 10送20 实锤)
  - amount 除以 10000 (TDX 单位万元, lc5 单位元)

接缝校准: 缓存起点日 (如 2024-06-27) lc5 与缓存有重叠 bar,
  k = 缓存close / (lc5_raw_close × F(接缝日)), 全部导入 bar 再乘 k。
  缓存按 ≤80 交易日分段复权 (段外事件不含), 故 k 可远大于 1 (正常);
  k 超出 [0.1, 10] 才视为异常 (代码映射错/除权数据严重缺失), 跳过并记录。
  所有 k 记录在状态文件 k_values 里供复查。

只导入 date < 缓存起点 的 bar (严格小于, 不碰缓存已有 bar)。
无缓存记录的股 (已退市/北交所新代码) 全量导入, k=1 (锚定自身末端)。
重跑安全: 已导入的股缓存起点已前移, 自动跳过。

用法:
    python tools/import_lc5_to_cache.py --limit 10   # 试点
    python tools/import_lc5_to_cache.py              # 全量
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from core.kline_cache import KlineCache

LC5_DTYPE = np.dtype([
    ('date', '<u2'), ('time', '<u2'),
    ('open', '<f4'), ('high', '<f4'), ('low', '<f4'), ('close', '<f4'),
    ('amount', '<f4'), ('vol', '<u4'), ('res', '<u4'),
])

SRC = Path(r"E:\BaiduNetdiskDownload\通达信5分钟-7月22\全部")
STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "kline_cache" \
    / "import_lc5_state.json"

# TDX 文件名 → 本项目代码。sh000001 是上证指数 (项目用 999999.SH)。
def map_code(fname: str) -> str | None:
    stem = fname[:-4]  # 去 .lc5
    mkt, num = stem[:2], stem[2:]
    if mkt == "bj":
        return f"{num}.BJ"
    if mkt == "sh":
        if num == "000001":
            return "999999.SH"
        if num.startswith("000"):
            return f"{num}.SH"      # 上证指数
        if num[0] in "56":
            return f"{num}.SH"      # 600/601/603/605/688
        return None                 # 债券/基金等跳过
    if mkt == "sz":
        if num.startswith("399"):
            return f"{num}.SZ"      # 深证指数
        if num[0] in "0123":
            return f"{num}.SZ"
        return None
    return None


def read_lc5(path: Path) -> pd.DataFrame:
    a = np.fromfile(path, dtype=LC5_DTYPE)
    if len(a) == 0:
        return pd.DataFrame()
    r = a['date'] % 2048
    day = pd.to_datetime(dict(year=a['date'] // 2048 + 2004,
                              month=r // 100, day=r % 100))
    minutes = (a['time'] // 60) * 60 + (a['time'] % 60)
    idx = day + pd.to_timedelta(minutes, unit='m')
    return pd.DataFrame({
        'open': a['open'], 'high': a['high'], 'low': a['low'],
        'close': a['close'], 'volume': a['vol'].astype(float),
        'amount': a['amount'].astype(float) / 10000.0,
    }, index=idx)


def compute_factors(div: pd.DataFrame, daily_close: pd.Series) -> pd.Series:
    """每事件前复权系数 r(e), 与 tqcenter._calculate_forward_factors_from_dividends 同公式。"""
    ratios = {}
    for dt, row in div.sort_index().iterrows():
        try:
            d = pd.Timestamp(dt)
        except Exception:
            continue
        prev = daily_close[daily_close.index < d]
        if prev.empty:
            continue
        prev_close = float(prev.iloc[-1])
        if prev_close <= 0:
            continue
        denom = 1 + float(row['ShareBonus']) / 10.0 + float(row['Allotment']) / 10.0
        if denom <= 0:
            denom = 1.0
        ex_price = (prev_close - float(row['Bonus']) / 10.0) / denom
        ratios[d.normalize()] = ex_price / prev_close
    return pd.Series(ratios).sort_index()


def factor_at(ratios: pd.Series, day: pd.Timestamp) -> float:
    """某日的前复权累计系数 F(day) = 所有 event_date > day 的 r 连乘。"""
    if ratios.empty:
        return 1.0
    pos = np.searchsorted(ratios.index.values, np.datetime64(day), side='right')
    return float(np.prod(ratios.values[pos:]))


def apply_front_adjust(df: pd.DataFrame, ratios: pd.Series) -> pd.DataFrame:
    """OHLC × F(day); F(day) = 所有 event_date > day 的 r 连乘。volume/amount 不动。"""
    if ratios.empty:
        return df
    days = df.index.normalize()
    # 累计因子: 从后往前累乘
    ev_dates = ratios.index.values
    ev_vals = ratios.values
    # 对每个 bar 日, 找第一个 > day 的事件位置
    pos = np.searchsorted(ev_dates, days.values, side='right')
    cum = np.ones(len(ratios) + 1)
    for i in range(len(ratios) - 1, -1, -1):
        cum[i] = cum[i + 1] * ev_vals[i]
    F = cum[pos]
    for col in ['open', 'high', 'low', 'close']:
        df[col] = df[col] * F
    return df


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--codes", default="",
                    help="只处理指定代码, 逗号分隔 (如 000001.SZ,002594.SZ)")
    args = ap.parse_args()

    state = {"done": [], "calib_fail": [], "error": [], "k_values": {}}
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    done = set(state["done"])
    calib_fail = set(state["calib_fail"])
    k_values: dict = state.get("k_values", {})

    cache = KlineCache(
        str(Path(__file__).resolve().parent.parent / "data" / "kline_cache"),
        tdx_fetcher=lambda *a, **k: {},   # 本脚本不经过 miss-fetch
        calendar_fetcher=lambda: DataFetcher.get_trading_dates("SH", "20100101", "20991231"))

    files = sorted(SRC.glob("*/*.lc5"))
    if args.codes:
        wanted = {c.strip() for c in args.codes.split(",") if c.strip()}
        files = [f for f in files if map_code(f.name) in wanted]
    if args.limit:
        files = files[:args.limit]
    total = len(files)
    print(f"[lc5导入] {total} 个文件", flush=True)

    TdxConnector.initialize()
    n_import = n_skip = n_nomap = 0
    t0 = time.time()
    try:
        for i, src in enumerate(files, 1):
            code = map_code(src.name)
            if not code:
                n_nomap += 1
                continue
            if code in done:
                n_skip += 1
                continue
            try:
                df = read_lc5(src)
                if df.empty:
                    done.add(code)
                    continue
                rec = cache._manifest_get(code, "5m")
                cutoff = pd.Timestamp(rec[0]) if rec else None
                if cutoff is not None:
                    hist = df[df.index.normalize() < cutoff.normalize()]
                else:
                    hist = df
                if hist.empty:
                    done.add(code)   # 缓存已覆盖 lc5 全部范围
                    n_skip += 1
                    continue
                # 除权事件 (全历史)
                try:
                    div = DataFetcher._connector().tq().get_divid_factors(code)
                except Exception:
                    div = pd.DataFrame()
                daily_close = df.groupby(df.index.normalize())['close'].last()
                ratios = compute_factors(div, daily_close) if not div.empty else pd.Series()
                adj = apply_front_adjust(hist.copy(), ratios)
                # 接缝校准。注意: 缓存本身按 ≤80 交易日分段拉取, 每段只复权
                # 段内除权事件 (2026-08-02 实测: 002594 缓存首bar 只含 2024-07-29
                # 一次分红, 未含 2025-07-29 的 10送20), 所以 k 可以远大于 1
                # (002594 k≈3.05 属正常, 含义=把接缝后的大事件折算回去)。
                # k 只管"接缝连续", 导入段内部的除权形状由 ratios 保证。
                k = 1.0
                if cutoff is not None:
                    seam_cache = cache._close_at(code, "5m", cutoff)
                    seam_day = df[df.index.normalize() == cutoff.normalize()]
                    if seam_cache and not seam_day.empty:
                        F_seam = factor_at(ratios, cutoff.normalize())
                        mine = float(seam_day['close'].iloc[0]) * F_seam
                        if mine > 0:
                            k = float(seam_cache) / mine
                k_values[code] = round(k, 4)
                if not (0.1 <= k <= 10.0):
                    calib_fail.add(code)
                    print(f"[lc5导入] {code} 校准系数异常 k={k:.4f}, 跳过", flush=True)
                    continue
                if k != 1.0:
                    for col in ['open', 'high', 'low', 'close']:
                        adj[col] = adj[col] * k
                adj.index.name = "date"
                cache._write_merge(code, "5m", adj)
                cache._refresh_manifest(code, "5m")
                done.add(code)
                n_import += 1
            except Exception as e:
                print(f"[lc5导入] {code} 异常: {e}", flush=True)
                state.setdefault("error", []).append(code)
            if i % 100 == 0 or i == total:
                STATE_PATH.write_text(json.dumps(
                    {"done": sorted(done), "calib_fail": sorted(calib_fail),
                     "error": state.get("error", []), "k_values": k_values},
                    ensure_ascii=False), encoding="utf-8")
                print(f"[lc5导入] {i}/{total} 导入{n_import} 跳过{n_skip} "
                      f"未映射{n_nomap} 校准失败{len(calib_fail)} | {time.time()-t0:.0f}s",
                      flush=True)
    finally:
        STATE_PATH.write_text(json.dumps(
            {"done": sorted(done), "calib_fail": sorted(calib_fail),
             "error": state.get("error", []), "k_values": k_values},
            ensure_ascii=False), encoding="utf-8")
        TdxConnector.close()
    print(f"[lc5导入] 完成: 导入{n_import} 跳过{n_skip} 未映射{n_nomap} "
          f"校准失败{len(calib_fail)} | 总耗时 {time.time()-t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
