"""盘后日报纯计算域 (2026-08-19 深模块治理, 从 trade_main.py TradeApp 下沉)。

TradeApp._notify_daily 是编排器 (查资产→算盈亏→拼 payload→推飞书→落库),
它调用的三个"计算"步骤搬到这里, 变纯函数: 依赖 (all_trades / quotes) 由
调用方传入, 函数零 self 依赖, 可直接单测。

- build_trade_summary: 当日成交明细 → 交易摘要 + 卖出明细 + 交易明细混排
- enrich_sell_quotes: 卖单补「盘中最高涨幅 / 卖出时点涨幅」卖飞信号
- diff_positions: 昨仓 vs 今仓 → 新进/清仓/加仓/减仓

依赖方向: trade.daily_report → trade.book (compute_remaining_map/DIRECTION_BUY),
不反向。全部纯函数, 无 IO。
"""
from __future__ import annotations

from trade.book import DIRECTION_BUY, compute_remaining_map


def build_trade_summary(trades_detail: list[dict], all_trades: list[dict]) -> dict:
    """从当日成交明细算交易摘要 + 卖出明细 + 交易明细混排。

    all_trades: 全历史成交 (store.load_all_trades() 的产物), 重放算每笔剩余/
    卖出比例 (trades 表不存剩余)。为纯函数, 由调用方负责取数。
    sell_count=0 → win_rate=None (不除零); sell_details 按 ts 升序超 8 笔折叠;
    trade_details 按 ts 升序超 12 笔折叠 (trade_details_folded 只汇总卖出盈亏)。
    """
    buy_count = sell_count = 0
    turnover = 0.0
    realized_pnl = 0.0
    wins = 0
    sells: list[dict] = []
    details: list[dict] = []
    tgt_tids = {str(t.get("traded_id")) for t in trades_detail
                if t.get("traded_id")}
    remain_map = (compute_remaining_map(all_trades, tgt_tids) if tgt_tids else {})
    for t in trades_detail:
        turnover += abs(float(t.get("amount", 0.0) or 0.0))
        tid = str(t.get("traded_id", ""))
        rm = remain_map.get(tid, {})
        rec: dict = {
            "code": t["code"], "direction": t.get("direction"),
            "price": float(t.get("price", 0.0) or 0.0),
            "qty": int(t.get("qty", 0) or 0),
            "amount": float(t.get("amount", 0.0) or 0.0),
            "reason": t.get("reason", ""), "ts": t.get("ts", 0.0),
            "remaining_vol": rm.get("remaining_vol"),
            "remaining_value": rm.get("remaining_value"),
        }
        if t.get("direction") == DIRECTION_BUY:
            buy_count += 1
            rec["pnl_amount"] = None
            rec["pnl_pct"] = None
        else:
            sell_count += 1
            pnl = float(t.get("pnl_amount", 0.0) or 0.0)
            realized_pnl += pnl
            if pnl > 0:
                wins += 1
            rec["pnl_amount"] = pnl
            rec["pnl_pct"] = t.get("pnl_pct")
            rec["sell_ratio"] = rm.get("sell_ratio")
            sells.append({"code": t["code"], "reason": t.get("reason", ""),
                          "pnl_amount": pnl, "pnl_pct": t.get("pnl_pct"),
                          "ts": t.get("ts", 0.0)})
        details.append(rec)
    out: dict = {
        "buy_count": buy_count, "sell_count": sell_count,
        "turnover": round(turnover, 2),
        "realized_pnl": round(realized_pnl, 2),
        "win_rate": round(wins / sell_count, 4) if sell_count > 0 else None,
    }
    if sells:
        sells.sort(key=lambda s: s["ts"])
        if len(sells) > 8:
            folded = sells[8:]
            out["sell_details"] = sells[:8]
            out["sell_details_folded"] = {
                "count": len(folded),
                "sum_pnl_amount": round(sum(s["pnl_amount"] for s in folded), 2),
            }
        else:
            out["sell_details"] = sells
    if details:
        details.sort(key=lambda x: x["ts"])
        cap = 12
        if len(details) > cap:
            folded_d = details[cap:]
            out["trade_details"] = details[:cap]
            out["trade_details_folded"] = {
                "count": len(folded_d),
                "sum_sell_pnl": round(
                    sum(d["pnl_amount"] for d in folded_d
                        if d["pnl_amount"] is not None), 2),
            }
        else:
            out["trade_details"] = details
    return out


def enrich_sell_quotes(trade_details: list[dict], quotes: dict) -> None:
    """卖单补「盘中最高涨幅 / 卖出时点涨幅」—— 卖飞信号。

    quotes 为 None/空 dict 时逐票跳过 (调用方查询失败已 fail-soft 兜底);
    昨收/卖价 <= 0 跳过, 不写字段。
    """
    for t in (trade_details or []):
        if t.get("direction") == DIRECTION_BUY or not t.get("price"):
            continue
        q = quotes.get(t["code"]) if quotes else None
        if not q:
            continue
        prev = float(q.get("prev_close") or 0.0)
        high = float(q.get("high") or 0.0)
        price = float(t.get("price") or 0.0)
        if prev <= 0 or price <= 0:
            continue
        t["intraday_high_pct"] = round((high / prev - 1.0) * 100, 2)
        t["sell_pct_vs_prev"] = round((price / prev - 1.0) * 100, 2)


def diff_positions(prev: dict, curr: dict) -> dict:
    """仓位变动: 按 (今 curr vs 昨 prev) 净 volume diff 分类。

    prev={code:{volume,..}} (store 快照), curr={code:PositionView} (book)。
    return {new, closed, added, reduced}, 每项 [{code, delta}]。
    """
    prev_vols = {c: int(p.get("volume", 0)) for c, p in prev.items()}
    curr_vols = {c: int(getattr(p, "volume", 0)) for c, p in curr.items()}
    new, closed, added, reduced = [], [], [], []
    for code in set(prev_vols) | set(curr_vols):
        pv, cv = prev_vols.get(code, 0), curr_vols.get(code, 0)
        delta = cv - pv
        if pv == 0 and cv > 0:
            new.append({"code": code, "delta": cv})
        elif cv == 0 and pv > 0:
            closed.append({"code": code, "delta": -pv})
        elif delta > 0:
            added.append({"code": code, "delta": delta})
        elif delta < 0:
            reduced.append({"code": code, "delta": delta})
    return {"new": new, "closed": closed, "added": added, "reduced": reduced}
