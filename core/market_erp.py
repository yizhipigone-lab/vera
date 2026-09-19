# -*- coding: utf-8 -*-
"""core/market_erp.py — ERP 股债性价比 (估值维度)

2026-09-19 架构修订批次 5.1 第二刀: 自 core/market_position_runner.py 端出。
依赖只有共享底座 core/market_position_io (路径/原语) (路径走 mpio.ERP_PATH 调用期取值)。
"""
from __future__ import annotations

import datetime as dt
import json
import os

import numpy as np
import pandas as pd

from core import market_position_io as mpio
from core.market_position_io import (  # 共享底座原语 (批次 5.1)
    _expected_trading_day,
    _f,
    _upsert,
)
from core.market_position import PCT_WINDOW_BARS
from utils.logger import get_logger

_logger = get_logger(__name__)


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

def _erp_fetch_enabled() -> bool:
    """是否允许联网取 ERP。测试与离线环境用 env 关掉 (默认允许)。"""
    return os.environ.get(ERP_FETCH_ENV, "").strip().lower() not in ("1", "true", "yes")

def _read_erp() -> pd.Series:
    """读本地 ERP 缓存 → 按日期升序的 float Series (索引 DatetimeIndex)。

    文件不存在/全是坏行 → 返空 Series (上层标【缺】, 绝不返回编造值)。
    """
    if not mpio.ERP_PATH.exists():
        return pd.Series(dtype=float)
    rows = []
    for line in mpio.ERP_PATH.read_text(encoding="utf-8").splitlines():
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
        n = _upsert(rows, path=mpio.ERP_PATH)
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

