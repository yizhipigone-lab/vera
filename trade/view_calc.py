"""交易页持仓视图纯计算域 (2026-09-02 热加载改造, 从 trade/api.py 下沉)。

build_positions_view 是 /api/trade/positions 的全部计算体: 只读
trade_app 的快照/库表/行情缓存, 不写任何状态、不碰订单 —— 因此本模块
可被 POST /api/trade/reload_view 在非交易时段安全重载, 改展示口径
(当日盈亏/浮盈这类) 不再需要重启 trade_main (省掉 QMT 冷启动流程)。

纪律 (违反即失去热加载资格):
- 本模块永远保持"纯函数 + 零模块级可变状态";
- 任何要写状态/下单的逻辑都不许搬进来
  (铁律: 交易状态唯一写者 = EventEngine);
- 依赖一律经 `trade.analysis` / `trade.book` 命名空间引用或函数内
  import, 保证 monkeypatch 与 reload 语义一致。

热生效机制: api.py 路由函数体内 `from trade.view_calc import ...`,
每次请求从 sys.modules 取最新模块 —— reload 端点重载后即刻生效。
"""
from __future__ import annotations

import time

import trade.analysis as _ana
from trade.book import DIRECTION_BUY, is_etf


def build_positions_view(trade_app) -> dict:
    """持仓页视图: positions 明细 + 已平仓列表 + 时间戳。

    口径与历史注释原样保留自 api.py (2026-07-30 ~ 2026-09-02 各次
    修正的完整演进见 git 历史; 关键事件: 300119/159949/159290/518880)。
    """
    snap = trade_app.book.snapshot()
    # 2026-07-30: 持仓明细增强 — 简称/入场时间/市值/盈亏比例/已平仓。
    # 名称表惰性加载一次 (DataFetcher.get_name_map, 失败回退空 → 前端显示代码)。
    # 2026-08-11: 数量=0 的幽灵持仓 (已卖光但 book key 未删) 改用真实已实现
    # 盈亏展示 —— 留在持仓表灰显 + "已平仓"徽标, 不再显示一堆 0, 也不另开
    # 分区 (用户裁决)。平仓票的 avg_cost/pnl/pnl_pct 由 summary 覆盖。
    entry_map, closed, summary = _ana.entry_and_closed(trade_app.store)
    # 2026-08-12: 当日盈亏精确化 — 昨仓按昨收、今买按买入均价。
    # 先汇总"最近一个交易日"的买卖 (尾盘新买的票不该把买入前当天
    # 的涨幅算成盈利)。2026-08-16 修复: 锚点从墙钟今天改成最近
    # 交易日 —— 周末/节假日墙钟今天不是交易日, 周五尾盘买入会被
    # 误判成过夜仓, 把周五全天涨幅算成"当日盈亏"。
    # 2026-09-02: 卖出聚合同源同窗口 (518880 事件当日盈亏修正)。
    today_buys: dict = {}
    today_sells: dict = {}
    try:
        ro = trade_app.store.open_readonly()
        try:
            lo, hi = _ana._last_trading_day_range()
            for code_, dir_, qty, amount in ro.execute(
                "SELECT code, direction, SUM(qty), SUM(amount) FROM trades "
                "WHERE ts>=? AND ts<? GROUP BY code, direction",
                (lo, hi)):
                tgt = (today_buys if dir_ == DIRECTION_BUY else today_sells)
                tgt[code_] = {"qty": qty or 0, "amount": amount or 0.0}
        finally:
            ro.close()
    except Exception:
        today_buys = {}
        today_sells = {}
    # 2026-08-18: 盘前(还没开盘)判定一次。QMT 的 prev_close 尚未翻日
    # (仍是"前前一个交易日"的收盘价), 直接 (last-prev_close) 会把前一交易日
    # 全天涨幅算成"当日盈亏" (159949: 08-17 尾盘买入, 08-18 盘前显示 +13950)。
    # 盘前"今日"无任何变动 → 当日盈亏应为 0。
    from trade.monitor import trading_session
    pre_open = trading_session() == "pre_open"
    result = []
    for code, p in sorted(snap["positions"].items()):
        quote = trade_app.monitor.quote_of(code)
        last = quote["last"] if quote else None
        prev_close = (quote.get("prev_close") or None) if quote else None
        # 当日涨幅 = 客观价格口径 (现价/昨收-1), 与是否持仓/何时买入无关:
        # 盘中实时、收盘/周末为最近交易日涨跌。2026-08-16 澄清 (尾盘买入不
        # 清零) + 2026-08-17 (已平仓票也要显示 —— 客观事实不因平仓消失)。
        day_chg_pct = (round((last / prev_close - 1) * 100, 2)
                       if last and prev_close else None)
        s = summary.get(code)
        # 平仓票: volume=0 且 trades 证明确已整周期闭环
        is_closed_pos = (p.volume == 0 and s is not None and s["is_closed"])
        etf = is_etf(code)
        if is_closed_pos:
            # 成本→买入均价; 浮盈→已实现盈亏; 盈亏%→已实现%; 市值无意义→None。
            # 当日涨幅(客观)已在上方算出, 保留。
            # 当日盈亏 = 今日价格变动 × 今日卖出量 = Σ当日卖出额 -
            # 昨收×Σ当日卖出量。2026-09-02 (518880 事件) 修正: 旧公式
            # 用整周期累计 (sell_avg - prev_close) × sell_qty, 隐含
            # "全部卖出发生在最近交易日" 假设 —— 跨日分批卖出
            # (08-21 卖 5600@9.385 + 09-02 卖遗产仓 200@8.892,
            # 昨收 9.118) 会把历史卖出日的价差也算进"今天",
            # 误显 +1450; 正确值为今日卖出腿 (8.892-9.118)×200=-45.2。
            # 最近交易日无卖出 → 0 (当日已不持有)。
            # (用户 2026-08-17 澄清: 当日盈亏≠已实现盈亏, 前者锚昨收、后者锚买入均价)
            day_chg_amt = None
            sell_avg = s["sell_avg"]
            exit_ts = s["exit_ts"]
            if prev_close and exit_ts:
                tsel = today_sells.get(code)
                if tsel and tsel["qty"]:
                    day_chg_amt = round(
                        tsel["amount"] - prev_close * tsel["qty"], 2)
                else:
                    day_chg_amt = 0.0
            market_value = None
            # 2026-08-27 (159290 事件): 成本/数量取被平仓口径 (遗产仓
            # 表内买入额只是零头, 用买入均价会把盈亏%分母缩错);
            # 无 pnl 可考的历史平仓回退表内买入口径 (行为不变)。
            avg_cost = (s["cost_avg"] if s["cost_avg"] is not None
                        else s["buy_avg"])
            pnl = s["realized_pnl"]
            pnl_pct = s["realized_pnl_pct"]
            entry_ts = s["entry_ts"]
            exit_ts = s["exit_ts"]
            hold = _ana.hold_days(s["entry_ts"], s["exit_ts"])
            buy_qty = s["closed_qty"]
            sell_avg = s["sell_avg"]
        else:
            # 2026-08-12: 当日盈亏精确化 — 昨仓部分按昨收, 今日买入部分
            # 按今日买入均价 (尾盘新买的票不该把买入前今天的涨幅算成盈利;
            # 300119 事件)。今买量从 trades 今日买入汇总取, 与持仓量取小
            # (今日卖了部分则剩余额按买入均价近似)。
            tb = today_buys.get(code, {})
            tb_qty = min(tb.get("qty", 0), p.volume)
            tb_avg = (tb["amount"] / tb["qty"]
                      if tb.get("qty") and tb["qty"] > 0 else None)
            yest_qty = max(0, p.volume - tb_qty)
            if last:
                if pre_open:
                    # 盘前: 今日无变动, 当日盈亏 = 0 (见上方 2026-08-18 注)
                    day_chg_amt = 0.0
                else:
                    yest_amt = ((last - prev_close) * yest_qty
                                if yest_qty > 0 and prev_close else 0.0)
                    tb_amt = ((last - tb_avg) * tb_qty
                              if tb_qty > 0 and tb_avg else 0.0)
                    base = ((prev_close or 0.0) * yest_qty
                            + (tb_avg or 0.0) * tb_qty)
                    day_chg_amt = round(yest_amt + tb_amt, 2) if base else None
            else:
                day_chg_amt = None
            market_value = round(last * p.volume, 2) if last else None
            avg_cost = p.avg_cost
            pnl = round((last - p.avg_cost) * p.volume, 2) if last else None
            pnl_pct = (round((last / p.avg_cost - 1) * 100, 2)
                       if last and p.avg_cost > 0 else None)
            entry_ts = entry_map.get(code)
            exit_ts = None
            hold = _ana.hold_days(entry_ts)
            buy_qty = None
            sell_avg = None
        result.append({
            "code": code, "name": _ana.name_of(code),
            "volume": p.volume, "can_use": p.can_use,
            "avg_cost": round(avg_cost, 2) if avg_cost is not None else None,
            "strategy": p.strategy,
            "last": last,
            "day_chg_pct": day_chg_pct,
            "day_chg_amt": day_chg_amt,
            "market_value": market_value,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "entry_ts": entry_ts,
            "exit_ts": exit_ts,
            "hold_days": hold,
            # 2026-08-11: 平仓票的买入量与卖出均价 (持仓票为 None)
            "buy_qty": buy_qty,
            "sell_avg": round(sell_avg, 2) if sell_avg is not None else None,
            "closed": is_closed_pos,
            "tiers_done": sorted(snap["tiers"].get(code, {}).get(
                time.strftime("%Y%m%d"), ())),
            # 2026-07-27 裁决③: ETF 明示不纳入自动管理
            "etf": etf,
            "managed": not (etf and trade_app.config.exclude_etf),
        })
    return {"positions": result, "closed": closed, "ts": time.time()}
