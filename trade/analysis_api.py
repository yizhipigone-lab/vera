"""trade/analysis_api.py — 分析域路由 (治理III W4-d 自 trade/api.py 抽出, 2026-09-05)。

trade/api.py (729 行) 曾混装交易操作/持仓视图/分析/配置四域。分析 5 端点
(/api/trade/analysis/*) 与交易操作无共享状态, 只读 trade_app 快照 → 按
「一域一文件」范式 (lab_api/analysis 先例) 抽成独立 APIRouter, 交易 API
只保留交易操作/持仓/配置, 改图不再碰下单代码。

纪律: 纯移动不改行为 —— 端点体与迁移前逐字节一致 (仅把闭包私有助手
_daily_asset_rows 内联为 router 闭包)。数学仍下沉在 trade/analysis 与
view_calc; 本文件只做"取数 + 渲染"。
"""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from trade.analysis import (build_daily_pnl_view, build_summary_view,
                            calc_drawdowns, rows_to_dicts)
from trade.book import DIRECTION_BUY, DIRECTION_SELL
from utils.logger import get_logger

logger = get_logger(__name__)


def analysis_router(trade_app) -> APIRouter:
    """装配分析域 5 端点。trade_app 为组合根, 路由只读它的快照/store。"""
    router = APIRouter()

    def _daily_asset_rows():
        return trade_app.store.daily_asset.get()

    def _gapfill_history(limit: int = 20) -> list[dict]:
        """最近若干次停机日补算留痕 (audit 读回, 新的在前; 无记录/读失败 → [])。

        2026-09-10: 补算由 trade_main 在消费者线程/启动路径跑 (自动 + 人工命令),
        结果落 audit(kind='gapfill_*')。取多次而不是只取最后一次 —— 一轮补算可能
        有多个缺口 (有的写成功、有的被拒), 只读最后一条会让"被拒的缺口"从界面上
        消失 (自审发现)。
        """
        try:
            ro = trade_app.store.open_readonly()
            try:
                rows = ro.execute(
                    "SELECT ts, kind, message, detail_json FROM audit "
                    "WHERE kind LIKE 'gapfill%' ORDER BY id DESC LIMIT ?",
                    (int(limit),)).fetchall()
            finally:
                ro.close()
        except Exception:
            logger.warning("读补算留痕失败", exc_info=True)
            return []
        out = []
        for ts, kind, message, detail_json in rows:
            try:
                detail = json.loads(detail_json or "{}")
            except Exception:
                detail = {}
            out.append({"ts": ts, "kind": kind, "message": message, **detail})
        return out

    @router.get("/api/trade/analysis/equity")
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

    @router.get("/api/trade/analysis/attribution")
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

    @router.get("/api/trade/analysis/daily_pnl")
    def analysis_daily_pnl(year: int = Query(default=0, ge=0, le=3000),
                           month: int = Query(default=0, ge=0, le=12)):
        """逐日盈亏 (日历格子数据源)。year/month=0 默认当前年月。

        计算已全部下沉 trade/analysis.build_daily_pnl_view (2026-09-15
        深模块治理), 本路由只取数: 当月快照行 + load_prev 基准行 +
        整月成交聚合 + 补算留痕。口径注释见该函数 docstring。"""
        if year <= 0 or month <= 0:
            now = datetime.now()
            year = year if year > 0 else now.year
            month = month if month > 0 else now.month
        prefix = f"{year}-{month:02d}"
        rows = trade_app.store.daily_asset.get(
            start=f"{prefix}-01", end=f"{prefix}-31")
        prev_month_row = trade_app.store.daily_asset.load_prev(f"{prefix}-01")
        # 批量加载当月全部成交 (单次查询, 避免 N+1), 按日期聚合
        import calendar as _cal
        month_start = datetime(year, month, 1).timestamp()
        days = _cal.monthrange(year, month)[1]
        month_end = datetime(year, month, days, 23, 59, 59).timestamp()
        ro = trade_app.store.open_readonly()
        try:
            all_trades = ro.execute(
                "SELECT ts, direction, traded_id FROM trades "
                "WHERE ts >= ? AND ts < ? ORDER BY ts ASC",
                (month_start, month_end + 1)).fetchall()
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
        return build_daily_pnl_view(rows, prev_month_row, daily_trades,
                                    _gapfill_history(), year, month)

    @router.get("/api/trade/analysis/daily_report")
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

    @router.post("/api/trade/analysis/gapfill")
    def analysis_gapfill(dry_run: bool = Query(default=True)):
        """停机日资产补算 (2026-09-10, 人工触发): dry_run=1 只预览不写。

        入队给消费者线程执行 (补算要读 QMT 日线, 不在 HTTP 线程碰网关);
        结果与预览都落 audit, 用下面的 gapfill_last 读回展示。
        """
        trade_app.submit_command({"action": "gapfill",
                                  "dry_run": bool(dry_run),
                                  "source": "manual_api"})
        return {"accepted": True, "dry_run": bool(dry_run)}

    @router.get("/api/trade/analysis/gapfill_last")
    def analysis_gapfill_last():
        """最近一次停机日补算结果 (无记录 → {"last": null})。"""
        history = _gapfill_history(limit=1)
        return {"last": history[0] if history else None}

    @router.get("/api/trade/analysis/summary")
    def analysis_summary():
        """累计 KPI (分析 Tab 卡片数据源)。

        计算已全部下沉 trade/analysis.build_summary_view (2026-09-15 深模块
        治理 + 夏普口径收口: 与回测页同调 MetricsCalculator), 本路由只取数:
        快照行 + 成交 SQL 聚合 + QMT 对账查询。"""
        rows = _daily_asset_rows()
        if not rows:
            return {"initial_capital": 0, "current_asset": 0,
                    "total_trades": 0, "start_date": "", "end_date": ""}
        ro = trade_app.store.open_readonly()
        try:
            total_trades = ro.execute(
                "SELECT COUNT(*) FROM trades").fetchone()[0]
            agg_rows = ro.execute(
                "SELECT code, direction, SUM(price*qty), SUM(qty) "
                "FROM trades GROUP BY code, direction").fetchall()
        finally:
            ro.close()
        buy_map: dict = {}
        sell_map: dict = {}
        for code, direction, total_amt, total_qty in agg_rows:
            if direction == DIRECTION_BUY:
                buy_map[code] = {"total_cost": total_amt, "total_qty": total_qty}
            else:
                sell_map[code] = {"total_proceeds": total_amt, "total_qty": total_qty}
        # QMT 对账 (只告警不回写)。审计 Q3 (2026-08-19): 查询失败 ≠ 对账通过
        # —— None 传给视图层即告警, 不可静默吞掉 (违反对账只告警铁律)。
        qmt_total = None
        try:
            qmt_asset = trade_app.gateway.query_asset()
            qmt_total = float(qmt_asset.get("totalAsset", rows[-1]["total_asset"]))
        except Exception:
            logger.exception("QMT 对账资产查询失败, 视为对账告警 (资产数字未与 QMT 核对)")
        return build_summary_view(rows, total_trades, buy_map, sell_map, qmt_total)

    return router
