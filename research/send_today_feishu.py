"""一次性: 用真实 trade.db 数据 + 系统 FeishuNotifier 发今天盘后日报到飞书。
复刻 trade_main._notify_daily 的 payload 构造, 数据源改静态表 (无运行进程):
- 资产/市值: daily_asset 8/7
- day_pnl 基准: 昨日日终 total_asset (注明非盘前 _day_baseline)
- realized_pnl: 重放 Book 全历史, 今天卖出取 apply 前 avg_cost (系统实时路径同口径)
- 持仓/浮盈: position_snapshot 最新 (QMT 真相源)
- 仓位变动: position_snapshot 无昨仓历史, skip
只发飞书, 不落 daily_report 表 (避免建表改真实库 schema)。
用法:
  python research/send_today_feishu.py --dry   # 只预览不发
  python research/send_today_feishu.py          # 真发飞书
  python research/send_today_feishu.py 2026-08-07
"""
import os
import sys
import time
import sqlite3
import datetime

sys.path.insert(0, ".")
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from trade.book import Book, DIRECTION_BUY, DIRECTION_SELL
from trade.notifier import FeishuNotifier

DB = "data/trade/trade.db"
DRY = "--dry" in sys.argv
VERIFY = "--verify" in sys.argv   # 直接 POST 读飞书 code (验证 column_set 是否被拒)
args = [a for a in sys.argv[1:] if a != "--dry" and a != "--verify"]
DATE = args[0] if args else "2026-08-07"
Y, M, D = map(int, DATE.split("-"))
START_TODAY = datetime.datetime(Y, M, D, 0, 0, 0).timestamp()
PREV_DATE = (datetime.date(Y, M, D) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

# 1. 资产 (daily_asset)
arow = conn.execute(
    "SELECT total_asset, market_value, available FROM daily_asset WHERE date=?",
    (DATE,),
).fetchone()
if not arow:
    print(f"[ERR] daily_asset 无 {DATE} 记录"); sys.exit(1)
total_asset = float(arow["total_asset"])
market_value = float(arow["market_value"])
# 2026-08-13: 优先真实 cash (available; 键名修复后 >0), 旧行 available=0
# (键名 bug 期间) 才回落 总资产−市值 倒推
cash = (float(arow["available"]) if float(arow["available"] or 0) > 0
        else round(total_asset - market_value, 2))

# 2. day_pnl 基准 = 昨日日终 (非盘前, 注明)
prow = conn.execute(
    "SELECT total_asset FROM daily_asset WHERE date=?", (PREV_DATE,)
).fetchone()
baseline = float(prow["total_asset"]) if prow else None
day_pnl = round(total_asset - baseline, 2) if baseline else None
day_pnl_pct = round((total_asset / baseline - 1) * 100, 2) if baseline else None

# 3. 重放 Book 全历史 → 今天卖出 pnl (系统实时路径同口径)
book = Book()
all_rows = list(conn.execute(
    "SELECT traded_id, order_id, code, direction, price, qty, amount, ts, reason "
    "FROM trades ORDER BY ts"))
realized = 0.0
wins = 0
buy_count = sell_count = 0
turnover = 0.0
sells_today = []
details_today = []   # 买卖混排 (飞书交易明细单列每条一块; 2026-08-07)
cum_buy = {}   # 累计买入 (卖出比例分母=当初买入总股数, 用户口径 2026-08-08)
for r in all_rows:
    d = dict(r)
    price = float(d["price"]); qty = int(d["qty"])
    code = d["code"]; direction = d["direction"]
    is_today = d["ts"] >= START_TODAY
    # 卖出 pnl: apply 前的 avg_cost (系统实时路径同口径)
    pnl = None; ppct = None
    if is_today and direction == DIRECTION_SELL:
        pos = book.snapshot()["positions"].get(code)
        avg = pos.avg_cost if (pos and pos.avg_cost > 0) else 0.0
        if avg > 0:
            pnl = round((price - avg) * qty, 2)
            ppct = round((price / avg - 1) * 100, 2)
    if direction == DIRECTION_BUY:   # 买入累计 (卖出比例分母, 含历史; 与 book.py 极性统一)
        cum_buy[code] = cum_buy.get(code, 0) + qty
    book.apply_trade(str(d["traded_id"]), str(d["order_id"]), code,
                     direction, price, qty)
    if not is_today:
        continue
    turnover += price * qty
    pos_after = book.snapshot()["positions"].get(code)
    rem = int(pos_after.volume) if pos_after else 0
    rem_val = round(rem * price, 2)
    if direction == DIRECTION_SELL:
        sell_count += 1
        if pnl is not None:
            realized += pnl
            if pnl > 0:
                wins += 1
        cb = cum_buy.get(code, 0)
        sr = round(qty / cb, 4) if cb > 0 else None
        sells_today.append({"code": code, "reason": d["reason"] or "",
                            "pnl_amount": pnl if pnl is not None else 0.0,
                            "pnl_pct": ppct, "ts": d["ts"]})
        details_today.append({
            "code": code, "direction": direction,
            "price": price, "qty": qty, "amount": float(d["amount"]),
            "pnl_amount": pnl if pnl is not None else 0.0, "pnl_pct": ppct,
            "reason": d["reason"] or "", "ts": d["ts"],
            "remaining_vol": rem, "remaining_value": rem_val, "sell_ratio": sr})
    else:
        buy_count += 1
        details_today.append({
            "code": code, "direction": direction,
            "price": price, "qty": qty, "amount": float(d["amount"]),
            "pnl_amount": None, "pnl_pct": None,
            "reason": d["reason"] or "", "ts": d["ts"],
            "remaining_vol": rem, "remaining_value": rem_val})
details_today.sort(key=lambda x: x["ts"])

# 4. 持仓/浮盈 (position_snapshot, QMT 真相源)
pos_rows = [dict(r) for r in conn.execute(
    "SELECT code, volume, avg_cost FROM position_snapshot WHERE volume > 0")]
pos_count = len(pos_rows)
cost_basis = sum(p["volume"] * p["avg_cost"] for p in pos_rows)
floating_pnl = round(market_value - cost_basis, 2)
conn.close()

# 5. sell_details 折叠 (前8 + 折叠, 复刻 _build_trade_summary)
sells_today.sort(key=lambda s: s.get("ts", 0))
SELL_CAP = 8
sell_details = sells_today[:SELL_CAP]
sell_details_folded = None
if len(sells_today) > SELL_CAP:
    rest = sells_today[SELL_CAP:]
    sell_details_folded = {
        "count": len(rest),
        "sum_pnl_amount": round(sum(s["pnl_amount"] or 0.0 for s in rest), 2),
    }

# 6. payload (复刻 _notify_daily 结构)
payload = {
    "total_asset": total_asset, "cash": cash, "market_value": market_value,
    "day_pnl": day_pnl, "day_pnl_pct": day_pnl_pct,
    "position_count": pos_count, "ts": time.time(),
    "buy_count": buy_count, "sell_count": sell_count,
    "turnover": round(turnover, 2),
    "realized_pnl": round(realized, 2),
    "win_rate": round(wins / sell_count, 4) if sell_count > 0 else None,
    "sell_details": sell_details,
    "sell_details_folded": sell_details_folded,
    "trade_details": details_today,
    "floating_pnl": floating_pnl,
}

# 预览
print("=" * 56)
print(f"  盘后日报 {DATE} 预览")
print("=" * 56)
print(f"  总资产 {total_asset:,.2f} | 市值 {market_value:,.2f} | 现金 {cash:,.2f}")
bnote = f"基准=昨日{PREV_DATE}日终(非盘前)" if baseline else "无基准"
print(f"  当日盈亏 {day_pnl} ({day_pnl_pct}%)  [{bnote}]")
print(f"  买{buy_count} · 卖{sell_count} | 成交额 {payload['turnover']:,.2f}")
print(f"  已实现盈亏 {payload['realized_pnl']:,.2f} | 胜率 "
      f"{round(payload['win_rate']*100,1) if payload['win_rate'] else 0}%")
print(f"  浮盈 {floating_pnl:,.2f} | 持仓 {pos_count} 只")
print(f"  交易明细 {len(details_today)} 笔 (买{buy_count}/卖{sell_count}):")
for s in details_today:
    if s["direction"] == DIRECTION_SELL:
        print(f"    卖 {s['code']} pnl={s['pnl_amount']}/{s['pnl_pct']} {s['reason'][:30]}")
    else:
        print(f"    买 {s['code']} {s['qty']}@{s['price']} ={s['amount']:,.0f}")
print("=" * 56)

if DRY:
    # 额外: 构建卡片验证交易明细单列每条一块结构 (不发; 2026-08-08)
    from trade.notifier import FeishuNotifier
    n = FeishuNotifier(lambda: True, lambda: "http://x")
    card = n._build_daily_card(payload, level="full")
    trade_blocks = [e for e in card["card"]["elements"]
                    if e.get("tag") == "div"
                    and "股 × " in e.get("text", {}).get("content", "")]
    print(f"--- 交易明细单列预览 ({len(trade_blocks)} 块, 每条一笔) ---")
    for e in trade_blocks:
        print("  " + e["text"]["content"].replace(chr(10), " / "))
    print(f"  卡片 elements: {[e.get('tag') for e in card['card']['elements']]}")
    print("[DRY] 仅预览, 未发送。去掉 --dry 真发飞书。")
    sys.exit(0)

# 7. 发飞书
webhook = os.environ.get("FEISHU_WEBHOOK_URL")
if not webhook:
    print("[ERR] 未配置 FEISHU_WEBHOOK_URL, 不发"); sys.exit(1)

if VERIFY:
    # 直接 POST + 读飞书业务码: HTTP 200 但 code!=0 = 卡片被拒 (2026-08-08 验证用)
    import urllib.request as _u, json as _j
    n = FeishuNotifier(enabled_getter=lambda: True, webhook_getter=lambda: webhook)
    card = n._build_daily_card(payload, level="full")
    data = _j.dumps(card, ensure_ascii=False).encode("utf-8")
    req = _u.Request(webhook, data=data, headers={"Content-Type": "application/json"})
    with _u.urlopen(req, timeout=10) as resp:
        body = resp.read().decode("utf-8")
    r = _j.loads(body)
    tags = [e.get("tag") for e in card["card"]["elements"]]
    trade_blocks = sum(1 for e in card["card"]["elements"]
                       if e.get("tag") == "div"
                       and "股 × " in e.get("text", {}).get("content", ""))
    print(f"[VERIFY] HTTP {resp.status} | 飞书 code={r.get('code')} msg={r.get('msg')}")
    print(f"[VERIFY] 卡片 elements: {tags}")
    print(f"[VERIFY] 交易明细块数: {trade_blocks} (单列每条一块; code=0 即合规)")
else:
    # 2026-08-13 补发修正版: 直接构建卡片 + POST (不走 worker 队列),
    # 标题加"(修正补发)"与当天早先的错版卡片区分; 读飞书业务码确认投递成功。
    import urllib.request as _u, json as _j
    n = FeishuNotifier(enabled_getter=lambda: True, webhook_getter=lambda: webhook)
    card = n._build_daily_card(payload, level="full")
    card["card"]["header"]["title"]["content"] = f"收盘日报(修正补发) · {DATE}"
    data = _j.dumps(card, ensure_ascii=False).encode("utf-8")
    req = _u.Request(webhook, data=data, headers={"Content-Type": "application/json"})
    with _u.urlopen(req, timeout=10) as resp:
        r = _j.loads(resp.read().decode("utf-8"))
    if r.get("code") == 0:
        print("[OK] 修正版收盘日报已推送至飞书")
    else:
        print(f"[ERR] 飞书返回 code={r.get('code')} msg={r.get('msg')}")
        sys.exit(1)
