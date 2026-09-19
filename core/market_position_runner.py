"""core/market_position_runner.py — 大盘位置采集 / 落盘 / 体温表 (2026-09-17)。

做什么: 读本地日线缓存 → 调 core/market_position 纯函数 → upsert JSONL
连续录像 → 组 Markdown「大盘体温表」。

为什么读 parquet 而不走 KlineCache.get():
    KlineCache.get() 会为 5211 只票各跑一次 _ensure(manifest sqlite 查询) 并把
    **6 个字段**都拼成宽表, 而我们只要 date/close/volume/amount 四列。
    实测直读 parquet 全量 17~28 秒, 且零网络动作、零副作用。
    目录约定由 tests/test_market_position_runner.py 锁死 (断言与
    KlineCache._parquet_path 生成同一路径), 防缓存布局改了这里静默读空。

铁律 (业务铁律 1): 本模块**只落盘与生成报告**, 绝不 import trade,
绝不写任何仓位/闸门。tests/test_market_position.py 有 AST 静态断言守护。

公开接口 (7, 铁律 8 以内):
    collect(*, bars, write, expected) -> dict
    latest() -> dict | None
    history(limit) -> list[dict]
    mirror(top_n) -> dict
    shadow_replay() -> dict
    thermometer_md(rec, mirror_data, shadow_data) -> str
    push_thermometer(rec, title) -> dict
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

from core.kline_cache import STUB_TRADED_RATIO
from core.limit_ratio import limit_ratio
from core.market_position import (PCT_WINDOW_BARS, POSITION_COLUMNS,
                                  RECENT_EXCLUDE_BARS, RET_1Y_BARS,
                                  SIMILAR_FEATURES, breadth_frame,
                                  forward_return, index_position_series,
                                  last_valid_date, limit_counts_series,
                                  similar_days)
# 私有接缝 (有意为之): 纯数学层公开函数已顶到铁律 8 上限, 分桶统计以私有名
# 引入, 本模块是它唯一生产消费者 (其余只许是测试)。
from core.market_position import _momentum_bucket_stats
# 2026-09-19 批次 5.1: 共享底座端出 core/market_position_io.py。
# 路径常量/落盘原语/读取原语都在那边, 这里**不再留副本** ——
# 本模块函数一律用 `mpio.X` 调用期取值 (测试隔离只 patch 基座一处即全局生效),
# 旧入口 `mpr.DAILY_PATH` / `mpr.KLINE_1D_DIR` 由文件末尾的模块级 __getattr__ 转发。
from core import market_position_io as mpio  # noqa: E402
# 模块对象本身也要绑定 (文件末尾的 _FORWARD_TABLE 按归属转发旧入口)
from core import (  # noqa: E402
    market_erp,
    market_mirror,
    market_regime,
    market_shadow_replay,
    market_thermometer,
    market_validity,
)
# 2026-09-19 批次 5.1 第二刀: 三块分析逻辑端出 (regime/体检/ERP),
# 均为**函数对象**显式 import (合法: 模块内裸全局名不走 __getattr__)。
# 2026-09-19 批次 5.1 第三刀: 影子回放端出 (函数对象显式 import)
# 2026-09-19 批次 5.1 第四/五刀: 照镜子 + 体温表文案端出
from core.market_mirror import MIRROR_WARNING, YEAR_DOMINANCE_WARNING, mirror  # noqa: E402,F401
from core.market_thermometer import thermometer_md, push_thermometer  # noqa: E402,F401

from core.market_shadow_replay import (  # noqa: E402,F401
    MIN_SEGMENTS_FOR_T,
    SHADOW_RULES,
    WINDOW_SPLIT,
    shadow_replay,
)
from core.market_position_io import _year_breakdown  # noqa: E402,F401  (照镜子用它)

from core.market_erp import (  # noqa: E402,F401
    ERP_CALIBER,
    ERP_FETCH_ENV,
    ERP_MIN_OBS,
    ERP_SOURCE,
    ERP_SOURCE_PLAIN,
    _erp_fetch_enabled,
    _erp_snapshot,
    _erp_table,
    _read_erp,
    _refresh_erp,
)
from core.market_regime import (  # noqa: E402,F401
    _regime_all,
    _regime_episodes,
    _regime_summary,
)
from core.market_validity import (  # noqa: E402,F401
    BARS_PER_MONTH,
    VALIDITY_FIELDS,
    VALIDITY_HORIZONS,
    VALIDITY_MIN_MONTHS,
    _dimension_validity,
    _quintile_spread,
    _spearman,
)
from core.market_position_io import (  # noqa: E402,F401
    _features_frame,
    _num,
    _pct,
    _rat,
    _yi,
)

from core.market_position_io import (  # noqa: E402,F401  (re-export: 旧 mpr.X 入口不变)
    INDEX_SPECS,
    _expected_trading_day,
    _f,
    _index_series,
    _upsert,          # 本模块内部也用 (collect 落盘 / ERP 落盘); 注意模块级
    #                   __getattr__ 只管"外部属性访问", 模块内裸全局名必须显式 import
    history,
    latest,
)

#: `POSITION_COLUMNS` 里**是文本**的列（牛熊标签）。
#: 其余都是数值列、走 `_f()` 转 float —— 两类必须分开处理，否则文本列会被
#: `float("bull")` 打回 None 并**静默丢掉**（2026-09-17 M7 自查实测：`regime_20`
#: 在 5501 条记录里 0 条非空）。
TEXT_COLS = ("regime", "regime_20")

try:  # pragma: no cover - 循环导入兜底 (logger 永远可用, 这里只是防御)
    from utils.logger import get_logger
    _logger = get_logger(__name__)
except Exception:  # pragma: no cover
    import logging
    _logger = logging.getLogger(__name__)

__all__ = ["collect", "latest", "history", "mirror", "shadow_replay",
           "momentum_buckets", "thermometer_md", "push_thermometer",
           "INDEX_SPECS", "SHADOW_RULES", "DAILY_PATH", "MIRROR_WARNING",
           "MOMENTUM_BUCKET_WARNING"]


#: 沪深股票代码 (沪市 6 开头, 深市 000/001/002/003/300/301) —— 排除指数/ETF/债券
_STOCK_RE = re.compile(r"^(6\d{5}\.SH|(000|001|002|003|300|301)\d{3}\.SZ)$")
#: 日常采集窗口: 覆盖"成交额一年百分位"(250 根) + 60 日新高低 + 缓冲
DEFAULT_BARS = 300
#: 「成交额一年百分位」的回看窗口 / 最少样本数 —— **一处定义、两处引用**
#: (计算在 `amt_rank`, 预热在 `WARMUP_BARS`)。
#: **注意 `min_periods < window` 是个陷阱**：满 120 根就有数, 但那个数是"在 120 个值里
#: 排名"而不是"在 250 个值里排名", 是**错的**。要满 250 根才正确。
AMOUNT_RANK_WINDOW = 250
AMOUNT_RANK_MIN_PERIODS = 120
#: **预热窗口 (2026-09-17 实测抓到的真 bug 的修法)**: 日常采集算 DEFAULT_BARS 天,
#: 但窗口头部那些天**算不出/算不对**需要长回看的指标 —— `amt_rank` 是
#: `rolling(250, min_periods=120)`, 头 120 天没有数, **120~249 天有数但是错的**;
#: 新高新低是 60 日窗。
#: 而日常采集会把这些记录 **upsert 覆盖**回录像, 于是**历史上本来是好的记录
#: 被改成了缺值**。实测（2026-09-17 M7 用当时录像重放"无预热"那条路径）：
#: **单次** `collect(bars=300)` 就让 **72 条**记录 (2025-06-26 ~ 2025-10-13) 的
#: `amount_pct_1y` 由"有值"变 null, 且 **0 条变好** —— 只坏不好。而日常是**每天
#: 滑一次窗**, 受损区间会一路向历史蔓延（另一轮独立审计用更长的重放窗口数到
#: 118~119 条 / 起点 2025-04-16 —— 两个数都对, 差别只在"重放了几天日常采集"）。
#: 修法 = 多读 WARMUP_BARS 根做预热, **只输出窗口内最后 bars 天**。
#: 余量**不是拍脑袋**（2026-09-17 M7 冻结数据实测, 只让预热变、数据不变）:
#: 「成交额一年百分位」与长期预热基准不一致的天数 = 预热 0 → 243 天、60 → 187、
#: 120 → 126、180 → 67、240 → 10、**250 起 → 0**。所以下限就是 250 整窗, 取 300。
WARMUP_BARS = AMOUNT_RANK_WINDOW + 50
#: 回填窗口: 0 = 全量历史
BACKFILL_BARS = 0
#: 有效交易日判据: 当日有成交的股票占比。空壳 bar 的比例会掉到 1% 以下。
MIN_TRADED_RATIO = STUB_TRADED_RATIO     # 引 core.kline_cache 的单一真相源, 不写第二份 0.5
#: TDX 日线 amount 字段单位 = 万元 → 除以 1e4 得亿元
AMOUNT_WAN_PER_YI = 1e4
#: 「前期12个月涨跌 → 未来12个月收益」分桶图的诚实边界 (2026-09-17,
#: 源自对外部 926 号回测的独立复核)。**页面必须原样展示** (与 MIRROR_WARNING
#: 同纪律)。大白话硬条款 (AGENTS.md 沟通风格第 7 条): 不许出现统计黑话。
MOMENTUM_BUCKET_WARNING = (
    "这张图说的是「历史上前期涨/跌成这样的月份，之后一年实际怎么走」——"
    "是**历史经验的分布，不是预测**。月份样本看着上百，但相邻月份的「未来一年」"
    "窗口互相重叠 11 个月，真正独立的信息只有约十几份（一轮牛熊摊不到几份）。"
    "另外用本机数据复核过：**两头的桶方向稳**（跌透了会弹、涨疯了会落），"
    "**中间几个桶的排名换个指数口径就会变** —— 别拿中间档的先后当下注依据。")

#: 采集串行锁: 页面手动采集与调度器 15:50 的 job 可能同时跑,
#: 两个写入方并发读写同一个 JSONL 会互相覆盖丢记录 (upsert 是"读全量→写全量")。
_COLLECT_LOCK = threading.Lock()



# ───────────────────── 内部: 取数 ─────────────────────


def _load_matrices(bars: int):
    """读全部沪深日线 → (close, volume, amount) 三个宽表 (索引=日期, 列=代码)。"""
    files = sorted(f for f in mpio.KLINE_1D_DIR.glob("*.parquet")
                   if _STOCK_RE.match(f.stem))
    cs, vs, ams = {}, {}, {}
    bad = 0
    for f in files:
        try:
            df = pd.read_parquet(f, columns=["date", "close", "volume", "amount"])
        except Exception as e:      # 单文件坏不影响全市场 (fail-soft)
            bad += 1
            _logger.warning("大盘位置: 读日线失败 %s: %s", f.name, e)
            continue
        if bars and len(df) > bars:
            df = df.iloc[-bars:]
        if len(df) == 0:
            continue
        idx = pd.to_datetime(df["date"])
        cs[f.stem] = pd.Series(df["close"].to_numpy(dtype=float), index=idx)
        vs[f.stem] = pd.Series(df["volume"].to_numpy(dtype=float), index=idx)
        ams[f.stem] = pd.Series(df["amount"].to_numpy(dtype=float), index=idx)
    if bad:
        _logger.warning("大盘位置: %d 个日线文件读取失败 (已跳过)", bad)
    if not cs:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    return (pd.DataFrame(cs).sort_index(), pd.DataFrame(vs).sort_index(),
            pd.DataFrame(ams).sort_index())








# ───────────────────── 内部: 记录组装 ─────────────────────


def _shadow_states(d, bf_row, hs_close: pd.Series | None,
                   hs_ma20: pd.Series | None, hs_regime) -> dict:
    """三条候选择时规则的当日状态 (只记录, 不交易)。

    ma20      : 沪深300 收盘 > 自身 20 日均线 → on (最经典的均线择时)
    breadth50 : 站上 20 日均线家数占比 ≥ 50% → on (用宽度而非指数)
    regime    : core/index_regime 判为牛 → on (项目既有牛熊口径, 作对照组)
    """
    on = "on"
    off = "off"
    ma20 = off
    if hs_close is not None and hs_ma20 is not None:
        try:
            c, m = float(hs_close.loc[d]), float(hs_ma20.loc[d])
            if c == c and m == m and m > 0 and c > m:
                ma20 = on
        except (KeyError, TypeError, ValueError):
            pass
    breadth = bf_row.get("above_ma20_pct")
    breadth50 = on if (breadth is not None and breadth == breadth
                       and float(breadth) >= 50) else off
    # 2026-09-17 修复: 原写成 str(hs_regime) 落的是 'bull'/'range',
    # 而回放按 =='on' 判定 → regime 规则持仓占比恒为 0%。三条规则必须同口径。
    regime = on if str(hs_regime) == "bull" else off
    return {"ma20": ma20, "breadth50": breadth50, "regime": regime}


def _build_record(d, bf_row, idx_hist: dict, hs_raw, hs_ma20, total_amt,
                  amt_rank, limit_map: dict, exp_ts) -> dict:
    """组装一条日记录 (d = 数据日期)。

    hs_raw / hs_ma20 / limit_map 由调用方**在循环外算好传入** —— 回填要组
    2500+ 条, 若每条都重算一遍全序列 rolling 与整张 5000×5000 掩码矩阵,
    分钟级会变小时级 (2026-09-17 首跑实测踩到)。
    """
    indices = {}
    for key, name, code in INDEX_SPECS:
        h = idx_hist.get(key)
        if h is None or d not in h.index:
            indices[key] = {"name": name, "code": code, "close": None}
            continue
        r = h.loc[d]
        item = {"name": name, "code": code}
        for col in POSITION_COLUMNS:
            # **文本列 vs 数值列必须分开**（2026-09-17 M7 自查抓到的真 bug）:
            # 原判断写的是 `col != "regime"`, 于是新加的 `regime_20` 走了 `_f()`
            # → `float("bull")` 抛 TypeError → `_f` 吞掉并返回 None
            # → **5501 条记录里 regime_20 全是 None, 静默丢了这一维**(实测 0/5501 非空)。
            # 同类坑: 以后再加文本列, 必须一并列进 TEXT_COLS。
            item[col] = (None if r[col] is None or r[col] != r[col] else str(r[col])) \
                if col in TEXT_COLS else _f(r[col])
        indices[key] = item

    traded = bf_row.get("traded")
    traded = int(traded) if traded == traded else 0
    amt = total_amt.get(d)
    rec = {
        "date": pd.Timestamp(d).date().isoformat(),
        "expected_date": pd.Timestamp(exp_ts).date().isoformat(),
        "stale": bool(pd.Timestamp(d) < pd.Timestamp(exp_ts)),
        "indices": indices,
        "breadth": {
            "traded": traded,
            "above_ma20_pct": _f(bf_row.get("above_ma20_pct")),
            "above_ma60_pct": _f(bf_row.get("above_ma60_pct")),
            "new_high_60": int(bf_row.get("new_high") or 0),
            "new_low_60": int(bf_row.get("new_low") or 0),
            "hl_spread": int(bf_row.get("hl_spread") or 0),
        },
        "turnover": {
            "amount_yi": None if amt != amt else round(float(amt) / AMOUNT_WAN_PER_YI, 1),
            "amount_pct_1y": _f(amt_rank.get(d)),
        },
        "limit": limit_map.get(pd.Timestamp(d),
                               {"up": 0, "down": 0, "traded": 0,
                                "source": "kline_cache"}),
    }
    hs_regime = None
    h = idx_hist.get("hs300")
    if h is not None and d in h.index:
        hs_regime = h.loc[d]["regime"]
    rec["shadow"] = _shadow_states(d, bf_row, hs_raw, hs_ma20, hs_regime)
    return rec


# ───────────────── 内部: 估值维度 (ERP 股债性价比) ─────────────────
#
# 计划书 §14.4「估值维度」+ §14.7「维度体检」。**先说清楚它为什么必须存在**:
# 现在的位置指标全是**价格衍生量**(百分位/宽度/新高低/成交额/波动), 没有一维回答
# 「贵不贵」。而外部的独立研究 (邮件《单因子独立回测》) 实测 ERP 是唯一强有效的
# 长周期因子: rho=+0.481 (p<0.001)、五等分价差 +26.7pp。
# **价格分位 ≠ 估值分位** —— 指数可以在价格高位而估值不高 (盈利涨得比价格快)。












# ───────────────── 内部: 牛熊区间与时长 (§14.5/§14.6) ─────────────────








# ───────────────── 内部: 维度体检 (§14.7 + §16.1/§16.3/§16.5) ─────────────────
#
# **验证纪律以 `docs/公式因子体检方法论.md` 为准, 这里不另立**(§16.3):
#   - 纪律 2「双窗口一致才算数」→ 本体检也把月频样本对半切开, 两半同号才算数;
#   - 纪律 3「数族不数因子」→ 同族的指标只算 1 份独立证据, 防多重检验挖矿;
#   - 纪律 5「报告必带可信度警告」→ 幸存者偏差等限制写进输出文案。
# **不做 DSR/PBO**(2026-07-26 用户已拍板), 只如实披露"共检验了多少个组合"。









# ───────────────────── 内部: JSONL 读写 ─────────────────────










# ───────────────────── 公开接口 ─────────────────────


def collect(*, bars: int = DEFAULT_BARS, write: bool = True,
            expected: dt.date | None = None) -> dict:
    """采集一批日记录 (窗口内**所有**有效交易日) 并 upsert 落盘。

    bars=DEFAULT_BARS(300): 日常模式。每次算 300 个交易日的记录全部 upsert ——
    于是"停机三天后开机"会自动补上那三天, 不需要单独的补数逻辑 (自愈)。
    bars=BACKFILL_BARS(0): 全量历史回填, 让"照镜子"第一天就有参照系。

    返回 {"ok", "asof", "expected", "stale", "records", "lines", "snapshot"}。
    """
    if not _COLLECT_LOCK.acquire(blocking=False):
        return {"ok": False, "reason": "另一次采集正在进行中 (稍后重试)"}
    try:
        try:
            return _collect_locked(bars=bars, write=write, expected=expected)
        except TimeoutError as e:
            # 2026-09-19 批次 4.4: 跨进程锁超时 → 人话错误 (另一进程在写)
            return {"ok": False, "reason": f"另一个进程正在采集: {e}"}
    finally:
        _COLLECT_LOCK.release()


def _collect_locked(*, bars: int, write: bool,
                    expected: dt.date | None) -> dict:
    """collect 的实际实现 (调用方已持 _COLLECT_LOCK)。"""
    # 多读 WARMUP_BARS 做预热 (否则窗口头部的长回看指标是 NaN, 会把历史记录改缺)
    close_df, vol_df, amt_df = _load_matrices(bars + WARMUP_BARS if bars else 0)
    if close_df.empty:
        return {"ok": False, "reason": f"本地日线缓存为空 ({mpio.KLINE_1D_DIR})"}
    asof = last_valid_date(vol_df, min_ratio=MIN_TRADED_RATIO)
    if asof is None:
        return {"ok": False, "reason": "没有一天满足有效成交判据 (全是空壳 bar?)"}

    bf = breadth_frame(close_df, vol_df)
    exp_ts = pd.Timestamp(expected or _expected_trading_day())
    stale = pd.Timestamp(asof) < exp_ts
    if stale:
        _logger.warning("大盘位置: 日线缓存最新有效交易日 %s 落后应有交易日 %s "
                        "(记录会打 stale 标记, 体温表抬头写明)", asof.date(), exp_ts.date())

    # 指数: 全序列指标一次算完, 再对齐到宽度表的交易日 (两边日期集合可能不同)
    idx_hist, idx_raw = {}, {}
    for key, _name, code in INDEX_SPECS:
        s = _index_series(code)
        if s is None:
            idx_hist[key] = None
            idx_raw[key] = None
            continue
        s = s[s.index <= asof]          # 截到 asof: 指数文件自己也可能带空壳 bar
        idx_raw[key] = s
        idx_hist[key] = index_position_series(s).reindex(bf.index)

    # 全市场成交额 (空壳 bar 的 0 不算) + 一年百分位
    total_amt = amt_df.where(vol_df > 0).sum(axis=1)
    amt_rank = (total_amt.rolling(AMOUNT_RANK_WINDOW,
                                 min_periods=AMOUNT_RANK_MIN_PERIODS)
                .rank(pct=True) * 100)

    ratios = pd.Series({c: limit_ratio(c) for c in close_df.columns}, dtype=float)
    hs_raw = idx_raw.get("hs300")
    hs_ma20 = hs_raw.rolling(20, min_periods=20).mean() if hs_raw is not None else None
    valid = [d for d in bf.index
             if bf.at[d, "traded_ratio"] == bf.at[d, "traded_ratio"]
             and bf.at[d, "traded_ratio"] >= MIN_TRADED_RATIO]
    # 首行没有"昨收"可比, 涨跌停会算成 0 → 跳过, 不写假 0
    valid = [d for d in valid if d > bf.index[0]]
    # 预热段只用来把指标"喂热", 不算正式记录 (否则会把历史 upsert 成缺值)
    if bars:
        valid = valid[-bars:]

    # 涨停/跌停: 掩码与昨收在 limit_counts_series 内部只算一次, 逐日只做轻量比较
    limit_map = limit_counts_series(close_df, vol_df, ratios, valid)

    # 估值维度 (§14.4): 先保证本地 ERP 缓存覆盖到 asof (一天最多联网一次),
    # 再把当日的 ERP 与百分位附到**每条**记录上 —— 与日线无关, 缺了就是缺了。
    if write:
        _refresh_erp()
    erp_table = _erp_table()

    records = [_build_record(d, bf.loc[d], idx_hist, hs_raw, hs_ma20, total_amt,
                             amt_rank, limit_map, exp_ts)
               for d in valid]
    for rec in records:
        rec["valuation"] = _erp_snapshot(erp_table, rec["date"])
    lines = _upsert(records) if write else 0
    return {"ok": True, "asof": pd.Timestamp(asof).date().isoformat(),
            "expected": exp_ts.date().isoformat(), "stale": bool(stale),
            "records": len(records), "lines": lines,
            "snapshot": records[-1] if records else None}






# ───────────────── 内部: 统计 (成本 / HAC / 持有段 / 双窗口) ─────────────────
#
# 这些不是"大盘位置指标", 而是**评估一套择时规则好不好**要用的统计工具,
# 所以放在 IO 层的私有接缝里, 不占 core/market_position.py 的公开名额 (§8.1)。

















def momentum_buckets() -> dict:
    """前期12个月涨跌幅 → 未来12个月收益 的分桶统计 (三指数并排, 各算各的)。

    2026-09-17 新增: 源自对外部 926 号回测 (129 个月分桶表) 的独立复核 ——
    图的形式值得借鉴, 但数字必须用本机数据重算 (复核发现原报告「跌0~10%
    是最差档」的结论在本机三个指数口径下不成立, 只有两头桶的方向稳定)。

    只读本地指数日线缓存, **不联网**。逐指数独立返回 (指数历史长度不同:
    沪深300 从 2005 年起、创业板指从 2010 年起), 不合并成一个"全市场"
    口径 —— 复核实测中间桶排名对指数口径敏感, 合并会假装存在一个唯一答案。
    诚实边界写在 MOMENTUM_BUCKET_WARNING, 调用方必须原样展示。
    """
    out = {"ok": True, "indices": [], "warning": MOMENTUM_BUCKET_WARNING}
    for key, name, code in INDEX_SPECS:
        s = _index_series(code)
        if s is None:
            out["indices"].append(
                {"key": key, "name": name, "code": code, "ok": False,
                 "reason": "本地没有该指数的日线缓存"})
            continue
        st = _momentum_bucket_stats(s)
        st["key"], st["name"], st["code"] = key, name, code
        out["indices"].append(st)
    if not any(i.get("ok") for i in out["indices"]):
        return {"ok": False,
                "reason": "三个指数都读不到本地日线缓存 (先在数据准备页补指数日线)",
                "warning": MOMENTUM_BUCKET_WARNING}
    return out






# ───────── 内部: 把数字翻译成人话 (模板, 不靠每次自觉; §16.9 第 10 条) ─────────
#
# **为什么必须是函数而不是写死的句子**: 写"八成个股已经跌破 20 日均线"很顺口,
# 但明天宽度涨到 80% 那句话就成了假话。解释必须**由数字生成**。
























# ───────────────── 模块级转发 (2026-09-19 批次 5.1) ─────────────────
#: 旧入口 → 新归处的**归属表**。PEP 562 模块级 __getattr__ **动态**转发 ——
#: 不做 `X = mod.X` 快照: 快照会在基座被 patch (测试隔离) 后变陈旧, 谁读它谁写
#: 生产路径。按表转发是确定性的: 不会把某个模块的 import (如 pd/dt) 也漏出去,
#: 也不会因为"多个模块恰好同名"而转发到错的那家。
_FORWARD_TABLE = (
    (mpio, (
        "_ROOT", "KLINE_1D_DIR", "DAILY_PATH", "INDEX_SPECS", "ERP_PATH",
        "_UPSERT_LOCK_TIMEOUT", "_index_series", "_f", "_expected_trading_day",
        "_upsert", "_upsert_locked", "_cross_process_lock", "_lock_file",
        "_unlock_file", "history", "latest",
        "_num", "_pct", "_rat", "_yi", "_features_frame", "_year_breakdown",
    )),
    (market_erp, (
        "ERP_CALIBER", "ERP_FETCH_ENV", "ERP_MIN_OBS", "ERP_SOURCE",
        "ERP_SOURCE_PLAIN", "_erp_fetch_enabled", "_read_erp", "_refresh_erp",
        "_erp_table", "_erp_snapshot",
    )),
    (market_regime, ("_regime_episodes", "_regime_summary", "_regime_all")),
    (market_validity, (
        "VALIDITY_FIELDS", "VALIDITY_HORIZONS", "VALIDITY_MIN_MONTHS",
        "BARS_PER_MONTH", "_spearman", "_quintile_spread", "_dimension_validity",
    )),
    (market_mirror, (
        "MIRROR_WARNING", "YEAR_DOMINANCE_WARNING", "MIRROR_BAND_QUANTILE",
        "MIRROR_BAND_MAX", "YEAR_DOMINANCE", "mirror",
    )),
    (market_thermometer, (
        "CALIBER_FOOTER", "thermometer_md", "_width_plain", "_hl_plain",
        "_amount_plain", "_zdt_plain", "_position_plain", "_stars",
        "push_thermometer",
    )),
    (market_shadow_replay, (
        "SHADOW_RULES", "MIN_SEGMENTS_FOR_T", "WINDOW_SPLIT",
        "_cost_params", "_hac_tstat", "_holding_segments", "_segment_stats",
        "shadow_replay",
    )),
)

#: 归属表里的**可变状态** (路径/阈值) —— 这几个绝不许在本模块留下实体副本:
#: 副本是 import 时快照, 基座被 patch (测试隔离) 后会指向生产路径。
#: 其余名字是**函数对象**, 显式 import 是合法且必要的 (模块内裸全局名不走
#: __getattr__; 函数对象也不承载可变状态)。测试 `TestIsolationGuard` 用它守门。
_IO_STATE_NAMES = frozenset({
    "_ROOT", "KLINE_1D_DIR", "DAILY_PATH", "ERP_PATH", "_UPSERT_LOCK_TIMEOUT",
})


def __getattr__(name: str):
    """旧入口按归属表转发 (PEP 562)。未知名照常 AttributeError。"""
    for _mod, _names in _FORWARD_TABLE:
        if name in _names:
            return getattr(_mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
