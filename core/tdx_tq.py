# -*- coding: utf-8 -*-
"""core/tdx_tq.py — 通达信 TQ-Python 数据通道薄封装 (2026-08-28)。

设计意图:
    把 tdx-quant 技能 (research/tdx_skillhub/skills/tdx-quant_SKILL.md)
    里对 VERA 有价值的只读数据接口收口成一个薄模块: 懒加载 (模块导入
    零 TDX 依赖, 测试/server 起动不需要通达信) + fail-soft (任何异常
    返回 None/空, 研究数据绝不抛) + 纯归一化函数可独立单测。

数据面结论 (2026-08-28 本机实测, 诊断脚本 research/tdx_skillhub/
tq_probe_诊断脚本.py):
    通: 指数→跟踪 ETF 映射 (.SH/.SZ 指数如 000016/399006/399673;
        部分 .CSI 宽基如 000300/000905/000852 服务端返回 none,
        特殊 .CSI 如 950162 反而通 —— 用 .SH/.SZ 代码为准)
    通: get_more_info 88 字段 (市值/涨停价/多日涨幅/换手)
    通: get_market_snapshot 实时五档/内外盘/基金净值
    空 (需客户端下载扩展数据包): get_financial_data 专业财务 FN、
        get_scjy_value 市场统计 SC、get_bkjy_value 板块统计 BK
        (BK 板块代码须带 .SH 后缀)
    口径: get_kzz_info 按**转债代码**查 (传正股代码报错)
    不封装: 全部交易接口 —— QMT 是唯一交易通道 (实盘铁律)。

依赖: core/tdx_path.tdx_plugins_user (TDX_HOME 环境变量可覆盖)。
线程: 模块锁串行化 TQ 调用 (tqcenter 类级连接, 非线程安全)。
"""
from __future__ import annotations

import threading

from core.tdx_path import tdx_plugins_user

__all__ = ["available", "etf_of_index", "norm_etf_list", "norm_snapshot",
           "norm_stock_ext", "snapshot", "stock_ext"]

_LOCK = threading.Lock()
_TQ = None            # 懒加载的 tq 类 (None = 未连)
_INIT_FAILED = False  # 初始化失败标记 (失败不重试到天荒地老: 每进程一次)


# ── 内部: 懒加载连接 ────────────────────────────────────────────

def _tq():
    """返回 tqcenter.tq 类; 未初始化则初始化 (每进程一次)。失败抛异常。"""
    global _TQ, _INIT_FAILED
    if _TQ is not None:
        return _TQ
    if _INIT_FAILED:
        raise RuntimeError("TQ 初始化本进程已失败, 不再重试")
    import os
    import sys
    plugin_dir = tdx_plugins_user()
    if not os.path.isfile(os.path.join(plugin_dir, "tqcenter.py")):
        _INIT_FAILED = True
        raise RuntimeError(f"tqcenter.py 不存在: {plugin_dir}")
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)
    from tqcenter import tq as _cls  # noqa: PLC0415 (懒加载铁律)
    _cls.initialize(__file__)
    _TQ = _cls
    return _TQ


def _f(v):
    """字符串数值 → float; 空串/None/非法 → None。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


# ── 纯归一化函数 (零 TDX 依赖, 可独立单测) ──────────────────────

def norm_etf_list(raw) -> list[dict]:
    """get_trackzs_etf_info 原始返回 → [{code,name,price,prev_close,
    iopv,scale_yi}]; scale_yi = 基金规模(亿元), iopv = 参考净值。"""
    if not raw:
        return []
    out = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        out.append({
            "code": r.get("Code") or "",
            "name": r.get("Name") or "",
            "price": _f(r.get("NowPrice")),
            "prev_close": _f(r.get("PreClose")),
            "iopv": _f(r.get("IOPV")),
            "scale_yi": _f(r.get("Sz")),
        })
    return out


def norm_stock_ext(raw) -> dict:
    """get_more_info 88 字段 → 摘要 dict (涨幅系列%/市值(亿)/涨停跌停价/
    换手%)。raw 为 None/空 → {}。"""
    if not raw:
        return {}
    return {
        "main_business": raw.get("MainBusiness") or "",
        "hq_date": raw.get("HqDate") or "",
        "zaf_pct": _f(raw.get("ZAF")),
        "zaf_yesterday_pct": _f(raw.get("ZAFYesterday")),
        "zaf_pre2d_pct": _f(raw.get("ZAFPre2D")),
        "zaf_pre5d_pct": _f(raw.get("ZAFPre5D")),
        "zaf_pre10d_pct": _f(raw.get("ZAFPre10D")),
        "zaf_pre20d_pct": _f(raw.get("ZAFPre20D")),
        "zaf_pre30d_pct": _f(raw.get("ZAFPre30D")),
        "zaf_pre60d_pct": _f(raw.get("ZAFPre60D")),
        "zsz_yi": _f(raw.get("Zsz")),
        "ltsz_yi": _f(raw.get("Ltsz")),
        "zt_price": _f(raw.get("ZTPrice")),
        "dt_price": _f(raw.get("DTPrice")),
        "hsl_pct": _f(raw.get("fHSL")),
    }


def norm_snapshot(raw) -> dict:
    """get_market_snapshot → {now,open,high,low,prev_close,volume,amount,
    jjjz(基金净值),inside(内盘),outside(外盘)}。raw 为 None/空 → {}。"""
    if not raw:
        return {}
    return {
        "now": _f(raw.get("Now")),
        "open": _f(raw.get("Open")),
        "high": _f(raw.get("Max")),
        "low": _f(raw.get("Min")),
        "prev_close": _f(raw.get("LastClose")),
        "volume": _f(raw.get("Volume")),
        "amount": _f(raw.get("Amount")),
        "jjjz": _f(raw.get("Jjjz")),
        "inside": _f(raw.get("Inside")),
        "outside": _f(raw.get("Outside")),
    }


# ── TQ 薄封装 (fail-soft, 锁串行) ───────────────────────────────

def available() -> bool:
    """TQ 通道是否可用 (目录 + tqcenter.py 存在且能初始化)。纯探测, 不抛。"""
    import os
    try:
        if not os.path.isfile(os.path.join(tdx_plugins_user(),
                                           "tqcenter.py")):
            return False
        _tq()
        return True
    except Exception:
        return False


def etf_of_index(zs_code: str) -> list[dict]:
    """跟踪指定指数的全部 ETF (归一化列表)。指数代码用 .SH/.SZ 后缀
    (如 '399673.SZ' 创业板50; 部分 .CSI 宽基服务端返回空, 见模块 docstring)。
    失败/为空 → []。"""
    try:
        with _LOCK:
            raw = _tq().get_trackzs_etf_info(zs_code=zs_code)
        return norm_etf_list(raw)
    except Exception:
        return []


def stock_ext(code: str) -> dict | None:
    """单只股票扩展信息 (88 字段摘要: 市值/涨幅系列/涨停价/换手)。
    失败 → None (调用方 fail-soft)。"""
    try:
        with _LOCK:
            raw = _tq().get_more_info(stock_code=code)
        return norm_stock_ext(raw) or None
    except Exception:
        return None


def snapshot(code: str) -> dict | None:
    """单只证券实时快照 (现价/五档摘要/基金净值)。失败 → None。"""
    try:
        with _LOCK:
            raw = _tq().get_market_snapshot(stock_code=code)
        return norm_snapshot(raw) or None
    except Exception:
        return None
