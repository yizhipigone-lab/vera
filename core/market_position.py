"""core/market_position.py — 大盘位置指标纯数学 (2026-09-17)。

定位: **零 IO / 零网络 / 零 trade 依赖**的纯函数层。输入 DataFrame/Series,
输出数字或 dict, 可独立单测 (照 core/index_regime.py 与 core/window.py 的分层)。
取数、落盘、组装 Markdown、推送在 core/market_position_runner.py。

为什么单独一个模块: 大盘位置最容易被写出第二份口径(前端算一遍、报告算一遍、
大脑再算一遍 → 必然漂移)。数学只此一份, 三个消费方(页签/体温表/大脑)共用。

口径复用 (不写第二份):
    - 牛熊状态 → core/index_regime.classify (治理III W1-b 单一真相源)
    - 涨跌停幅度 → core/limit_ratio.limit_ratio (2026-08-01 P1 单一真相源)

【重要】宽度计算一律先按"有成交"掩码: 实测本地日线缓存盘前抓数会造出
"有日期、无成交"的空壳 bar (2026-09-16 09:16 写入的 000001.SZ 那根
open=high=low=close 且 volume=0)。不掩码就会拿盘前价冒充收盘价。

公开接口 (8, 铁律 8 以内):
    index_position(closes, *, window_bars) -> dict
    index_position_series(closes, *, window_bars) -> DataFrame
    breadth_frame(close_df, volume_df, *, ma_window, hl_window) -> DataFrame
    last_valid_date(volume_df, *, min_ratio) -> Timestamp | None
    limit_counts(close_df, volume_df, ratios, date) -> dict
    limit_counts_series(close_df, volume_df, ratios, dates) -> dict
    similar_days(target, history, *, top_n, exclude_recent, min_gap,
                 quantile, max_band, features) -> dict
    forward_return(closes, start, horizon) -> float | None
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "PCT_WINDOW_BARS", "MA_WINDOW", "HL_WINDOW", "RET_1Y_BARS",
    "RECENT_EXCLUDE_BARS", "MIN_MATCH_GAP_BARS", "REGIME_20_THRESHOLD",
    "SIMILAR_FEATURES", "HISTORY_COLUMNS", "POSITION_COLUMNS",
    "index_position", "index_position_series", "breadth_frame",
    "last_valid_date", "limit_counts", "limit_counts_series",
    "similar_days", "forward_return",
]

#: 十年 ≈ 2430 个交易日 (实测 2016-01~2026-09 共 2602 个交易日 / 10.7 年)
PCT_WINDOW_BARS = 2430
#: 宽度用的均线窗口 (站上 20 日均线占比)
MA_WINDOW = 20
#: 宽度用的新高/新低窗口 (创 60 日新高/新低家数)
HL_WINDOW = 60
#: 一年 ≈ 243 个交易日 (实测 2016-01~2026-09 共 2602 个交易日 / 10.7 年)
RET_1Y_BARS = 243
#: 照镜子时排除最近多少个交易日 (**2026-09-17 由 20 改为 252**: 原值等于允许
#: 拿"上个月"当历史参照, 是数据窥探。252 = 一年)
RECENT_EXCLUDE_BARS = 252
#: 两个入选"相似日"之间至少隔多少个交易日 (避免一次照出一串连续同一天)
MIN_MATCH_GAP_BARS = 20
#: 相似日匹配用的特征列 (必须全部是"当日已知"的量, 不含任何未来信息)
SIMILAR_FEATURES = ("sh_pct", "hs300_pct", "above_ma20_pct",
                    "hl_spread_pct", "vol_ann_20", "amount_pct_1y")
#: 历史指标序列的列 (runner 组装, similar_days 消费)
HISTORY_COLUMNS = SIMILAR_FEATURES


def _clean(closes) -> pd.Series:
    """→ 正数、非 NaN、按日期升序的 float Series (索引转 DatetimeIndex)。"""
    s = pd.Series(closes).astype(float)
    if not isinstance(s.index, pd.DatetimeIndex):
        s.index = pd.to_datetime(s.index)
    s = s[~s.index.duplicated(keep="last")].sort_index()
    s = s.dropna()
    return s[s > 0]


#: 位置序列的输出列 (index_position 的 dict 键即此列名)
#: `regime` = 年线(MA250)斜率口径; `regime_20` = **20% 法则**口径。
#: **两条口径必须并列给, 不许挑一个** (§14.6): 同一天同两封外部研究邮件,
#: 一条 20% 法则说"现在是牛市", 一条十特征历史类比说"牛市已结束" —— 相差 47 分钟、
#: 同一批数据, 结论相反。根因就是口径不同。挑一个口径就会得出"唯一答案"的假象。
POSITION_COLUMNS = ("close", "pct_10y", "from_high_pct", "vol_ann_20",
                    "vol_ann_60", "ret_1y_pct", "ma250_dev_pct", "regime",
                    "regime_20")
#: 20% 法则的阈值: 从低点涨 20% 确认牛, 从高点跌 20% 确认熊
REGIME_20_THRESHOLD = 0.20


def _regime_20pct(closes, *, threshold: float = REGIME_20_THRESHOLD) -> pd.Series:
    """**20% 法则**的牛熊划分 (与年线斜率口径并列的第二把尺子)。

    规则 (口语版): 从最近的低点**涨够 20%** 就算牛, 从最近的高点**跌够 20%**
    就算熊, 都没够就是震荡。状态**粘滞** —— 不到 20% 不换状态, 所以不会天天跳。

    为什么要有它: 年线斜率口径偏"慢而钝"(要跌破年线且年线走平才转),
    20% 法则偏"看幅度"。两者**经常不一致**, 而不一致本身就是信息
    (例如价格已从高点跌 18%、但还在年线上方 → 一个口径说震荡、一个说牛)。

    实现要点: 牛/熊各自记住"本轮起点"(牛记起点低点、熊记起点高点), 换状态时
    把起点重置为当天 —— 这样每一轮区间的涨跌幅都能从真正的转折点算起。
    """
    s = _clean(closes)
    if len(s) == 0:
        return pd.Series(dtype=object)
    labels = np.empty(len(s), dtype=object)
    vals = s.to_numpy(dtype=float)
    state = "range"
    peak = trough = vals[0]
    for i, px in enumerate(vals):
        if state == "bull":
            peak = max(peak, px)
            if px <= peak * (1.0 - threshold):
                state, peak, trough = "bear", px, px
        elif state == "bear":
            trough = min(trough, px)
            if px >= trough * (1.0 + threshold):
                state, peak, trough = "bull", px, px
        else:                                   # range: 双向都可能突破
            peak, trough = max(peak, px), min(trough, px)
            if px >= trough * (1.0 + threshold):
                state, peak = "bull", px        # trough 保留 = 本轮牛市起点
            elif px <= peak * (1.0 - threshold):
                state, trough = "bear", px      # peak 保留 = 本轮熊市起点
        labels[i] = state
    return pd.Series(labels, index=s.index, dtype=object)


def index_position_series(closes, *,
                          window_bars: int = PCT_WINDOW_BARS) -> pd.DataFrame:
    """单指数的**全序列**位置指标 → DataFrame (索引=日期, 列=POSITION_COLUMNS)。

    存在的理由: 十年回填要 2000+ 天的指标, 逐日调 index_position 等于把
    同一段均线重算 2000 遍。这里全序列向量化算一次, 末行即"今天的位置"。
    **index_position 就是这个函数的末行** —— 两个接口不可能漂移。

    口径: pct_10y 用 rolling(window_bars).rank(pct=True) —— 当前值在过去
    window_bars 个交易日里的百分位; **min_periods=750 (满 3 年才给数)** ——
    指标名为"十年百分位", 但指数缓存最早只到 2013 年, 2016~2019 年的百分位
    实际用的是不足十年的窗口。门槛定 3 年是为了不让"半年的百分位"冒充十年,
    这段差异已写进 CALIBER_FOOTER 的已知偏差, 报告与页面都会带出。
    regime 与 ma250_dev_pct 走 core/index_regime 单一真相源。
    """
    from core.index_regime import (BULL, BEAR, RANGE, ma_and_slope,
                                   regime_series)
    s = _clean(closes)
    if len(s) < 2:
        return pd.DataFrame(columns=list(POSITION_COLUMNS))
    bars = int(window_bars)
    min_bars = min(750, bars)
    out = pd.DataFrame(index=s.index)
    out["close"] = s.round(2)
    rank = s.rolling(bars, min_periods=min_bars).rank(pct=True)
    out["pct_10y"] = (rank * 100).round(1)
    hi10 = s.rolling(bars, min_periods=min_bars).max()
    out["from_high_pct"] = ((s / hi10 - 1) * 100).round(1)
    r = np.log(s / s.shift(1))
    out["vol_ann_20"] = (r.rolling(20).std() * np.sqrt(252) * 100).round(1)
    out["vol_ann_60"] = (r.rolling(60).std() * np.sqrt(252) * 100).round(1)
    if len(s) > RET_1Y_BARS:
        out["ret_1y_pct"] = ((s / s.shift(RET_1Y_BARS) - 1) * 100).round(1)
    else:
        out["ret_1y_pct"] = np.nan
    ma, _slope = ma_and_slope(s)
    out["ma250_dev_pct"] = ((s / ma - 1) * 100).round(1)
    out["regime"] = regime_series(s)
    out["regime_20"] = _regime_20pct(s)
    # 只有 MA 未成形处才不给牛熊/偏离年线 (短样本不冒充)。
    # **不可**拿十年分位的可用性连坐: 400 根数据足以判牛熊, 但不足以给十年百分位,
    # 两者门槛不同 (2026-09-17 首版把两者绑在一起, 导致 3 年样本判不出牛熊)。
    # regime_20 不依赖均线, 故**不**跟着置空 (它从第一根就有定义)。
    out.loc[ma.isna(), ["ma250_dev_pct", "regime"]] = None
    return out[list(POSITION_COLUMNS)]


def index_position(closes, *, window_bars: int = PCT_WINDOW_BARS) -> dict:
    """单指数**最后一个交易日**的位置指标 → dict (样本不足的字段为 None)。

    键: close / pct_10y(十年百分位) / from_high_pct(距十年最高) /
        vol_ann_20 / vol_ann_60(年化波动率) / ret_1y_pct /
        ma250_dev_pct(偏离年线) / regime(牛熊, 年线斜率口径) /
        regime_20(牛熊, 20% 法则口径)。
    """
    out = {k: None for k in POSITION_COLUMNS}
    df = index_position_series(closes, window_bars=window_bars)
    if len(df) == 0:
        return out
    last = df.iloc[-1]
    for k in POSITION_COLUMNS:
        v = last[k]
        if v is None or (isinstance(v, float) and v != v):
            out[k] = None
        elif k in ("regime", "regime_20"):
            out[k] = str(v)
        elif k == "close":
            out[k] = round(float(v), 2)
        else:
            out[k] = round(float(v), 1)
    return out


def last_valid_date(volume_df: pd.DataFrame,
                    *, min_ratio: float = 0.5) -> pd.Timestamp | None:
    """最后一个"全市场真在成交"的交易日 (防盘前空壳 bar 冒充收盘)。

    判据: 该日成交量 > 0 的股票数 / 该日有记录的股票数 ≥ min_ratio。
    全都不达标返 None (调用方按"无可用数据"处理, 不猜)。
    """
    if volume_df is None or len(volume_df) == 0:
        return None
    has_rec = volume_df.notna().sum(axis=1)
    traded = (volume_df > 0).sum(axis=1)
    ratio = traded / has_rec.replace(0, np.nan)
    ok = ratio >= float(min_ratio)
    ok = ok.fillna(False)
    if not bool(ok.any()):
        return None
    return pd.Timestamp(volume_df.index[ok][-1])


def breadth_frame(close_df: pd.DataFrame, volume_df: pd.DataFrame, *,
                  ma_window: int = MA_WINDOW,
                  hl_window: int = HL_WINDOW) -> pd.DataFrame:
    """市场宽度时间序列 → DataFrame (索引=日期)。

    列: traded(当日有成交家数) / traded_ratio(成交家数占比, 判空壳 bar 用) /
        above_ma20_pct / above_ma60_pct / new_high(创 hl_window 日新高家数) /
        new_low / hl_spread(新高−新低)。

    **停牌与空壳 bar 一律先掩码成 NaN**: 只有"真成交了"的那天才参与
    均线比较与高低点统计 (分母也是"有成交家数", 与市场惯例一致)。
    新高低用 min_periods=hl_window 满窗才算, 防新股上市头几天被记成"创新高"。
    """
    if close_df is None or volume_df is None or len(close_df) == 0:
        return pd.DataFrame(
            columns=["traded", "traded_ratio", "above_ma20_pct",
                     "above_ma60_pct", "new_high", "new_low", "hl_spread"])
    close = close_df.astype(float).sort_index()
    vol = volume_df.reindex(index=close.index, columns=close.columns)
    traded = vol > 0
    c = close.where(traded)          # 掩码: 无成交的收盘价不参与计算
    ma20 = c.rolling(ma_window, min_periods=max(2, ma_window // 2)).mean()
    ma60 = c.rolling(60, min_periods=30).mean()
    hi = c.rolling(hl_window, min_periods=hl_window).max()
    lo = c.rolling(hl_window, min_periods=hl_window).min()
    denom = traded.sum(axis=1).astype(float)
    denom[denom == 0] = np.nan
    out = pd.DataFrame(index=close.index)
    out["traded"] = traded.sum(axis=1)
    out["above_ma20_pct"] = ((c > ma20) & traded).sum(axis=1) / denom * 100
    out["above_ma60_pct"] = ((c > ma60) & traded).sum(axis=1) / denom * 100
    out["new_high"] = (c >= hi).where(traded).sum(axis=1)
    out["new_low"] = (c <= lo).where(traded).sum(axis=1)
    out["hl_spread"] = out["new_high"] - out["new_low"]
    # 成交占比: 调用方据此判"这一天是不是真的交易过"(空壳 bar 的比例会掉到 1% 以下)
    _has_rec = close.notna().sum(axis=1).astype(float)
    _has_rec[_has_rec == 0] = np.nan
    out["traded_ratio"] = traded.sum(axis=1) / _has_rec
    return out


def limit_counts(close_df: pd.DataFrame, volume_df: pd.DataFrame,
                 ratios, date) -> dict:
    """某交易日涨停/跌停家数 (本地推导, 离线可算)。单日便捷入口。"""
    return limit_counts_series(close_df, volume_df, ratios, [date]).get(
        pd.Timestamp(date), {"up": 0, "down": 0, "traded": 0,
                             "source": "kline_cache"})


def limit_counts_series(close_df: pd.DataFrame, volume_df: pd.DataFrame,
                        ratios, dates) -> dict:
    """多日涨停/跌停家数 → {Timestamp: {"up","down","traded","source"}}。

    ratios: {代码: 涨跌停幅度} 或已按列对齐的 pd.Series —— 由
    core/limit_ratio.limit_ratio 生成 (单一真相源)。传 Series 时直接复用,
    不再逐票重建。
    **已知偏差**: limit_ratio 要 st 布尔标记, 而本地日线缓存没有 ST 标记,
    故 ST 股 (±5%) 的涨停会被漏计 —— 方向是低估, 宁可少报不虚报。

    涨停价按交易所惯例四舍五入到分: floor(昨收 × (1+幅度) × 100 + 0.5) / 100。
    掩码(只有成交才算)与昨收在循环**外**算一次 —— 回填 2500+ 天时若逐日重算
    整张 5000×5000 矩阵, 分钟级会变小时级。
    """
    empty = {"up": 0, "down": 0, "traded": 0, "source": "kline_cache"}
    out: dict = {}
    if close_df is None or volume_df is None or len(close_df) == 0:
        return out
    vol = volume_df.reindex(index=close_df.index, columns=close_df.columns)
    c = close_df.astype(float).where(vol > 0)
    prev = c.shift(1)
    if isinstance(ratios, pd.Series):
        ratio = ratios.reindex(c.columns).fillna(0.10).astype(float)
    else:
        ratio = pd.Series({code: float(ratios.get(code, 0.10))
                           for code in c.columns}, dtype=float)
    for d in dates:
        ts = pd.Timestamp(d) if d is not None else None
        if ts is None or ts not in c.index:
            continue
        row, prow = c.loc[ts], prev.loc[ts]
        live = row.notna() & prow.notna()
        n_live = int(live.sum())
        if n_live == 0:
            out[ts] = dict(empty)
            continue
        up_px = np.floor(prow * (1 + ratio) * 100 + 0.5) / 100
        dn_px = np.floor(prow * (1 - ratio) * 100 + 0.5) / 100
        out[ts] = {"up": int(((row >= up_px - 1e-6) & live).sum()),
                   "down": int(((row <= dn_px + 1e-6) & live).sum()),
                   "traded": n_live, "source": "kline_cache"}
    return out


def similar_days(target: dict, history: pd.DataFrame, *, top_n: int = 5,
                 exclude_recent: int = RECENT_EXCLUDE_BARS,
                 min_gap: int = MIN_MATCH_GAP_BARS,
                 quantile: float | None = None,
                 max_band: int = 300,
                 features: tuple = SIMILAR_FEATURES) -> dict:
    """历史照镜子: 找出与 target 指标向量最像的历史交易日。

    做法: 对每个特征按历史全序列 z-score 标准化 (减均值除标准差) 后算欧氏距离。

    **两条纪律 (2026-09-17 修正, 原来只有 20 天 = 数据窥探)**:

    - `exclude_recent` (默认 **252** 个交易日): 排除最近这一段, 不许拿"上个月"当历史。
      原默认 20 天等于允许拿一个月前的日子当"历史参照", 是**数据窥探**。
      这个门槛是从外部研究产物学来的 (那封邮件写的是"排除最后 252 天避免数据窥探")。
    - `min_gap` (默认 20): 两个入选的"相似日"之间至少隔这么多交易日,
      否则一次会照出一串连续的同一天。

    `gap` 这个"一个参数同时管两条纪律"的兼容写法**已于 2026-09-17 M7 删除**
    （审计 F-13）：它允许 `gap=0` 静默把两条纪律一起关掉（不抛错、不告警），
    等于给数据窥探防护留了个后门。现在两个纪律必须分别显式给，删掉参数后
    误传 `gap=` 会直接 `TypeError`，是**响亮地坏**而不是**安静地错**。

    `quantile` (如 0.05): 额外给出"**距离最近的这一档**"全体 (前 5%)。
    为什么要它: 只报 top-5 的中位数**在统计上没有意义** (5 个样本不构成统计量);
    给一档样本才能看分布。`max_band` 给档内样本数封顶, 防它大到离谱。

    Returns:
        dict, 键::

            {"picks":       [{"date","distance"}, ...] 最像的几天 (top_n 个),
             "band":        [{"date","distance"}, ...] 距离最近的一档 (quantile 为 None 时 []),
             "n_band":      int   档内样本数,
             "eligible":    int   可参与类比的历史日总数 (已排除最近 exclude_recent 天),
             "exclude_recent": int, "min_gap": int, "quantile": float|None}

        **无可用样本时 picks/band 均为空表**, 不抛异常。
        `n_eff` (有效独立样本) 由调用方按 `n_band / 持有期交易日数` 估 —— 前向收益
        窗口高度重叠, 原始样本数会严重高估信息量, 所以必须由知道持有期的一方算。
    """
    feats = list(features)
    empty = {"picks": [], "band": [], "n_band": 0, "eligible": 0,
             "exclude_recent": int(exclude_recent), "min_gap": int(min_gap),
             "quantile": quantile}
    exclude_recent, min_gap = int(exclude_recent), int(min_gap)
    if history is None or len(history) == 0:
        return empty
    if any(f not in history.columns for f in feats):
        return empty
    h = history[feats].sort_index().dropna()
    if len(h) < max(exclude_recent + 1, min_gap * 2):
        return empty
    tvec = pd.Series({f: target.get(f) for f in feats}, dtype=float)
    if tvec.isna().any():
        return empty
    mu = h.mean()
    sd = h.std(ddof=0)
    sd = sd.where(sd > 0)          # 零方差特征无法标准化 → 整题放弃 (不硬算)
    if sd.isna().any():
        return empty
    z = (h - mu) / sd
    tz = (tvec - mu) / sd
    dist = np.sqrt(((z - tz) ** 2).sum(axis=1))
    cutoff = h.index[-exclude_recent]        # 纪律①: 只在这天(含)之前找
    elig = dist[dist.index <= cutoff]
    if len(elig) == 0:
        return empty
    order = list(elig.sort_values(kind="stable").index)
    pos = {d: i for i, d in enumerate(h.index)}
    picked: list = []
    for d in order:
        if any(abs(pos[d] - pos[p]) <= min_gap for p in picked):   # 纪律② 去重
            continue
        picked.append(d)
        if len(picked) >= top_n:
            break
    band: list = []
    if quantile and 0 < float(quantile) < 1:
        n_band = max(5, min(int(len(elig) * float(quantile)), int(max_band)))
        band = order[:n_band]
    return {
        "picks": [{"date": pd.Timestamp(d).date().isoformat(),
                   "distance": round(float(dist[d]), 3)} for d in picked],
        "band": [{"date": pd.Timestamp(d).date().isoformat(),
                  "distance": round(float(dist[d]), 3)} for d in band],
        "n_band": len(band),
        "eligible": int(len(elig)),
        "exclude_recent": exclude_recent,
        "min_gap": min_gap,
        "quantile": quantile,
    }


def forward_return(closes, start, horizon: int) -> float | None:
    """从 start (含) 起持有 horizon 个交易日的收益率 (%)。

    start 不是交易日则顺延到之后第一个交易日 (bfill 语义); 样本不够返 None
    (绝不用不足 horizon 的短样本冒充)。
    """
    s = _clean(closes)
    if len(s) < 2 or horizon <= 0:
        return None
    i = int(s.index.searchsorted(pd.Timestamp(start), side="left"))
    j = i + int(horizon)
    if i >= len(s) or j >= len(s):
        return None
    base = float(s.iloc[i])
    if base <= 0:
        return None
    return round((float(s.iloc[j]) / base - 1) * 100, 2)
