"""一次性只读诊断: 查 2026-08-07 日报 realized_pnl 是否因补记漏算 pnl。
只读连接 (mode=ro), 绝不写真实 trade.db。跑完: python research/check_today_daily.py
"""
import sqlite3
import json
import datetime
import sys

DB = "data/trade/trade.db"
DATE = sys.argv[1] if len(sys.argv) > 1 else "2026-08-07"

# 只读连接 — 物理上不可能写
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

y, m, d = map(int, DATE.split("-"))
start_ts = datetime.datetime(y, m, d, 0, 0, 0).timestamp()

print("=" * 60)
print(f"日报 {DATE}:")
r = conn.execute(
    "SELECT payload_json FROM daily_report WHERE date=?", (DATE,)
).fetchone()
if r:
    p = json.loads(r["payload_json"])
    print(f"  realized_pnl = {p.get('realized_pnl')}")
    print(f"  total_pnl    = {p.get('total_pnl')}")
    print(f"  day_pnl      = {p.get('day_pnl')}")
    print(f"  trades 笔数  = {len(p.get('trades', []))}")
    for t in p.get("trades", []):
        # direction 23=买 24=卖 (xtquant 约定)
        tag = "卖" if t.get("direction") == 24 else "买"
        print(f"    [{tag}] {t.get('code')} {t.get('qty')}@{t.get('price')} "
              f"pnl={t.get('pnl_amount')}/{t.get('pnl_pct')} src={t.get('source')}")
else:
    print(f"  (该日无日报记录)")

print("=" * 60)
print(f"trades 表当日成交 (ts >= {DATE} 00:00):")
rows = list(conn.execute(
    "SELECT code, direction, price, qty, pnl_amount, pnl_pct, source, reason "
    "FROM trades WHERE ts >= ? ORDER BY ts", (start_ts,)
))
print(f"  共 {len(rows)} 笔")
for row in rows:
    d2 = dict(row)
    tag = "卖" if d2["direction"] == 24 else "买"
    flag = "  <-- 卖出 pnl=0 漏算?" if (d2["direction"] == 24 and d2["pnl_amount"] == 0) else ""
    print(f"  [{tag}] {d2['code']} {d2['qty']}@{d2['price']} "
          f"pnl={d2['pnl_amount']}/{d2['pnl_pct']} src={d2['source']} rsn={d2['reason']}{flag}")

print("=" * 60)
print("audit 当日补记/认领记录:")
try:
    arows = list(conn.execute(
        "SELECT kind, detail FROM audit WHERE ts >= ?", (start_ts,)
    ))
    hit = [dict(r) for r in arows
           if "backfill" in str(r["kind"]) or "adopt" in str(r["kind"])]
    print(f"  共 {len(hit)} 条")
    for h in hit:
        print(f"  {h['kind']}: {h['detail']}")
except Exception as e:
    print(f"  audit 查询失败: {e}")

conn.close()
