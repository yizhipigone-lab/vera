"""core/kline_view.py — field-major K 线 → records/rows 视图变形 (纯函数)。

2026-09-15 深模块治理: /api/benchmark/history 与 /api/stock/kline 两个路由
闭包里各埋了一份"get_kline 输出 (field-major: {字段: DataFrame, code 为列,
DatetimeIndex}) → JSON 行" 的变形, 同一变形家族两份, 下沉到此收口。

- close_records: 收盘序列 → [{date, close}] (指数基准对比线用)
- ohlcv_rows: 五字段 → [{date, open, high, low, close, volume}],
  任何字段 NaN/inf/缺失的行整行丢弃 (FastAPI allow_nan=False, 漏一个就 500)
"""
from __future__ import annotations

import math

_FIELDS = ("Open", "High", "Low", "Close", "Volume")


def close_records(kline: dict | None, code: str) -> list[dict]:
    """field-major K 线 → [{date, close}], 按日期升序; 无数据 → []。"""
    close_df = (kline or {}).get("Close")
    if close_df is None or close_df.empty or code not in close_df.columns:
        return []
    return [{"date": str(idx)[:10], "close": round(float(val), 2)}
            for idx, val in close_df[code].dropna().items()]


def ohlcv_rows(kline: dict | None, code: str) -> list[dict]:
    """field-major K 线 → [{date, open..volume}]; 含 NaN/inf/缺失的行整行丢弃。"""
    close_df = (kline or {}).get("Close")
    if close_df is None or close_df.empty or code not in close_df.columns:
        return []
    series = {}
    for f in _FIELDS:
        df = kline.get(f)
        series[f] = df[code] if (df is not None and code in df.columns) else None
    rows = []
    for idx in sorted(close_df.index):
        vals = []
        for f in _FIELDS:
            s = series[f]
            v = s.get(idx) if s is not None else None
            try:
                v = float(v)
            except (TypeError, ValueError):
                v = float("nan")
            vals.append(v)
        if not all(math.isfinite(v) for v in vals):
            continue
        rows.append({
            "date": (idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime")
                     else str(idx)[:10]),
            "open": vals[0], "high": vals[1], "low": vals[2],
            "close": vals[3], "volume": vals[4],
        })
    return rows
