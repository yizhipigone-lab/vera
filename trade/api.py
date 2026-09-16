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
import re
import time
from datetime import datetime

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from trade.analysis import (  # 2026-08-19 深模块治理: 计算逻辑下沉
    _last_trading_day_range,  # 2026-09-02: 单一实现移 analysis (兼容 re-import, 测试按历史双 patch)  # noqa: F401
    deep_merge,
    diff_dicts,
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


class ArmRequest(BaseModel):
    """武装打字确认 (方案设计书 §5.5: 武装有仪式, 解除零摩擦)。"""
    confirm: str = Field(min_length=1)

class ChannelRequest(BaseModel):
    """切换目标通道。"""
    channel: str = Field(min_length=1)


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
        st = trade_app.channel_mgr_state
        return {
            "connected": trade_app.connected,
            "kill_active": trade_app.kill.is_active(),
            "reconciled": trade_app.reconciled,
            "monitor_healthy": trade_app.monitor.is_healthy(),
            # 2026-07-27 裁决②: 时段 + 人话原因, 前端不再笼统红色"降级"
            "session": trade_app.session,
            "monitor_reason": trade_app.monitor_reason,
            # 2026-09-07 T4: 通道/武装状态 (ths 通道才有 mgr; 其余 None)
            "channel": trade_app.channel,
            "armed_intent": (st or {}).get("armed_intent"),
            "armed_effective": (st or {}).get("armed_effective"),
            "last_probe": (st or {}).get("last_probe"),
            "channel_down": bool((st or {}).get("channel_down", False)),
            "ts": time.time(),
        }

    @app.get("/api/trade/channel")
    def channel_info():
        # 静态通道信息 + 运行时 mgr 状态 (2026-09-07 T4)
        return {
            "channel": trade_app.channel,
            "available": ["qmt", "ths", "fake"],
            "mgr": trade_app.channel_mgr_state,
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

    # ── 通道管理 (2026-09-07 T4, 方案设计书 §5.5) ──────────
    @app.post("/api/trade/channel/probe")
    def channel_probe():
        if trade_app.channel != "ths":
            raise HTTPException(409, "仅同花顺通道需要探针 (只读校验)")
        trade_app.submit_command({"action": "ths_probe"})
        return {"accepted": True}

    @app.post("/api/trade/channel/arm")
    def channel_arm(req: ArmRequest):
        if req.confirm != "确认实盘":
            raise HTTPException(422, "确认串必须是「确认实盘」")
        if trade_app.channel != "ths":
            raise HTTPException(409, "仅同花顺通道有武装概念")
        trade_app.submit_command({"action": "ths_arm"})
        return {"accepted": True}

    @app.post("/api/trade/channel/disarm")
    def channel_disarm():
        if trade_app.channel != "ths":
            raise HTTPException(409, "仅同花顺通道有武装概念")
        trade_app.submit_command({"action": "ths_disarm"})
        return {"accepted": True}

    @app.post("/api/trade/channel/switch")
    def channel_switch(req: ChannelRequest):
        # 盘中禁切 (仿 reload_view 时段门禁): 切换 = 换枪, 休市才许
        from trade.monitor import trading_session
        sess = trading_session()
        if sess in ("continuous", "auction"):
            raise HTTPException(403, f"盘中 ({sess}) 禁止切通道, 请在盘前/午休/收盘后操作")
        if req.channel not in ("qmt", "ths", "fake"):
            raise HTTPException(422, "channel 只能是 qmt/ths/fake")
        base = trade_config_to_dict(trade_app.config)
        if base.get("channel") == req.channel:
            return {"accepted": True, "restart_required": True, "changed": []}
        merged = deep_merge(base, {"channel": req.channel})
        new_cfg = trade_config_from_dict(merged)
        changed = diff_dicts(trade_config_to_dict(trade_app.config),
                              trade_config_to_dict(new_cfg))
        # 通道切换走配置热更同路径 (channel 属 restart_required_for)。
        # 重启后由 channel_mgr 按新通道意图 + 启动探针重新武装 ——
        # 切通道即切武装依据, 换枪必须重验保险 (方案设计书 §5.5)。
        trade_app.submit_command({
            "action": "update_config", "config_obj": new_cfg,
            "changed": changed + ["channel(需重启生效)"]})
        return {"accepted": True, "restart_required": True, "changed": changed}

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
            "signal_day": list(trade_app.config.rotation.signal_day),
            "execute_time": trade_app.config.rotation.execute_time,
        }}

    # ── 分析 Tab 端点 ──────────────────────────────────────────

    # 分析域 5 端点已抽到 trade/analysis_api.py (治理III W4-d) ——
    # 路由由 include_router 注入, api.py 只留交易操作/持仓/配置
    from trade.analysis_api import analysis_router
    app.include_router(analysis_router(trade_app))
    return app
