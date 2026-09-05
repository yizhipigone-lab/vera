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
from datetime import datetime, timedelta

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from trade.book import is_etf, DIRECTION_BUY, DIRECTION_SELL
from trade.analysis import (  # 2026-08-19 深模块治理: 计算逻辑下沉
    _last_trading_day_range,  # 2026-09-02: 单一实现移 analysis (兼容 re-import)
    calc_drawdowns,
    deep_merge,
    diff_dicts,
    entry_and_closed,
    hold_days,
    name_of,
    rows_to_dicts,
)
from trade.config import (
    trade_config_from_dict,
    trade_config_to_dict,
)
from utils.logger import get_logger

logger = get_logger(__name__)

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
            rows = trade_app.store.daily_asset.get(end=today)
            prev = [r for r in rows if r["date"] < today]
            a["prev_day_asset"] = prev[-1]["total_asset"] if prev else None
        except Exception:
            a["prev_day_asset"] = None  # 取不到不阻断, 前端回退旧逻辑
        return a

    @app.get("/api/trade/positions")
    def positions():
        # 2026-09-02: 计算体下沉 trade/view_calc.py (热加载层)。函数内
        # import 每次请求从 sys.modules 取最新模块 —— POST /api/trade/
        # reload_view 重载后即刻生效, 改展示口径不再重启 trade_main。
        from trade.view_calc import build_positions_view
        return build_positions_view(trade_app)

    @app.post("/api/trade/reload_view")
    def reload_view():
        """非交易时段热重载 trade.view_calc (持仓页展示计算层)。

        流程: 时段校验 → 源码语法预检 → importlib.reload → 烟测调用
        → audit 留痕。放行时段: 盘前(<09:15)/午休(11:30-13:00)/
        收盘后(≥15:00)/非交易日 (用户 2026-09-02 拍板); 开盘竞价与
        连续竞价一律 403 —— 展示层虽无交易副作用, 但"盘中换代码"的
        操作习惯必须杜绝。

        边界: 烟测失败时模块已是新版 (半坏状态, positions 端点会跟着
        抛错) —— 修复文件后再次 reload, 或重启 trade_main 兜底。
        语法预检挡住最常见的坏文件 (SyntaxError 不 reload, 进程内
        保持旧版可用)。只允许重载 view_calc 这一个模块 —— 交易核心
        (book/executor/monitor/risk/gateway/store) 永不热加载。
        """
        from trade.monitor import trading_session
        sess = trading_session()
        if sess in ("continuous", "auction"):
            trade_app.store.write_audit(
                "view_reload_rejected", f"盘中拒绝热加载 (session={sess})")
            raise HTTPException(
                403, f"盘中 ({sess}) 禁止热加载, 请在盘前/午休/收盘后操作")
        import trade.view_calc as vc
        try:
            src = open(vc.__file__, encoding="utf-8").read()
            compile(src, vc.__file__, "exec")
        except Exception as e:
            trade_app.store.write_audit(
                "view_reload_syntax_error", f"语法预检失败, 未重载: {e}")
            raise HTTPException(
                422, f"view_calc 语法错误, 未重载 (进程内保持旧版): {e}")
        import importlib
        try:
            importlib.reload(vc)
            vc.build_positions_view(trade_app)   # 烟测: 新代码能算出视图
        except Exception as e:
            trade_app.store.write_audit(
                "view_reload_failed", f"重载后烟测失败: {e}")
            raise HTTPException(
                500, f"重载成功但烟测失败, 请修复文件后再次 reload "
                     f"或重启 trade_main 兜底: {e}")
        trade_app.store.write_audit(
            "view_reload", f"view_calc 重载成功 (session={sess})")
        return {"ok": True, "module": "trade.view_calc", "session": sess}

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
            rows = rows_to_dicts(cur)
            for r in rows:
                r["name"] = name_of(r["code"])
            return {"orders": rows}
        finally:
            ro.close()

    @app.get("/api/trade/deals")
    def deals(date: str = Query(default=""),
              start: str = Query(default=""),
              end: str = Query(default=""),
              limit: int = Query(default=200, ge=1, le=5000),
              offset: int = Query(default=0, ge=0)):
        """成交记录 (2026-07-30 交易记录 TAB)。date=YYYYMMDD 查历史
        (缺省当日); limit/offset 翻页。数据源 trades 表
        (成交回调 + sync_reports 双向补记, traded_id 幂等)。
        2026-08-07: 卖出盈亏改读 trades.pnl_amount/pnl_pct 列 (book 成本法,
        与飞书成交卡/_on_trade 同源); 历史行 (上线前) 列=0 → 显示 None
        (不回溯 SQL 现算 —— 旧口径不扣已卖部分会错位)。
        2026-08-13: 新增 start/end=YYYYMMDD 闭区间范围查询 (分析页盈亏分布
        需要全量历史; 此前缺省只给当日, 分布图永远凑不出买卖对)。"""
        try:
            if start and end:
                start_ts = _day_range(start)[0]
                end_ts = _day_range(end)[1]  # end 当日 inclusive
            elif date:
                start_ts, end_ts = _day_range(date)
            else:
                start_ts, end_ts = _today_range()
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        ro = trade_app.store.open_readonly()
        try:
            cur = ro.execute(
                "SELECT * FROM trades WHERE ts >= ? AND ts < ? "
                "ORDER BY ts DESC, traded_id DESC LIMIT ? OFFSET ?",
                (start_ts, end_ts, limit, offset))
            rows = rows_to_dicts(cur)
            from trade.book import DIRECTION_SELL
            for r in rows:
                r["name"] = name_of(r["code"])
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
            recs = rows_to_dicts(cur)
            for r in recs:
                code = r.get("code", "")
                r["name"] = name_of(code) if code else ""
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
            recs = rows_to_dicts(cur)
            for r in recs:
                detail = {}
                try:
                    detail = json.loads(r.get("detail_json", "{}") or "{}")
                except (json.JSONDecodeError, TypeError):
                    pass
                code = detail.get("code", "")
                if code:
                    r["name"] = name_of(code)
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
        merged = deep_merge(base, body)
        try:
            new_cfg = trade_config_from_dict(merged)
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        changed = diff_dicts(trade_config_to_dict(trade_app.config),
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

    # ── ETF 轮动 (2026-08-14) ──────────────────────────────────

    @app.post("/api/trade/rotation/run")
    def rotation_run():
        """立即执行一次 ETF 轮动 (人工触发, 算信号+调仓, 任何时段放行)。"""
        trade_app.submit_command({"action": "rotation_run",
                                  "source": "manual_api"})
        return {"accepted": True}

    @app.get("/api/trade/rotation/last")
    def rotation_last():
        """最近一次轮动: 时间/来源/信号明细。"""
        last = trade_app.rotation_last
        return {"last": last, "config": {
            "enabled": trade_app.config.rotation.enabled,
            "etf_ratio": trade_app.config.rotation.etf_ratio,
            "cyb_etf": trade_app.config.rotation.cyb_etf,
            "risk_etf2": trade_app.config.rotation.risk_etf2,
            "gold_etf": trade_app.config.rotation.gold_etf,
            "hedge_etf2": trade_app.config.rotation.hedge_etf2,
            "hedge_ratio": trade_app.config.rotation.hedge_ratio,
            "momentum_window": trade_app.config.rotation.momentum_window,
            "trailing_stop_pct": trade_app.config.rotation.trailing_stop_pct,
            "signal_day": trade_app.config.rotation.signal_day,
            "execute_time": trade_app.config.rotation.execute_time,
        }}

    # ── 分析 Tab 端点 ──────────────────────────────────────────

    def _daily_asset_rows():
        return trade_app.store.daily_asset.get()

    @app.get("/api/trade/analysis/equity")
    def analysis_equity():
        """逐日资产净值曲线 (分析 Tab 权益曲线数据源)。
        2026-08-13 Phase 2: 追加 "rolling" key (滚动指标, 形状同回测侧
        rolling_metrics); 日级净值直接可算, 失败/空数据不加 key,
        既有 "equity" 字段不动。"""
        rows = _daily_asset_rows()
        if not rows:
            return {"equity": []}
        equities = [r["total_asset"] for r in rows]
        dds = calc_drawdowns(equities)
        resp = {"equity": [
            {"date": rows[i]["date"], "equity": equities[i],
             "drawdown": dds[i]}
            for i in range(len(rows))
        ]}
        try:
            import pandas as pd
            from backtest.metrics import MetricsCalculator
            eq_df = pd.DataFrame({"date": [r["date"] for r in rows],
                                  "equity": equities})
            resp["rolling"] = MetricsCalculator.rolling_metrics(eq_df)
        except Exception:
            logger.warning("rolling 计算失败, 跳过该 key", exc_info=True)
        return resp

    @app.get("/api/trade/analysis/attribution")
    def analysis_attribution():
        """业绩归因 (2026-08-13 Phase 2): 全部历史卖出按行业/个股聚合。

        归一口径: 只取 direction==SELL 且 pnl_amount 非空的行
        (pnl 列=0 的历史行/盈亏恰为 0 的卖出视为无 pnl, 同 /deals 先例),
        无 pnl 的卖出笔数计入 meta.skipped_no_pnl。
        sector index 首次构建 ~17s, sync def 由 FastAPI 线程池跑, 不阻塞事件循环。
        """
        ro = trade_app.store.open_readonly()
        try:
            cur = ro.execute("SELECT code, direction, pnl_amount FROM trades")
            rows = rows_to_dicts(cur)
        finally:
            ro.close()
        items = []
        skipped = 0
        for r in rows:
            if r.get("direction") != DIRECTION_SELL:
                continue
            pnl = r.get("pnl_amount")
            if pnl is None or float(pnl) == 0.0:
                skipped += 1
                continue
            items.append({"code": str(r.get("code", "") or ""), "pnl": float(pnl)})
        try:
            from policy_kb.build_sector_index import build_stock_sector_index
            sector_index = build_stock_sector_index()
        except Exception:
            logger.warning("build_stock_sector_index 失败, 全部归入未标", exc_info=True)
            sector_index = {}
        sector_names = {}
        stock_names = {}
        try:
            from core.data_fetcher import DataFetcher
            sector_names = {s.get("code", ""): s.get("name", "")
                            for s in DataFetcher.get_sector_list()}
            stock_names = DataFetcher.get_name_map() or {}
        except Exception:
            logger.warning("行业名/股票名加载失败, 名称兜底", exc_info=True)
        from backtest.attribution import attribute_returns
        result = attribute_returns(items, sector_index,
                                   sector_names=sector_names,
                                   stock_names=stock_names)
        result["meta"] = {"skipped_no_pnl": skipped}
        return result

    @app.get("/api/trade/analysis/daily_pnl")
    def analysis_daily_pnl(year: int = Query(default=0, ge=0, le=3000),
                           month: int = Query(default=0, ge=0, le=12)):
        """逐日盈亏 (日历格子数据源)。year/month=0 默认当前年月。

        2026-09-04: 响应追加 "_month" 月度汇总 (日历月收益行数据源),
        键以 "_" 开头与日期键 (YYYY-MM-DD) 无碰撞, 按日期键查找的老
        消费方自动忽略。同时修复首日基准 bug: 旧代码把前月基准行切掉、
        且仅 i>0 才算盈亏, 每月首个交易日恒显 0.00%。
        同日对手审计+复验修复: ① 基准行改 store.load_prev 单查 (无窗口
        概念, 前月整月停机也取得到真基准); ② 月买卖笔数按整月成交聚合
        (快照洞日成交不丢); ③ year/month 上界收紧 (3000/12, 手输越界
        原为 500, 现 422; 0=缺省当月哨兵保留; le=3000 取 Windows
        mktime64 平台上限)。"""
        if year <= 0 or month <= 0:
            now = datetime.now()
            year = year if year > 0 else now.year
            month = month if month > 0 else now.month
        prefix = f"{year}-{month:02d}"
        # 当月行 + 基准行分开取 (2026-09-04 复验遗留①根治):
        # 基准 = store.load_prev 单查「当月1号前最后一行」——无窗口概念,
        # 任意长度停机 (哪怕前月整月黑) 都能取到真实基准, 不再误判
        # "账户首月"; 且自带 total_asset>0 过滤, 零资产脏行不做基准。
        # 旧窗口法 (起点放多早, 总有更长的停机逃出去) 至此废弃。
        rows = trade_app.store.daily_asset.get(
            start=f"{prefix}-01", end=f"{prefix}-31")
        prev_month_row = trade_app.store.daily_asset.load_prev(f"{prefix}-01")
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
        win_days = loss_days = 0
        for i, r in enumerate(rows):
            # 首日基准 = 前月最后一行 (2026-09-04 修复: 旧代码 i==0 恒 0);
            # 账户首月无前月行 → prev=None, 首日盈亏不可算, 如实给 0
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
            date_str = r["date"]
            tinfo = daily_trades.get(date_str, {"buy": set(), "sell": set()})
            result[date_str] = {
                "pnl_rate": round(pnl_rate, 6),
                "pnl_amount": round(pnl_amount, 2),
                "buy_count": len(tinfo["buy"]),
                "sell_count": len(tinfo["sell"]),
            }
        # 月买卖笔数按整月成交聚合, 不随快照行累加 —— 快照洞日 (停机缺
        # 资产行) 的成交同样计入月汇总 (对手审计 2026-09-04 修复)
        buy_total = sum(len(v["buy"]) for v in daily_trades.values())
        sell_total = sum(len(v["sell"]) for v in daily_trades.values())
        # 月度汇总: 期末资产相对基准行资产的总涨跌 (与逐日链复利合成等价,
        # 但用原值一次除法, 无连乘舍入)。基准 = 前月最后交易日资产;
        # 账户首月无前月行 → 回退当月首行 (汇总自次个交易日起算,
        # baseline_is_prev_month=False 供前端区分提示)。空月 → None。
        # trading_days 为快照口径: 缺快照日的涨跌并入下一有行日。
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
            }
        else:
            result["_month"] = None
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
            rep = trade_app.store.daily_report.load(date)
        else:
            rep = trade_app.store.daily_report.load_latest()
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
        dds = calc_drawdowns(equities)
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
            # 审计 Q3 (2026-08-19): 查询失败 ≠ 对账通过 —— 补日志并置告警。
            # fail-closed: 对账结果未知即提示人工核对, 不可静默吞掉
            # (原 pass 让"没查成"与"对账通过"无法区分, 违反对账只告警铁律)。
            logger.exception("QMT 对账资产查询失败, 视为对账告警 (资产数字未与 QMT 核对)")
            reconciliation_warning = True
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
