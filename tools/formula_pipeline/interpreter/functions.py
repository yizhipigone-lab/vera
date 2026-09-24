# -*- coding: utf-8 -*-
"""通达信函数库 — numpy/scipy 向量化实现 (2026-08-26, 全新编写)。

矩阵约定: (T, N) — T 个交易日 × N 只股票, 时间轴 = axis 0。
NaN 语义 (对齐 TDX, 交叉验证锚定):
  - 无数据 (未上市/停牌) = NaN; 比较运算 NaN → False
  - 严格窗口族 (MA/STD/STDP/AVEDEV/SLOPE/FORCAST): 不足 N 根有效 → NaN
  - 宽松窗口族 (HHV/LLV/SUM/COUNT/EVERY/EXIST): 不足 N 根用现有数据
  - N=0: HHV/LLV → 历史极值; SUM/COUNT → 开市累计
REF 负周期 = 引用未来 → UnsupportedFormula (防隐性未来函数)。
"""
import fnmatch

import numpy as np
import pandas as pd
from scipy.ndimage import maximum_filter1d, minimum_filter1d
from scipy.signal import lfilter


class UnsupportedFormula(Exception):
    """公式用到本地无法实现的函数/语义 → 该公式出局。"""


# ---------------------------------------------------------------- 基础

def _as_arr(x):
    return x if isinstance(x, np.ndarray) else np.asarray(x, dtype=np.float64)


def _int_n(v, name="N"):
    """窗口参数 → int; 矩阵参数 (非常量) → Unsupported。负数 (未来) 拒绝。"""
    a = _as_arr(v)
    if a.ndim == 0 or a.size == 0 or np.allclose(a, np.ravel(a)[0]):
        n = int(np.ravel(a)[0]) if a.size else 0
    else:
        raise UnsupportedFormula(f"{name} 为动态矩阵参数, 不支持")
    if n < 0:
        raise UnsupportedFormula(f"{name}={n} 为负 (未来引用), 拒绝执行")
    return n


def _shift(x: np.ndarray, n: int) -> np.ndarray:
    """向后移 n 根 (REF), 前面填 NaN。"""
    if n == 0:
        return x.copy()
    out = np.full_like(x, np.nan)
    if 0 < n < x.shape[0]:
        out[n:] = x[:-n]
    return out


def _roll_diff(c: np.ndarray, n: int) -> np.ndarray:
    """w[t] = c[t] - c[t-n] (t<n 时按 c 负下标=0)。标准滚动和技巧。"""
    cm = np.concatenate([np.zeros((n,) + c.shape[1:], dtype=c.dtype), c], axis=0)
    return cm[n:] - cm[:-n] if n else c.copy()


def _valid_cnt(x: np.ndarray, n: int) -> np.ndarray:
    """窗口内有效 (非 NaN) bar 数。"""
    return _roll_diff(np.cumsum(~np.isnan(x), axis=0).astype(np.float64), n)


# ---------------------------------------------------------------- 滚动窗口族 (宽松)

def _roll_max(x: np.ndarray, n: int) -> np.ndarray:
    if n == 0:
        return np.maximum.accumulate(x)
    valid = ~np.isnan(x)
    xf = np.where(valid, x, -np.inf)
    out = _roll_filter(xf, maximum_filter1d, n)
    return np.where(_valid_cnt(x, n) > 0, out, np.nan)


def _roll_min(x: np.ndarray, n: int) -> np.ndarray:
    if n == 0:
        return np.minimum.accumulate(x)
    valid = ~np.isnan(x)
    xf = np.where(valid, x, np.inf)
    out = _roll_filter(xf, minimum_filter1d, n)
    return np.where(_valid_cnt(x, n) > 0, out, np.nan)


def _roll_filter(xf, filt, n):
    """右对齐窗口 [t-n+1, t] 的 scipy 滤波。

    scipy 语义: origin 正值使窗口向过去方向偏移; 右对齐恒为 origin=(n-1)//2
    (size=3→1, size=4→1, size=5→2, 均经实验验证)。**必须 axis=0**
    (默认 axis=-1 会沿股票轴滑, (T,1) 时窗口退化为单元素)。
    单测锚定 HHV([3,1,4,1,5],3) = [3,3,4,4,5]。
    """
    return filt(xf, size=n, origin=(n - 1) // 2, mode="nearest", axis=0)


def _roll_sum(x: np.ndarray, n: int) -> np.ndarray:
    """NaN 视 0 参与的滚动和; 不足窗口给部分和; 全 NaN 窗口 → NaN。"""
    if n == 0:
        return np.cumsum(np.nan_to_num(x, nan=0.0), axis=0)
    c = np.cumsum(np.nan_to_num(x, nan=0.0), axis=0)
    w = _roll_diff(c, n)
    return np.where(_valid_cnt(x, n) > 0, w, np.nan)


def _roll_count(cond: np.ndarray, n: int) -> np.ndarray:
    c = (np.nan_to_num(cond, nan=0.0) > 0).astype(np.float64)
    if n == 0:
        return np.cumsum(c, axis=0)
    return _roll_sum(c, n)


def _roll_every(cond: np.ndarray, n: int) -> np.ndarray:
    c = (np.nan_to_num(cond, nan=0.0) > 0)
    if n == 0:
        return np.cumprod(c, axis=0)
    false_cnt = _roll_sum((~c).astype(np.float64), n)
    valid_cnt = _valid_cnt(cond, n)
    return (false_cnt == 0) & (valid_cnt > 0)


def _roll_exist(cond: np.ndarray, n: int) -> np.ndarray:
    c = (np.nan_to_num(cond, nan=0.0) > 0).astype(np.float64)
    if n == 0:
        return np.cumsum(c, axis=0) > 0
    return _roll_sum(c, n) > 0


# ---------------------------------------------------------------- 严格窗口族

def _strict_sum(x: np.ndarray, n: int) -> np.ndarray:
    """窗口和, 不足 n 个有效值 → NaN。"""
    c = np.cumsum(np.nan_to_num(x, nan=0.0), axis=0)
    w = _roll_diff(c, n)
    return np.where(_valid_cnt(x, n) >= n, w, np.nan)


def _f_ma(x, n):
    if n <= 0:
        raise UnsupportedFormula("MA(X,0) 非法")
    return _strict_sum(x, n) / n


def _rolling_stat(x, n, ddof):
    """滚动均值/方差/样本标准差 — cumsum/cumsum² 闭式, 严格窗口。"""
    valid = ~np.isnan(x)
    xf = np.where(valid, x, 0.0)
    c1 = np.cumsum(xf, axis=0)
    c2 = np.cumsum(xf * xf, axis=0)
    w1 = _roll_diff(c1, n)
    w2 = _roll_diff(c2, n)
    cn = _roll_diff(np.cumsum(valid, axis=0).astype(np.float64), n)
    mean = w1 / n
    var = (w2 - w1 * mean) / (n - ddof)
    out = np.sqrt(np.maximum(var, 0))
    return np.where(cn >= n, out, np.nan), np.where(cn >= n, mean, np.nan)


def _ewm(x, alpha):
    """指数递推 Y = alpha*X + (1-alpha)*Y', Y0 = X0 (TDX 语义)。

    lfilter 初值 zi=(1-alpha)*X0 使 y[0]=X0; NaN 当 0 参与递推且输出 NaN。
    """
    valid = ~np.isnan(x)
    xf = np.nan_to_num(x, nan=0.0)
    zi = (1.0 - alpha) * xf[0:1, :] if xf.ndim == 2 else \
        np.array([(1.0 - alpha) * xf[0]])
    y = lfilter([alpha], [1.0, -(1.0 - alpha)], xf, axis=0, zi=zi)[0]
    return np.where(valid, y, np.nan)


# ---------------------------------------------------------------- 函数实现

def _f_ema(x, n):
    return _ewm(x, 2.0 / (n + 1))


def _f_sma(x, n, m):
    """SMA(X,N,M): Y=(M*X+(N-M)*Y')/N。TDX 允许 M>N (alpha>1)。"""
    return _ewm(x, m / n)


def _f_mema(x, n):
    return _ewm(x, 1.0 / n)


def _f_dma(x, a):
    """DMA(X,A): Y=A*X+(1-A)*Y'; A 可为序列。逐 bar 递推 (列向量化)。"""
    T = x.shape[0]
    y = np.full(x.shape, np.nan)
    for t in range(T):
        if t == 0:
            y[t] = x[t]
        else:
            at = a if np.ndim(a) == 0 else a[t]
            prev = np.nan_to_num(y[t - 1], nan=0.0)
            y[t] = np.where(np.isnan(x[t]), y[t - 1], at * x[t] + (1 - at) * prev)
    return y


def _f_cross(a, b):
    pa, pb = _shift(a, 1), _shift(b, 1)
    cross = (a > b) & ~(pa > pb) & ~np.isnan(pa) & ~np.isnan(pb) \
        & ~np.isnan(a) & ~np.isnan(b)
    return cross.astype(np.float64)


def _f_longcross(a, b, n):
    less = (a < b) & ~np.isnan(a) & ~np.isnan(b)
    keep = _roll_every(less.astype(np.float64), n) if n > 1 else less
    prev_keep = np.nan_to_num(_shift(keep.astype(np.float64), 1), nan=0.0) > 0
    return ((_f_cross(a, b) > 0) & prev_keep).astype(np.float64)


def _idx_grid(shape):
    T = shape[0]
    return np.arange(T)[:, None] * np.ones(shape[1])


def _f_barslast(x):
    cond = np.nan_to_num(x, nan=0.0) > 0
    idx = _idx_grid(x.shape)
    fired = np.where(cond, idx, np.nan)
    last = pd.DataFrame(fired).ffill().values
    dist = idx - last
    return np.where(np.isnan(last), np.nan, dist)


def _f_barssince(x):
    cond = np.nan_to_num(x, nan=0.0) > 0
    idx = _idx_grid(x.shape)
    fired = np.where(cond, idx, np.nan)
    first = pd.DataFrame(fired).cummin().values
    return np.where(np.isnan(first), np.nan, idx - first)


def _f_barscount(x):
    cnt = np.cumsum(~np.isnan(x), axis=0)
    return np.where(cnt > 0, cnt, np.nan).astype(np.float64)


def _f_barslastcount(x):
    cond = np.nan_to_num(x, nan=0.0) > 0
    out = np.full(x.shape, np.nan)
    run = np.zeros(x.shape[1])
    for t in range(x.shape[0]):
        run = np.where(cond[t], run + 1, 0.0)
        out[t] = np.where(cond[t], run, np.nan)
    return out


def _f_filter(x, n):
    """FILTER(X,N): X 满足后其后 N 周期内信号全删 (顺序语义, TDX 保真)。
    列向量化递推: 每列维护最近一次输出位置。"""
    cond = np.nan_to_num(x, nan=0.0) > 0
    T, N = cond.shape
    out = np.zeros(cond.shape, dtype=np.float64)
    last_out = np.full(N, -10 ** 9)
    for t in range(T):
        row = cond[t]
        allow = row & (t - last_out > n)
        out[t] = np.where(allow, 1.0, 0.0)
        last_out = np.where(allow, t, last_out)
    return out


def _f_valuewhen(cond, x):
    c = np.nan_to_num(cond, nan=0.0) > 0
    out = np.full(x.shape, np.nan)
    last = np.full(x.shape[1:], np.nan)
    for t in range(x.shape[0]):
        last = np.where(c[t], x[t], last)
        out[t] = last
    return out


def _f_avedev(x, n):
    """AVEDEV(X,N): 平均绝对偏差 (严格窗口, 滑窗视图分块控内存)。"""
    if n <= 0:
        raise UnsupportedFormula("AVEDEV N<=0")
    T, N = x.shape
    out = np.full(x.shape, np.nan)
    if T < n:
        return out
    step = max(1, int(2e7 // max(n, 1)))  # 视图 ≈20MB/块
    for c0 in range(0, N, step):
        c1 = min(N, c0 + step)
        v = np.lib.stride_tricks.sliding_window_view(x[:, c0:c1], n, axis=0)
        nanmask = np.isnan(v).any(axis=-1)
        vv = np.where(np.isnan(v), 0.0, v)
        mean = vv.mean(axis=-1)
        mad = np.abs(vv - mean[..., None]).sum(axis=-1) / n
        out[n - 1:, c0:c1] = np.where(nanmask, np.nan, mad)
    return out


def _hhvbars(x, n):
    """HHVBARS: 距窗口最高点的周期数 (滑窗视图 argmax, 分块)。"""
    if n == 0:
        T, N = x.shape
        out = np.full(x.shape, np.nan)
        for c in range(N):
            col = x[:, c]
            hh = np.maximum.accumulate(col)
            out[:, c] = _bars_since_val(col, hh)
        return out
    return _hhvbars_generic(x, n, low=False)


def _bars_since_val(x_col, running_val):
    """历史最高 (n=0) 的距离 — 逐行递推 (列向量)。"""
    T = x_col.shape[0]
    out = np.full(x_col.shape, np.nan)
    bars = np.zeros(x_col.shape[1])
    for t in range(T):
        newhigh = x_col[t] >= running_val[t]
        bars = np.where(newhigh & ~np.isnan(x_col[t]), 0, bars + 1)
        out[t] = np.where(np.isnan(running_val), np.nan, bars)
    return out


def _llvbars(x, n):
    """LLVBARS: 距窗口最低点的周期数。"""
    if n == 0:
        T, N = x.shape
        out = np.full(x.shape, np.nan)
        for c in range(N):
            col = x[:, c]
            ll = np.minimum.accumulate(col)
            out[:, c] = _bars_since_val_low(col, ll)
        return out
    return _hhvbars_generic(x, n, low=True)


def _hhvbars_generic(x, n, low=False):
    T, N = x.shape
    out = np.full(x.shape, np.nan)
    if T < 1:
        return out
    step = max(1, int(2e7 // max(n, 1)))
    for c0 in range(0, N, step):
        c1 = min(N, c0 + step)
        v = np.lib.stride_tricks.sliding_window_view(x[:, c0:c1], n, axis=0)
        nanmask = np.isnan(v).any(axis=-1)
        vv = np.where(np.isnan(v), np.inf if low else -np.inf, v)
        am = vv.argmin(axis=-1) if low else vv.argmax(axis=-1)
        out[n - 1:, c0:c1] = np.where(nanmask, np.nan, n - 1 - am)
    if n > 1:
        # 前 n-1 根: 不足窗口, 用现有数据 (TDX 宽松族)
        for t in range(min(n - 1, T)):
            seg = x[:t + 1]
            nanm = np.isnan(seg).all(axis=0)
            s2 = np.where(np.isnan(seg), np.inf if low else -np.inf, seg)
            am = s2.argmin(axis=0) if low else s2.argmax(axis=0)
            out[t] = np.where(nanm, np.nan, t - am)
    return out


def _bars_since_val_low(x_col, running_val):
    T = x_col.shape[0]
    out = np.full(x_col.shape, np.nan)
    bars = np.zeros(x_col.shape[1])
    for t in range(T):
        newlow = x_col[t] <= running_val[t]
        bars = np.where(newlow & ~np.isnan(x_col[t]), 0, bars + 1)
        out[t] = np.where(np.isnan(running_val), np.nan, bars)
    return out


def _f_reg(x, n):
    """滚动线性回归 → (slope, 拟合末端值 FORCAST)。自变量 = 窗口内位置 0..n-1。

    窗口 [t-n+1, t] 内 Σ(j·y) = Σ(g·y) - (t-n+1)·Σy (g 为全局索引),
    据此用 cumsum 差分闭式实现 O(1)/窗口。
    """
    T, N = x.shape
    slope = np.full(x.shape, np.nan)
    fitv = np.full(x.shape, np.nan)
    if T < n or n < 2:
        return slope, fitv
    valid = ~np.isnan(x)
    xf = np.where(valid, x, 0.0)
    i = np.arange(n, dtype=np.float64)
    si, sii = i.sum(), (i * i).sum()
    denom = n * sii - si * si
    g = np.arange(T, dtype=np.float64)[:, None]          # 全局索引 (T,1)
    c1 = np.cumsum(xf, axis=0)                            # Σy
    cg = np.cumsum(xf * g, axis=0)                        # Σ(g·y)
    w1 = _roll_diff(c1, n)
    wg = _roll_diff(cg, n)
    cn = _roll_diff(np.cumsum(valid, axis=0).astype(np.float64), n)
    t_grid = np.arange(T, dtype=np.float64)[:, None]
    base = np.maximum(t_grid - (n - 1), 0.0)              # 窗口起点全局索引
    wI = wg - base * w1                                   # Σ(j·y), j∈[0,n-1]
    b = (n * wI - si * w1) / denom
    a = (w1 - b * si) / n
    ok = cn >= n
    slope = np.where(ok, b, np.nan)
    fitv = np.where(ok, a + b * (n - 1), np.nan)
    return slope, fitv


# ---------------------------------------------------------------- 分发层
# 统一入口: 每个函数 fn(args_list, ctx) → (T,N) 或标量; 求值层负责 broadcast。

def _b(x):
    return np.asarray(x, dtype=np.float64)


def _num(args, i, default=None):
    if i >= len(args) or args[i] is None:
        if default is not None:
            return default
        raise UnsupportedFormula("参数缺失")
    return _b(args[i])


def _nn(args, i, name):
    return _int_n(_num(args, i), name)


DISPATCH = {
    "MA": lambda a, c: _f_ma(_b(a[0]), _nn(a, 1, "MA")),
    "SUM": lambda a, c: _roll_sum(_b(a[0]), _nn(a, 1, "SUM")),
    "REF": lambda a, c: _shift(_b(a[0]), _nn(a, 1, "REF")),
    "HHV": lambda a, c: _roll_max(_b(a[0]), _nn(a, 1, "HHV")),
    "LLV": lambda a, c: _roll_min(_b(a[0]), _nn(a, 1, "LLV")),
    "HHVBARS": lambda a, c: _hhvbars(_b(a[0]), _nn(a, 1, "HHVBARS")),
    "LLVBARS": lambda a, c: _hhvbars_generic(_b(a[0]), _nn(a, 1, "LLVBARS"), low=True),
    "EMA": lambda a, c: _f_ema(_b(a[0]), _nn(a, 1, "EMA")),
    "EXPMA": lambda a, c: _f_ema(_b(a[0]), _nn(a, 1, "EXPMA")),
    "MEMA": lambda a, c: _f_mema(_b(a[0]), _nn(a, 1, "MEMA")),
    "SMA": lambda a, c: _f_sma(_b(a[0]), _nn(a, 1, "SMA"),
                               _int_n(_num(a, 2, 1), "SMA_M")),
    "DMA": lambda a, c: _f_dma(_b(a[0]), _num(a, 1, 1.0)),
    "CROSS": lambda a, c: _f_cross(_b(a[0]), _b(a[1])),
    "LONGCROSS": lambda a, c: _f_longcross(_b(a[0]), _b(a[1]),
                                           _nn(a, 2, "LONGCROSS") if len(a) > 2 else 1),
    "BARSLAST": lambda a, c: _f_barslast(_b(a[0])),
    "BARSSINCE": lambda a, c: _f_barssince(_b(a[0])),
    "BARSCOUNT": lambda a, c: _f_barscount(_b(a[0])),
    "BARSLASTCOUNT": lambda a, c: _f_barslastcount(_b(a[0])),
    "FILTER": lambda a, c: _f_filter(_b(a[0]), _nn(a, 1, "FILTER")),
    "VALUEWHEN": lambda a, c: _f_valuewhen(_b(a[0]), _b(a[1])),
    "STD": lambda a, c: _rolling_stat(_b(a[0]), _nn(a, 1, "STD"), 1)[0],
    "STDP": lambda a, c: _rolling_stat(_b(a[0]), _nn(a, 1, "STDP"), 0)[0],
    "VAR": lambda a, c: np.square(_rolling_stat(_b(a[0]), _nn(a, 1, "VAR"), 1)[0]),
    "AVEDEV": lambda a, c: _f_avedev(_b(a[0]), _nn(a, 1, "AVEDEV")),
    "COUNT": lambda a, c: _roll_count(_b(a[0]), _nn(a, 1, "COUNT")),
    "EVERY": lambda a, c: _roll_every(_b(a[0]), _nn(a, 1, "EVERY")).astype(np.float64),
    "EXIST": lambda a, c: _roll_exist(_b(a[0]), _nn(a, 1, "EXIST")).astype(np.float64),
    "IF": lambda a, c: np.where(np.nan_to_num(_b(a[0]), nan=0.0) > 0,
                                 _b(a[1]), _b(a[2])),
    "NOT": lambda a, c: np.where(np.isnan(_b(a[0])), np.nan,
                                  (np.nan_to_num(_b(a[0]), nan=0.0) == 0
                                   ).astype(np.float64)),
    "AND": lambda a, c: ((_b(a[0]) > 0) & (_b(a[1]) > 0)).astype(np.float64),
    "OR": lambda a, c: ((_b(a[0]) > 0) | (_b(a[1]) > 0)).astype(np.float64),
    "ABS": lambda a, c: np.abs(_b(a[0])),
    "MAX": lambda a, c: np.maximum(_b(a[0]), _b(a[1])),
    "MIN": lambda a, c: np.minimum(_b(a[0]), _b(a[1])),
    "POW": lambda a, c: np.power(_b(a[0]), _b(a[1])),
    "SQRT": lambda a, c: np.sqrt(np.clip(_b(a[0]), 0, None)),
    "EXP": lambda a, c: np.exp(_b(a[0])),
    "LN": lambda a, c: np.log(_b(a[0])),
    "ATAN": lambda a, c: np.arctan(_b(a[0])),
    "INTPART": lambda a, c: np.trunc(_b(a[0])),
    "MOD": lambda a, c: np.mod(_b(a[0]), _b(a[1])),
    "BETWEEN": lambda a, c: np.where(np.isnan(_b(a[0])), np.nan,
                                     ((_b(a[0]) >= _b(a[1]))
                                      & (_b(a[0]) <= _b(a[2]))).astype(np.float64)),
    "RANGE": lambda a, c: np.where(np.isnan(_b(a[0])), np.nan,
                                   ((_b(a[0]) > _b(a[1]))
                                    & (_b(a[0]) < _b(a[2]))).astype(np.float64)),
    "NDAY": lambda a, c: _roll_every(
        (((_b(a[0]) > _b(a[1])) & ~np.isnan(_b(a[0])) & ~np.isnan(_b(a[1])))
         ).astype(np.float64), _nn(a, 2, "NDAY") if len(a) > 2 else 1).astype(np.float64),
    "CONST": lambda a, c: _const_val(_b(a[0])),
    "SLOPE": lambda a, c: _f_reg(_b(a[0]), _nn(a, 1, "SLOPE"))[0],
    "FORCAST": lambda a, c: _f_reg(_b(a[0]), _nn(a, 1, "FORCAST"))[1],
}


def _const_val(x):
    flat = np.ravel(x)
    ok = flat[~np.isnan(flat)]
    return np.full(x.shape, ok[0] if ok.size else np.nan)


def call_function(name: str, args, ctx):
    """分发; CODELIKE 特殊处理 (字符串参数); 未知函数 → Unsupported。

    0 维标量实参统一广播为 (T,N) 全同矩阵——窗口参数 (如 REF(C,5) 的 5)
    由 _int_n 的"全同值放行"提取回常量, 双参比较类 (如 CROSS(A,1)) 得以
    矩阵化运算 (标量透传会在 _shift 等处崩溃)。
    """
    if name in DISPATCH:
        T, N = ctx.get("T", 1), ctx.get("N", 1)
        args2 = []
        for a in args:
            if isinstance(a, str):
                args2.append(a)  # 字符串 (CODELIKE pattern 等)
            elif not isinstance(a, np.ndarray):
                a = np.full((T, N), float(a))  # python 标量
            elif a.ndim == 0:
                a = np.full((T, N), float(a))  # 0 维
            args2.append(a)
        return DISPATCH[name](args2, ctx)
    if name == "CODELIKE":
        pat = args[0] if args else ""
        codes = ctx.get("codes", [])
        T = ctx.get("T", 1)
        col = np.array([1.0 if _code_like(c, pat) else 0.0 for c in codes])
        return np.broadcast_to(col, (T, len(codes))).astype(np.float64).copy()
    if name in ("REVERSE", "REFDATE", "BARSSINCEN", "CYC", "L2_AMO", "TESTSKIP"):
        raise UnsupportedFormula(f"{name} 不支持")
    raise UnsupportedFormula(f"未知函数 {name}")


def _code_like(code: str, pat: str) -> bool:
    p = str(pat)
    if "%" not in p:
        p = p + "%"
    return fnmatch.fnmatch(code, p)
