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

import pandas as pd

from core.limit_ratio import limit_ratio
from core.market_position import (POSITION_COLUMNS, SIMILAR_FEATURES,
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
    "样本只有 {n} 个, 且 A 股几十年只经历过屈指可数的几轮周期 —— "
    "「历史相似」不是预测, 只是提供一个参照系: 上表说的是\"那几次之后实际怎么走\", "
    "不等于这次也会那样走。")
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
    picks = similar_days(target, hist, top_n=top_n)
    hs = _index_series("000300.SH")
    sh = _index_series("000001.SH")
    for p in picks:
        p["fwd_20_hs300_pct"] = forward_return(hs, p["date"], 20) if hs is not None else None
        p["fwd_60_hs300_pct"] = forward_return(hs, p["date"], 60) if hs is not None else None
        p["fwd_20_sh_pct"] = forward_return(sh, p["date"], 20) if sh is not None else None
    f20 = [p["fwd_20_hs300_pct"] for p in picks if p["fwd_20_hs300_pct"] is not None]
    f60 = [p["fwd_60_hs300_pct"] for p in picks if p["fwd_60_hs300_pct"] is not None]
    summary = {
        "n": len(picks),
        "fwd_20_median": _f(pd.Series(f20).median(), 2) if f20 else None,
        "fwd_20_up_ratio": _f(sum(1 for x in f20 if x > 0) / len(f20) * 100, 0) if f20 else None,
        "fwd_60_median": _f(pd.Series(f60).median(), 2) if f60 else None,
        "fwd_60_up_ratio": _f(sum(1 for x in f60 if x > 0) / len(f60) * 100, 0) if f60 else None,
    }
    return {"ok": True, "asof": recs[-1]["date"], "target": target,
            "matches": picks, "summary": summary,
            "warning": MIRROR_WARNING.format(n=len(picks))}


def shadow_replay() -> dict:
    """三条候选择时规则的假想成绩 (只做研究, 绝不接仓位)。

    **T+1 口径** (项目管理纪律, 2026-08-24 教训): T 日收盘产生的信号,
    T+1 日才生效 —— 实现为把当日状态 shift(1) 后再乘当日收益。
    原"T 日当天生效"口径对高频规则系统性乐观, 本项目已因此得出过一次假结论。
    多空只有满仓/空仓两态 (空仓按 0 收益, 不计无风险利率)。
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

    def _stats(series: pd.Series) -> dict:
        equity = (1.0 + series).cumprod()
        n = len(equity)
        if n < 2:
            return {}
        ann = float(equity.iloc[-1]) ** (1.0 / years) - 1   # 按真实跨度年化
        mdd = MetricsCalculator.max_drawdown(equity)
        return {"annualized_pct": _f(ann * 100, 1),
                "max_drawdown_pct": _f(mdd * 100, 1),
                "sharpe": _f(MetricsCalculator.sharpe_ratio(equity), 2),
                "calmar": _f(MetricsCalculator.calmar_ratio(ann, mdd), 2),
                "total_pct": _f((float(equity.iloc[-1]) - 1) * 100, 1)}

    rows = []
    for rule in SHADOW_RULES:
        on = (df[rule] == "on")
        pos = on.shift(1, fill_value=False).astype(float)   # T+1 生效
        st = _stats(pos * ret)
        st.update({"rule": rule, "exposure_pct": _f(pos.mean() * 100, 1),
                   "on_days": int(on.sum()), "days": int(len(on))})
        rows.append(st)
    bh = _stats(ret)
    bh.update({"rule": "buy_hold", "exposure_pct": 100.0,
               "on_days": len(ret), "days": len(ret)})
    return {"ok": True, "start": df.index[0].date().isoformat(),
            "end": df.index[-1].date().isoformat(),
            "years": _f(years, 1), "days": len(df),
            "rows": rows, "buy_hold": bh,
            "caliber": "信号 T 日收盘产生 → T+1 生效; 空仓按 0 收益; "
                       "年化按真实日历跨度折算"}


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
        out.append("## 照镜子（历史上最像的几天，之后实际怎么走）\n\n"
                   f"【缺】{md.get('reason')}")
    else:
        s = md.get("summary") or {}
        mir = [f"最近 {s.get('n')} 次相似日之后: 沪深300 20日中位数 "
               f"{_pct(s.get('fwd_20_median'))}、上涨占比 {_rat(s.get('fwd_20_up_ratio'))}; "
               f"60日中位数 {_pct(s.get('fwd_60_median'))}。",
               "| 相似日 | 距离 | 之后20日(沪深300) | 之后60日(沪深300) | 之后20日(上证) |",
               "|---|---|---|---|---|"]
        for m in md.get("matches") or []:
            mir.append(f"| {m['date']} | {m['distance']} | "
                       f"{_pct(m.get('fwd_20_hs300_pct'))} | "
                       f"{_pct(m.get('fwd_60_hs300_pct'))} | "
                       f"{_pct(m.get('fwd_20_sh_pct'))} |")
        mir.append(md.get("warning", ""))
        out.append("## 照镜子（历史上最像的几天，之后实际怎么走）\n"
                   + "\n".join(mir))

    sd = shadow_data if isinstance(shadow_data, dict) else shadow_replay()
    sh_now = rec.get("shadow") or {}
    sd_lines = ["今日状态: " + ", ".join(
        f"{k}={'多头' if v == 'on' else '空头'}" for k, v in sh_now.items())]
    if not sd.get("ok"):
        sd_lines.append(f"【缺】{sd.get('reason')}")
    else:
        sd_lines.append(f"回放口径: {sd.get('caliber')}"
                        f"（{sd.get('start')} ~ {sd.get('end')}）")
        sd_lines += ["| 规则 | 年化 | 最大回撤 | 夏普 | 卡玛 | 持仓占比 |",
                     "|---|---|---|---|---|---|"]
        for r in (sd.get("rows") or []) + [sd.get("buy_hold") or {}]:
            if not r:
                continue
            label = {"ma20": "沪深300 > MA20",
                     "breadth50": "宽度≥50%",
                     "regime": "牛熊口径=牛",
                     "buy_hold": "买入持有（对照）"}.get(r.get("rule"), r.get("rule"))
            sd_lines.append(
                f"| {label} | {_pct(r.get('annualized_pct'))} | "
                f"{_pct(r.get('max_drawdown_pct'))} | {_num(r.get('sharpe'), 2)} | "
                f"{_num(r.get('calmar'), 2)} | {_rat(r.get('exposure_pct'))} |")
    out.append("## 候选择时规则影子记录（只记录不交易）\n" + "\n".join(sd_lines))
    out.append("---\n" + CALIBER_FOOTER)
    return "\n\n".join(out)


def _num(v, nd: int = 2) -> str:
    return "【缺】" if v is None else f"{v:.{nd}f}"


def _pct(v) -> str:
    """带符号百分数 (收益/偏离/回撤 这类有方向的量)。"""
    return "【缺】" if v is None else f"{v:+.1f}%"


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
