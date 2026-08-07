"""trade/api.py — 交易 HTTP API (FastAPI 路由, 刻意的薄层)。

设计意图:
    读快照 + 发命令, 零业务逻辑。所有命令端点只做入参校验 →
    put EVENT_COMMAND → 立即返回"已受理"; 结果经快照/audit 体现。
    HTTP 线程绝不直接碰交易状态 (唯一写者 = 消费者线程)。
    独立 app 由 trade_main 启动 (默认 8081) —— 不挂进 server.py:
    交易系统是独立单进程, 寄生在回测服务器里会让 EventEngine 的
    归属变成悬案 (计划书 §二: 单进程)。
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from trade.book import is_etf, DIRECTION_BUY, DIRECTION_SELL
from trade.config import (
    trade_config_from_dict,
    trade_config_to_dict,
)

# 审计L12修复: code 入参格式校验 —— 畸形代码在 HTTP 边界就拒掉,
# 不进事件队列 (消费者线程不该为格式垃圾浪费一轮风控检查)
_CODE_PATTERN = r"^\d{6}\.(SH|SZ|BJ)$"


class BuyRequest(BaseModel):
    """人工确认买入。qty 必须整手 (A 股买入 100 股整数倍)。"""
    code: str = Field(pattern=_CODE_PATTERN)
    qty: int = Field(gt=0, multiple_of=100)
    price: float | None = Field(default=None, gt=0)


class SellRequest(BaseModel):
    code: str = Field(pattern=_CODE_PATTERN)
    # 2026-07-30 (用户要求): 可选数量 — 指定则卖 qty (后端校验 ≤ 可用),
    # 留空 = 卖全部可用 (旧行为)
    qty: int | None = Field(default=None, gt=0)


class CancelRequest(BaseModel):
    order_id: str = Field(min_length=1)


def _rows_to_dicts(cursor) -> list[dict]:
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


# 股票名查询 (惰性加载, worker 线程首次查名时加载)
_name_map: dict[str, str] | None = None


def _name_of(code: str) -> str:
    """股票代码 → 简称。惰性加载, 查不到返回空串。"""
    global _name_map
    if _name_map is None:
        try:
            from core.data_fetcher import DataFetcher
            _name_map = DataFetcher.get_name_map() or {}
        except Exception:
            _name_map = {}
    return _name_map.get(code, "")


_SECONDS_PER_DAY = 86400


def _today_range() -> tuple[float, float]:
    """当日 [00:00, 次日 00:00) epoch 秒。"""
    start = datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    return start, start + _SECONDS_PER_DAY


def _day_range(date: str) -> tuple[float, float]:
    """YYYYMMDD → [当日 00:00, 次日 00:00) epoch 秒。格式非法抛 ValueError。"""
    if not re.fullmatch(r"\d{8}", date or ""):
        raise ValueError(f"date 必须是 YYYYMMDD, 实际 {date!r}")
    start = datetime.strptime(date, "%Y%m%d").timestamp()
    return start, start + _SECONDS_PER_DAY


def _diff_dicts(old: dict, new: dict, prefix: str = "") -> list[str]:
    """递归 diff 两个配置 dict, 返回变更字段的 dotted 路径列表
    (audit 记录用 —— "改了什么"必须可追溯)。"""
    changed = []
    for key in sorted(set(old) | set(new)):
        path = f"{prefix}{key}"
        if key not in old or key not in new:
            changed.append(path)
        elif isinstance(old[key], dict) and isinstance(new[key], dict):
            changed.extend(_diff_dicts(old[key], new[key], path + "."))
        elif old[key] != new[key]:
            changed.append(path)
    return changed


def _deep_merge(base: dict, override: dict) -> dict:
    """dict 递归合并, 非 dict 值 (含 levels 列表) 整体替换 —
    与 utils/config_loader._deep_merge 同语义, 但这边界内聚在 api
    薄层自己的合并点 (config_loader 是回测侧配置链, 不跨侧复用)。"""
    result = dict(base)
    for key, value in override.items():
        if (key in result and isinstance(result[key], dict)
                and isinstance(value, dict)):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# ── 2026-07-30: 持仓明细增强辅助 ─────────────────────────────

_NAME_MAP: dict | None = None


def _name_of(code: str) -> str:
    """股票简称 (DataFetcher.get_name_map 惰性加载一次; 失败回退空串 → 前端显示代码)。"""
    global _NAME_MAP
    if _NAME_MAP is None:
        try:
            from core.data_fetcher import DataFetcher
            _NAME_MAP = DataFetcher.get_name_map()
        except Exception:
            _NAME_MAP = {}
    return _NAME_MAP.get(code, "")


def _entry_and_closed(trade_app) -> tuple[dict, list]:
    """从 trades 表算: 各代码首笔买入时间 (entry) + 已平仓列表 (entry/exit/已实现盈亏)。

    已平仓 = 该代码累计买入量 == 累计卖出量 且卖出量 > 0;
    已实现盈亏 = Σ卖出金额 - Σ买入金额 (整周期闭环, 税费未计入 — 与 book 口径一致)。
    """
    entry_map: dict = {}
    closed: list = []
    try:
        ro = trade_app.store.open_readonly()
    except Exception:
        return entry_map, closed
    try:
        cur = ro.execute(
            "SELECT code, direction, MIN(ts), MAX(ts), SUM(qty), SUM(amount) "
            "FROM trades GROUP BY code, direction")
        per_code: dict = {}
        for code, direction, min_ts, max_ts, qty, amount in cur.fetchall():
            d = per_code.setdefault(code, {})
            d[direction] = {"min_ts": min_ts, "max_ts": max_ts,
                            "qty": qty or 0, "amount": amount or 0.0}
        from trade.book import DIRECTION_BUY, DIRECTION_SELL
        for code, d in per_code.items():
            buy = d.get(DIRECTION_BUY)
            sell = d.get(DIRECTION_SELL)
            if buy:
                entry_map[code] = buy["min_ts"]
            if buy and sell and sell["qty"] > 0 and sell["qty"] >= buy["qty"]:
                closed.append({
                    "code": code, "name": _name_of(code),
                    "entry_ts": buy["min_ts"], "exit_ts": sell["max_ts"],
                    "qty": buy["qty"],
                    "realized_pnl": round(sell["amount"] - buy["amount"], 2),
                })
        closed.sort(key=lambda x: x["exit_ts"] or 0, reverse=True)
        return entry_map, closed[:20]
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


def _hold_days(entry_ts: float | None):
    """持仓天数: T+1 起算 (买入日不计, 之后第一个交易日为第 1 天)。
    日历滞后 (trading_days.parquet 未覆盖到今天) 时尾部按工作日近似;
    日历整体缺失时全段工作日近似。无 entry_ts (QMT 恢复的老仓) 给 None。"""
    if not entry_ts:
        return None
    ed = time.strftime("%Y%m%d", time.localtime(entry_ts))
    today = time.strftime("%Y%m%d")
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
        # 尾部从 max(日历尾, 买入日) 起算, 防日历滞后段与买入日前段重复计数
        n += _weekdays_between(max(days[-1], ed), today)   # 尾部工作日近似
    elif not days:
        n = _weekdays_between(ed, today)
    return n


def create_api_app(trade_app, allowed_origins: list[str] | None = None) -> FastAPI:
    """装配路由。trade_app 是 composition root, 路由只读它的
    只读快照 / 调它的 submit_command, 不知道任何内部细节。
    allowed_origins: CORS 放行源 (页面服务器的 origin); None 保持
    默认 8080 两个 —— 页面由回测服务器 serve, 放行的是它的 origin
    (2026-08-01 参数化, 此前硬编码)。"""
    app = FastAPI(title="VERA 实盘交易系统", version="1.0.0")
    # 交易页由回测服务器 (8080) serve, 跨域打这里 —— 只放本机源
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins or
        ["http://127.0.0.1:8080", "http://localhost:8080"],
        allow_methods=["*"], allow_headers=["*"],
    )

    # ── 读快照 ──────────────────────────────────────────────────

    @app.get("/api/trade/status")
    def status():
        return {
            "connected": trade_app.connected,
            "kill_active": trade_app.kill.is_active(),
            "reconciled": trade_app.reconciled,
            "monitor_healthy": trade_app.monitor.is_healthy(),
            # 2026-07-27 裁决②: 时段 + 人话原因, 前端不再笼统红色"降级"
            "session": trade_app.session,
            "monitor_reason": trade_app.monitor_reason,
            "ts": time.time(),
        }

    @app.get("/api/trade/asset")
    def asset():
        """QMT 资产实时查询 (铁律1: QMT 是资产唯一真相源). 失败返 503.

        2026-08-07: 附 prev_day_asset (最近一个非当日的日终资产快照),
        前端当日盈亏基准统一用它 (此前用 localStorage 首拉基准,
        换浏览器/晚开页面即漂移, 与分析板块日历对不上)。"""
        try:
            a = trade_app.gateway.query_asset()
        except Exception as e:
            raise HTTPException(503, f"QMT 资产查询失败: {e}")
        try:
            today = time.strftime("%Y-%m-%d")
            rows = trade_app.store.get_daily_assets(end=today)
            prev = [r for r in rows if r["date"] < today]
            a["prev_day_asset"] = prev[-1]["total_asset"] if prev else None
        except Exception:
            a["prev_day_asset"] = None  # 取不到不阻断, 前端回退旧逻辑
        return a

    @app.get("/api/trade/positions")
    def positions():
        snap = trade_app.book.snapshot()
        # 2026-07-30: 持仓明细增强 — 简称/入场时间/市值/盈亏比例/已平仓。
        # 名称表惰性加载一次 (DataFetcher.get_name_map, 失败回退空 → 前端显示代码)。
        entry_map, closed = _entry_and_closed(trade_app)
        result = []
        for code, p in sorted(snap["positions"].items()):
            quote = trade_app.monitor.quote_of(code)
            last = quote["last"] if quote else None
            # 2026-07-31: 当日涨跌 (昨收来自 monitor quote 缓存的 prev_close,
            # 即网关 tick 的 lastClose)。幅度按价格, 金额按持仓市值口径
            # ((现价-昨收)×数量, 即当日浮动盈亏额)。无价/无昨收 → None。
            prev_close = (quote.get("prev_close") or None) if quote else None
            day_chg_pct = (round((last / prev_close - 1) * 100, 2)
                           if last and prev_close else None)
            day_chg_amt = (round((last - prev_close) * p.volume, 2)
                           if last and prev_close else None)
            etf = is_etf(code)
            result.append({
                "code": code, "name": _name_of(code),
                "volume": p.volume, "can_use": p.can_use,
                "avg_cost": p.avg_cost, "strategy": p.strategy,
                "last": last,
                "day_chg_pct": day_chg_pct,
                "day_chg_amt": day_chg_amt,
                "market_value": round(last * p.volume, 2) if last else None,
                "pnl": round((last - p.avg_cost) * p.volume, 2)
                if last else None,
                "pnl_pct": round((last / p.avg_cost - 1) * 100, 2)
                if last and p.avg_cost > 0 else None,
                "entry_ts": entry_map.get(code),
                "hold_days": _hold_days(entry_map.get(code)),
                "tiers_done": sorted(snap["tiers"].get(code, {}).get(
                    time.strftime("%Y%m%d"), ())),
                # 2026-07-27 裁决③: ETF 明示不纳入自动管理
                "etf": etf,
                "managed": not (etf and trade_app.config.exclude_etf),
            })
        return {"positions": result, "closed": closed, "ts": time.time()}

    @app.get("/api/trade/orders")
    def orders(date: str = Query(default=""),
               limit: int = Query(default=200, ge=1, le=1000),
               offset: int = Query(default=0, ge=0)):
        """委托记录 (只读连接, WAL 下不堵写者)。
        date=YYYYMMDD 查历史 (缺省当日); limit/offset 翻页。
        2026-08-03 修复: 用 updated_ts 过滤 (非 created_ts) ——
        QMT order_id 跨会话复用, 同一 order_id 可能先创建于旧日期、
        今天被新订单更新, created_ts 仍指旧日期会漏掉。"""
        try:
            start, end = _day_range(date) if date else _today_range()
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        ro = trade_app.store.open_readonly()
        try:
            cur = ro.execute(
                "SELECT * FROM orders WHERE "
                "(created_ts >= ? AND created_ts < ?) OR "
                "(updated_ts >= ? AND updated_ts < ?) "
                "ORDER BY updated_ts DESC, order_id DESC LIMIT ? OFFSET ?",
                (start, end, start, end, limit, offset))
            rows = _rows_to_dicts(cur)
            for r in rows:
                r["name"] = _name_of(r["code"])
            return {"orders": rows}
        finally:
            ro.close()

    @app.get("/api/trade/deals")
    def deals(date: str = Query(default=""),
              limit: int = Query(default=200, ge=1, le=5000),
              offset: int = Query(default=0, ge=0)):
        """成交记录 (2026-07-30 交易记录 TAB)。date=YYYYMMDD 查历史
        (缺省当日); limit/offset 翻页。数据源 trades 表
        (成交回调 + sync_reports 双向补记, traded_id 幂等)。
        2026-08-07: 卖出盈亏改读 trades.pnl_amount/pnl_pct 列 (book 成本法,
        与飞书成交卡/_on_trade 同源); 历史行 (上线前) 列=0 → 显示 None
        (不回溯 SQL 现算 —— 旧口径不扣已卖部分会错位)。"""
        try:
            start, end = _day_range(date) if date else _today_range()
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        ro = trade_app.store.open_readonly()
        try:
            cur = ro.execute(
                "SELECT * FROM trades WHERE ts >= ? AND ts < ? "
                "ORDER BY ts DESC, traded_id DESC LIMIT ? OFFSET ?",
                (start, end, limit, offset))
            rows = _rows_to_dicts(cur)
            from trade.book import DIRECTION_SELL
            for r in rows:
                r["name"] = _name_of(r["code"])
                # pnl 改读列 (单一 book 口径, 与飞书日报同源); 历史/买入行 → None
                col_amt = r.get("pnl_amount") or 0
                col_pct = r.get("pnl_pct") or 0
                if r["direction"] == DIRECTION_SELL and col_amt:
                    r["pnl_amount"] = round(float(col_amt), 2)
                    r["pnl_pct"] = round(float(col_pct), 2) if col_pct else None
                else:
                    r["pnl_amount"] = None
                    r["pnl_pct"] = None
            return {"deals": rows}
        finally:
            ro.close()

    @app.get("/api/trade/reconciles")
    def reconciles(limit: int = Query(default=50, ge=1, le=200),
                   offset: int = Query(default=0, ge=0)):
        ro = trade_app.store.open_readonly()
        try:
            cur = ro.execute(
                "SELECT * FROM reconcile_log ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset))
            recs = _rows_to_dicts(cur)
            for r in recs:
                code = r.get("code", "")
                r["name"] = _name_of(code) if code else ""
            return {"reconciles": recs}
        finally:
            ro.close()

    @app.get("/api/trade/audits")
    def audits(limit: int = Query(default=50, ge=1, le=200),
               offset: int = Query(default=0, ge=0)):
        ro = trade_app.store.open_readonly()
        try:
            cur = ro.execute(
                "SELECT * FROM audit ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset))
            recs = _rows_to_dicts(cur)
            for r in recs:
                detail = {}
                try:
                    detail = json.loads(r.get("detail_json", "{}") or "{}")
                except (json.JSONDecodeError, TypeError):
                    pass
                code = detail.get("code", "")
                if code:
                    r["name"] = _name_of(code)
            return {"audits": recs}
        finally:
            ro.close()

    # ── 设置面板 (读配置 + 热更新) ──────────────────────────────

    @app.get("/api/trade/config")
    def get_config():
        """当前生效配置的结构化 JSON (设置面板打开时填充表单用)。"""
        return trade_config_to_dict(trade_app.config)

    @app.put("/api/trade/config")
    def put_config(body: dict = Body(...)):
        """保存配置。语义 = **以当前配置为底深合并 body 再全量校验** —
        设置面板只需发它覆盖的字段 (stop/sizing/监控参数), 账号与
        路径类字段永远不被面板误清 (关键安全语义: 若按"缺省走默认"
        全量替换, 面板没包含 account_id 就会把真账号清成空串)。
        校验复用 load_trade_config 同一份逻辑 (判断不出路由层);
        校验过 → put 命令 → accepted; 失败 → 422 + 字段错误。"""
        base = trade_config_to_dict(trade_app.config)
        merged = _deep_merge(base, body)
        try:
            new_cfg = trade_config_from_dict(merged)
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        changed = _diff_dicts(trade_config_to_dict(trade_app.config),
                              trade_config_to_dict(new_cfg))
        trade_app.submit_command({
            "action": "update_config", "config_obj": new_cfg,
            "changed": changed,
        })
        return {"accepted": True, "changed": changed}

    # ── 发命令 (校验 → put → 已受理; 不在这里做任何交易动作) ────

    @app.post("/api/trade/kill")
    def kill_on():
        trade_app.submit_command({"action": "kill_on", "source": "manual_api"})
        return {"accepted": True}

    @app.post("/api/trade/unkill")
    def kill_off():
        trade_app.submit_command({"action": "kill_off"})
        return {"accepted": True}

    @app.post("/api/trade/buy")
    def buy(req: BuyRequest):
        trade_app.submit_command({
            "action": "manual_buy", "code": req.code,
            "qty": req.qty, "price": req.price,
        })
        return {"accepted": True}

    @app.post("/api/trade/sell")
    def sell(req: SellRequest):
        cmd = {"action": "manual_sell", "code": req.code}
        if req.qty is not None:
            cmd["qty"] = req.qty
        trade_app.submit_command(cmd)
        return {"accepted": True}

    @app.post("/api/trade/cancel")
    def cancel(req: CancelRequest):
        trade_app.submit_command(
            {"action": "cancel", "order_id": req.order_id})
        return {"accepted": True}

    @app.post("/api/trade/ladder")
    def ladder():
        trade_app.submit_command({"action": "place_ladder"})
        return {"accepted": True}

    # ── 尾盘自动买入 (2026-07-27 MVP) ───────────────────────────

    @app.post("/api/trade/auto_buy")
    def auto_buy_now():
        """立即执行一次尾盘选股买入 (人工触发, 任何时段放行 —
        裁决①人工命令不受时段约束; 选股在工作线程跑)。"""
        trade_app.submit_command({"action": "auto_buy",
                                  "source": "manual_api"})
        return {"accepted": True}

    @app.get("/api/trade/auto_buy/last")
    def auto_buy_last():
        """最近一次运行: 时间/来源/选中数/买入数/每票处置。"""
        last = trade_app.auto_buy_last
        return {"last": last, "config": {
            "enabled": trade_app.config.auto_buy.enabled,
            "time": trade_app.config.auto_buy.time,
            "formula_name": trade_app.config.auto_buy.formula_name,
            "amount_per_stock": trade_app.config.auto_buy.amount_per_stock,
            "max_buys_per_day": trade_app.config.auto_buy.max_buys_per_day,
        }}

    # ── 分析 Tab 端点 ──────────────────────────────────────────

    def _daily_asset_rows():
        return trade_app.store.get_daily_assets()

    def _calc_drawdowns(equities: list[float]) -> list[float]:
        """从净值序列计算逐日回撤。"""
        peak = equities[0] if equities else 1.0
        dds = []
        for eq in equities:
            if eq > peak:
                peak = eq
            dds.append((eq / peak - 1) if peak > 0 else 0.0)
        return dds

    @app.get("/api/trade/analysis/equity")
    def analysis_equity():
        """逐日资产净值曲线 (分析 Tab 权益曲线数据源)。"""
        rows = _daily_asset_rows()
        if not rows:
            return {"equity": []}
        equities = [r["total_asset"] for r in rows]
        dds = _calc_drawdowns(equities)
        return {"equity": [
            {"date": rows[i]["date"], "equity": equities[i],
             "drawdown": dds[i]}
            for i in range(len(rows))
        ]}

    @app.get("/api/trade/analysis/daily_pnl")
    def analysis_daily_pnl(year: int = Query(default=0),
                           month: int = Query(default=0)):
        """逐日盈亏 (日历格子数据源)。year/month=0 默认当前年月。"""
        if year <= 0 or month <= 0:
            now = datetime.now()
            year = year if year > 0 else now.year
            month = month if month > 0 else now.month
        prefix = f"{year}-{month:02d}"
        # 拉前月最后一行做首日基准 (避免首日盈亏恒为0)
        rows_all = trade_app.store.get_daily_assets(
            start=f"{year - 1 if month == 1 else year}-{(month - 1) if month > 1 else 12:02d}-25",
            end=f"{prefix}-31")
        # 只保留当月行; 但前月最后一行用于 i>0 计算
        first_month_idx = 0
        for idx, r in enumerate(rows_all):
            if r["date"].startswith(prefix):
                first_month_idx = idx
                break
        else:
            first_month_idx = len(rows_all)
        rows = rows_all[first_month_idx:]
        result: dict = {}
        # 批量加载当月全部成交 (单次查询, 避免 N+1)
        month_start = datetime(year, month, 1).timestamp()
        import calendar as _cal
        days = _cal.monthrange(year, month)[1]
        month_end = datetime(year, month, days, 23, 59, 59).timestamp()
        ro = trade_app.store.open_readonly()
        try:
            all_trades = ro.execute(
                "SELECT ts, direction, traded_id FROM trades "
                "WHERE ts >= ? AND ts < ? ORDER BY ts ASC",
                (month_start, month_end + 1)).fetchall()
            # 按日期聚合
            from collections import defaultdict
            daily_trades = defaultdict(lambda: {"buy": set(), "sell": set()})
            for ts_val, direction, traded_id in all_trades:
                d = datetime.fromtimestamp(ts_val).strftime("%Y-%m-%d")
                if direction == DIRECTION_BUY:
                    daily_trades[d]["buy"].add(traded_id)
                elif direction == DIRECTION_SELL:
                    daily_trades[d]["sell"].add(traded_id)
        finally:
            ro.close()
        for i, r in enumerate(rows):
            pnl_amount = 0.0
            pnl_rate = 0.0
            if i > 0:
                pnl_amount = r["total_asset"] - rows[i - 1]["total_asset"]
                prev = rows[i - 1]["total_asset"]
                pnl_rate = pnl_amount / prev if prev > 0 else 0.0
            date_str = r["date"]
            tinfo = daily_trades.get(date_str, {"buy": set(), "sell": set()})
            result[date_str] = {
                "pnl_rate": round(pnl_rate, 6),
                "pnl_amount": round(pnl_amount, 2),
                "buy_count": len(tinfo["buy"]),
                "sell_count": len(tinfo["sell"]),
            }
        return result

    @app.get("/api/trade/analysis/daily_report")
    def analysis_daily_report(date: str = Query(default="")):
        """盘后日报全明细 (2026-08-07 日历点击回看)。date=YYYY-MM-DD;
        缺省取最近一份。源 daily_report 表 (发飞书时同时落盘, 与卡片同源)。
        无记录 → {"report": null}。"""
        if date:
            try:
                datetime.strptime(date, "%Y-%m-%d")
            except ValueError as e:
                raise HTTPException(422, f"date 需为 YYYY-MM-DD: {e}") from e
            rep = trade_app.store.load_daily_report(date)
        else:
            rep = trade_app.store.load_latest_daily_report()
        return {"report": rep}

    @app.get("/api/trade/analysis/summary")
    def analysis_summary():
        """累计 KPI (分析 Tab 卡片数据源)。"""
        rows = _daily_asset_rows()
        if not rows:
            return {"initial_capital": 0, "current_asset": 0,
                    "total_trades": 0, "start_date": "", "end_date": ""}
        equities = [r["total_asset"] for r in rows]
        initial = equities[0]
        current = equities[-1]
        cum_ret = (current / initial - 1) if initial > 0 else 0.0
        # 年化
        start_d = datetime.strptime(rows[0]["date"], "%Y-%m-%d")
        end_d = datetime.strptime(rows[-1]["date"], "%Y-%m-%d")
        years = max(0.01, (end_d - start_d).days / 365.25)
        ann_ret = ((1 + cum_ret) ** (1 / years) - 1) if cum_ret > -1 else 0.0
        # 回撤
        dds = _calc_drawdowns(equities)
        max_dd = min(dds) if dds else 0.0
        # 回撤修复天数: 从谷底到创新高的交易日数。未修复则计到序列末尾
        max_dd_idx = dds.index(max_dd) if dds else 0
        recovery_days = 0
        recovered = False
        prev_peak = equities[max_dd_idx] / (1 + max_dd) if max_dd < -0.0001 else equities[max_dd_idx]
        for i in range(max_dd_idx + 1, len(equities)):
            recovery_days += 1
            if equities[i] >= prev_peak - 0.01:  # 容差1分钱
                recovered = True
                break
        # 日收益序列
        daily_rets = [(equities[i] / equities[i - 1] - 1)
                      for i in range(1, len(equities)) if equities[i - 1] > 0]
        mean_ret = sum(daily_rets) / len(daily_rets) if daily_rets else 0.0
        std_ret = (sum((r - mean_ret) ** 2 for r in daily_rets) / len(daily_rets)) ** 0.5 if daily_rets else 0.0
        sharpe = (mean_ret / std_ret * math.sqrt(252)) if std_ret > 0 else 0.0
        # Sortino
        down_rets = [r for r in daily_rets if r < 0]
        down_std = (sum((r - 0) ** 2 for r in down_rets) / len(down_rets)) ** 0.5 if down_rets else 0.0
        sortino = (mean_ret / down_std * math.sqrt(252)) if down_std > 0 else 0.0
        calmar = (ann_ret / abs(max_dd)) if max_dd < 0 else 0.0
        # 成交统计 (SQL GROUP BY 聚合, 全表扫描但避免 Python 侧全量 fetch)
        all_trades_cur = trade_app.store.open_readonly()
        try:
            total_trades = all_trades_cur.execute(
                "SELECT COUNT(*) FROM trades").fetchone()[0]
            # 按 code+direction 汇总均价
            agg_rows = all_trades_cur.execute(
                "SELECT code, direction, SUM(price*qty), SUM(qty) "
                "FROM trades GROUP BY code, direction").fetchall()
            buy_map: dict = {}
            sell_map: dict = {}
            for code, direction, total_amt, total_qty in agg_rows:
                if direction == DIRECTION_BUY:
                    buy_map[code] = {"total_cost": total_amt, "total_qty": total_qty}
                else:
                    sell_map[code] = {"total_proceeds": total_amt, "total_qty": total_qty}
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
        finally:
            all_trades_cur.close()
        # QMT 对账
        reconciliation_warning = False
        try:
            qmt_asset = trade_app.gateway.query_asset()
            qmt_total = qmt_asset.get("totalAsset", current)
            if abs(qmt_total - current) / max(qmt_total, 1) > 0.01:
                reconciliation_warning = True
        except Exception:
            pass
        return {
            "start_date": rows[0]["date"],
            "end_date": rows[-1]["date"],
            "initial_capital": round(initial, 2),
            "current_asset": round(current, 2),
            "cumulative_return": round(cum_ret, 6),
            "annualized_return": round(ann_ret, 6),
            "max_drawdown": round(max_dd, 6),
            "max_dd_recovery_days": recovery_days,
            "max_dd_recovered": recovered,
            "sharpe_ratio": round(sharpe, 4),
            "sortino_ratio": round(sortino, 4),
            "calmar_ratio": round(calmar, 4),
            "win_rate": round(win_rate, 4),
            "profit_loss_ratio": round(plr, 4),
            "profit_factor": round(profit_factor, 4),
            "total_trades": total_trades,
            "reconciliation_warning": reconciliation_warning,
        }

    return app
