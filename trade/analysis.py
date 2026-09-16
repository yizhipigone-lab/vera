"""交易分析纯计算域 (2026-08-19 深模块治理, 从 trade/api.py 下沉)。

api.py 路由闭包里埋的计算函数搬到这里, 变模块级纯函数, 可独立单测。
路由只留薄转发 (读快照 + 发命令)。

- calc_drawdowns: 净值序列 → 逐日回撤
- entry_and_closed: trades 表 → 持仓入场/已平仓/买卖汇总三件套
- hold_days: 持仓天数 (T+1 起算)
- name_of: 股票代码 → 简称 (惰性加载)
- rows_to_dicts: sqlite 游标 → dict 列表
- diff_dicts / deep_merge: 配置 dict 的 diff 与递归合并
- build_daily_pnl_view: 逐日盈亏日历视图 (2026-09-15 自 analysis_api 路由下沉)
- build_summary_view: 累计 KPI 视图 (2026-09-15 自 analysis_api 路由下沉,
  夏普/索提诺/卡玛/回撤修复改调 backtest.metrics 单一实现 —— 与回测页同口径)

公开函数 9 个 (超铁律 8 一条: 两个视图构建函数与同城计算内聚, 记录在案)。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from trade.book import DIRECTION_BUY, DIRECTION_SELL


_SECONDS_PER_DAY = 86400


def _today_range() -> tuple[float, float]:
    """当日 [00:00, 次日 00:00) epoch 秒。"""
    start = datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    return start, start + _SECONDS_PER_DAY


def _last_trading_day_range(now: float | None = None) -> tuple[float, float]:
    """最近一个交易日 (≤ now) 的 [00:00, 次日 00:00) epoch 秒。

    持仓页"当日盈亏"的锚点 —— 尾盘新买的票要按买入均价算当日盈亏,
    不该把买入前当天的涨幅算成盈利; 而"今天"在周末/节假日是非交易日,
    若仍用墙钟今天, 上一交易日 (如周五) 的尾盘买入会被误判成过夜仓,
    把周五全天涨幅算进"当日盈亏" (2026-08-16 中捷精工/联检科技/蓝箭
    电子事件: 尾盘买入却显示当日盈亏 +1770)。

    最多回退 15 天 (法定长假 + 日历异常兜底); 连续 15 天都判不到
    交易日则退回墙钟今天 (旧行为, 不阻塞页面)。now 缺省取当前。

    2026-09-02: 从 trade/api.py 移入 (单一实现 —— positions 计算体
    下沉 view_calc 后 api 与 view_calc 共用; api.py re-import 兼容)。
    """
    from trade.monitor import is_trading_day_cached
    cur = datetime.fromtimestamp(now) if now is not None else datetime.now()
    d = cur.date()
    for _ in range(15):
        if is_trading_day_cached(d):
            start = datetime(d.year, d.month, d.day).timestamp()
            return start, start + _SECONDS_PER_DAY
        d -= timedelta(days=1)
    return _today_range()


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

    2026-08-27 (159290 事件) 已实现盈亏% 分母修正:
    遗产仓 (买入早于系统上线, 成本来自 QMT 灌仓) 的买入额不在 trades
    表内, 旧口径 pnl ÷ 表内买入额会把分母缩成零头 (159290: -46,579.53
    ÷ 222.6 = -20,925%)。修正为 pnl ÷ 被卖股票的买入成本基数
    (= Σ卖出额 - Σ盈亏, 只取带 pnl 的卖出行)。账本移动加权成本守恒
    (全平仓时 Σ已卖成本 = Σ表内买入额), 全程表内的普通平仓数值不变。
    closed_qty / cost_avg 同理取被平仓口径, 让已平仓行的
    数量/成本/盈亏三个数字讲同一个故事。
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
            "SUM(pnl_amount), "
            "SUM(CASE WHEN pnl_amount <> 0 THEN qty ELSE 0 END), "
            "SUM(CASE WHEN pnl_amount <> 0 THEN amount ELSE 0 END) "
            "FROM trades GROUP BY code, direction")
        per_code: dict = {}
        for (code, direction, min_ts, max_ts, qty, amount, pnl_amt,
             pnl_qty, pnl_turnover) in cur.fetchall():
            d = per_code.setdefault(code, {})
            d[direction] = {"min_ts": min_ts, "max_ts": max_ts,
                            "qty": qty or 0, "amount": amount or 0.0,
                            "pnl": pnl_amt or 0.0,
                            "pnl_qty": pnl_qty or 0,
                            "pnl_turnover": pnl_turnover or 0.0}
        for code, d in per_code.items():
            buy = d.get(DIRECTION_BUY)
            sell = d.get(DIRECTION_SELL)
            buy_qty = buy["qty"] if buy else 0
            buy_amount = buy["amount"] if buy else 0.0
            buy_avg = (buy_amount / buy_qty) if buy_qty > 0 else None
            sell_qty = sell["qty"] if sell else 0
            sell_amount = sell["amount"] if sell else 0.0
            sell_pnl = sell["pnl"] if sell else 0.0
            # 被平仓口径: 带 pnl 的卖出行 (历史无 pnl 行成本不可考, 剔除)
            pnl_qty = sell["pnl_qty"] if sell else 0
            cost_basis = (sell["pnl_turnover"] - sell_pnl
                          if sell and pnl_qty > 0 else 0.0)
            cost_avg = (cost_basis / pnl_qty
                        if pnl_qty > 0 and cost_basis > 0 else None)
            is_closed = bool(sell and sell_qty > 0 and sell_qty >= buy_qty)
            entry_ts = buy["min_ts"] if buy else None
            if buy:
                entry_map[code] = entry_ts
            summary[code] = {
                "buy_qty": buy_qty,
                "buy_avg": buy_avg,
                "sell_qty": sell_qty,
                "sell_avg": (sell_amount / sell_qty) if sell_qty > 0 else None,
                "entry_ts": entry_ts,
                "exit_ts": sell["max_ts"] if sell else None,
                "realized_pnl": (round(sell_pnl, 2) if is_closed else None),
                "realized_pnl_pct": (round(sell_pnl / cost_basis * 100, 2)
                                     if is_closed and cost_basis > 0 and sell_pnl
                                     else None),
                "is_closed": is_closed,
                # 被平仓口径的数量/成本 (无 pnl 可考时回退表内买入口径)
                "closed_qty": pnl_qty if pnl_qty > 0 else buy_qty,
                "cost_avg": cost_avg,
            }
        closed = [{
            "code": code, "name": name_of(code),
            "entry_ts": summary[code]["entry_ts"],
            "exit_ts": summary[code]["exit_ts"],
            "qty": summary[code]["closed_qty"],
            "buy_avg": (summary[code]["cost_avg"]
                        if summary[code]["cost_avg"] is not None
                        else summary[code]["buy_avg"]),
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


def build_daily_pnl_view(rows: list[dict], prev_month_row: dict | None,
                         daily_trades: dict, gapfill_history: list[dict],
                         year: int, month: int) -> dict:
    """逐日盈亏日历视图 — 2026-09-15 自 analysis_api 路由下沉 (纯移动不改行为)。

    输入: 当月快照行 rows (date/total_asset/source)、前月基准行、
    当月成交聚合 daily_trades {date: {"buy": set, "sell": set}}、
    补算留痕 gapfill_history (audit 读回, 新的在前)。
    输出: {日期: {...}, "_month": {...}|None, "_missing": {...}, "_gapfill": {...}|None}。

    口径要点 (沿用原路由注释):
    - 首日基准 = 前月最后一行 (load_prev 单查, 任意长停机都取得到真基准);
      账户首月无前月行 → 首日盈亏如实给 0。
    - source='derived' 的停机日推算行照常参与盈亏链, 但计数标出。
    - 月买卖笔数按整月成交聚合, 快照洞日的成交不丢。
    - 月度汇总 = 期末资产相对基准行一次除法 (与逐日链复利等价, 无连乘舍入)。
    - _missing 只报本月且自愈 (后来补上的日不再标)。
    """
    prefix = f"{year}-{month:02d}"
    result: dict = {}
    win_days = loss_days = derived_days = 0
    for i, r in enumerate(rows):
        prev = rows[i - 1] if i > 0 else prev_month_row
        pnl_amount = 0.0
        pnl_rate = 0.0
        if prev is not None:
            pnl_amount = r["total_asset"] - prev["total_asset"]
            prev_asset = prev["total_asset"]
            pnl_rate = pnl_amount / prev_asset if prev_asset > 0 else 0.0
        if pnl_rate > 0:
            win_days += 1
        elif pnl_rate < 0:
            loss_days += 1
        src = r.get("source") or "eod"
        if src == "derived":
            derived_days += 1
        date_str = r["date"]
        tinfo = daily_trades.get(date_str, {"buy": set(), "sell": set()})
        result[date_str] = {
            "pnl_rate": round(pnl_rate, 6),
            "pnl_amount": round(pnl_amount, 2),
            "buy_count": len(tinfo["buy"]),
            "sell_count": len(tinfo["sell"]),
            "source": src,
        }
    buy_total = sum(len(v["buy"]) for v in daily_trades.values())
    sell_total = sum(len(v["sell"]) for v in daily_trades.values())
    if rows:
        baseline = prev_month_row if prev_month_row is not None else rows[0]
        base_asset = baseline["total_asset"]
        m_amount = rows[-1]["total_asset"] - base_asset
        m_rate = m_amount / base_asset if base_asset > 0 else 0.0
        result["_month"] = {
            "pnl_rate": round(m_rate, 6),
            "pnl_amount": round(m_amount, 2),
            "baseline_date": baseline["date"],
            "baseline_is_prev_month": prev_month_row is not None,
            "trading_days": len(rows),
            "win_days": win_days,
            "loss_days": loss_days,
            "buy_count": buy_total,
            "sell_count": sell_total,
            "derived_days": derived_days,
        }
    else:
        result["_month"] = None
    last = gapfill_history[0] if gapfill_history else None
    missing: dict = {}
    for rep in gapfill_history:
        for run in (rep.get("runs") or []):
            if run.get("status") == "write":
                continue
            for ds in (run.get("dates") or []):
                if not str(ds).startswith(prefix):
                    continue      # _missing 只报本月
                if ds in result:
                    continue      # 后来补上了 → 自愈, 不留陈旧标记
                missing.setdefault(ds, {"reason": run.get("reason", ""),
                                        "residual": run.get("residual", 0.0),
                                        "status": run.get("status", "")})
    result["_missing"] = missing
    result["_gapfill"] = ({"ts": last.get("ts"), "kind": last.get("kind"),
                           "message": last.get("message"),
                           "status": last.get("status"),
                           "dates": last.get("dates") or [],
                           "residual": last.get("residual", 0.0),
                           "reason": last.get("reason", ""),
                           "dry_run": bool(last.get("dry_run"))}
                          if last else None)
    return result


def build_summary_view(rows: list[dict], total_trades: int,
                       buy_map: dict, sell_map: dict,
                       qmt_total: float | None) -> dict:
    """累计 KPI 视图 (分析 Tab 卡片) — 2026-09-15 自 analysis_api 路由下沉。

    🔴 口径收口 (2026-09-15 审计): 夏普/索提诺/卡玛/最大回撤/回撤修复
    改调 backtest.metrics.MetricsCalculator 单一实现 —— 与回测页同口径
    (夏普扣 1.5%/年无风险利率)。此前本视图手写夏普不扣无风险利率,
    分析页与回测页"夏普"不是一个数, 用户对比必然打架。

    qmt_total: QMT 查询到的总资产; None = 查询失败 → 对账告警 (fail-closed,
    查询失败 ≠ 对账通过)。
    """
    import pandas as pd

    from backtest.metrics import MetricsCalculator

    equities = [r["total_asset"] for r in rows]
    initial = equities[0]
    current = equities[-1]
    cum_ret = (current / initial - 1) if initial > 0 else 0.0
    eq = pd.Series(equities, dtype=float)
    # 年化/回撤/夏普/索提诺/卡玛/修复天数: MetricsCalculator 单一实现
    # (ppy=252 日频口径, 与回测页同一把尺)
    mc = MetricsCalculator.compute_all(
        pd.DataFrame({"equity": eq}), pd.DataFrame(),
        initial_capital=initial)
    max_dd = mc.get("max_drawdown", 0.0)
    wins = 0
    total_closed = 0
    profit_sum = 0.0
    loss_sum = 0.0
    for code in sell_map:
        if code in buy_map:
            buy_avg = (buy_map[code]["total_cost"] /
                       buy_map[code]["total_qty"]
                       if buy_map[code]["total_qty"] else 0)
            sell_avg = (sell_map[code]["total_proceeds"] /
                        sell_map[code]["total_qty"]
                        if sell_map[code]["total_qty"] else 0)
            if buy_avg > 0:
                pnl = (sell_avg - buy_avg) / buy_avg
                total_closed += 1
                if pnl > 0:
                    wins += 1
                    profit_sum += pnl
                elif pnl < 0:
                    loss_sum += abs(pnl)
    win_rate = wins / total_closed if total_closed > 0 else 0.0
    avg_profit = profit_sum / wins if wins > 0 else 0.0
    avg_loss = loss_sum / (total_closed - wins) if (total_closed - wins) > 0 else 0.0
    profit_factor = (profit_sum / loss_sum) if loss_sum > 0 else 0.0
    plr = (avg_profit / avg_loss) if avg_loss > 0 else 0.0
    # QMT 对账: 查询失败 (None) 即告警 (审计 Q3 fail-closed);
    # 偏差 >1% 告警 (只告警不回写, 铁律)。
    reconciliation_warning = (
        qmt_total is None
        or abs(qmt_total - current) / max(qmt_total, 1) > 0.01)
    return {
        "start_date": rows[0]["date"],
        "end_date": rows[-1]["date"],
        "initial_capital": round(initial, 2),
        "current_asset": round(current, 2),
        "cumulative_return": round(cum_ret, 6),
        "annualized_return": round(mc.get("annualized_return", 0.0), 6),
        "max_drawdown": round(max_dd, 6),
        "max_dd_recovery_days": mc.get("max_dd_recovery_days", 0),
        "max_dd_recovered": mc.get("max_dd_recovered", True),
        "sharpe_ratio": round(mc.get("sharpe_ratio", 0.0), 4),
        "sortino_ratio": round(mc.get("sortino_ratio", 0.0), 4),
        "calmar_ratio": round(mc.get("calmar_ratio", 0.0), 4),
        "win_rate": round(win_rate, 4),
        "profit_loss_ratio": round(plr, 4),
        "profit_factor": round(profit_factor, 4),
        "total_trades": total_trades,
        "reconciliation_warning": reconciliation_warning,
    }
