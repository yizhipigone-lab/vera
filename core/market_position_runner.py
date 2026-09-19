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

#: 外部估值序列 (ERP 股债性价比) 的本地缓存, 一天一行。
#: **为什么单独一个文件**: 它来自网络 (乐咕乐股), 与日线缓存这个数据源无关;
#: 混进 daily.jsonl 会让"回填"这条纯本地路径变成联网路径。
ERP_PATH = mpio._ROOT / "data" / "market_position" / "erp.jsonl"
#: 估值维度的数据源 (2026-09-17 实测核实, 不是猜的):
#:   akshare `stock_ebs_lg()` → 乐咕乐股「股债性价比(股债利差)」,
#:   日频 2005-04-08 ~ 2026-09-16 共 5207 条, 无缺失。
#: **口径已用算术核对 (相对误差 0.0006%)**:
#:   股债利差 = 1 / 沪深300 滚动市盈率(PE-TTM) − 10 年期中国国债收益率
#:   实测 2026-09-16: 1/12.67 − 1.6858% = 6.2069% = 源里的 6.2069%。
#: **口径如实标注**: 这是 **沪深300** 口径, 不是邮件里用的「万得全A」口径;
#: 两者不是同一个数, 报告里必须写清楚, 不许含糊成"全市场估值"。
ERP_SOURCE = "akshare stock_ebs_lg (乐咕乐股 股债性价比)"
#: 上面那个是**技术来源**（写进数据文件、给开发者看）；下面这个才是给用户看的说法。
#: 用户可见文案里不许出现库名/接口名 —— 他不需要知道我们用哪个库抓的数据。
ERP_SOURCE_PLAIN = "乐咕乐股公布的「股债利差」（本机每天自动取一次）"
ERP_CALIBER = ("沪深300 口径: 用「市盈率的倒数」当作股票的盈利收益率，再减掉 10 年期国债收益率（越高越划算；负数=拿着股票还不如买国债）")
#: ERP 算百分位至少要多少条历史 (一年 ≈243 条, 这里要满 3 年才给数,
#: 与 core/market_position.PCT_WINDOW_BARS 的 min_periods 精神一致: 不足就不给)
ERP_MIN_OBS = 750
#: 关掉 ERP 联网取数的环境变量 (测试/离线用; tests/conftest.py 默认设上)
ERP_FETCH_ENV = "VERA_MP_NO_ERP_FETCH"

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
#: 三条候选择时规则 (只记录不交易; 定义见 _shadow_states)
SHADOW_RULES = ("ma20", "breadth50", "regime")
#: 照镜子必须原样带出的警告 (写成常量, 不靠各处自觉)
MIRROR_WARNING = (
    "「历史相似」不是预测 —— 上表说的是「历史上跟今天像的那些日子, 之后实际怎么走」, "
    "不等于这次也会那样走。命中 {n} 个交易日看着不少, 但它们挨得很近、涨跌高度重叠, "
    "**真正独立的信息只有大约 {n_eff} 份**; 而 A 股几十年也只经历过屈指可数的几轮周期。")
#: 结论被单一年份主导时的点名警告 (§14.3; 有测试锁住"必须出现")
YEAR_DOMINANCE_WARNING = (
    "⚠ **本结论由 {year} 年主导** —— 这一年在 {n} 个命中日里占了 {share}。"
    "换句话说, 上面的\"历史上像今天的时候之后怎么走\", 主要是\"{year} 年那一次怎么走\", "
    "不是很多次独立经验的平均。")
#: 照镜子的结论依据 = 「距离最近的一档」(前 5%), 不再拿 top-5 的中位数当结论 (§14.2)。
#: 原因: 5 个样本的中位数不是统计量, 报它等于虚报精度。
MIRROR_BAND_QUANTILE = 0.05
#: 「前期12个月涨跌 → 未来12个月收益」分桶图的诚实边界 (2026-09-17,
#: 源自对外部 926 号回测的独立复核)。**页面必须原样展示** (与 MIRROR_WARNING
#: 同纪律)。大白话硬条款 (AGENTS.md 沟通风格第 7 条): 不许出现统计黑话。
MOMENTUM_BUCKET_WARNING = (
    "这张图说的是「历史上前期涨/跌成这样的月份，之后一年实际怎么走」——"
    "是**历史经验的分布，不是预测**。月份样本看着上百，但相邻月份的「未来一年」"
    "窗口互相重叠 11 个月，真正独立的信息只有约十几份（一轮牛熊摊不到几份）。"
    "另外用本机数据复核过：**两头的桶方向稳**（跌透了会弹、涨疯了会落），"
    "**中间几个桶的排名换个指数口径就会变** —— 别拿中间档的先后当下注依据。")
#: 分位带内样本数上限 (防"最近的一档"大到几百天)
MIRROR_BAND_MAX = 300
#: 单个年份占分位带比例 > 该值 → 必须点名"本结论由该年主导" (§14.3)
YEAR_DOMINANCE = 0.5
#: 持有段数 < 该值 → 不给 t 值, 只给描述统计并标"样本不足" (§16.7 MED-4)
MIN_SEGMENTS_FOR_T = 30
#: 双窗口切分点: 前一半 / 后一半 (复用《公式因子体检方法论》纪律 2「双窗口一致才算数」)。
#: 为什么不是 70/30: 全样本 13.7 年, 70/30 会让后段只剩约 4 年, 而 `regime` 规则
#: 全样本只有 15 个持有段 → 后段约 4 段, 根本判不了。对半切每段仍有 ~6.9 年。
WINDOW_SPLIT = 0.5
#: 体温表尾部铁律提示 + 已知偏差 (**2026-09-17 用大白话重写**: 用户是小白,
#: 原来的「生存者偏差」「ST 股 (±5%) 会漏计」他看不懂 —— 见计划书 §16.9 第 10 条
#: 「大白话·硬条款」。**黑话一律翻译掉**: 技术术语只留在代码注释与 CLAUDE.md 里。)
CALIBER_FOOTER = (
    "只读参考，不联入任何仓位调度（业务铁律 1：宏观与研判只出报告，"
    "最后一步永远由人来做）。指标怎么算，唯一真相源 = `core/market_position.py`；"
    "牛熊怎么判 = `core/index_regime.py`；涨跌停幅度 = `core/limit_ratio.py`。\n"
    "**这份表有三处「看不清」，读的时候要一起记住**：\n"
    "① **涨停家数是本地按 10%/20% 的涨跌幅自己算的**，而 ST 股（被交易所特别处理、"
    "每天最多只能涨跌 ±5% 的那些股票）用的不是这个幅度 → **它们会被漏掉，"
    "所以涨停家数偏低**（真实可能更多）。\n"
    "② **历史数据里只有「今天还活着的公司」** —— 已经退市的公司，我们手里没有它们当年的数据。"
    "而退市的往往是当年的差股票，**所以过去的「多少股票在涨」会被算得比当年实际好看**"
    "（方向是**高估**；说白了就是：只有活到今天的公司被统计进去了）。\n"
    "③ **「十年百分位」在早年其实凑不满十年** —— 本机指数数据最早只到 2013 年，"
    "而这项指标要求至少满 3 年才给数，所以 **2016~2019 年那几年的百分位是拿更短的窗口算的**，"
    "不能和今天的数字完全并排比较。")

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


def _erp_fetch_enabled() -> bool:
    """是否允许联网取 ERP。测试与离线环境用 env 关掉 (默认允许)。"""
    return os.environ.get(ERP_FETCH_ENV, "").strip().lower() not in ("1", "true", "yes")


def _read_erp() -> pd.Series:
    """读本地 ERP 缓存 → 按日期升序的 float Series (索引 DatetimeIndex)。

    文件不存在/全是坏行 → 返空 Series (上层标【缺】, 绝不返回编造值)。
    """
    if not ERP_PATH.exists():
        return pd.Series(dtype=float)
    rows = []
    for line in ERP_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
            v = float(o["erp"])
            d = pd.Timestamp(str(o["date"]))
        except Exception:
            continue           # 坏行跳过 (fail-soft, 不因一行坏掉整段历史)
        if v == v:
            rows.append((d, v))
    if not rows:
        return pd.Series(dtype=float)
    s = pd.Series([v for _, v in rows], index=[d for d, _ in rows], dtype=float)
    return s[~s.index.duplicated(keep="last")].sort_index()


def _refresh_erp() -> dict:
    """联网拉 ERP 历史并 upsert 到 `data/market_position/erp.jsonl`。

    **fail-soft, 绝不抛**: 网络不通 / akshare 没装 / 端点改版, 都只是保持旧缓存
    并把结果标成不可用 —— 估值这一维缺了, 体温表其余部分照常出。
    已覆盖到"应有交易日"时不重复拉 (一天最多一次联网)。
    """
    if not _erp_fetch_enabled():
        return {"ok": False, "reason": f"已用 {ERP_FETCH_ENV} 关闭联网取数"}
    have = _read_erp()
    try:
        want = pd.Timestamp(_expected_trading_day())
    except Exception:
        want = pd.Timestamp(dt.date.today())
    if len(have) and have.index[-1] >= want:
        return {"ok": True, "skipped": True, "rows": len(have),
                "last": have.index[-1].date().isoformat()}
    try:
        import akshare as ak
        df = ak.stock_ebs_lg()
    except Exception as e:
        _logger.warning("大盘位置: 拉 ERP 失败 (保持旧缓存): %s", e)
        return {"ok": False, "reason": f"拉取失败: {e}", "rows": len(have)}
    try:
        rows = []
        for _, r in df.iterrows():
            d = pd.Timestamp(str(r["日期"]))
            v = float(r["股债利差"])
            if v == v:
                rows.append({"date": d.date().isoformat(), "erp": round(v, 6)})
        if not rows:
            return {"ok": False, "reason": "端点返回空表", "rows": len(have)}
        n = _upsert(rows, path=ERP_PATH)
        return {"ok": True, "rows": n, "added": len(rows),
                "last": rows[-1]["date"]}
    except Exception as e:
        _logger.warning("大盘位置: ERP 落盘失败: %s", e)
        return {"ok": False, "reason": f"落盘失败: {e}", "rows": len(have)}


def _erp_table(s: pd.Series | None = None) -> pd.DataFrame:
    """ERP 的**十年滚动统计表**, **一次向量化算完** → 按日期查表即可。

    列: `erp`(当日值%) / `pct_10y`(十年百分位) / `median_10y` / `min_10y` /
    `max_10y` / `n_obs`(十年窗口里的样本数)。

    **为什么必须向量化**: 回填要算 5000+ 天, 若每天现算一遍
    「截到该日 → 取最近 2430 条 → 比较大小」, 就是 4000 万次比较,
    实测会从"秒级"掉到"分钟级"(与小节开头的性能提醒同一类坑)。
    用 `rolling(...).rank(pct=True)` 一次算完, 之后只是查表。
    """
    if s is None:
        s = _read_erp()
    if s is None or len(s) == 0:
        return pd.DataFrame()
    r = s.rolling(PCT_WINDOW_BARS, min_periods=ERP_MIN_OBS)
    out = pd.DataFrame({
        "erp": s * 100,
        "pct_10y": r.rank(pct=True) * 100,
        "median_10y": r.median() * 100,
        "min_10y": r.min() * 100,
        "max_10y": r.max() * 100,
        "n_obs": r.count(),
    })
    return out


def _erp_snapshot(table: pd.DataFrame, asof) -> dict | None:
    """按日期查 ERP 快照 (当日值 + 十年百分位 + 十年区间)。

    返回 None = 没缓存 / 该日之前没有值 / 该日的历史不足 `ERP_MIN_OBS` 条
    (十年窗口没满 3 年就不给数, 与位置百分位同一条纪律)。
    **百分位读法**: 越高 = 越划算 (过去十年里只有这么少的时间比现在更划算)。
    """
    if table is None or len(table) == 0:
        return None
    ts = pd.Timestamp(asof)
    i = int(table.index.searchsorted(ts, side="right")) - 1
    if i < 0:
        return None
    row = table.iloc[i]
    if row["pct_10y"] != row["pct_10y"]:        # NaN → 历史不足
        return None
    return {"erp_pct": _f(row["erp"], 2),
            "erp_pct_10y": _f(row["pct_10y"], 1),
            "erp_median_10y_pct": _f(row["median_10y"], 2),
            "erp_min_10y_pct": _f(row["min_10y"], 2),
            "erp_max_10y_pct": _f(row["max_10y"], 2),
            "n_obs": int(row["n_obs"]),
            "asof": table.index[i].date().isoformat(),
            "source": ERP_SOURCE, "source_plain": ERP_SOURCE_PLAIN,
            "caliber": ERP_CALIBER}


# ───────────────── 内部: 牛熊区间与时长 (§14.5/§14.6) ─────────────────


def _regime_episodes(labels: pd.Series) -> list[dict]:
    """把逐日的牛/熊/震荡标签切成**连续的区间** → 每段的起止/交易日数/涨跌幅。

    计划书 §14.5 的缺口: `index_regime.classify` 只给**单点状态**(今天牛还是熊),
    答不了"**这轮牛走了多久、超出历史中位多少**" —— 而"走了多久"正是"位置"的一部分。
    """
    out: list[dict] = []
    cur, start = None, None
    idx = list(labels.index)
    for i, v in enumerate(labels.to_numpy()):
        v = None if v is None or v != v else str(v)
        if v != cur:
            if cur is not None and start is not None and i - 1 >= start:
                out.append({"state": cur, "start_i": start, "end_i": i - 1})
            cur, start = v, i
    if cur is not None and start is not None:
        out.append({"state": cur, "start_i": start, "end_i": len(idx) - 1})
    return [e for e in out if e["state"]]


def _regime_summary(labels: pd.Series, closes: pd.Series, *,
                    caliber: str) -> dict | None:
    """一条牛熊口径的**区间统计** + 当前这一段走到哪了。

    两条口径都在体温表里并列给 (§14.6): 年线斜率口径与 20% 法则口径。
    返回 None = 标签全空 (样本不足)。

    **实测发现 (2026-09-17, 必须如实带出)**: 年线(MA250)斜率口径在**日频上会频繁翻状态** ——
    沪深300 历史 71 段、中位只有 0.3 个月, 于是"本轮已走 0.9 个月 / 历史中位 0.3 个月"
    这类对比基本是噪声。处置**不是**偷偷给它加去抖(那等于发明第三种口径), 而是:
    ①照实给; ②当某口径的中位区间长度 < 1 个月时, 输出里**明写"这个口径的『走了多久』不可用"**。
    """
    eps = _regime_episodes(labels)
    if not eps:
        return None
    px = closes.reindex(labels.index)
    idx = list(labels.index)
    rows = []
    for e in eps:
        a, b = e["start_i"], e["end_i"]
        p0, p1 = float(px.iloc[a]), float(px.iloc[b])
        rows.append({
            "state": e["state"],
            "start": idx[a].date().isoformat(),
            "end": idx[b].date().isoformat(),
            "days": b - a + 1,
            "months": _f((idx[b] - idx[a]).days / 30.44, 1),
            "ret_pct": _f((p1 / p0 - 1) * 100, 1) if p0 > 0 else None,
        })
    for i, r in enumerate(rows):
        r["ongoing"] = (i == len(rows) - 1)
    cur = rows[-1]
    hist = [r for r in rows[:-1] if r["state"] == cur["state"]]
    allm = pd.Series([r["months"] for r in rows])
    same = pd.Series([r["months"] for r in hist]) if hist else pd.Series(dtype=float)
    years = max((idx[-1] - idx[0]).days / 365.25, 1e-9)
    flicker = bool(len(hist) >= 5 and float(same.median()) < 1.0)
    return {"caliber": caliber, "state": cur["state"], "since": cur["start"],
            "months": cur["months"], "ret_pct": cur["ret_pct"],
            "n_episodes": len(rows), "n_same_state": len(hist),
            "median_months": _f(same.median(), 1) if hist else None,
            "median_ret_pct": _f(pd.Series([r["ret_pct"] for r in hist]).median(), 1)
            if hist else None,
            "months_percentile": (_f(float((same <= cur["months"]).mean()) * 100, 0)
                                  if hist else None),
            "all_median_months": _f(allm.median(), 1),
            "flips_per_year": _f(len(rows) / years, 1),
            "too_flickery": flicker,
            "flicker_note": (
                f"该口径在日频上翻状态很勤（历史 {len(rows)} 段、{len(rows) / years:.1f} 段/年、"
                f"中位只有 {_num(allm.median(), 1)} 个月），所以它的「本轮已走多久」"
                "参考价值有限 —— 这正是需要第二条口径的原因" if flicker else ""),
            "same_state_rows": hist,
            # 明细表只列"历史上最长的 8 段": 抖动的口径会产出几十段 0.0 个月的碎片,
            # 全列出来只会淹掉真正有意义的那几轮周期 (选最长是描述, 不是阈值)
            "longest_rows": sorted(hist, key=lambda r: -r["months"])[:8]}


def _regime_all() -> dict:
    """三大指数 × 两条口径的区间统计 (体温表「这轮走了多久」一节用)。"""
    out = {}
    for key, name, code in INDEX_SPECS:
        s = _index_series(code)
        if s is None or len(s) < 30:
            out[key] = None
            continue
        s = s.dropna()
        s = s[s > 0]
        df = index_position_series(s)
        out[key] = {
            "name": name, "code": code,
            "ma250": _regime_summary(df["regime"], s, caliber="年线斜率口径"),
            "pct20": _regime_summary(df["regime_20"], s, caliber="20% 法则口径"),
        }
    return out


# ───────────────── 内部: 维度体检 (§14.7 + §16.1/§16.3/§16.5) ─────────────────
#
# **验证纪律以 `docs/公式因子体检方法论.md` 为准, 这里不另立**(§16.3):
#   - 纪律 2「双窗口一致才算数」→ 本体检也把月频样本对半切开, 两半同号才算数;
#   - 纪律 3「数族不数因子」→ 同族的指标只算 1 份独立证据, 防多重检验挖矿;
#   - 纪律 5「报告必带可信度警告」→ 幸存者偏差等限制写进输出文案。
# **不做 DSR/PBO**(2026-07-26 用户已拍板), 只如实披露"共检验了多少个组合"。

#: 体检的持有期: (交易日数, 中文名)
VALIDITY_HORIZONS = ((21, "1 个月"), (63, "3 个月"), (126, "6 个月"), (252, "12 个月"))
#: 体检的指标: (特征名, 中文名, **族**) —— 族决定"算几份独立证据"(纪律 3)
VALIDITY_FIELDS = (
    ("sh_pct", "上证十年百分位", "价格位置族"),
    ("hs300_pct", "沪深300 十年百分位", "价格位置族"),
    ("above_ma20_pct", "站上 20 日均线占比", "宽度族"),
    ("hl_spread_pct", "创新高与新低的差", "宽度族"),
    ("amount_pct_1y", "成交额一年百分位", "量能族"),
    ("vol_ann_20", "20 日年化波动", "波动族"),
    ("erp", "股债性价比(ERP)", "估值族"),
)
#: 每月至少要有多少个月的样本才做体检 (少于这个数就是伪精度)
VALIDITY_MIN_MONTHS = 36
#: 一个月 ≈ 多少个交易日 (把持有期的交易日数换成"月数", 算有效独立样本用)
BARS_PER_MONTH = 21.0


def _spearman(x: list[float], y: list[float]) -> tuple[float | None, float | None]:
    """Spearman 秩相关 + p 值。scipy 不可用时**只给 rho, p 值返 None**(不编 p)。

    为什么用 Spearman 而不是 Pearson: 这些指标与收益的关系明显非线性
    (位置极高与极低都可能反转), 秩相关对异常值稳健, 也是外部研究用的口径。
    """
    if len(x) < 8 or len(x) != len(y):
        return None, None
    try:
        import warnings

        from scipy.stats import spearmanr
        with warnings.catch_warnings():
            # 常量输入时 scipy 会警告并返回 NaN —— 那是"无法定义", 不是错误
            warnings.simplefilter("ignore")
            r = spearmanr(x, y)
        rho, p = float(r.statistic), float(r.pvalue)
        if rho != rho or p != p:
            return None, None
        return rho, p
    except Exception:                 # scipy 缺失 → 自己算 rho (秩的 Pearson), 不给 p
        a = pd.Series(x).rank().to_numpy()
        b = pd.Series(y).rank().to_numpy()
        if a.std() == 0 or b.std() == 0:
            return None, None
        return float(np.corrcoef(a, b)[0, 1]), None


def _quintile_spread(vals: list[float], fwds: list[float]) -> float | None:
    """把指标从小到大分五组, 算「最高一组 − 最低一组」之后平均收益差 (百分点)。

    样本不足 25 个月**不给数** —— 5 组各 5 个点以下的分位差没有意义。
    """
    if len(vals) < 25 or len(vals) != len(fwds):
        return None
    try:
        g = pd.qcut(pd.Series(vals), 5, labels=False, duplicates="drop")
    except Exception:
        return None
    if g.nunique() < 5:
        return None
    f = pd.Series(fwds)
    hi, lo = f[g == 4].mean(), f[g == 0].mean()
    return _f(hi - lo, 2) if hi == hi and lo == lo else None


def _dimension_validity() -> dict:
    """**维度体检**: 每个现有指标 vs 未来 1/3/6/12 个月收益, 到底有没有相关性。

    这是计划书 §14.7「最高优先」那一节, 也是决定"该留哪些指标、该补什么维度"的
    唯一数据依据。**它回答的问题**: 我这套大盘指标里, 有哪一维被验证过能预测收益?

    三条纪律 (抄 `docs/公式因子体检方法论.md`, 不自创):
      1. **月频采样**: 日频观测的"有效独立样本"只有个位数 (§16.1),
         按 2200 个日频观测报 p<0.001 是**虚构精度**;
      2. **双窗口一致才算数**: 月频样本对半切, 两半同号才算数, 否则标"待复核";
      3. **数族不数因子**: 同族指标只算 1 份独立证据。
    """
    recs = history(limit=0)
    hist = _features_frame(recs)
    if len(hist) < 500:
        return {"ok": False, "reason": f"连续录像只有 {len(recs)} 条, 维度体检至少要 500 条"}
    hs = _index_series("000300.SH")
    if hs is None:
        return {"ok": False, "reason": "读不到沪深300日线"}
    hist.index = pd.to_datetime(hist.index)
    erp = _erp_table()
    if len(erp):                       # 没有 ERP 缓存就不并这一列 (不拿别的列冒充)
        hist = hist.join(erp[["erp"]], how="left")
    if "erp" not in hist.columns:
        hist["erp"] = np.nan
    # **月频采样**: 每月取月末最后一个有数据的交易日
    mon = hist.resample("ME").last()
    mon = mon.dropna(how="all")
    n_months = int(len(mon))
    if n_months < VALIDITY_MIN_MONTHS:
        return {"ok": False,
                "reason": f"月频样本只有 {n_months} 个月, 至少需要 {VALIDITY_MIN_MONTHS} 个月"}

    # 前向收益: **复用 forward_return**, 不写第二份收益定义
    fwd = {k: [forward_return(hs, d, k) for d in mon.index] for k, _ in VALIDITY_HORIZONS}
    rows = []
    for field, name, family in VALIDITY_FIELDS:
        if field not in mon.columns:
            continue
        for k, kcn in VALIDITY_HORIZONS:
            pairs = [(v, f) for v, f in zip(mon[field].tolist(), fwd[k])
                     if v == v and f is not None]
            if len(pairs) < VALIDITY_MIN_MONTHS // 2:
                continue
            xs = [float(p[0]) for p in pairs]
            ys = [float(p[1]) for p in pairs]
            rho, p = _spearman(xs, ys)
            # 双窗口切分必须**按该指标的可用样本**切, 不能按整张表切
            # (2026-09-17 实测踩到: 用全局 n//2 切, 短历史的指标前段就吃掉全部样本、
            #  后段为空 → rho_out 恒 None → 所有显著项都被误判成"两半不一致")
            half = len(pairs) // 2
            rho_i, _ = _spearman(xs[:half], ys[:half])
            rho_o, _ = _spearman(xs[half:], ys[half:])
            consistent = (rho_i is not None and rho_o is not None
                          and (rho_i > 0) == (rho_o > 0))
            if rho is None:
                verdict = "算不了"
            elif p is None or p >= 0.05:
                verdict = "看不出相关性"
            elif not consistent:
                verdict = "样本内相关但两半不一致 → 待复核"
            else:
                verdict = "样本内可用（正向）" if rho > 0 else "样本内可用（反向）"
            rows.append({"field": field, "name": name, "family": family,
                         "horizon_days": k, "horizon": kcn,
                         "n": len(pairs),
                         # **月频采样**的有效独立样本 = 月数 ÷ 持有期月数
                         # (不是 ÷ 持有期交易日数: 观测间隔本身就是一个月)
                         "n_eff": _f(len(pairs) / (k / BARS_PER_MONTH), 1),
                         "rho": _f(rho, 3) if rho is not None else None,
                         "p": _f(p, 4) if p is not None else None,
                         "rho_in": _f(rho_i, 3) if rho_i is not None else None,
                         "rho_out": _f(rho_o, 3) if rho_o is not None else None,
                         "consistent": consistent,
                         "quintile_spread_pct": _quintile_spread(xs, ys),
                         "verdict": verdict})
    if not rows:
        return {"ok": False, "reason": "没有任何指标有足够的月频样本"}
    n_tests = len(rows)
    # 每个指标取"最能说明问题"的那一行 (优先 12 个月, 没有就取最长的) 作为总结论
    summary = []
    for field, name, family in VALIDITY_FIELDS:
        mine = [r for r in rows if r["field"] == field]
        if not mine:
            continue
        best = max(mine, key=lambda r: r["horizon_days"])
        strong = [r for r in mine
                  if r["p"] is not None and r["p"] < 0.05 and r["consistent"]]
        summary.append({
            "field": field, "name": name, "family": family,
            "rho_12m": best["rho"], "p_12m": best["p"],
            "n_12m": best["n"],
            #: 五分位差与"是哪个持有期"都取自**同一行** `best`。
            #: 2026-09-17 M7：正文原来写死 `by_field[field][252]`，而 `best` 是
            #: "优先 12 个月、没有就取最长的" —— 两者不是同一行时，正文会出现
            #: "12 个月 rho +0.99、五分位差算不出来"这种自相矛盾的组合。
            "spread": best.get("quintile_spread_pct"),
            "best_horizon": best["horizon"],
            "verdict_12m": best["verdict"],
            "any_significant_consistent": bool(strong),
            "significant_horizons": [r["horizon"] for r in strong],
            "label": (("可用（样本内，在 " + "、".join(r["horizon"] for r in strong)
                       + " 上显著且两半一致）") if strong
                      else "仅描述现状，不作预测依据")})
    families = {}
    for s in summary:
        f = families.setdefault(s["family"], {"n_fields": 0, "usable": False})
        f["n_fields"] += 1
        f["usable"] = f["usable"] or s["any_significant_consistent"]
    #: 取**实际有结果的最长持有期**（不是写死的 12 个月）—— 录像短的时候 12 个月那一档
    #: 可能一行都没有，写死就会印出「各指标落在 个位数」这种半截话。
    _present = sorted({r["horizon_days"] for r in rows}, reverse=True)
    longest = _present[0] if _present else max(k for k, _ in VALIDITY_HORIZONS)
    longest_cn = dict(VALIDITY_HORIZONS).get(longest, f"{longest} 个交易日")
    at_longest = [r for r in rows if r["horizon_days"] == longest]
    eff = [r["n_eff"] for r in at_longest if r["n_eff"] is not None]
    months_n = [r["n"] for r in at_longest if r["n"] is not None]
    thinnest = min(at_longest, key=lambda r: r["n"], default=None)
    widest = max(at_longest, key=lambda r: r["n"], default=None)
    usable_fams = sorted(f for f, v in families.items() if v["usable"])
    _eff_sentence = (
        f"**{longest_cn}持有期的有效独立样本，各指标落在 {min(eff)} ~ {max(eff)} 份**"
        if eff else
        f"**{longest_cn}持有期在本机数据上还凑不出有效样本**（录像不够长）")
    return {"ok": True, "n_months": n_months,
            "start": mon.index[0].date().isoformat(),
            "end": mon.index[-1].date().isoformat(),
            "rows": rows, "summary": summary,
            "n_tests": n_tests, "n_fields": len(summary),
            "n_families": len(families), "n_families_usable": len(usable_fams),
            "usable_families": usable_fams, "families": families,
            "limitations": [
                "**幸存者偏差是满格的**（实测 2026-09-17：本地日线缓存 5211 只股票里，"
                "最后交易日早于 2026-08-01 的有 **0 只**）—— 缓存里一只退市股都没有。"
                "而这些票当年通常是弱票，所以历史宽度序列被**系统性高估**，"
                "用宽度类指标算出来的相关性都建立在这条被污染的序列上。",
                f"**月频采样得 {n_months} 个月（{mon.index[0].date()} ~ "
                f"{mon.index[-1].date()}），但各指标历史长短不同**："
                + (f"能用的月份数从 **{min(months_n)} 个月**（{thinnest['name']}）"
                   f"到 **{max(months_n)} 个月**（{widest['name']}）不等"
                   if months_n and thinnest and widest else "各指标可用月份数不等")
                + "（这是各列自己的可用起点不同：十年百分位要满 3 年预热才给数，"
                  "宽度/新高低/成交额几乎从录像开头就有）。",
                _eff_sentence
                + "（见下方「有效独立样本」列）—— 所以 p 值只能当参考，不能当结论；"
                  "持有期越长，重叠越少、但样本也越少。",
                f"**共检验 {n_tests} 个组合**（{len(summary)} 个指标 × "
                f"{len(VALIDITY_HORIZONS)} 个持有期），"
                f"按纪律 3「数族不数因子」归到 {len(families)} 个族 —— "
                f"其中 **{len(usable_fams)} 个族**（{'、'.join(usable_fams) or '无'}）"
                f"**至少在一个持有期上**找到了可用证据；"
                "判断依据是各指标自己那一行写明的持有期，"
                "**不等于它在 12 个月上也显著**。"
                "从这么多次比较里挑出显著的那几个，本身就是过拟合风险；"
                "本项目**不做**「多重检验校正」（那是为了对付「试很多次、挑出最好的那个」这类"
                "偏差的统计处理；2026-07-26 用户拍板不做），"
                "所以这里如实披露检验次数，由读者自己打折。",
                "**本体检只说明样本内相关性，不改任何仓位**（业务铁律 1）；"
                "没通过的一律标「仅描述现状，不作预测依据」，"
                "**不引入权重、不合成总分**（与「四指数并行不合成」的既定拍板一致）。"]}


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

#: 默认成本参数进程内缓存一次 (构造空配置引擎只为读三个默认值)
_COST_CACHE: dict = {}


def _cost_params() -> dict | None:
    """项目默认交易成本 —— **现读 `backtest/engine.py`, 不在这里写第二份**。

    单一真相源 = `BacktestEngine.__init__` 里 `config.get("commission"/"stamp_tax"/
    "slippage")` 的默认值 (实测 0.0003 / 0.0005 / 0.001)。构造一个空配置的引擎只读
    这三个数, 结果进程内缓存 (首次约 0.5 秒, 全是 import 开销)。

    **口径 (§16.2)**: 这里给出的 `round_trip` = 佣金×2 + 印花税 (卖出单边)
    = 0.11%, **不含滑点** —— 这是"单次往返至少花掉多少"的**下限**。
    另给 `round_trip_with_slippage` = 再加滑点×2 = 0.31%, 报告里一并披露,
    免得"只计 0.11%"被误读成"成本已经算全了"。

    读失败一律返 None → 上层把净口径标成【缺】, **绝不编一个成本数出来**。
    """
    if "v" in _COST_CACHE:
        return _COST_CACHE["v"]
    v = None
    try:
        from backtest.engine import BacktestEngine
        e = BacktestEngine({})
        comm, tax, slip = float(e.commission), float(e.stamp_tax), float(e.slippage)
        v = {"commission": comm, "stamp_tax": tax, "slippage": slip,
             "round_trip": comm * 2 + tax,
             "round_trip_with_slippage": comm * 2 + tax + slip * 2}
    except Exception as ex:      # fail-soft: 净口径标缺, 不影响毛口径与体温表
        _logger.warning("大盘位置: 读不到默认成本参数, 净口径标缺: %s", ex)
    _COST_CACHE["v"] = v
    return v


def _hac_tstat(x: pd.Series, *, lags: int | None = None) -> dict | None:
    """均值是否显著不为 0 的 **Newey-West (HAC) t 值 + 95% 置信区间**。

    **为什么不能直接用普通 t 检验** (§15.1 E1): 择时规则的日收益**自己跟自己相关**
    (今天持仓, 明天多半还持仓; 空仓日更是一连串的 0)。普通标准误把"天数"当成
    "独立样本数", 会把显著性吹大。Newey-West 用 Bartlett 权重给前 L 阶自协方差
    打折后重算方差, 顺带得到**方差膨胀因子** `vif` 与**有效独立样本数**
    `n_eff = n / vif` —— 这才是"真正独立的信息有多少份"。

    带宽 `lags=None` 时用经验值 `ceil(4·(n/100)^(2/9))` (n=3365 时约 9 阶)。

    返回的 `ci_low/ci_high` 是**日均收益**的区间; 上层乘 `RET_1Y_BARS` 换成"年化
    几个百分点"再展示 (t 值对线性缩放不变, 两种写法同一个数)。
    返回 None = 样本不足 20 天、或序列是常量 (方差 0), 上层标【缺】不硬算。
    """
    v = pd.Series(x).astype(float).dropna()
    n = len(v)
    if n < 20:
        return None
    d = v - float(v.mean())
    g0 = float((d * d).mean())
    # 数值噪声兜底: 常量序列的 g0 可能是 1e-35 而不是精确的 0 (浮点减法残留),
    # 不拦就会算出 t=5.5e7 这种荒唐值。判据 = 方差相对自身量级小到是噪声。
    if not g0 > 1e-12 * max(1.0, float(v.mean()) ** 2):
        return None
    if lags is None:
        lags = int(np.ceil(4.0 * (n / 100.0) ** (2.0 / 9.0)))
    lags = max(0, min(int(lags), n - 2))
    nw = g0
    dn = d.to_numpy()
    for k in range(1, lags + 1):
        gk = float((dn[k:] * dn[:-k]).mean())
        nw += 2.0 * (1.0 - k / (lags + 1.0)) * gk
    nw = max(nw, 1e-18)
    se = (nw / n) ** 0.5
    mean = float(v.mean())
    vif = nw / g0
    return {"mean": mean, "se": se, "t": (mean / se) if se > 0 else None,
            "ci_low": mean - 1.96 * se, "ci_high": mean + 1.96 * se,
            "n": n, "lags": lags, "vif": vif, "n_eff": n / vif}


def _holding_segments(pos: pd.Series) -> list[tuple[int, int, int]]:
    """持仓段 = position 连续为 1 的区间 → `[(起下标, 止下标, 长度), ...]`。

    "建仓一次"算一段 (§16.2 实测: ma20 196 次 / breadth50 181 次 / regime 15 次)。
    **持仓占比 ≠ 换手率**: 前者是"多少天在场内", 后者看的是"翻仓多少次"。
    """
    out: list[tuple[int, int, int]] = []
    start = None
    for i, on in enumerate(pos.to_numpy()):
        if on and start is None:
            start = i
        elif not on and start is not None:
            out.append((start, i - 1, i - start))
            start = None
    if start is not None:
        out.append((start, len(pos) - 1, len(pos) - start))
    return out


def _segment_stats(gross: pd.Series, segs: list[tuple[int, int, int]],
                   cost_rate: float) -> dict:
    """持有段的描述统计 + **每笔下注盈亏**的 t 检验 (§15.1 E2 + §16.7 MED-4)。

    **段收益的定义 (必须写清楚, 否则这些数没法读)**: 段收益 = 段内日收益复合
    (从第 a 天拿到第 b 天), 再扣掉 `cost_rate`。检验的是**这些段收益的均值是否为 0**
    —— 也就是"每开一次仓, 平均是赚还是亏"。

    **为什么不做长度归一化 —— 这是实测出来的坑, 不是个人偏好**: 计划书 §16.7 曾建议
    「段内日均收益」或「折算年化」。实测 (2013-01-04 ~ 2026-09-16, `ma20` 规则 196 段)
    发现这两种归一化会**把结论的符号弄反**:

    | 口径 | 结果 |
    |---|---|
    | 每笔平均盈亏 (不归一化) | **+0.28%** (中位 −0.74%, 胜率 22%) |
    | 段内日均收益 (除以段长) | **−0.39%/天** |
    | 按天数加权的真实日均收益 | **+0.028%/天** |

    原因: 除以段长会给**长段**(赢家)打折, 却不给**短段**(输家)打折 —— 长赢家被稀释、
    短输家原样保留, 均值必然被推成负数。而按天数加权的口径与"每笔口径"同号。
    所以这里**只做每笔口径**; 按天数加权的口径由主表的 HAC t 值承担。

    段数 < `MIN_SEGMENTS_FOR_T` (30) → **不给 t 值**, 只给描述统计, 并写明"样本不足"
    (实测 `regime` 只有 15 段, 硬给一个 t 值就是伪精度)。

    返回的 `t` 是**净口径**(先扣 cost_rate), `t_gross` 是毛口径, 两个一起给,
    方便看出"成本是不是把结论方向改掉了"。
    """
    if not segs:
        return {"n": 0, "note": "没有持仓段"}
    rets_net, rets_gross, days = [], [], []
    for a, b, n in segs:
        g = float((1.0 + gross.iloc[a:b + 1]).prod()) - 1.0
        rets_gross.append(g)
        rets_net.append(g - cost_rate)
        days.append(n)

    def _t(vals) -> float | None:
        if len(vals) < MIN_SEGMENTS_FOR_T:
            return None
        s = pd.Series(vals)
        sd = float(s.std(ddof=1))
        return _f(float(s.mean()) / (sd / len(s) ** 0.5), 2) if sd > 0 else None

    return {"n": len(segs),
            "median_days": _f(pd.Series(days).median(), 0),
            "p90_days": _f(pd.Series(days).quantile(0.9), 0),
            "min_days": int(min(days)), "max_days": int(max(days)),
            "mean_return_pct": _f(pd.Series(rets_net).mean() * 100, 2),
            "median_return_pct": _f(pd.Series(rets_net).median() * 100, 2),
            "win_ratio_pct": _f(sum(1 for r in rets_net if r > 0) / len(rets_net) * 100, 0),
            "t": _t(rets_net), "t_gross": _t(rets_gross),
            "note": "" if len(segs) >= MIN_SEGMENTS_FOR_T else
                    (f"持有段只有 {len(segs)} 个 (<{MIN_SEGMENTS_FOR_T}), "
                     "样本不足, 不给 t 值")}


def _year_breakdown(items: list[dict], key: str) -> list[dict]:
    """按年份拆解命中日: 该年命中几天、之后涨跌的中位数、上涨占比 (§14.3)。

    **为什么必须有这张表**: 没有它, "命中日之后 20 日中位数 −12.3%" 就是一个没有
    出处的数字 —— 它可能来自 **5 个不同的年份**(那是 5 份独立经验), 也可能来自
    **同一个年份的 20 个交易日**(那其实是 1 份经验的 20 个分身, 涨跌还高度重叠)。
    这两种情况的含义天差地别。
    """
    grp: dict[str, list] = {}
    for it in items:
        v = it.get(key)
        if v is None:
            continue
        grp.setdefault(str(it["date"])[:4], []).append(float(v))
    out = []
    for y in sorted(grp):
        vs = grp[y]
        out.append({"year": y, "n": len(vs),
                    "median_pct": _f(pd.Series(vs).median(), 2),
                    "up_ratio_pct": _f(sum(1 for x in vs if x > 0) / len(vs) * 100, 0)})
    return out


def _features_frame(recs: list[dict]) -> pd.DataFrame:
    """连续录像 → 照镜子用的特征表 (列 = SIMILAR_FEATURES, 索引 = 日期)。"""
    rows = []
    for r in recs:
        idx = r.get("indices") or {}
        b = r.get("breadth") or {}
        t = r.get("turnover") or {}
        traded = b.get("traded") or 0
        hs = idx.get("hs300") or {}
        sh = idx.get("shanghai") or {}
        rows.append({
            "date": r["date"],
            "sh_pct": sh.get("pct_10y"),
            "hs300_pct": hs.get("pct_10y"),
            "above_ma20_pct": b.get("above_ma20_pct"),
            "hl_spread_pct": (b.get("hl_spread") / traded * 100) if traded else None,
            "vol_ann_20": hs.get("vol_ann_20"),
            "amount_pct_1y": t.get("amount_pct_1y"),
        })
    df = pd.DataFrame(rows)
    return df.set_index("date") if len(df) else df


def mirror(top_n: int = 5) -> dict:
    """历史照镜子: 今天最像历史上哪几天 + 那几天之后实际怎么走。

    诚实边界写在 MIRROR_WARNING 里, 调用方 (页面/体温表) 必须原样展示。
    """
    recs = history(limit=0)
    if len(recs) < 120:
        return {"ok": False,
                "reason": f"连续录像只有 {len(recs)} 条, 至少需要 120 条; "
                          "先跑 tools/market_position_collect.py --backfill"}
    hist = _features_frame(recs)
    target = {k: hist[k].dropna().iloc[-1] if hist[k].notna().any() else None
              for k in SIMILAR_FEATURES}
    # 纪律①(排除最近 252 天)与纪律②(命中日之间至少隔 20 天)在 pure 层;
    # 结论依据 = 距离最近的一档(前 5%), top_n 明细只作"最像的几天"展示 (§14.2)。
    res = similar_days(target, hist, top_n=top_n, quantile=MIRROR_BAND_QUANTILE,
                       max_band=MIRROR_BAND_MAX)
    picks, band = res["picks"], res["band"]
    hs = _index_series("000300.SH")
    sh = _index_series("000001.SH")

    def _add_fwd(items: list[dict]) -> list[dict]:
        for p in items:
            p["fwd_20_hs300_pct"] = forward_return(hs, p["date"], 20) if hs is not None else None
            p["fwd_60_hs300_pct"] = forward_return(hs, p["date"], 60) if hs is not None else None
            p["fwd_20_sh_pct"] = forward_return(sh, p["date"], 20) if sh is not None else None
        return items

    _add_fwd(picks)
    _add_fwd(band)
    if not band and not picks:
        used = len(hist.dropna()) if len(hist) else 0
        return {"ok": False,
                "reason": f"六个相似度特征齐全的历史交易日只有 {used} 条, 而"
                          f"「排除最近 {RECENT_EXCLUDE_BARS} 天」这条纪律要求至少 "
                          f"{RECENT_EXCLUDE_BARS + 1} 条; 先跑全量回填把录像补长。"
                          "（十年百分位要满 3 年才有数, 所以录像的头几年特征不全。）"}

    def _vals(items, key):
        return [float(p[key]) for p in items if p.get(key) is not None]

    f20, f60 = _vals(band, "fwd_20_hs300_pct"), _vals(band, "fwd_60_hs300_pct")

    def _n_eff(k: int) -> float | None:
        """有效独立样本 = 档内命中日数 ÷ 持有期天数 (重叠窗口会严重高估信息量)。

        实测 110 个命中日 / 20 日 ≈ **5.5 份**、/ 60 日 ≈ 1.8 份 —— 看着一百多个样本,
        真正独立的信息不到 6 份, 这就是为什么必须把它写在结论旁边。
        """
        return _f(len(band) / float(k), 1) if k > 0 else None

    s20, s60 = pd.Series(f20), pd.Series(f60)
    summary = {
        "n": len(band),
        "n_picks": len(picks),
        "fwd_20_median": _f(s20.median(), 2) if f20 else None,
        "fwd_20_mean": _f(s20.mean(), 2) if f20 else None,
        "fwd_20_q25": _f(s20.quantile(0.25), 2) if f20 else None,
        "fwd_20_q75": _f(s20.quantile(0.75), 2) if f20 else None,
        "fwd_20_up_ratio": _f(sum(1 for x in f20 if x > 0) / len(f20) * 100, 0) if f20 else None,
        "fwd_60_median": _f(s60.median(), 2) if f60 else None,
        "fwd_60_up_ratio": _f(sum(1 for x in f60 if x > 0) / len(f60) * 100, 0) if f60 else None,
        "n_eff_20": _n_eff(20),
        "n_eff_60": _n_eff(60),
    }
    years = _year_breakdown(band, "fwd_20_hs300_pct")
    total = sum(y["n"] for y in years)
    dom = None
    warnings = [MIRROR_WARNING.format(n=len(band), n_eff=summary["n_eff_20"])]
    if total:
        top = max(years, key=lambda y: y["n"])
        if top["n"] / total > YEAR_DOMINANCE:
            dom = top["year"]
            warnings.append(YEAR_DOMINANCE_WARNING.format(
                year=dom, n=f"{total} 个里的 {top['n']} 个",
                share=_rat(top["n"] / total * 100)))
    return {"ok": True, "asof": recs[-1]["date"], "target": target,
            "matches": picks, "band": band, "summary": summary,
            "years": years, "dominance": dom,
            "eligible": res["eligible"], "exclude_recent": res["exclude_recent"],
            "min_gap": res["min_gap"], "band_quantile": res["quantile"],
            "warning": " ".join(warnings)}


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


def shadow_replay() -> dict:
    """三条候选择时规则的假想成绩 (只做研究, 绝不接仓位)。

    **T+1 口径** (项目管理纪律, 2026-08-24 教训): T 日收盘产生的信号,
    T+1 日才生效 —— 实现为把当日状态 shift(1) 后再乘当日收益。
    原"T 日当天生效"口径对高频规则系统性乐观, 本项目已因此得出过一次假结论。
    多空只有满仓/空仓两态 (空仓按 0 收益, 不计无风险利率)。

    **2026-09-17 加厚 (计划书 §15.1 / §16.2 / §16.7)**:

    1. **毛/净并列**。原来只有毛口径 —— 省略成本会把结论方向都改掉: 实测
       `ma20` 与 `breadth50` 每年翻仓约 15 次 (持有中位 4~5 天), 单次往返 ≥0.11%,
       `breadth50` 毛年化 +0.6% **扣费后转负**。
    2. **HAC(Newey-West) t 值 + 95% 置信区间 + 有效样本量**。日收益自相关会让
       普通标准误把显著性吹大; 报 `n_eff` 才知道"真正独立的信息有多少份"。
    3. **按持有段**的显著性 (§15.1 E2), 且处理**段长异质** (§16.7 MED-4) ——
       段数 <30 的规则不给 t 值。
    4. **双窗口** (§15.1 E6): 复用《公式因子体检方法论》**纪律 2「双窗口一致
       才算数」**, 前一半 / 后一半分别出净口径年化, 同号才算"一致"; 不一致的
       结论一律标"待复核"。
    5. 措辞口径 (§15.2 E4): 置信区间含 0 只能写「**无显著净边际**」,
       **不写"无效"、也不写"跑输"**。
    """
    recs = history(limit=0)
    if len(recs) < 250:
        return {"ok": False,
                "reason": f"连续录像只有 {len(recs)} 条, 至少需要 250 条才能回放"}
    hs = _index_series("000300.SH")
    if hs is None:
        return {"ok": False, "reason": "读不到沪深300日线"}
    df = pd.DataFrame(
        {rule: [(r.get("shadow") or {}).get(rule) for r in recs]
         for rule in SHADOW_RULES},
        index=pd.to_datetime([r["date"] for r in recs]))
    df = df[~df.index.duplicated(keep="last")].sort_index()
    # 回放区间 = 指数真有数据的交易日。2026-09-17 实测踩坑: 录像从 2004-02 开始,
    # 而沪深300指数文件最早只到 2013-01, 若把没指数数据的 2174 天也算进年化分母,
    # 「买入持有」会被压成 +2.6% (真实 +4.2%) —— 分母用的是录像条数而非真实交易日。
    # 状态按完整指数日历 ffill (缺录像的日子沿用上一个已知状态), 保证收益不丢天。
    hs = hs.dropna()
    hs = hs[hs > 0]
    if len(hs) < 250:
        return {"ok": False, "reason": "沪深300指数样本不足 250 个交易日"}
    df = df.reindex(hs.index).ffill()
    first_valid = df.dropna(how="all")
    if len(first_valid) < 250:
        return {"ok": False, "reason": "录像与指数日历无足够重叠"}
    df = df[df.index >= first_valid.index[0]]
    ret = hs.pct_change().reindex(df.index).fillna(0.0)
    years = max((df.index[-1] - df.index[0]).days / 365.25, 1e-9)

    from backtest.metrics import MetricsCalculator  # 指标口径不写第二份

    cost = _cost_params()
    rt = float(cost["round_trip"]) if cost else 0.0

    def _stats(series: pd.Series, yrs: float) -> dict:
        equity = (1.0 + series).cumprod()
        n = len(equity)
        if n < 2:
            return {}
        ann = float(equity.iloc[-1]) ** (1.0 / yrs) - 1   # 按真实跨度年化
        mdd = MetricsCalculator.max_drawdown(equity)
        out = {"annualized_pct": _f(ann * 100, 1),
               "max_drawdown_pct": _f(mdd * 100, 1),
               "sharpe": _f(MetricsCalculator.sharpe_ratio(equity), 2),
               "calmar": _f(MetricsCalculator.calmar_ratio(ann, mdd), 2),
               "total_pct": _f((float(equity.iloc[-1]) - 1) * 100, 1)}
        sig = _hac_tstat(series)
        if sig:
            # 置信区间换成"年化几个百分点"展示 (t 值线性缩放不变, 同一个数)
            out.update({"t": _f(sig["t"], 2),
                        "ci_low_pct": _f(sig["ci_low"] * RET_1Y_BARS * 100, 1),
                        "ci_high_pct": _f(sig["ci_high"] * RET_1Y_BARS * 100, 1),
                        "n_eff": _f(sig["n_eff"], 0), "hac_lags": sig["lags"]})
        return out

    def _cost_series(pos: pd.Series) -> pd.Series:
        """逐日摊成本: 买入当天收佣金, 卖出当天收佣金+印花税。

        单次往返合计 = 佣金×2 + 印花税 = 0.11% (项目默认值, **不含滑点**)。
        """
        if not cost:
            return pd.Series(0.0, index=pos.index)
        d = pos.diff()
        buy = (d > 0).astype(float) * float(cost["commission"])
        sell = (d < 0).astype(float) * (float(cost["commission"]) + float(cost["stamp_tax"]))
        return buy + sell

    def _windows(pos: pd.Series, entry_cost: float = 0.0) -> dict:
        """双窗口 (前一半 / 后一半) 的**净口径**成绩 + 是否同向 (§15.1 E6 / §16.3)。

        `entry_cost` = 窗口第一天就另外补一笔成本 (买入持有用: 它在窗口起点建仓,
        不经过 0→1 的跳变, 逐日摊法抓不到那一笔)。

        **每个窗口还要报自己的持有段数**（审计 F-12）：只看"两半年化同不同号"会漏掉
        一件事 —— 对 `regime` 这种全样本只有 15 段的规则，半段可能只有几段，
        同号与否几乎没有信息量。所以段数 < `MIN_SEGMENTS_FOR_T` 的窗口标
        「样本不足」，**同号也不算数**（与单窗口那条纪律一致）。
        """
        cut = int(len(pos) * WINDOW_SPLIT)
        res: dict = {}
        for name, sub in (("in", pos.iloc[:cut]), ("out", pos.iloc[cut:])):
            if len(sub) < 2:
                res[name] = {}
                continue
            yrs = max((sub.index[-1] - sub.index[0]).days / 365.25, 1e-9)
            sub_ret = ret.reindex(sub.index)
            cs = _cost_series(sub)
            if entry_cost and len(cs):
                cs.iloc[0] += entry_cost
            n_seg = len(_holding_segments(sub))
            res[name] = {"start": sub.index[0].date().isoformat(),
                         "end": sub.index[-1].date().isoformat(),
                         "years": _f(yrs, 1),
                         "round_trips": n_seg,
                         "enough": n_seg >= MIN_SEGMENTS_FOR_T,
                         **_stats(sub * sub_ret - cs, yrs)}
        a, b = res["in"].get("annualized_pct"), res["out"].get("annualized_pct")
        both_enough = bool(res["in"].get("enough") and res["out"].get("enough"))
        res["consistent"] = bool(both_enough and a is not None and b is not None
                                 and (a > 0) == (b > 0))
        res["note"] = ("" if both_enough else
                       f"有一半窗口的持有段不足 {MIN_SEGMENTS_FOR_T} 段"
                       f"（前半 {res['in'].get('round_trips')} 段 / "
                       f"后半 {res['out'].get('round_trips')} 段），同号也不算数")
        return res

    rows = []
    for rule in SHADOW_RULES:
        on = (df[rule] == "on")
        pos = on.shift(1, fill_value=False).astype(float)   # T+1 生效
        gross = pos * ret
        segs = _holding_segments(pos)
        st = _stats(gross, years)
        st.update({"rule": rule, "exposure_pct": _f(pos.mean() * 100, 1),
                   "on_days": int(on.sum()), "days": int(len(on)),
                   "round_trips": len(segs),
                   "segments": _segment_stats(gross, segs, rt),
                   "windows": _windows(pos),
                   "net": _stats(gross - _cost_series(pos), years)})
        rows.append(st)

    # 买入持有: 一次性买入 (一个往返), 成本整笔扣在起点 —— 与规则口径一致。
    bh_gross = ret
    bh_net = ret.copy()
    if cost:
        bh_net.iloc[0] = (1.0 + float(ret.iloc[0])) * (1.0 - rt) - 1.0
    bh = _stats(bh_gross, years)
    bh.update({"rule": "buy_hold", "exposure_pct": 100.0,
               "on_days": len(ret), "days": len(ret), "round_trips": 1,
               "segments": _segment_stats(bh_gross, [(0, len(ret) - 1, len(ret))], rt),
               "windows": _windows(pd.Series(1.0, index=ret.index), entry_cost=rt),
               "net": _stats(bh_net, years)})
    caliber = ("信号在**收盘后**产生、**第二天才生效**（不允许拿当天收盘价再当天吃收益，"
               "否则等于抄答案）；空仓的时段按「不赚不赔」算，不计利息；"
               "年化按**真实经过的年份长度**折算（不是拿录像条数当分母）；"
               "**毛/净并列** —— 「毛」不算交易费用，「净」按项目默认费用扣掉，"
               "只扣了佣金和印花税、**没扣滑点**，单次往返 "
               + (f"{rt * 100:.2f}%" if cost else "【缺】") +
               "，所以**净口径的结果是偏乐观的下限**；"
               "显著性用一套「会给自己人跟自己人相关的部分打折」的算法算 t 值"
               "（免得把显著性吹大 —— 相邻日子的涨跌是重叠的，不打折就会虚高），"
               "并同时报「有效独立样本数」")
    out = {"ok": True, "start": df.index[0].date().isoformat(),
           "end": df.index[-1].date().isoformat(),
           "years": _f(years, 1), "days": len(df),
           "rows": rows, "buy_hold": bh,
           "cost": ({"round_trip_pct": _f(rt * 100, 3),
                     "round_trip_with_slippage_pct":
                         _f(float(cost["round_trip_with_slippage"]) * 100, 3),
                     "note": "只计佣金+印花税, 未计滑点; 滑点按项目默认 0.1%/边 另加 0.2%"}
                    if cost else None),
           "window_split": WINDOW_SPLIT,
           "caliber": caliber}
    return out


def thermometer_md(rec: dict | None = None, mirror_data: dict | None = None,
                   shadow_data: dict | None = None,
                   validity_data: dict | None = None) -> str:
    """组「大盘体温表」Markdown (飞书推送与页面共用同一份文案)。

    三个 `*_data` 参数是**注入接缝**: 传进来就直接用 (页面/测试避免重复计算),
    传 None 就现算。生产路径全部传 None。
    """
    rec = rec or latest()
    if not rec:
        return "# 大盘体温表\n\n【缺】还没有连续录像, 先跑 " \
               "`python tools/market_position_collect.py --backfill`"
    st = ("（**数据滞后**: 最新有效交易日 " + rec["date"] + ", 应有 "
          + rec.get("expected_date", "?") + "）") if rec.get("stale") else ""
    out = [f"# 大盘体温表 {rec['date']} {st}".rstrip()]
    # 审计 F-09：数据滞后时正文里十几处「今天」会让人把日期认错。**由数据生成日词**，
    # 并在抬头紧跟一句说明 —— 只改这一处比改十几处文案可靠（改文案总会漏）。
    try:
        _d = pd.Timestamp(rec["date"])
        day_word = "今天" if not rec.get("stale") else f"{_d.month}月{_d.day}日"
    except Exception:
        day_word = "今天"
    if rec.get("stale"):
        out.append(f"⚠ **下面正文里凡说「{day_word}」的地方，指的都是 "
                   f"{rec['date']} 收盘 —— 不是今天。**"
                   "（本地日线缓存还没拿到更新的收盘数据，报告不拿旧数据冒充新数据。）")
    b = rec.get("breadth") or {}
    t = rec.get("turnover") or {}
    lim = rec.get("limit") or {}
    traded = b.get("traded") or 0
    w = b.get("above_ma20_pct")
    hi, lo, spread = b.get("new_high_60"), b.get("new_low_60"), b.get("hl_spread")
    apct = t.get("amount_pct_1y")
    out.append(
        f"**一句话（先说人话，再给数字）**\n"
        f"{day_word}全市场有 **{traded}** 只股票在交易。\n"
        f"- **站上 20 日均线的只有 {_rat(w)}**（20 日均线 = 最近一个月的平均买入成本，"
        f"跌破它意味着最近一个月买的人大多在亏）—— {_width_plain(w)}\n"
        f"- 一边创新高的有 **{hi}** 家、一边创新低的却有 **{lo}** 家"
        f"（差 {spread}）—— {_hl_plain(hi, lo)}\n"
        f"- 全市场今天成交 **{_yi(t.get('amount_yi'))}**（这是把当天所有股票的成交金额加起来），"
        f"处在**过去一年 {_rat(apct)} 分位** —— {_amount_plain(apct)}\n"
        f"- 涨停 **{lim.get('up')}** 家、跌停 **{lim.get('down')}** 家 —— {_zdt_plain(lim)}")

    rows = ["| 指数 | 收盘 | 十年百分位 | 距十年最高 | 年化波动20日 | 近一年 | 偏离年线 | 牛熊 |",
            "|---|---|---|---|---|---|---|---|"]
    for _key, _name, _code in INDEX_SPECS:
        item = (rec.get("indices") or {}).get(_key) or {}
        _p = item.get("pct_10y")
        rows.append("| {n}({c}) | {cl} | {p} {lbl} | {h} | {v} | {r} | {m} | {g} |".format(
            n=item.get("name", _key), c=item.get("code", _code),
            cl=_num(item.get("close"), 2), p=_rat(_p),
            lbl=_position_plain(_p) if _p is not None else "",
            h=_pct(item.get("from_high_pct")), v=_rat(item.get("vol_ann_20")),
            r=_pct(item.get("ret_1y_pct")), m=_pct(item.get("ma250_dev_pct")),
            g={"bull": "牛", "bear": "熊", "range": "震荡"}.get(
                item.get("regime"), "【缺】")))
    out.append(
        "## 位置（三大指数，各自独立，不合成总分）\n" + "\n".join(rows) + "\n\n"
        "**怎么读这张表**：\n"
        "- **十年百分位** = 「现在的点位比过去十年里百分之多少的交易日都高」。"
        "90.9% 就是「比过去十年 90.9% 的日子都高」→ **偏贵区**。"
        "但它只回答「贵不贵」，**不回答「接下来涨还是跌」**。\n"
        "- **距十年最高** = 离过去十年的最高点还差多少（负数 = 还没回到最高点）。\n"
        "- **年化波动20日** = 最近一个月价格晃得有多凶（越大越坐过山车）。\n"
        "- **偏离年线** = 现在比「年线」（过去 250 个交易日的平均价）高还是低。\n"
        "- **牛熊** = 按「年线斜率」这套口径判的（下面「牛熊区间」一节有第二套口径）。\n"
        "- **为什么不给总分**：三个指数各自独立看。我们**不合成一个总分**，"
        "因为同一天、同一批数据，**换一个判定口径就能得出相反结论**"
        "（下面「牛熊区间」一节有真实例子）。")

    # 牛熊区间与时长 (§14.5): 单点状态答不了"这轮走了多久", 而那是"位置"的一部分。
    # **两条口径并排** (§14.6): 同一天两封外部邮件结论相反, 根因就是口径不同。
    rg = _regime_all()
    rg_lines = [
        "**为什么给两条口径**：2026年9月13日，同一条外部研究流水线在**同一天**发的两封邮件"
        "结论相反（相隔 47 分钟、同一批数据）—— 一封按「20% 法则」说**现在是牛市**，"
        "另一封按「历史特征类比」说**牛市已结束、顶部已过**。根因就是判定口径不同。"
        "**挑一个口径就会得出「唯一答案」的假象，所以两条都给你。**\n"
        "两条口径分别是什么：**年线斜率** = 看价格在「年线」（过去 250 个交易日的平均价）"
        "上方还是下方、且年线自己在往上还是往下；**20% 法则** = 从最近的低点涨够 20% 就算牛、"
        "从最近的高点跌够 20% 就算熊，都没够就是震荡。",
        "",
        "| 指数 | 口径 | 现在 | 本轮起点 | 已走多久 | 本轮涨跌 | 历史上同状态的中位 | 本轮月数的百分位 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    rg_state = {"bull": "牛", "bear": "熊", "range": "震荡"}
    detail: list[str] = []
    flicked: list[str] = []
    for key, _name, _code in INDEX_SPECS:
        item = rg.get(key) or {}
        for ck, short in (("ma250", "年线斜率"), ("pct20", "20% 法则")):
            s = item.get(ck)
            if not s:
                rg_lines.append(f"| {(item or {}).get('name', key)}"
                                f"({(item or {}).get('code', _code)}) | {short} |"
                                " 【缺】 | | | | | |")
                continue
            rg_lines.append(
                f"| {item['name']}({item['code']}) | {short} | "
                f"**{rg_state.get(s['state'], s['state'])}** | {s['since']} | "
                f"{_num(s['months'], 1)} 个月 | {_pct(s['ret_pct'])} | "
                f"{_num(s['median_months'], 1)} 个月 / {_pct(s['median_ret_pct'])} | "
                f"{_rat(s['months_percentile'])} |")
        # 沪深300 的历史明细单独列 (它是影子回放与基准的参照指数)
        if key == "hs300":
            for ck, short in (("ma250", "年线斜率"), ("pct20", "20% 法则")):
                s = item.get(ck)
                if not s or not s.get("longest_rows"):
                    continue
                detail += ["", f"**沪深300 历史上「{rg_state.get(s['state'], s['state'])}」"
                            f"最长的几段（{short}口径，从长到短）**:", "",
                           "| 起 | 止 | 持续月数 | 区间涨跌 |", "|---|---|---|---|"]
                for r in s["longest_rows"]:
                    detail.append(f"| {r['start']} | {r['end']} | {_num(r['months'], 1)} | "
                                  f"{_pct(r['ret_pct'])} |")
        flick = [f"{item['name']}·{(item.get(ck) or {}).get('caliber', ck)}"
                 for ck in ("ma250", "pct20")
                 if (item.get(ck) or {}).get("flicker_note")]
        if flick:
            flicked.extend(flick)
    if flicked:
        detail.append("\n⚠ **有一个口径要打折看**：" + "、".join(flicked) +
                      " —— 该口径在日频上翻状态太勤（中位区间不到 1 个月），"
                      "所以它的「本轮已走多久 / 历史中位」参考价值有限；"
                      "这正是要把两条口径并排摆出来的理由（挑一个就会以为答案唯一）。")
    out.append("## 牛熊区间（这轮走了多久，两条口径并排看）\n"
               + "\n".join(rg_lines + detail))

    # 估值维度 (§14.4): 价格分位 ≠ 估值分位 —— 指数可以在价格高位而估值不高。
    v = rec.get("valuation")
    if v:
        out.append(
            "## 估值（贵不贵，跟位置是两回事）\n"
            f"**股债性价比 {_num(v.get('erp_pct'), 2)}%**"
            f"（= 沪深300 的盈利收益率 1/PE 减掉 10 年期国债收益率），"
            f"处在**过去十年 {_rat(v.get('erp_pct_10y'))} 分位**。\n\n"
            f"怎么读：这个数**越高越划算**（拿着股票的预期回报比拿着国债强多少）。"
            f"现在的 {_num(v.get('erp_pct'), 2)}% 意味着过去十年里只有 "
            f"{_rat(100 - (v.get('erp_pct_10y') or 0))} 的时间比现在更划算；"
            f"十年中位数是 {_num(v.get('erp_median_10y_pct'), 2)}%，"
            f"十年区间 {_num(v.get('erp_min_10y_pct'), 2)}% ~ "
            f"{_num(v.get('erp_max_10y_pct'), 2)}%。\n\n"
            f"口径：{v.get('caliber')}（数据日 {v.get('asof')}，"
            f"十年窗口 {v.get('n_obs')} 个交易日；"
            f"来源：{v.get('source_plain') or v.get('source')}）。"
            "**注意这是沪深300 口径，不是「全市场」口径**。\n\n"
            "**为什么单列这一节**：前面那张位置表全是**价格**算出来的——"
            "价格在高位不等于**贵**（如果公司盈利涨得比股价还快，价格高但估值不高）。"
            "股债性价比是目前唯一有外部独立证据支持的长周期维度"
            "（外部研究实测它与未来 12 个月收益的相关性 rho=+0.48、五等分价差 +26.7pp）。")
    else:
        out.append("## 估值（贵不贵，跟位置是两回事）\n\n"
                   "【缺】没有本地 ERP（股债性价比）缓存。补的办法："
                   "`python tools/market_position_collect.py`（不带参数就是日常采集，"
                   "会联网拉一次）,"
                   "或手工把 `data/market_position/erp.jsonl` 造出来。")

    md = mirror_data if isinstance(mirror_data, dict) else mirror()
    if not md.get("ok"):
        out.append("## 照镜子（历史上跟今天最像的那些日子，之后实际怎么走）\n\n"
                   f"【缺】{md.get('reason')}")
    else:
        s = md.get("summary") or {}
        mir = [
            "**先说清楚：这不是预测。** 下面是把今天的六个指标（三大指数各自的十年位置、"
            "多少股票站在 20 日均线上方、创新高与新低的差、波动大小、成交额高低）"
            "去跟过去每一天比「像不像」，挑出最像的一批，再看**它们之后实际怎么走**。"
            "历史像，不等于这次也会照着走。",
            f"- 能当参照的历史交易日有 **{md.get('eligible')} 天**"
            f"（已经把最近 {md.get('exclude_recent')} 个交易日排除掉 —— "
            "不许拿上个月的日子冒充「历史」，那等于用答案去对答案）。",
            f"- **最像的一档**（把历史上所有的日子按「像今天的程度」排序，"
            f"取最像的前 {_rat((md.get('band_quantile') or 0) * 100)}，共 {s.get('n')} 天）："
            f"这些日子之后 **20 个交易日**（约一个月），沪深300 涨跌的"
            f"**中位数是 {_pct(s.get('fwd_20_median'))}**（一半的日子比它好、一半比它差），"
            f"平均 {_pct(s.get('fwd_20_mean'))}，中间一半落在 "
            f"{_pct(s.get('fwd_20_q25'))} 到 {_pct(s.get('fwd_20_q75'))} 之间，"
            f"上涨的占 {_rat(s.get('fwd_20_up_ratio'))}。",
            f"- **但这个数字要狠狠打折**：这 {s.get('n')} 天挨得很近、涨跌高度重叠，"
            f"**真正独立的信息只有大约 {_num(s.get('n_eff_20'), 1)} 份**"
            "（说白了：看着有一百多个样本，其实只相当于几次互不相干的经历）。"
            f"再往后看 **60 个交易日**（约三个月），中位数 "
            f"{_pct(s.get('fwd_60_median'))}、上涨占比 {_rat(s.get('fwd_60_up_ratio'))}"
            f"（独立信息约 {_num(s.get('n_eff_60'), 1)} 份）。",
        ]
        yrs = md.get("years") or []
        if yrs:
            top2 = sorted(yrs, key=lambda y: -y["n"])[:2]
            tot = sum(y["n"] for y in yrs) or 1
            mir += ["", "**按年份拆开看**（看有没有哪一年在唱独角戏）:", ""]
            if len(top2) == 2:
                mir.append(
                    f"命中日最集中的两年是 **{top2[0]['year']} 年**（{top2[0]['n']} 天）和 "
                    f"**{top2[1]['year']} 年**（{top2[1]['n']} 天），"
                    f"两者合计占了 {_rat((top2[0]['n'] + top2[1]['n']) / tot * 100)} —— "
                    "也就是说，上面的「历史平均」里有多少是这两年的经验，要心里有数。")
            mir += ["",
                    "| 命中日所属年份 | 命中几天 | 之后20日(沪深300)中位 | 上涨占比 |",
                    "|---|---|---|---|"]
            for y in yrs:
                mir.append(f"| {y['year']} 年 | {y['n']} 天 | "
                           f"{_pct(y.get('median_pct'))} | {_rat(y.get('up_ratio_pct'))} |")
        mir += ["", "**最像的几天（明细）**:", "",
                "| 相似日 | 像的程度(距离，越小越像) | 之后20日(沪深300) | "
                "之后60日(沪深300) | 之后20日(上证) |",
                "|---|---|---|---|---|"]
        for m in md.get("matches") or []:
            mir.append(f"| {m['date']} | {m['distance']} | "
                       f"{_pct(m.get('fwd_20_hs300_pct'))} | "
                       f"{_pct(m.get('fwd_60_hs300_pct'))} | "
                       f"{_pct(m.get('fwd_20_sh_pct'))} |")
        mir += ["", md.get("warning", "")]
        out.append("## 照镜子（历史上跟今天最像的那些日子，之后实际怎么走）\n"
                   + "\n".join(mir))

    sd = shadow_data if isinstance(shadow_data, dict) else shadow_replay()
    sh_now = rec.get("shadow") or {}
    _rule_cn = {"ma20": "沪深300 收盘站上自己的 20 日均线就满仓",
                "breadth50": "全市场有一半以上股票站上 20 日均线就满仓",
                "regime": "项目牛熊口径判为「牛」就满仓",
                "buy_hold": "什么都不做，一直拿着（对照用）"}
    sd_lines = [f"**{day_word}这三条规则各自怎么说**: " + "；".join(
        f"{_rule_cn.get(k, k)} → "
        f"{'**在场内**' if v == 'on' else '**空仓**'}" for k, v in sh_now.items()),
        "注意：这只是**影子记录**，系统绝不会按它下单（业务铁律 1：宏观与研判只出报告，"
        "不接仓位）。"]
    if not sd.get("ok"):
        sd_lines.append(f"【缺】{sd.get('reason')}")
    else:
        sd_lines.append(
            f"**下面这段在算什么**: 假设从 {sd.get('start')} 到 {sd.get('end')}"
            f"（{_num(sd.get('years'), 1)} 年）一直照某一条规则做，事后算账会是多少。"
            "**毛** = 不算交易费用；**净** = 按项目默认费用扣掉。费用只算了佣金和印花税，"
            "**没算滑点**，所以真实的净结果只会更差。")
        sd_lines += ["",
                     "| 规则 | 毛年化 | 净年化 | 净口径95%区间 | 净最大回撤 | "
                     "净夏普 | 建仓次数 | 在场时间占比 |",
                     "|---|---|---|---|---|---|---|---|"]
        for r in (sd.get("rows") or []) + [sd.get("buy_hold") or {}]:
            if not r:
                continue
            net = r.get("net") or {}
            sd_lines.append(
                f"| {_rule_cn.get(r.get('rule'), r.get('rule'))} | "
                f"{_pct(r.get('annualized_pct'))} | {_pct(net.get('annualized_pct'))} | "
                f"{_pct(net.get('ci_low_pct'))} ~ {_pct(net.get('ci_high_pct'))} | "
                f"{_pct(net.get('max_drawdown_pct'))} | {_num(net.get('sharpe'), 2)} | "
                f"{r.get('round_trips')} 次 | {_rat(r.get('exposure_pct'))} |")
        sd_lines += ["", f"回放口径: {sd.get('caliber')}", "",
                     "**怎么读这张表**:\n"
                     "- **t 值** = 这个结果离「纯属碰运气」有多远，**越大越不像碰运气**"
                     "（一般要 2 以上才算像样；这里全是 1 上下，也就是**看不出真本事**）。\n"
                     "- **95% 区间** = 「真实水平大概落在这个范围里」。只要这个范围"
                     "**跨过 0**（左边负、右边正），就只能说「**看不出显著的优势或劣势**」"
                     "—— **既不写「这条规则无效」，也不写「跑输一直拿着」**。"
                     "只有整个范围都在 0 以下，才能说「明显比一直拿着差」。\n"
                     "- **有效独立样本** = 天数看着很多，但相邻日子的涨跌是重叠的，"
                     "**真正独立的信息没那么多**，这个数就是打了折之后的信息量。"
                     "⚠ **它是「按天算」的口径**（把每个持仓日当一个观测），所以数出来"
                     "接近总天数；**真正不重复的「下注次数」是「建仓次数」那一列**"
                     "（一个往返 = 建仓到清仓算一次下注）。两个数要一起看："
                     "按天的样本大、按次的下注少，后者才是保守的下界。"]
        # 逐条写人话结论 (§15.2 E4 措辞纪律: 不说"跑输", 说"无显著净边际")
        for r in (sd.get("rows") or []):
            net = r.get("net") or {}
            lo, hi = net.get("ci_low_pct"), net.get("ci_high_pct")
            seg = r.get("segments") or {}
            dtxt = (f"{_num(net.get('t'), 2)}（按天算的有效独立样本约 "
                    f"{_num(net.get('n_eff'), 0)} 天；但**真正独立的下注只有 "
                    f"{_num(seg.get('n'), 0)} 次**（= 建仓次数），"
                    f"**以少的那个为准**）" if net.get("t") is not None else "【缺】")
            if lo is None or hi is None:
                verdict = "数据不足，判不了"
            elif lo <= 0 <= hi:
                verdict = "看不出显著的优势或劣势（区间跨过 0）"
            elif hi < 0:
                verdict = "明显比一直拿着差（整个区间都在 0 以下）"
            else:
                verdict = "明显比一直拿着好（整个区间都在 0 以上）"
            sd_lines.append(
                f"- **{_rule_cn.get(r.get('rule'), r.get('rule'))}**："
                f"这 {_num(sd.get('years'), 1)} 年里一共建仓 {r.get('round_trips')} 次，"
                f"每次持仓中位 {_num(seg.get('median_days'), 0)} 个交易日"
                f"（最短 {seg.get('min_days')} 天、最长 {seg.get('max_days')} 天），"
                f"在场时间占 {_rat(r.get('exposure_pct'))}。"
                f"按**每一笔**算（扣费后）：平均 {_pct(seg.get('mean_return_pct'))}、"
                f"中位 {_pct(seg.get('median_return_pct'))}、"
                f"赚钱的只占 {_rat(seg.get('win_ratio_pct'))}"
                f"（多数小亏、少数大赚，是趋势类规则的典型长相）；"
                f"每笔口径 t 值 {_num(seg.get('t'), 2)}。"
                f"整段的净口径 t 值 {dtxt}；结论：**{verdict}**。"
                + (f"（{seg.get('note')}）" if seg.get("note") else ""))
        # 双窗口一致性 (复用《公式因子体检方法论》纪律 2)
        sd_lines += ["",
                     "**双窗口一致性**（纪律 2「双窗口一致才算数」：把这段历史对半切开，"
                     "两半各算一遍，**只有两半同向才算数**，不一致的结论一律标「待复核」）:",
                     "",
                     "| 规则 | 前半段净年化 | 后半段净年化 | 前半段持有段数 | "
                     "后半段持有段数 | 一致? |", "|---|---|---|---|---|---|"]
        for r in (sd.get("rows") or []) + [sd.get("buy_hold") or {}]:
            if not r:
                continue
            w = r.get("windows") or {}
            wi, wo = w.get("in") or {}, w.get("out") or {}
            if w.get("consistent"):
                verdict = "✅ 同向，算数"
            elif w.get("note"):
                verdict = "⚠ 样本不足，不算数"
            else:
                verdict = "⚠ 不一致，待复核"
            sd_lines.append(
                f"| {_rule_cn.get(r.get('rule'), r.get('rule'))} | "
                f"{_pct(wi.get('annualized_pct'))}（{wi.get('start')} 起） | "
                f"{_pct(wo.get('annualized_pct'))}（{wo.get('start')} 起） | "
                f"{_num(wi.get('round_trips'), 0)} 段 | "
                f"{_num(wo.get('round_trips'), 0)} 段 | {verdict} |")
        _wnote = next((w.get("note") for w in
                       [(r.get("windows") or {}) for r in
                        (sd.get("rows") or []) + [sd.get("buy_hold") or {}]] if w.get("note")),
                      "")
        if _wnote:
            sd_lines += ["",
                         f"（为什么有「样本不足」：{_wnote}。"
                         f"一个往返 = 建仓到清仓算一次「下注」，"
                         f"段数太少时「两半同号」可能只是碰巧 —— "
                         f"所以按纪律 2 的精神标「不算数」，而不是当它通过了。）"]
        c = sd.get("cost") or {}
        if c:
            sd_lines += [
                "",
                f"成本口径: 单次往返 {_num(c.get('round_trip_pct'), 2)}%"
                f"（佣金+印花税）；若再按项目默认滑点 0.1%/边 加 0.2%，"
                f"单次往返就是 {_num(c.get('round_trip_with_slippage_pct'), 2)}% —— "
                "**上表的净口径是乐观下限**。"]
    out.append("## 候选择时规则影子回放（只记录不交易）\n" + "\n".join(sd_lines))

    # 维度体检 (§14.7): 上面那些指标到底有没有用 —— 用数据说话, 不靠"看起来有道理"。
    vd = validity_data if isinstance(validity_data, dict) else _dimension_validity()
    vd_lines = [
        "**为什么要有这一节**：上面的指标**全是价格算出来的**（位置/宽度/量能/波动）。"
        "一条外部研究给了句很扎心的批评：「**该有效的没被重用，不该用的占了 85% 权重**」——"
        "这话对我们是**接近 100%**。所以必须用数据确认「到底哪一维真能预测收益」，"
        "不能靠「看起来有道理」。",
        "",
        "做法：**每个月取一个观测**（不是每天 —— 每天的数据前后重叠得太厉害，"
        "「有效独立样本」只剩个位数，按天算出来的显著性是**假的精度**），"
        "算指标与之后 1/3/6/12 个月沪深300 涨跌的**排名相关性（rho）**"
        "（把指标和收益各自排个名，看两个名次合不合拍；越远离 0 关系越强）"
        "**+ 五分位差**（把历史上所有日子按指标从低到高分成五组，"
        "算「最高那组」比「最低那组」之后多赚/少赚多少）**+ 有效独立样本数**。",
    ]
    if not vd.get("ok"):
        vd_lines.append(f"\n【缺】{vd.get('reason')}")
    else:
        vd_lines += [
            "",
            "| 指标 | 族 | 1 个月 | 3 个月 | 6 个月 | 12 个月 | 12 个月五分位差 | "
            "12 个月的有效独立样本 | 判定 |",
            "|---|---|---|---|---|---|---|---|---|"]
        by_field: dict = {}
        for r in vd["rows"]:
            by_field.setdefault(r["field"], {})[r["horizon_days"]] = r
        warn_cells: list[str] = []      # 收集所有 ⚠（审计 F-04：正文不许与表格打架）

        def _cell(r):
            if not r or r.get("rho") is None:
                return "—"
            ok = r.get("consistent")
            mark = "⚠" if not ok else ""
            if not ok:
                warn_cells.append(f"{r['name']}·{r['horizon']}")
            return f"{r['rho']:+.2f}{_stars(r.get('p'))}{mark}"

        for s in vd["summary"]:
            h = by_field.get(s["field"], {})
            q = (h.get(252) or {}).get("quintile_spread_pct")
            vd_lines.append(
                f"| {s['name']} | {s['family']} | {_cell(h.get(21))} | {_cell(h.get(63))} | "
                f"{_cell(h.get(126))} | {_cell(h.get(252))} | {_pct(q)} | "
                f"{_num((h.get(252) or {}).get('n_eff'), 1)} 份 | {s['label']} |")
        if warn_cells:
            vd_lines.append(
                f"\n**本次共 {len(warn_cells)} 处标了 ⚠**（两半方向打架 → 一律待复核）："
                + "、".join(warn_cells)
                + "。**这些格子里的数一个都不能当结论用。**（这段是从数据生成的，"
                  "不是手写的 —— 免得正文与表格打架。）")
        vd_lines += [
            "",
            "**怎么读**：rho 是「名次合不合拍」—— **+0.5 表示指标越高、之后涨得越多**，"
            "**−0.5 就是反过来**（指标越高、之后跌得越多）。"
            "星号是「这不太可能是碰巧」的可信程度：`***` = 很可信、`**` = 可信、"
            "`*` = 勉强、**没有星号 = 看不出来**。"
            "**⚠ = 两半打架**（把样本按时间对半切开，两半的方向不一致）→ 一律标「待复核」，"
            "**不算数**（项目《公式因子体检方法论》纪律 2「双窗口一致才算数」）。",
            # 审计 F-04：原来这里硬编码「本次唯一一处 ⚠ 是 ERP 的 1 个月」，而同一份
            # 报告的表里其实有 10 处 ⚠ —— **正文与自己的表格打架，会系统性高估可信度**。
            # 改成**由数据生成**（下面的 _warn_cells 在渲染表格时收集）。
            "",
            "**表头怎么读**：「有效独立样本」= 月数 ÷ 持有期月数"
            "（重叠窗口会让信息量远小于观测条数）；「五分位差」= 把历史日子按指标"
            "从低到高分成五组后，最高一组减最低一组之后多赚或少赚的百分点。",
            "",
            "**这次实测出来的几件事**（数字全部由本次数据现算，不写死 —— "
            "写死的统计结论会随数据漂移变成假话，审计 F-06/F-08/【F-04】同一根因）："]
        # 下面三条的**挑法**写死、**数字**全部现算。挑法：12 个月上显著且两半一致的
        # 指标里，rho 最大的是"最强正向"、最小的是"最强负向"；一个持有期都没通过的族
        # 就是"看不出相关性"的族。
        _sig = [s for s in vd["summary"]
                if s["p_12m"] is not None and s["p_12m"] < 0.05
                and s["any_significant_consistent"]]
        _pos = max((s for s in _sig if (s["rho_12m"] or 0) > 0),
                   key=lambda s: s["rho_12m"], default=None)
        _neg = min((s for s in _sig if (s["rho_12m"] or 0) < 0),
                   key=lambda s: s["rho_12m"], default=None)
        _weak = sorted(f for f, v in (vd.get("families") or {}).items() if not v["usable"])
        _weak_fields = [s["name"] for s in vd["summary"] if s["family"] in _weak]
        _facts: list[str] = []

        def _spread(s) -> str:
            q = s.get("spread")
            if q is None:
                return "五分位差算不出来（月频样本不足）"
            return (f"五分位差 **{_pct(q)}**（把历史日子按这个指标从低到高分成五组，"
                    f"最高那组之后比最低那组**{'多' if q > 0 else '少'}赚 "
                    f"{_num(abs(q), 1)} 个百分点**）")

        if _pos:
            _erp_note = ("—— **独立复现了外部研究的结果**（他们 +0.48 / +26.7 个百分点）。"
                         "前面「估值」那一节之所以必须单列，就是这条证据撑起来的。"
                         if _pos["field"] == "erp" else "")
            _facts.append(
                f"**{_pos['name']}是 {_pos['best_horizon']}上最强的正向维度**："
                f"rho **{_pos['rho_12m']:+.2f}**、{_spread(_pos)}"
                + _erp_note)
        if _neg:
            _facts.append(
                f"**{_neg['name']}是 {_neg['best_horizon']}上最强的负相关**"
                f"（方向和直觉相反）："
                f"rho **{_neg['rho_12m']:+.2f}**、{_spread(_neg)}。"
                f"所以位置指标**不是没用，而是方向跟直觉相反** —— 位置越高，之后一年越差"
                f"（样本内均值回归）。这条要与「现在指数在什么位置」放在一起读。")
        if _weak_fields:
            _facts.append(
                f"**{'、'.join(_weak_fields)}（{'、'.join(_weak)}）在四个持有期上全部看不出"
                f"相关性**，与外部研究「资金层/广度层被证伪」的结论一致 —— 所以它们"
                f"**只描述现状，不作预测依据**（注意：我们**不给它们权重、也不合成总分**）。")
        if not _facts:
            _facts.append("**本次没有任何指标在任何持有期上通过双窗口检验** —— "
                          "所有指标都只能描述现状、不作预测依据。")
        vd_lines += [f"{i}. {t}" for i, t in enumerate(_facts, 1)]
        vd_lines += [
            "",
            f"规模：月频样本 **{vd['n_months']} 个月**（{vd['start']} ~ {vd['end']}）、"
            f"共检验 **{vd['n_tests']} 个组合**、归到 **{vd['n_families']} 个族**"
            f"（其中 **{vd['n_families_usable']} 个族**至少在一个持有期上有可用证据："
            f"{'、'.join(vd.get('usable_families') or []) or '无'}）。",
            "",
            "**诚实限制（必须一起读）**："]
        vd_lines += [f"- {x}" for x in vd["limitations"]]
    out.append("## 指标体检（这些指标到底有没有用？）\n" + "\n".join(vd_lines))
    out.append("---\n" + CALIBER_FOOTER)
    return "\n\n".join(out)


# ───────── 内部: 把数字翻译成人话 (模板, 不靠每次自觉; §16.9 第 10 条) ─────────
#
# **为什么必须是函数而不是写死的句子**: 写"八成个股已经跌破 20 日均线"很顺口,
# 但明天宽度涨到 80% 那句话就成了假话。解释必须**由数字生成**。


def _width_plain(w) -> str:
    """站上 20 日均线占比 → 一句人话(含"意味着什么")。"""
    if w is None:
        return "（今天算不出宽度：日线数据不够）"
    below = 100 - float(w)
    if w >= 70:
        feel = "绝大多数股票都在往上走，市场是**普涨**的底子"
    elif w >= 50:
        feel = "过半股票还在往上走，市场**不算弱**"
    elif w >= 30:
        feel = "只有三到五成的股票还在往上走，市场**偏弱**"
    elif w >= 15:
        feel = "只剩不到三分之一的股票还在往上走，**个股层面已经明显失血**"
    else:
        feel = "还在往上走的股票不到六分之一，**绝大多数个股已经跌破**"
    return f"换句话说 **{_rat(below)} 的股票已经跌到 20 日均线下方**，{feel}。"


def _hl_plain(hi, lo) -> str:
    """创新高/新低家数 → 一句人话(强弱对比)。"""
    hi, lo = int(hi or 0), int(lo or 0)
    if hi == 0 and lo == 0:
        return "两边都没有，**看不出方向**。"
    if hi == 0:
        return f"创新高一家都没有、创新低有 {lo} 家，**一边倒地向下**。"
    ratio = lo / hi
    if ratio >= 3:
        feel = "**跌到新低的家数远多于创新高的**，破位下跌的股票明显更多"
    elif ratio >= 1.5:
        feel = "跌到新低的比创新高的多一些，**偏弱**"
    elif ratio >= 0.7:
        feel = "两边差不多，**没有明显方向**"
    else:
        feel = "创新高的明显多于创新低的，**走强的股票更多**"
    return f"跌到新低的家数是创新高的 **{ratio:.1f} 倍**，{feel}。"


def _amount_plain(apct) -> str:
    """成交额的一年百分位 → 一句人话(量能冷热)。"""
    if apct is None:
        return "（量能冷热算不出来：历史不足一年）"
    a = float(apct)
    if a >= 80:
        feel = "**成交非常活跃**，处在过去一年最热闹的那一档"
    elif a >= 60:
        feel = "成交偏活跃"
    elif a >= 40:
        feel = "成交中等，不温不火"
    elif a >= 20:
        feel = "成交偏冷"
    else:
        feel = (f"**成交冷到了地板上**，过去一年里 {_rat(100 - a)} 的交易日"
                "都比今天热闹")
    return (f"{feel}（量能是行情的燃料：燃料少的时候，"
            "涨也涨不远、跌也跌不深）。")


def _zdt_plain(lim) -> str:
    """涨停/跌停家数 → 一句人话(多空强弱)。"""
    up, dn = int((lim or {}).get("up") or 0), int((lim or {}).get("down") or 0)
    if up == 0 and dn == 0:
        return "两边都没有，**没有极端情绪**。"
    if dn == 0:
        return f"涨停 {up} 家、跌停一家都没有，**情绪偏多**。"
    ratio = up / dn
    if ratio >= 2:
        feel = "**涨停明显多于跌停，情绪偏多**"
    elif ratio <= 0.5:
        feel = "**跌停明显多于涨停，情绪偏空**"
    else:
        feel = "**涨的和跌的差不多，今天没有明显方向**"
    return f"涨停是跌停的 **{ratio:.1f} 倍**，{feel}。"


def _position_plain(pct) -> str:
    """十年百分位 → 一个短标签 (表格里跟在数字后面, 不用读者自己换算)。"""
    if pct is None:
        return ""
    p = float(pct)
    if p >= 80:
        return "（偏贵区）"
    if p >= 60:
        return "（偏高）"
    if p >= 40:
        return "（中间）"
    if p >= 20:
        return "（偏低）"
    return "（便宜区）"


def _stars(p) -> str:
    """p 值 → 星号 (p 为 None 时不写星号, 表示"没算出/不显著", 绝不冒充显著)。"""
    if p is None:
        return ""
    if p < 0.01:
        return "***"
    if p < 0.05:
        return "**"
    if p < 0.10:
        return "*"
    return ""


def _num(v, nd: int = 2) -> str:
    return "【缺】" if v is None else f"{v:.{nd}f}"


def _pct(v) -> str:
    """带符号百分数 (收益/偏离/回撤 这类有方向的量)。

    四舍五入后是 0 时不写符号 —— 写 "+0.0%" / "-0.0%" 会被当成有方向, 误导。
    """
    if v is None:
        return "【缺】"
    return "0.0%" if abs(float(v)) < 0.05 else f"{float(v):+.1f}%"


def _rat(v) -> str:
    """不带符号百分数 (占比/分位 这类 0~100 的量, 写 +90.9% 会误导)。"""
    return "【缺】" if v is None else f"{v:.1f}%"


def _yi(v) -> str:
    return "【缺】" if v is None else f"{v:,.0f}亿元"


def push_thermometer(rec: dict | None = None, title: str | None = None,
                     mirror_data: dict | None = None,
                     shadow_data: dict | None = None) -> dict:
    """把体温表推飞书 (复用 tools/send_report_feishu.py, 不写第二份推送)。

    永远 fail-soft: webhook 未配置 / 推送失败都只返 {"ok": False, "reason": ...},
    绝不抛 —— 调度器 job 与页面按钮都不该因为推送失败而中断。
    """
    rec = rec or latest()
    if not rec:
        return {"ok": False, "reason": "还没有连续录像"}
    md = thermometer_md(rec, mirror_data=mirror_data, shadow_data=shadow_data)
    try:
        from tools.send_report_feishu import (chunk_sections, load_webhook,
                                              md_to_lark, send_card)
    except Exception as e:
        return {"ok": False, "reason": f"飞书推送器不可用: {e}"}
    try:
        webhook = load_webhook()
    except SystemExit:
        return {"ok": False, "reason": "未配置 FEISHU_WEBHOOK_URL (.env)"}
    except Exception as e:
        return {"ok": False, "reason": f"读取 webhook 失败: {e}"}
    title = title or f"大盘体温表 {rec.get('date', '')}"
    try:
        chunks = chunk_sections(md_to_lark(md))
        codes = []
        for i, c in enumerate(chunks, 1):
            r = send_card(webhook, title, c, i, len(chunks))
            codes.append(r.get("code", r.get("StatusCode")))
        return {"ok": True, "cards": len(chunks), "codes": codes}
    except Exception as e:
        return {"ok": False, "reason": f"推送失败: {e}"}


# ───────────────── 模块级转发 (2026-09-19 批次 5.1) ─────────────────
#: 已搬到 core/market_position_io.py 的旧入口名单。用 PEP 562 模块级 __getattr__
#: **动态**转发 —— 不做 `X = mpio.X` 快照: 快照会在 conftest patch 基座后变成陈旧
#: 副本 (谁读它谁写生产路径), 动态转发永远拿到基座当前值。
_FORWARDED_TO_IO = frozenset({
    "_ROOT", "KLINE_1D_DIR", "DAILY_PATH", "INDEX_SPECS",
    "_UPSERT_LOCK_TIMEOUT", "_index_series", "_f", "_expected_trading_day",
    "_upsert", "_upsert_locked", "_cross_process_lock", "_lock_file",
    "_unlock_file", "history", "latest",
})

#: 上面那批里的**可变状态** (路径/阈值) —— 这几个绝不许在本模块留下实体副本:
#: 副本是 import 时快照, 基座被 patch (测试隔离) 后会指向生产路径。
#: 其余名字是**函数对象**, 显式 import 是合法且必要的 (模块内裸全局名不走
#: __getattr__; 函数对象也不承载可变状态)。测试 `TestIsolationGuard` 用它守门。
_IO_STATE_NAMES = frozenset({
    "_ROOT", "KLINE_1D_DIR", "DAILY_PATH", "_UPSERT_LOCK_TIMEOUT",
})


def __getattr__(name: str):
    """旧入口转发到共享底座 (PEP 562)。未知名照常 AttributeError。"""
    if name in _FORWARDED_TO_IO:
        return getattr(mpio, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
