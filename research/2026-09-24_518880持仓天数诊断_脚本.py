"""只读诊断: 黄金ETF华安(518880.SH) 全部成交记录。

只读连接 (mode=ro), 绝不写真实 trade.db。
跑法: python research/2026-09-24_518880持仓天数诊断_脚本.py
"""
import datetime
import sqlite3

DB = "data/trade/trade.db"

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.execute("PRAGMA query_only=1")

print("== trades 表 518880.SH 全部成交 (按时间) ==")
cur = conn.execute(
    "SELECT ts, direction, qty, price, amount, pnl_amount FROM trades "
    "WHERE code='518880.SH' ORDER BY ts")
for ts, d, q, p, a, pnl in cur.fetchall():
    t = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    side = "买入" if d == 23 else ("卖出" if d == 24 else str(d))
    print(f"{t}  {side}  {q:>8} 股  价 {p:.3f}  金额 {a:>12.2f}  "
          f"盈亏 {pnl if pnl is not None else 0:.2f}")

print()
print("== 当前 book 里的持仓快照 (positions 相关表) ==")
tables = [r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("表清单:", ", ".join(tables))
for tname in tables:
    if tname in ("positions", "position_snapshot", "book"):
        try:
            rows = conn.execute(
                f"SELECT * FROM {tname} WHERE code='518880.SH'").fetchall()
            cols = [d[0] for d in conn.execute(
                f"SELECT * FROM {tname} LIMIT 1").description]
            for r in rows:
                print(tname, dict(zip(cols, r)))
        except Exception as e:
            print(tname, "查询失败:", e)
conn.close()
