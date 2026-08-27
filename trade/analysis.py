"""交易分析纯计算域 (2026-08-19 深模块治理, 从 trade/api.py 下沉)。

api.py 路由闭包里埋的计算函数搬到这里, 变模块级纯函数, 可独立单测。
路由只留薄转发 (读快照 + 发命令)。

- calc_drawdowns: 净值序列 → 逐日回撤
- entry_and_closed: trades 表 → 持仓入场/已平仓/买卖汇总三件套
- hold_days: 持仓天数 (T+1 起算)
- name_of: 股票代码 → 简称 (惰性加载)
- rows_to_dicts: sqlite 游标 → dict 列表
- diff_dicts / deep_merge: 配置 dict 的 diff 与递归合并
"""
from __future__ import annotations

import time

from trade.book import DIRECTION_BUY, DIRECTION_SELL


def rows_to_dicts(cursor) -> list[dict]:
    """sqlite 游标 → dict 列表 (列名为键)。"""
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


_name_map: dict[str, str] | None = None


def name_of(code: str) -> str:
    """股票代码 → 简称。惰性加载, 查不到返回空串。

    2026-08-27 修复 (页面简称全丢事件): 加载失败时**不缓存空表**。
    旧逻辑在 TDX 未启动的窗口把 {} 永久缓存, 之后即使 TDX 恢复,
    进程存活期内所有页面简称都是空。现在失败返回空串但不落缓存,
    下次调用重试; 拿到非空表才缓存 (进程级)。"""
    global _name_map
    if _name_map is None:
        try:
            from core.data_fetcher import DataFetcher
            m = DataFetcher.get_name_map() or {}
        except Exception:
            return ""            # 失败不缓存, 下次调用重试
        if not m:
            return ""            # 空表同上 (TDX 刚启动未就绪)
        _name_map = m
    return _name_map.get(code, "")


def diff_dicts(old: dict, new: dict, prefix: str = "") -> list[str]:
    """递归 diff 两个配置 dict, 返回变更字段的 dotted 路径列表 (audit 记录用)。"""
    changed = []
    for key in sorted(set(old) | set(new)):
        path = f"{prefix}{key}"
        if key not in old or key not in new:
            changed.append(path)
        elif isinstance(old[key], dict) and isinstance(new[key], dict):
            changed.extend(diff_dicts(old[key], new[key], path + "."))
        elif old[key] != new[key]:
            changed.append(path)
    return changed


def deep_merge(base: dict, override: dict) -> dict:
    """dict 递归合并, 非 dict 值 (含 levels 列表) 整体替换。"""
    result = dict(base)
    for key, value in override.items():
        if (key in result and isinstance(result[key], dict)
                and isinstance(value, dict)):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def entry_and_closed(store) -> tuple[dict, list, dict]:
    """从 trades 表算 entry_map / closed / summary 三件套。

    - entry_map: 首笔买入时间 (持仓票入场时间用)
    - closed: 已平仓列表 (前 20, 兼容保留)
    - summary: {code: 买卖汇总} —— 平仓票真实盈亏/均价/出场时间

    is_closed = 累计卖出量 > 0 且 >= 累计买入量。
    realized_pnl = Σ 卖方 pnl_amount (账本成本法, 与 deals/飞书同源)。
    """
    entry_map: dict = {}
    summary: dict = {}
    try:
        ro = store.open_readonly()
    except Exception:
        return entry_map, [], summary
    try:
        cur = ro.execute(
            "SELECT code, direction, MIN(ts), MAX(ts), SUM(qty), SUM(amount), "
            "SUM(pnl_amount) FROM trades GROUP BY code, direction")
        per_code: dict = {}
        for code, direction, min_ts, max_ts, qty, amount, pnl_amt in cur.fetchall():
            d = per_code.setdefault(code, {})
            d[direction] = {"min_ts": min_ts, "max_ts": max_ts,
                            "qty": qty or 0, "amount": amount or 0.0,
                            "pnl": pnl_amt or 0.0}
        for code, d in per_code.items():
            buy = d.get(DIRECTION_BUY)
            sell = d.get(DIRECTION_SELL)
            buy_qty = buy["qty"] if buy else 0
            buy_amount = buy["amount"] if buy else 0.0
            sell_qty = sell["qty"] if sell else 0
            sell_amount = sell["amount"] if sell else 0.0
            is_closed = bool(sell and sell_qty > 0 and sell_qty >= buy_qty)
            entry_ts = buy["min_ts"] if buy else None
            if buy:
                entry_map[code] = entry_ts
            summary[code] = {
                "buy_qty": buy_qty,
                "buy_avg": (buy_amount / buy_qty) if buy_qty > 0 else None,
                "sell_qty": sell_qty,
                "sell_avg": (sell_amount / sell_qty) if sell_qty > 0 else None,
                "entry_ts": entry_ts,
                "exit_ts": sell["max_ts"] if sell else None,
                "realized_pnl": (round(sell["pnl"], 2) if is_closed else None),
                "realized_pnl_pct": (round(sell["pnl"] / buy_amount * 100, 2)
                                     if is_closed and buy_amount > 0 and sell["pnl"]
                                     else None),
                "is_closed": is_closed,
            }
        closed = [{
            "code": code, "name": name_of(code),
            "entry_ts": summary[code]["entry_ts"],
            "exit_ts": summary[code]["exit_ts"],
            "qty": summary[code]["buy_qty"],
            "buy_avg": summary[code]["buy_avg"],
            "sell_avg": summary[code]["sell_avg"],
            "realized_pnl": summary[code]["realized_pnl"],
            "realized_pnl_pct": summary[code]["realized_pnl_pct"],
            "hold_days": hold_days(summary[code]["entry_ts"],
                                   summary[code]["exit_ts"]),
        } for code in summary if summary[code]["is_closed"]]
        closed.sort(key=lambda x: x["exit_ts"] or 0, reverse=True)
        return entry_map, closed[:20], summary
    finally:
        ro.close()


_TRADING_DAYS: list | None = None


def _trading_days() -> list:
    """交易日历 (data/kline_cache/calendar, 惰性加载一次; 缺失回退空列表)。"""
    global _TRADING_DAYS
    if _TRADING_DAYS is None:
        try:
            from pathlib import Path

            import pandas as pd
            p = (Path(__file__).resolve().parent.parent
                 / "data" / "kline_cache" / "calendar" / "trading_days.parquet")
            _TRADING_DAYS = sorted(
                pd.read_parquet(p)["date"].astype(str).tolist())
        except Exception:
            _TRADING_DAYS = []
    return _TRADING_DAYS


def hold_days(entry_ts: float | None, end_ts: float | None = None):
    """持仓天数: T+1 起算 (买入日不计, 之后第一个交易日为第 1 天)。

    end_ts 缺省=今天 (在持仓票); 传 exit_ts → 算到出场日。日历滞后时尾部
    按工作日近似; 日历整体缺失时全段工作日近似。无 entry_ts 给 None。
    """
    if not entry_ts:
        return None
    ed = time.strftime("%Y%m%d", time.localtime(entry_ts))
    today = (time.strftime("%Y%m%d", time.localtime(end_ts))
             if end_ts else time.strftime("%Y%m%d"))
    days = _trading_days()
    n = sum(1 for d in days if ed < d <= today)

    import datetime as _dt

    def _weekdays_between(start: str, end: str) -> int:
        cur = _dt.datetime.strptime(start, "%Y%m%d").date()
        end_d = _dt.datetime.strptime(end, "%Y%m%d").date()
        cnt = 0
        while cur < end_d:
            cur += _dt.timedelta(days=1)
            if cur.weekday() < 5:
                cnt += 1
        return cnt

    if days and days[-1] < today:
        n += _weekdays_between(max(days[-1], ed), today)
    elif not days:
        n = _weekdays_between(ed, today)
    return n


def calc_drawdowns(equities: list[float]) -> list[float]:
    """从净值序列计算逐日回撤 (peak = 历史最高净值)。"""
    peak = equities[0] if equities else 1.0
    dds = []
    for eq in equities:
        if eq > peak:
            peak = eq
        dds.append((eq / peak - 1) if peak > 0 else 0.0)
    return dds
