"""只读验证: 轮次切分后的 entry_and_closed 在真实 trade.db 上的输出。

只读连接 (mode=ro), 绝不写真实 trade.db。用一个最小 shim 包装只读连接,
冒充 TradeStore 传给 entry_and_closed (它只调用 store.open_readonly())。
跑法: python research/2026-09-24_518880轮次切分验证_脚本.py
"""
import datetime
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trade.analysis import entry_and_closed

DB = "data/trade/trade.db"


class _RoStore:
    """最小只读 shim: 只提供 open_readonly()。"""

    def open_readonly(self):
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=1")
        return conn


def _fmt(ts):
    return (datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
            if ts else "—")


entry_map, closed, summary = entry_and_closed(_RoStore())

print("== 黄金ETF华安(518880.SH) summary (页面持仓行用的就是这份) ==")
s = summary.get("518880.SH")
if s:
    for k in ("is_closed", "entry_ts", "exit_ts", "closed_qty", "cost_avg",
              "buy_avg", "sell_avg", "realized_pnl", "realized_pnl_pct"):
        v = s[k]
        if k in ("entry_ts", "exit_ts"):
            v = _fmt(v)
        print(f"  {k}: {v}")

print()
print("== 已平仓列表里的 518880 (每轮一条) ==")
for c in closed:
    if c["code"] == "518880.SH":
        print(f"  入场 {_fmt(c['entry_ts'])} → 出场 {_fmt(c['exit_ts'])}  "
              f"数量 {c['qty']}  买入均价 {c['buy_avg']}  "
              f"卖出均价 {c['sell_avg']}  盈亏 {c['realized_pnl']}  "
              f"({c['realized_pnl_pct']}%)  持仓 {c['hold_days']} 天")

print()
print("== entry_map 里还有没有 518880 (已平仓不该有) ==")
print(" ", entry_map.get("518880.SH", "无 (正确)"))

print()
print("== 全库已平仓列表前 8 条 (看看别的票有没有被切碎) ==")
for c in closed[:8]:
    print(f"  {c['code']} {_fmt(c['entry_ts'])} → {_fmt(c['exit_ts'])}  "
          f"{c['qty']} 股  盈亏 {c['realized_pnl']}  持仓 {c['hold_days']} 天")
