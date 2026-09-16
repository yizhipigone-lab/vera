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

import datetime as dt
import json
import os
import re
import threading
from pathlib import Path

import numpy as np
import pandas as pd

from core.limit_ratio import limit_ratio
from core.market_position import (POSITION_COLUMNS, RECENT_EXCLUDE_BARS,
                                  RET_1Y_BARS, SIMILAR_FEATURES,
                                  breadth_frame, forward_return,
                                  index_position_series, last_valid_date,
                                  limit_counts_series, similar_days)

try:  # pragma: no cover - 循环导入兜底 (logger 永远可用, 这里只是防御)
    from utils.logger import get_logger
    _logger = get_logger(__name__)
except Exception:  # pragma: no cover
    import logging
    _logger = logging.getLogger(__name__)

__all__ = ["collect", "latest", "history", "mirror", "shadow_replay",
           "thermometer_md", "push_thermometer",
           "INDEX_SPECS", "SHADOW_RULES", "DAILY_PATH", "MIRROR_WARNING"]

_ROOT = Path(__file__).resolve().parent.parent
#: 日线缓存目录 (与 core.kline_cache 的 <cache_dir>/<period>/<code>.parquet 约定一致)
KLINE_1D_DIR = _ROOT / "data" / "kline_cache" / "1d"
#: 连续录像落盘路径 (JSONL, 一天一行)
DAILY_PATH = _ROOT / "data" / "market_position" / "daily.jsonl"

#: 三大指数: (内部键, 中文名, TDX 代码)
INDEX_SPECS = (("shanghai", "上证指数", "000001.SH"),
               ("hs300", "沪深300", "000300.SH"),
               ("chuangyeban", "创业板指", "399006.SZ"))
#: 沪深股票代码 (沪市 6 开头, 深市 000/001/002/003/300/301) —— 排除指数/ETF/债券
_STOCK_RE = re.compile(r"^(6\d{5}\.SH|(000|001|002|003|300|301)\d{3}\.SZ)$")
#: 日常采集窗口: 覆盖"成交额一年百分位"(250 根) + 60 日新高低 + 缓冲
DEFAULT_BARS = 300
#: 回填窗口: 0 = 全量历史
BACKFILL_BARS = 0
#: 有效交易日判据: 当日有成交的股票占比。空壳 bar 的比例会掉到 1% 以下。
MIN_TRADED_RATIO = 0.5
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
#: 体温表尾部铁律提示 + 已知偏差 (写成常量, 不靠各处自觉)
CALIBER_FOOTER = (
    "只读参考, 不联入任何仓位调度 (业务铁律 1)。指标口径唯一真相源 = "
    "core/market_position.py; 牛熊走 core/index_regime.py; 涨跌停幅度走 "
    "core/limit_ratio.py。\n"
    "**三条已知偏差必须一起读**: ①涨停家数由本地日线按 10%/20% 幅度推导, "
    "ST 股 (±5%) 会漏计 (低估); ②宽度与相似日的历史序列只用「今天仍在缓存里」"
    "的股票, 已退市股不在内 (生存者偏差), 早年 (2016 年前) 缓存覆盖的股票数"
    "明显偏少; ③十年百分位在历史不足十年时按可用年份计算 (最少要求满 3 年才"
    "给数), 故 2016~2019 年的百分位窗口短于十年, 与今天不可完全并排比较。")

#: 采集串行锁: 页面手动采集与调度器 15:50 的 job 可能同时跑,
#: 两个写入方并发读写同一个 JSONL 会互相覆盖丢记录 (upsert 是"读全量→写全量")。
_COLLECT_LOCK = threading.Lock()


# ───────────────────── 内部: 取数 ─────────────────────


def _load_matrices(bars: int):
    """读全部沪深日线 → (close, volume, amount) 三个宽表 (索引=日期, 列=代码)。"""
    files = sorted(f for f in KLINE_1D_DIR.glob("*.parquet")
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


def _index_series(code: str) -> pd.Series | None:
    """读单个指数日线 close 序列; 文件不存在/为空返 None。"""
    p = KLINE_1D_DIR / f"{code}.parquet"
    if not p.exists():
        return None
    try:
        df = pd.read_parquet(p, columns=["date", "close"])
    except Exception as e:
        _logger.warning("大盘位置: 读指数 %s 失败: %s", code, e)
        return None
    if len(df) == 0:
        return None
    s = pd.Series(df["close"].to_numpy(dtype=float),
                  index=pd.to_datetime(df["date"]))
    return s[~s.index.duplicated(keep="last")].sort_index()


def _f(v, nd: int = 1):
    """→ float 或 None (NaN/inf 一律 None, 不拿 0 冒充缺失)。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x or x in (float("inf"), float("-inf")):
        return None
    return round(x, nd)


def _expected_trading_day() -> dt.date:
    """最近应有日线数据的交易日 (复用 kline_cache_maintenance 单一真相源)。"""
    try:
        from core.kline_cache_maintenance import expected_last_trading_day
        return expected_last_trading_day()
    except Exception as e:      # 日历不可用 → 退化为今天 (上层只会更宽松, 不谎报新鲜)
        _logger.warning("大盘位置: 交易日历不可用, 按今天处理: %s", e)
        return dt.date.today()


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
            item[col] = _f(r[col]) if col != "regime" else (
                None if r[col] is None or r[col] != r[col] else str(r[col]))
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


# ───────────────────── 内部: JSONL 读写 ─────────────────────


def _upsert(records: list[dict], path: Path | None = None) -> int:
    """按日期 upsert 到 JSONL (同一天覆盖, 不追加重复行) → 返回总行数。

    原子写: 临时文件 + os.replace (照 VeraScheduler._save_state 的写法),
    防崩溃写半个文件把整段录像毁掉。
    """
    if not records:
        return 0
    p = Path(path or DAILY_PATH)
    existing: dict[str, dict] = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(o, dict) and o.get("date"):
                existing[str(o["date"])] = o
    for r in records:
        existing[str(r["date"])] = r
    rows = [json.dumps(existing[k], ensure_ascii=False) for k in sorted(existing)]
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text("\n".join(rows) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return len(rows)


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
        return _collect_locked(bars=bars, write=write, expected=expected)
    finally:
        _COLLECT_LOCK.release()


def _collect_locked(*, bars: int, write: bool,
                    expected: dt.date | None) -> dict:
    """collect 的实际实现 (调用方已持 _COLLECT_LOCK)。"""
    close_df, vol_df, amt_df = _load_matrices(bars)
    if close_df.empty:
        return {"ok": False, "reason": f"本地日线缓存为空 ({KLINE_1D_DIR})"}
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
    amt_rank = total_amt.rolling(250, min_periods=120).rank(pct=True) * 100

    ratios = pd.Series({c: limit_ratio(c) for c in close_df.columns}, dtype=float)
    hs_raw = idx_raw.get("hs300")
    hs_ma20 = hs_raw.rolling(20, min_periods=20).mean() if hs_raw is not None else None
    valid = [d for d in bf.index
             if bf.at[d, "traded_ratio"] == bf.at[d, "traded_ratio"]
             and bf.at[d, "traded_ratio"] >= MIN_TRADED_RATIO]
    # 首行没有"昨收"可比, 涨跌停会算成 0 → 跳过, 不写假 0
    valid = [d for d in valid if d > bf.index[0]]

    # 涨停/跌停: 掩码与昨收在 limit_counts_series 内部只算一次, 逐日只做轻量比较
    limit_map = limit_counts_series(close_df, vol_df, ratios, valid)
    records = [_build_record(d, bf.loc[d], idx_hist, hs_raw, hs_ma20, total_amt,
                             amt_rank, limit_map, exp_ts)
               for d in valid]
    lines = _upsert(records) if write else 0
    return {"ok": True, "asof": pd.Timestamp(asof).date().isoformat(),
            "expected": exp_ts.date().isoformat(), "stale": bool(stale),
            "records": len(records), "lines": lines,
            "snapshot": records[-1] if records else None}


def history(limit: int = 250) -> list[dict]:
    """读连续录像, 按日期升序; limit=0 返全部。文件坏行跳过不抛。"""
    p = Path(DAILY_PATH)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(o, dict) and o.get("date"):
            out.append(o)
    out.sort(key=lambda r: str(r["date"]))
    return out[-int(limit):] if limit else out


def latest() -> dict | None:
    """最新一条记录 (无录像返 None)。"""
    h = history(limit=1)
    return h[-1] if h else None


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
            res[name] = {"start": sub.index[0].date().isoformat(),
                         "end": sub.index[-1].date().isoformat(),
                         "years": _f(yrs, 1),
                         **_stats(sub * sub_ret - cs, yrs)}
        a, b = res["in"].get("annualized_pct"), res["out"].get("annualized_pct")
        res["consistent"] = bool(a is not None and b is not None and (a > 0) == (b > 0))
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
    caliber = ("信号 T 日收盘产生 → T+1 生效; 空仓按 0 收益; 年化按真实日历跨度折算; "
               "**毛/净并列** —— 净口径按项目默认成本只计佣金×2 + 印花税(卖出单边), "
               "单次往返 " + (f"{rt * 100:.2f}%" if cost else "【缺】") +
               ", **未计滑点**(真实只会更差); 显著性用 Newey-West (HAC) t 值, "
               "有效样本量 = 天数 ÷ 方差膨胀因子")
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
                   shadow_data: dict | None = None) -> str:
    """组「大盘体温表」Markdown (飞书推送与页面共用同一份文案)。"""
    rec = rec or latest()
    if not rec:
        return "# 大盘体温表\n\n【缺】还没有连续录像, 先跑 " \
               "`python tools/market_position_collect.py --backfill`"
    st = ("（**数据滞后**: 最新有效交易日 " + rec["date"] + ", 应有 "
          + rec.get("expected_date", "?") + "）") if rec.get("stale") else ""
    out = [f"# 大盘体温表 {rec['date']} {st}".rstrip()]
    b = rec.get("breadth") or {}
    t = rec.get("turnover") or {}
    lim = rec.get("limit") or {}
    out.append(
        f"**一句话**: 全A {b.get('traded')} 只有成交, "
        f"站上20日均线 {_rat(b.get('above_ma20_pct'))}, "
        f"创60日新高 {b.get('new_high_60')} 家 vs 新低 {b.get('new_low_60')} 家 "
        f"(差 {b.get('hl_spread')}); 全A成交额 {_yi(t.get('amount_yi'))} "
        f"(一年百分位 {_rat(t.get('amount_pct_1y'))}); "
        f"涨停 {lim.get('up')} 家 / 跌停 {lim.get('down')} 家。")

    rows = ["| 指数 | 收盘 | 十年百分位 | 距十年最高 | 年化波动20日 | 近一年 | 偏离年线 | 牛熊 |",
            "|---|---|---|---|---|---|---|---|"]
    for _key, _name, _code in INDEX_SPECS:
        item = (rec.get("indices") or {}).get(_key) or {}
        rows.append("| {n}({c}) | {cl} | {p} | {h} | {v} | {r} | {m} | {g} |".format(
            n=item.get("name", _key), c=item.get("code", _code),
            cl=_num(item.get("close"), 2), p=_rat(item.get("pct_10y")),
            h=_pct(item.get("from_high_pct")), v=_rat(item.get("vol_ann_20")),
            r=_pct(item.get("ret_1y_pct")), m=_pct(item.get("ma250_dev_pct")),
            g={"bull": "牛", "bear": "熊", "range": "震荡"}.get(
                item.get("regime"), "【缺】")))
    out.append("## 位置（三大指数，各自独立，不合成总分）\n" + "\n".join(rows))

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
            "不许拿上个月的日子冒充「历史」）。",
            f"- 最像的一档（距离最近的 {_rat((md.get('band_quantile') or 0) * 100)}"
            f"，共 {s.get('n')} 天）：这些日子之后 **20 个交易日**（约一个月），"
            f"沪深300 涨跌的**中位数是 {_pct(s.get('fwd_20_median'))}**，"
            f"平均 {_pct(s.get('fwd_20_mean'))}，"
            f"中间一半落在 {_pct(s.get('fwd_20_q25'))} 到 {_pct(s.get('fwd_20_q75'))} 之间，"
            f"上涨的占 {_rat(s.get('fwd_20_up_ratio'))}。",
            f"- **但要把这个数字狠狠打个折**：这 {s.get('n')} 天挨得很近、涨跌高度重叠，"
            f"**真正独立的信息只有大约 {_num(s.get('n_eff_20'), 1)} 份**。"
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
    sd_lines = ["**今天这三条规则各自怎么说**: " + "；".join(
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
                     "**怎么读这张表**: 净口径的 95% 区间只要**跨过 0**"
                     "（左边是负的、右边是正的），就只能说「**看不出显著的优势或劣势**」"
                     "—— 既不写「这条规则无效」，也不写「跑输买入持有」。"
                     "只有整个区间都在 0 以下，才能说「明显比一直拿着差」。"]
        # 逐条写人话结论 (§15.2 E4 措辞纪律: 不说"跑输", 说"无显著净边际")
        for r in (sd.get("rows") or []):
            net = r.get("net") or {}
            lo, hi = net.get("ci_low_pct"), net.get("ci_high_pct")
            seg = r.get("segments") or {}
            dtxt = (f"{_num(net.get('t'), 2)}（有效独立样本约 "
                    f"{_num(net.get('n_eff'), 0)} 天）" if net.get("t") is not None else "【缺】")
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
                     "| 规则 | 前半段净年化 | 后半段净年化 | 一致? |", "|---|---|---|---|"]
        for r in (sd.get("rows") or []) + [sd.get("buy_hold") or {}]:
            if not r:
                continue
            w = r.get("windows") or {}
            wi, wo = w.get("in") or {}, w.get("out") or {}
            sd_lines.append(
                f"| {_rule_cn.get(r.get('rule'), r.get('rule'))} | "
                f"{_pct(wi.get('annualized_pct'))}（{wi.get('start')} 起） | "
                f"{_pct(wo.get('annualized_pct'))}（{wo.get('start')} 起） | "
                f"{'✅ 同向，算数' if w.get('consistent') else '⚠ 不一致，待复核'} |")
        c = sd.get("cost") or {}
        if c:
            sd_lines += [
                "",
                f"成本口径: 单次往返 {_num(c.get('round_trip_pct'), 2)}%"
                f"（佣金+印花税）；若再按项目默认滑点 0.1%/边 加 0.2%，"
                f"单次往返就是 {_num(c.get('round_trip_with_slippage_pct'), 2)}% —— "
                "**上表的净口径是乐观下限**。"]
    out.append("## 候选择时规则影子回放（只记录不交易）\n" + "\n".join(sd_lines))
    out.append("---\n" + CALIBER_FOOTER)
    return "\n\n".join(out)


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
