# -*- coding: utf-8 -*-
"""core/market_dashboard_runner.py — 大盘环境仪表盘·编排+取数+落库 (2026-09-18)。

职责(本模块是大盘仪表盘唯一"联网 + 调度 + 落库"的一方):
  取数(akshare 各源 + 复用本地日线缓存) → 打分(market_score) → 事件修正(market_events)
  → 落快照(data/market_position/dashboard.jsonl)。

铁律(与 core/market_position*.py 同一套, 由 tests 的 AST 断言守护):
  - **不 import trade**(业务铁律 1: 大盘研判只出报告, 不联入任何实盘仓位调度);
  - 总分是**温度分**(描述现状), 不是涨跌预测分; 页面/报告必须标注口径与可信度;
  - 数据拿不到时**沿用上一次有效值并标注"数据暂缺, 沿用历史得分"**(需求第七条第 2 款),
    严禁编造数据。

取数层三条硬规矩(来自《docs/2026-09-18_大盘仪表盘数据源可获得性_审计报告》§3.7/§3.8):
  1. 每个源最多重试 3 次、间隔 ~1.5 秒; 2. 单次超时 25 秒; 3. 失败降级沿用旧值并标 stale。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import socket
import threading
import time

import pandas as pd

from utils.logger import get_logger
from utils.sysutil import project_root
from core import market_events
from core import market_position_runner as mpr  # 复用本地缓存加载与原子写(不 import trade)
from core import market_score

_logger = get_logger(__name__)

_ROOT = project_root()
DASHBOARD_DIR = _ROOT / "data" / "market_position"
DASHBOARD_PATH = DASHBOARD_DIR / "dashboard.jsonl"

_LOCK = threading.Lock()

_FETCH_TIMEOUT = 25          # 单源超时(秒)
_FETCH_RETRIES = 3           # 单源重试次数
_FETCH_DELAY = 1.5           # 重试间隔(秒)

#: 宏观月度数据的"疑似发布窗口"(每月几号 ~ 几号 + 月末几天), 窗口内 refresh_macro 才真正取数,
#: 窗口外秒回 no-op。央行 M1/M2 多在 9~15 日发布, 统计局 PMI 在月末、PPI 在 9~10 日。
MACRO_WINDOW_DAYS = set(range(9, 16)) | {28, 29, 30, 31}

#: 沪深300 指数代码(本地日线缓存文件名)
_HS300_CODE = "000300.SH"
#: 200 日均线窗口(需求要 200 日, 现有 market_position 用的是 250 日, 这里独立算 200 日)
_MA200 = 200
#: 成交额/涨跌家数的"趋势均值"窗口(与需求"20日均值"一致)
_TREND_WIN = 20
#: 两融趋势的"60交易日"与"单周"窗口
_MARGIN_LONG = 60
_MARGIN_WEEK = 5
#: amount 列单位是"万元", 除以这个得"亿元"(照 market_position_runner.AMOUNT_WAN_PER_YI)
_WAN_PER_YI = 1e4


# ──────────────────────────── 取数工具 ────────────────────────────

def _ak():
    import akshare as ak  # lazy import(照 market_position_runner._refresh_erp 模式)
    return ak


def _call(fn, *args, retries=_FETCH_RETRIES, **kw):
    """带重试的调用: 成功返回 (result, None), 全失败返回 (None, err)。单源失败不抛出。"""
    err = None
    for i in range(retries):
        try:
            socket.setdefaulttimeout(_FETCH_TIMEOUT)
            return fn(*args, **kw), None
        except Exception as e:  # noqa: BLE001 (取数层必须 fail-soft)
            err = e
            _logger.warning("大盘仪表盘取数失败(第%d/%d次): %s", i + 1, retries, e)
            if i < retries - 1:
                time.sleep(_FETCH_DELAY)
    return None, err


def _num(v, nd=4):
    """→ float 或 None(NaN/inf 一律 None, 不拿 0 冒充缺失)。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x or x in (float("inf"), float("-inf")):
        return None
    return round(x, nd)


def _last_valid(series: pd.Series):
    """序列里最后一个非空值(用于美债这种"最新行是 nan、要往前找"的时差错位)。"""
    s = series.dropna()
    return (float(s.iloc[-1]), s.index[-1]) if len(s) else (None, None)


# ──────────────────────────── 各源取数 ────────────────────────────

def _fetch_valuation() -> dict:
    """估值维度: ERP / 全A PE分位(中位数) / 上证PB分位 / 股息率-国债利差。"""
    ak = _ak()
    out = {}
    # ERP 股债利差(沪深300口径, 日频)
    df, err = _call(ak.stock_ebs_lg)
    if df is not None and len(df):
        out["erp"] = (_num(df["股债利差"].iloc[-1] * 100, 4), str(df["日期"].iloc[-1]))
    # 全A PE 近10年分位(中位数口径, 接口直接给分位字段; 用户拍板用中位数)
    df, err = _call(ak.stock_a_ttm_lyr)
    if df is not None and len(df):
        out["pe_percentile"] = (
            _num(df["quantileInRecent10YearsMiddlePeTtm"].iloc[-1] * 100, 2),
            str(df["date"].iloc[-1]))
    # 上证 PB 近10年分位(月频序列, 自算分位; 无全A口径, 用上证并标注 —— 审计报告§3.3)
    df, err = _call(ak.stock_market_pb_lg, symbol="上证")
    if df is not None and len(df):
        v = pd.to_numeric(df["市净率"], errors="coerce").dropna()
        if len(v) >= 60:
            latest_v = float(v.iloc[-1])
            win = v.tail(min(len(v), 120))  # 近10年(约120个月)
            pct = float((win < latest_v).mean() * 100)
            out["pb_percentile"] = (_num(pct, 2), str(df["日期"].iloc[-1]))
    # 股息率 - 国债10年(利差, 百分点)
    gxl, _ = _call(ak.stock_a_gxl_lg)
    bond, _ = _call(ak.bond_zh_us_rate)
    if gxl is not None and len(gxl) and bond is not None and len(bond):
        div = _num(gxl["股息率"].iloc[-1], 4)
        cn10, _ = _last_valid(bond["中国国债收益率10年"])
        if div is not None and cn10 is not None:
            out["dividend_spread"] = (_num(div - cn10, 4), str(gxl["日期"].iloc[-1]))
    return out


def _fetch_macro() -> dict:
    """宏观维度: M1-M2 剪刀差 / 制造业PMI / PPI同比(含3个月前值判趋势)。月度, 倒序。"""
    ak = _ak()
    out = {}
    df, _ = _call(ak.macro_china_money_supply)
    if df is not None and len(df):
        r = df.iloc[0]  # 倒序, 第0行最新
        m1 = _num(r["货币(M1)-同比增长"])
        m2 = _num(r["货币和准货币(M2)-同比增长"])
        if m1 is not None and m2 is not None:
            out["m1m2_spread"] = (_num(m1 - m2, 2), str(r["月份"]))
    df, _ = _call(ak.macro_china_pmi)
    if df is not None and len(df):
        out["pmi"] = (_num(df.iloc[0]["制造业-指数"], 2), str(df.iloc[0]["月份"]))
    df, _ = _call(ak.macro_china_ppi)
    if df is not None and len(df):
        out["ppi"] = (_num(df.iloc[0]["当月同比增长"], 2), str(df.iloc[0]["月份"]))
        if len(df) > 3:  # 近3个月前值(判"由负转正"趋势)
            out["ppi_3m_ago"] = (_num(df.iloc[3]["当月同比增长"], 2), str(df.iloc[3]["月份"]))
    return out


def _fetch_margin() -> dict:
    """情绪维度-两融: 全市场融资余额的 60 日累计与单周变化(交易所官方, 不需要 Tushare)。"""
    ak = _ak()
    df, _ = _call(ak.stock_margin_account_info)
    out = {}
    if df is not None and len(df) > _MARGIN_LONG:
        bal = pd.to_numeric(df["融资余额"], errors="coerce").dropna()
        if len(bal) > _MARGIN_LONG:
            now = float(bal.iloc[-1])
            out["margin_60d_pct"] = (_num((now / float(bal.iloc[-_MARGIN_LONG]) - 1) * 100, 2),
                                     str(df["日期"].iloc[-1]))
            out["margin_week_pct"] = (_num((now / float(bal.iloc[-_MARGIN_WEEK]) - 1) * 100, 2),
                                      str(df["日期"].iloc[-1]))
    return out


def _fetch_pcr(asof: dt.date) -> dict:
    """情绪维度-期权PCR: 上证50ETF 认沽/认购(成交量口径), 当日可取。

    沪市接口给的是"成交量口径"(认沽/认购×100), 深市是"持仓口径" —— 口径不同,
    统一只用上证50ETF 的成交量口径(审计报告§3.2)。返回 0~1 小数(除以100)。
    期权数据只在交易日有 —— 先用交易日历跳过非交易日, 否则 akshare 对空表会报错刷屏。
    """
    try:
        from utils.trading_calendar import is_trading_day
    except Exception:  # 交易日历不可用则不过滤
        is_trading_day = lambda d: True  # noqa: E731
    for back in range(0, 6):
        day = asof - dt.timedelta(days=back)
        try:
            if not is_trading_day(day):
                continue
        except Exception:
            pass
        df, _ = _call(_ak().option_daily_stats_sse, date=day.strftime("%Y%m%d"))
        if df is None or not len(df):
            continue
        row = df[df["合约标的名称"].astype(str).str.contains("上证50ETF", na=False)]
        if not len(row):
            row = df.iloc[[0]]
        pcr = _num(row.iloc[0]["认沽/认购"], 4)
        if pcr is not None:
            return {"option_pcr": (_num(pcr / 100.0, 4), str(row.iloc[0].get("交易日", day)))}
    return {}


def _fetch_overseas() -> dict:
    """海外维度: 美债10年(T-1) + 美元兑人民币偏离年线(替代美元指数)。"""
    ak = _ak()
    out = {}
    df, _ = _call(ak.bond_zh_us_rate)
    if df is not None and len(df):
        sub = df.dropna(subset=["美国国债收益率10年"])
        if len(sub):  # 美债当日值在收盘前是 nan, 取最后一个非空行 + 它的日期列(时差错位)
            out["us10y"] = (_num(sub["美国国债收益率10年"].iloc[-1], 4),
                            str(sub["日期"].iloc[-1]))
    df, _ = _call(ak.currency_boc_safe)
    if df is not None and len(df) > 250:
        fx = pd.to_numeric(df["美元"], errors="coerce").dropna()  # 100美元兑人民币
        if len(fx) > 250:
            now = float(fx.iloc[-1])
            ma250 = float(fx.tail(250).mean())
            out["usdcny_ma_dev"] = (_num((now - ma250) / ma250 * 100, 2), str(df["日期"].iloc[-1]))
    return out


def _fetch_hs300_series():
    """沪深300 日线 close 序列: 优先新浪指数接口(总是最新 —— 实测发现本地指数缓存
    000300.SH 曾滞后个股缓存 2 个交易日), 失败回退本地缓存。"""
    try:
        df, _ = _call(lambda: _ak().stock_zh_index_daily(symbol="sh000300"), retries=2)
        if df is not None and len(df) > _MA200:
            s = pd.Series(pd.to_numeric(df["close"], errors="coerce").to_numpy(dtype=float),
                          index=pd.to_datetime(df["date"]))
            return s.dropna()
    except Exception as e:  # noqa: BLE001
        _logger.warning("沪深300 新浪指数取数失败, 回退本地缓存: %s", e)
    return mpr._index_series(_HS300_CODE)


def _fetch_local_trend() -> dict:
    """趋势维度: 沪深300偏离200日线(优先联网取最新) / 两市日均成交额 / 涨跌家数20日均值(本地缓存)。"""
    out = {}
    try:
        s = _fetch_hs300_series()
        if s is not None and len(s) > _MA200:
            ma = s.rolling(_MA200).mean().iloc[-1]
            last_idx = s.index[-1]
            asof = str(last_idx.date() if hasattr(last_idx, "date") else last_idx)
            out["hs300_vs_ma200"] = (_num((float(s.iloc[-1]) - float(ma)) / float(ma) * 100, 2), asof)
    except Exception as e:  # noqa: BLE001
        _logger.warning("大盘仪表盘: 沪深300均线计算失败 %s", e)
    try:
        close_df, volume_df, amount_df = mpr._load_matrices(mpr.DEFAULT_BARS + _TREND_WIN + 10)
        if not amount_df.empty:
            amt_yi = amount_df.sum(axis=1) / _WAN_PER_YI  # 全市场当日成交额(亿)
            out["turnover_amt"] = (_num(amt_yi.tail(_TREND_WIN).mean(), 1),
                                   str(amount_df.index[-1].date()))
        if not close_df.empty and not volume_df.empty:
            traded = volume_df > 0
            up = (close_df.diff() > 0) & traded
            up_ratio = (up.sum(axis=1) / traded.sum(axis=1) * 100).dropna()
            if len(up_ratio) >= _TREND_WIN:
                out["breadth_20d"] = (_num(up_ratio.tail(_TREND_WIN).mean(), 2),
                                      str(close_df.index[-1].date()))
    except Exception as e:  # noqa: BLE001
        _logger.warning("大盘仪表盘: 本地趋势指标计算失败 %s", e)
    return out


# ──────────────────────────── 落库 ────────────────────────────

def _read_all(path=None) -> list[dict]:
    p = path or DASHBOARD_PATH
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _upsert_dashboard(records: list[dict], path=None) -> int:
    """按日期 upsert(同一交易日只留最新), 原子写 —— 复用 market_position_runner._upsert。"""
    return mpr._upsert(records, path or DASHBOARD_PATH)


# ──────────────────────────── 快照组装 ────────────────────────────

def _build_snapshot(trade_date: dt.date, asof_type: str, ind_raw: dict,
                    prev_snapshot: dict | None) -> dict:
    """把"指标原始值"打成分, 合成完整快照。ind_raw: {key: (value, asof)}。"""
    prev_dim_avg = None
    if prev_snapshot and prev_snapshot.get("scores", {}).get("dimensions"):
        prev_dim_avg = {k: v.get("avg")
                        for k, v in prev_snapshot["scores"]["dimensions"].items()}
    values = {k: v[0] for k, v in ind_raw.items()}
    scores = market_score.build_scores(values, prev_dim_avg=prev_dim_avg)

    # 事件修正分(本日衰减+过期移除+合计±1)
    ev = market_events.daily_tick(trade_date)
    event_adj = ev["total_adj"]
    final = round(max(0.0, min(10.0, scores["base_total"] + event_adj)), 4)
    band = market_score.label_and_band(final)

    # 特殊指标的"展示值"取自其辅助输入(两融趋势展示60日累计变化; 它的分数由 score_all
    # 用 margin_60d_pct/margin_week_pct 算出, 但展示要给用户看的是 60 日累计变化这个数)
    _DISPLAY_FROM = {"margin_trend": "margin_60d_pct"}
    indicators = {}
    for key in market_score.THRESHOLDS:
        raw = ind_raw.get(_DISPLAY_FROM.get(key, key))
        indicators[key] = {
            "name": market_score.INDICATOR_NAMES[key],
            "value": raw[0] if raw else None,
            "score": scores["indicators"].get(key),
            "asof": raw[1] if raw else None,
            "stale": False,
        }
    return {
        "date": trade_date.isoformat(),
        "asof_type": asof_type,
        "updated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scores": {
            "base_total": scores["base_total"],
            "event_adj": event_adj,
            "final_total": final,
            "check_ok": scores["check_ok"],
            "check_err": scores["check_err"],
            "label": band["label"],
            "position_band": band["position_band"],
            "dimensions": scores["dimensions"],
        },
        "indicators": indicators,
        "events": {"count": ev["count"], "total_adj": event_adj, "list": ev["active"]},
        "weights": market_score.DIMENSION_WEIGHTS,
        "data_asof": _data_asof(indicators),
    }


def _data_asof(indicators: dict) -> dict:
    """汇总各数据域的截止时间(供状态栏分维度标注)。"""
    def _g(keys):
        vs = [indicators[k]["asof"] for k in keys if indicators.get(k, {}).get("asof")]
        return max(vs) if vs else None
    return {
        "a_share": _g(["erp", "pe_percentile", "pb_percentile", "dividend_spread",
                       "hs300_vs_ma200", "turnover_amt", "breadth_20d", "option_pcr"]),
        "margin": (indicators.get("margin_trend") or {}).get("asof"),
        "overseas": _g(["us10y", "usdcny_ma_dev"]),
        "macro": _g(["m1m2_spread", "pmi", "ppi"]),
    }


def _score_delta(now, before):
    """两期得分之差; 任一端缺失(冷启动/历史不足) → None(前端显示"历史数据不足")。"""
    if now is None or before is None:
        return None
    return round(float(now) - float(before), 4)


def _inject_indicator_compare(snap: dict, all_recs: list[dict]) -> None:
    """给每个指标算"较上一交易日 / 较30个交易日前"的得分变化, 就地写进 snap["indicators"]。

    总分的双周期对比也在这里算(scores.compare), 供 TAB1 总览用。
    历史快照不足 30 个交易日 → 该指标/总分的 day_30 为 None(需求§五.3 兜底)。
    """
    cur_date = dt.date.fromisoformat(snap["date"])
    past = sorted((r for r in all_recs if r.get("date") and r["date"] < snap["date"]),
                  key=lambda r: r["date"])
    prev = past[-1] if past else None
    d30 = None
    for r in reversed(past):
        if dt.date.fromisoformat(r["date"]) <= cur_date - dt.timedelta(days=30):
            d30 = r
            break
    for k, ind in snap["indicators"].items():
        prev_ind = (prev or {}).get("indicators", {}).get(k, {})
        d30_ind = (d30 or {}).get("indicators", {}).get(k, {})
        ind["delta_prev"] = _score_delta(ind.get("score"), prev_ind.get("score"))
        ind["delta_d30"] = _score_delta(ind.get("score"), d30_ind.get("score"))
    # 总分双周期对比
    prev_total = (prev or {}).get("scores", {}).get("final_total")
    d30_total = (d30 or {}).get("scores", {}).get("final_total")
    cur_total = snap["scores"]["final_total"]

    def _cmp(before):
        d = _score_delta(cur_total, before)
        if d is None:
            return {"delta": None, "direction": "历史数据不足，暂不提供对比"}
        if abs(d) < 0.05:
            return {"delta": d, "direction": "基本持平"}
        return {"delta": d, "direction": ("转暖" if d > 0 else "转冷")}

    snap["compare"] = {"prev_day": _cmp(prev_total), "day_30": _cmp(d30_total)}


def _apply_stale_from_prev(ind_raw: dict, prev_snapshot: dict | None) -> dict:
    """取数缺失的指标, 沿用上一日快照的旧值并标 stale(需求: 数据暂缺沿用历史得分)。"""
    if not prev_snapshot:
        return ind_raw
    prev_ind = prev_snapshot.get("indicators", {})
    for key in market_score.THRESHOLDS:
        if key not in ind_raw and key in prev_ind and prev_ind[key].get("value") is not None:
            ind_raw[key] = (prev_ind[key]["value"], prev_ind[key].get("asof"))
            ind_raw.setdefault("_stale_keys", set()).add(key)
    return ind_raw


# ──────────────────────────── 对外: 三版刷新 ────────────────────────────

def refresh_close(write: bool = True, asof: dt.date | None = None) -> dict:
    """16:30 A股收盘版: 全量取数 + 打分 + 落快照(两融/美债此时是 T-1, 如实标注)。

    快照按**交易日**记录: 用"最近已收盘交易日"作 date(周末/节假日手动刷新会回退到最近
    交易日并覆盖它, 而不是落一条周末快照 —— 需求第一条第 1 款③ 非交易日沿用最近交易日)。
    """
    if asof is None:
        try:
            asof = mpr._expected_trading_day()
        except Exception:  # 交易日历不可用则退回今天
            asof = dt.date.today()
    with _LOCK:
        prev = latest()
        ind_raw = {}
        ind_raw.update(_fetch_local_trend())      # 本地, 当日
        ind_raw.update(_fetch_valuation())         # ERP/估值/股息利差, 当日
        ind_raw.update(_fetch_macro())             # 宏观(月度)
        ind_raw.update(_fetch_margin())            # 两融(T-1)
        ind_raw.update(_fetch_pcr(asof))           # 期权PCR(当日)
        ind_raw.update(_fetch_overseas())          # 美债(T-1)/汇率
        return _finish(asof, "close", ind_raw, prev, write)


def refresh_fill(write: bool = True) -> dict:
    """次日 08:30 全球补齐版: 用刚到的两融/美债/汇率重算, **覆盖同一交易日快照**。

    只重取"滞后的"(两融/美债/汇率)与宏观, A股其余指标沿用上一日快照的原始值。
    """
    with _LOCK:
        prev = latest()
        if not prev:
            return {"ok": False, "reason": "无上一交易日快照可补齐"}
        trade_date = dt.date.fromisoformat(prev["date"])
        # 沿用上一日全部原始值
        ind_raw = {k: (v["value"], v.get("asof"))
                   for k, v in prev.get("indicators", {}).items()
                   if v.get("value") is not None}
        # 重取滞后项 + 宏观
        ind_raw.update(_fetch_margin())
        ind_raw.update(_fetch_overseas())
        ind_raw.update(_fetch_macro())
        return _finish(trade_date, "fill", ind_raw, prev, write)


def refresh_macro(write: bool = True) -> dict:
    """月度宏观轮询: 仅在发布窗口内真正取数, 否则秒回 no-op(需求第一条第 2 款)。"""
    today = dt.date.today()
    if today.day not in MACRO_WINDOW_DAYS:
        return {"ok": True, "skipped": True, "reason": "不在宏观发布窗口, 沿用现有得分"}
    with _LOCK:
        prev = latest()
        if not prev:
            return {"ok": False, "reason": "无上一交易日快照"}
        trade_date = dt.date.fromisoformat(prev["date"])
        ind_raw = {k: (v["value"], v.get("asof"))
                   for k, v in prev.get("indicators", {}).items()
                   if v.get("value") is not None}
        ind_raw.update(_fetch_macro())  # 只更新宏观三项
        return _finish(trade_date, "macro", ind_raw, prev, write)


def _finish(trade_date: dt.date, asof_type: str, ind_raw: dict,
            prev: dict | None, write: bool) -> dict:
    """组装快照 + 校验 + 落库。"""
    ind_raw = _apply_stale_from_prev(ind_raw, prev)
    stale_keys = ind_raw.pop("_stale_keys", set())
    snap = _build_snapshot(trade_date, asof_type, ind_raw, prev)
    for k in stale_keys:
        if k in snap["indicators"]:
            snap["indicators"][k]["stale"] = True
            snap["indicators"][k]["note"] = "数据暂缺，沿用历史得分"

    # 指标级双周期对比(较上一交易日 / 较30个交易日前, 需求§五.2); 快照不足则 delta=None
    _inject_indicator_compare(snap, _read_all())

    if not snap["scores"]["check_ok"]:  # 总分校验失败 → 重试一次 → 仍失败沿用前日, 不存异常
        _logger.warning("大盘仪表盘总分校验误差 %.3f 超容差, 重试一次", snap["scores"]["check_err"])
        snap = _build_snapshot(trade_date, asof_type, ind_raw, prev)
        for k in stale_keys:
            if k in snap["indicators"]:
                snap["indicators"][k]["stale"] = True
        if not snap["scores"]["check_ok"]:
            _logger.warning("大盘仪表盘总分校验二次仍失败, 沿用前日快照, 不保存异常结果")
            return {"ok": False, "reason": "总分校验异常, 沿用前日数据"}

    if write:
        all_recs = _read_all()
        all_recs = [r for r in all_recs if r.get("date") != snap["date"]]
        all_recs.append(snap)
        all_recs.sort(key=lambda r: r["date"])
        lines = _upsert_dashboard(all_recs)
        snap["_lines"] = lines
    return {"ok": True, "snapshot": snap}


# ──────────────────────────── 对外: 读取 ────────────────────────────

def latest() -> dict | None:
    recs = _read_all()
    return recs[-1] if recs else None


def history(days: int = 30) -> list[dict]:
    recs = _read_all()
    recs.sort(key=lambda r: r["date"])
    return recs[-days:] if days else recs


_TYPE_NAMES = {"close": "A股收盘版", "fill": "全球补齐版", "macro": "宏观专项"}


def erp_series() -> dict:
    """ERP 股债性价比全历史序列 (2005 至今), 供仪表盘「股债性价比长卷」图。

    只读本地文件, 不联网不写盘。jsonl 逐行解析是本函数的传输层职责
    (正典解析器是 market_position_runner._read_erp, 私有; 那里 8 个公开
    接口已满铁律 8 上限, 故读口落在这里 —— 本函数是第 7 个)。
    十年中位/极值/当前分位不重算, 直接带最新 daily 记录 valuation 块的
    现成口径 (与体温表同一来源, 防两套数字)。
    """
    out = {"success": True, "points": [], "median_10y": None,
           "min_10y": None, "max_10y": None, "cur_pct": None, "cur_pct_10y": None}
    try:
        if not mpr.ERP_PATH.exists():
            out["reason"] = "还没有 ERP 缓存 (data/market_position/erp.jsonl 不存在)"
            return out
        pts = []
        with open(mpr.ERP_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                    pts.append([o["date"], round(float(o["erp"]) * 100, 2)])
                except Exception:
                    continue          # 单行坏数据不拖垮整列
        out["points"] = pts
        rec = mpr.latest()
        v = (rec or {}).get("valuation") or {}
        out["median_10y"] = v.get("erp_median_10y_pct")
        out["min_10y"] = v.get("erp_min_10y_pct")
        out["max_10y"] = v.get("erp_max_10y_pct")
        out["cur_pct"] = v.get("erp_pct")
        out["cur_pct_10y"] = v.get("erp_pct_10y")
        if not pts:
            out["reason"] = "ERP 缓存是空的"
        return out
    except Exception as e:            # fail-soft: 图挂了不拖垮页签
        _logger.warning("ERP 序列读取异常: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


def status() -> dict:
    """更新状态(供状态栏 + 倒计时): 上次更新/下次更新时点/各维度数据截止。"""
    snap = latest()
    now = dt.datetime.now()
    nxt, nxt_type = _next_refresh_time(now)
    return {
        "has_data": snap is not None,
        "last_updated": snap.get("updated_at") if snap else None,
        "last_type": snap.get("asof_type") if snap else None,
        "last_type_name": _TYPE_NAMES.get(snap.get("asof_type"), "") if snap else "",
        "data_date": snap.get("date") if snap else None,
        "data_asof": snap.get("data_asof") if snap else None,
        "next_refresh": nxt.strftime("%Y-%m-%d %H:%M"),
        "next_refresh_ts": nxt.timestamp(),
        "next_refresh_type": nxt_type,
    }


def _next_refresh_time(now: dt.datetime) -> tuple[dt.datetime, str]:
    """下次更新(时点, 版本名), 用交易日历跳过周末/节假日(倒计时才准):
    - 今日是交易日且未到 16:30 → 今日 16:30(A股收盘版);
    - 否则 → 下一交易日 08:30(全球补齐版)。
    """
    from utils.trading_calendar import is_trading_day, next_trading_day
    today = now.date()
    try:
        if is_trading_day(today) and now.time() < dt.time(16, 30):
            return dt.datetime.combine(today, dt.time(16, 30)), "A股收盘版"
        nxt = next_trading_day(today)
    except Exception:  # 交易日历不可用时退化为自然日(倒计时退化为参考)
        if now.time() < dt.time(16, 30):
            return dt.datetime.combine(today, dt.time(16, 30)), "A股收盘版"
        nxt = today + dt.timedelta(days=1)
    return dt.datetime.combine(nxt, dt.time(8, 30)), "全球补齐版"
