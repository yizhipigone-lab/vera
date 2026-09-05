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

import math
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from trade.analysis import calc_drawdowns, rows_to_dicts
from trade.book import DIRECTION_BUY, DIRECTION_SELL
from utils.logger import get_logger

logger = get_logger(__name__)


def analysis_router(trade_app) -> APIRouter:
    """装配分析域 5 端点。trade_app 为组合根, 路由只读它的快照/store。"""
    router = APIRouter()

    def _daily_asset_rows():
        return trade_app.store.daily_asset.get()

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

    @router.get("/api/trade/analysis/summary")
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

    return router
