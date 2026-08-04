"""超赢王牛股 (用户 QUANTQQ) 通达信公式 → Python 信号实现, 两种口径:

公式原文 (用户贴出, TK 过滤在外层处理):
    MA5:=EMA(C,5); MA13:=EMA(C,13);
    UP:=DRAWLINE(L=LLV(L,BARSLAST(CROSS(MA13,MA5))+1), LLV(...),
                 H=HHV(H,BARSLAST(CROSS(MA5,MA13))+1), HHV(...), 0);
    DOWN:=DRAWLINE(H=HHV(...), HHV(...), L=LLV(...), LLV(...), 0);
    HRY33:=SQRT(HHV(HIGH,55)*LLV(LOW,55));
    TJ2:=REF(DOWN,1)<REF(DOWN,2) AND UP>REF(DOWN,1) AND C<HRY33;

两种口径:
1. causal  — 实盘尾盘可见口径: DRAWLINE 线段在终点锚定(COND2 触发)之前不存在,
   EXPAND=0 不延伸, 因此 t 时刻只能看到"已完成的线段"。数学化简后:
   - UP[t] 可见 ⟺ t 本身是 UP 终点 (创新高 bar), 值 = H[t]
   - DOWN[t-1] 可见 ⟺ 最近 DOWN 终点 ∈ {t-1, t}, 端点值=LLV, 内部点=线性插值
   - DOWN[t-2] 可见 ⟺ 最近 DOWN 终点 ∈ {t-2, t-1, t}
2. replica — TDX 历史图重绘口径: 所有线段最终全部画出, 后画覆盖先画,
   中间 bar 被回填插值。用于复刻服务端回测信号 (含未来函数)。

约定:
  A = "自上次死叉(MA13上穿MA5)以来创新低"  (UP 的 COND1 / DOWN 的 COND2)
  B = "自上次金叉(MA5上穿MA13)以来创新高"  (UP 的 COND2 / DOWN 的 COND1)
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _ema(s: pd.Series, n: int) -> pd.Series:
    """TDX EMA: alpha=2/(n+1), 首bar为种子 — 与 pandas ewm(adjust=False) 一致。"""
    return s.ewm(span=n, adjust=False).mean()


def _cross(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a > b) & (a.shift(1) <= b.shift(1))


def _rolling_interp(pos: np.ndarray, x0: np.ndarray, y0: np.ndarray,
                    x1: np.ndarray, y1: np.ndarray) -> np.ndarray:
    """在 (x0,y0)-(x1,y1) 线段上取 pos 处的插值; x0==x1 时取 y1。"""
    span = x1 - x0
    w = np.where(span > 0, (pos - x0) / np.maximum(span, 1), 1.0)
    return y0 + (y1 - y0) * w


def compute_signals(df: pd.DataFrame, mode: str = "causal") -> pd.Series:
    """输入单股日线 (date 升序, 列 open/high/low/close), 输出 bool Series (与 df 对齐)。

    mode: "causal" | "replica"
    """
    n = len(df)
    idx = np.arange(n)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)

    ma5 = _ema(df["close"], 5)
    ma13 = _ema(df["close"], 13)
    gold = _cross(ma5, ma13).to_numpy()    # MA5 上穿 MA13
    death = _cross(ma13, ma5).to_numpy()   # MA13 上穿 MA5

    # 分段: 死叉 bar 开启新段 (含死叉 bar 本身, 对应 BARSLAST+1 窗口)
    seg_d = pd.Series(death).cumsum().to_numpy()
    seg_g = pd.Series(gold).cumsum().to_numpy()
    min_since_death = pd.Series(low).groupby(seg_d).cummin().to_numpy()
    max_since_gold = pd.Series(high).groupby(seg_g).cummax().to_numpy()

    A = low == min_since_death    # 自死叉以来创新低
    B = high == max_since_gold    # 自金叉以来创新高

    hry33 = np.sqrt(
        df["high"].rolling(55, min_periods=1).max().to_numpy()
        * df["low"].rolling(55, min_periods=1).min().to_numpy()
    )

    # lastA[t] = 最近 <=t 的 A bar 下标 (无则 -1); lastB_before[t] = 最近 <t 的 B bar 下标
    lastA = pd.Series(np.where(A, idx, -1)).replace(-1, np.nan).ffill().fillna(-1).to_numpy(dtype=int)
    lastB = pd.Series(np.where(B, idx, -1)).replace(-1, np.nan).ffill().fillna(-1).to_numpy(dtype=int)
    lastB_before = np.roll(lastB, 1)
    lastB_before[0] = -1
    lastA_before = np.roll(lastA, 1)
    lastA_before[0] = -1

    if mode == "causal":
        # UP[t]: t 必须是 B (终点=今天) 且 t 之前存在 A (配对起点)
        U = np.where(B & (lastA_before >= 0), high, np.nan)

        # DOWN[t-1]: 最近 A 终点 j=lastA[t] ∈ {t-1, t}
        j = lastA
        D1 = np.full(n, np.nan)
        m1 = j == (idx - 1)                       # 终点在昨天 → 端点值
        D1[m1] = low[idx[m1] - 1] if m1.any() else D1[m1]
        m2 = j == idx                             # 终点在今天 → t-1 为线段内部点
        if m2.any():
            sb = lastB_before[m2]                 # DOWN 线段起点 = 该 A 之前最近的 B
            valid = sb >= 0
            ii = np.where(m2)[0][valid]
            sbv = sb[valid]
            D1[ii] = _rolling_interp(ii - 1, sbv, high[sbv], ii.astype(float), low[ii])
        # DOWN[t-2]: 最近 A 终点 ∈ {t-2, t-1, t}
        D2 = np.full(n, np.nan)
        m3 = j == (idx - 2)
        D2[m3] = low[idx[m3] - 2] if m3.any() else D2[m3]
        m4 = j >= (idx - 1)                       # 终点 t-1 或 t → t-2 为内部点
        if m4.any():
            sb = lastB_before[m4]
            valid = sb >= 0
            ii = np.where(m4)[0][valid]
            sbv = sb[valid]
            D2[ii] = _rolling_interp(ii - 2, sbv, high[sbv],
                                     j[ii].astype(float), low[j[ii]])
        tj2 = (D1 < D2) & (U > D1) & (close < hry33)
        tj2 &= ~np.isnan(D1) & ~np.isnan(D2) & ~np.isnan(U)
        return pd.Series(tj2, index=df.index)

    elif mode == "replica":
        # 最终图态: 按完成顺序画全部线段, 后画覆盖先画
        UP = np.full(n, np.nan)
        DOWN = np.full(n, np.nan)
        for j in np.where(B)[0]:                  # UP: 最近A→B
            a = lastA_before[j]
            if a >= 0:
                span = np.arange(a, j + 1)
                UP[a:j + 1] = _rolling_interp(span, float(a), low[a], float(j), high[j])
        for j in np.where(A)[0]:                  # DOWN: 最近B→A
            b = lastB_before[j]
            if b >= 0:
                span = np.arange(b, j + 1)
                DOWN[b:j + 1] = _rolling_interp(span, float(b), high[b], float(j), low[j])
        D1 = np.roll(DOWN, 1); D1[0] = np.nan
        D2 = np.roll(DOWN, 2); D2[:2] = np.nan
        tj2 = (D1 < D2) & (UP > D1) & (close < hry33)
        tj2 &= ~np.isnan(D1) & ~np.isnan(D2) & ~np.isnan(UP)
        return pd.Series(tj2, index=df.index)

    raise ValueError(f"unknown mode: {mode}")


EXCLUDE_CODES = {"300687", "920001"}  # TK: CODE<>300687 AND CODE<>920001


def scan_universe(kline_dir: str, mode: str = "causal",
                  start: str = "", end: str = "",
                  codes: list[str] | None = None) -> pd.DataFrame:
    """全市场扫描, 返回 selections DataFrame (stock_code, select_date)。"""
    from pathlib import Path

    records = []
    files = sorted(Path(kline_dir).glob("*.parquet"))
    for f in files:
        code = f.stem
        if code.split(".")[0] in EXCLUDE_CODES:
            continue
        if codes is not None and code not in codes:
            continue
        try:
            df = pd.read_parquet(f)
        except Exception:
            continue
        if len(df) < 60:
            continue
        df = df.sort_values("date").reset_index(drop=True)
        sig = compute_signals(df, mode=mode)
        if not sig.any():
            continue
        dates = df.loc[sig, "date"]
        for d in dates:
            ds = pd.Timestamp(d).strftime("%Y%m%d")
            if start and ds < start:
                continue
            if end and ds > end:
                continue
            records.append((code, d))
    out = pd.DataFrame(records, columns=["stock_code", "select_date"])
    if len(out):
        out["select_date"] = pd.to_datetime(out["select_date"])
        out = out.sort_values(["select_date", "stock_code"]).reset_index(drop=True)
    return out


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "causal"
    start = sys.argv[2] if len(sys.argv) > 2 else ""
    end = sys.argv[3] if len(sys.argv) > 3 else ""
    out = sys.argv[4] if len(sys.argv) > 4 else f"research/signals/py_{mode}_{start}_{end}.parquet"
    import time
    t0 = time.time()
    sel = scan_universe("data/kline_cache/1d", mode=mode, start=start, end=end)
    sel.to_parquet(out)
    print(f"[DONE] mode={mode} {start}~{end} signals={len(sel)} "
          f"stocks={sel['stock_code'].nunique() if len(sel) else 0} "
          f"elapsed={time.time()-t0:.0f}s -> {out}")
