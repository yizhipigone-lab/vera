# -*- coding: utf-8 -*-
"""一次性只读诊断: 518880 黄金ETF 为什么还出现在持仓表 (2026-09-03 用户问).

只读连接 (mode=ro), 绝不写真实 trade.db。跑完: python research/2026-09-03_518880持仓行诊断_脚本.py
"""
import json
import sqlite3
import sys

DB = "data/trade/trade.db"


def main() -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    print("=== trades 表结构 ===")
    for r in conn.execute("PRAGMA table_info(trades)"):
        print(dict(r))

    print("\n=== 518880 全部成交 (按时间) ===")
    rows = list(conn.execute(
        "SELECT * FROM trades WHERE code LIKE '%518880%' ORDER BY ts"))
    for r in rows:
        print(json.dumps(dict(r), ensure_ascii=False))

    print(f"\n共 {len(rows)} 笔")

    print("\n=== 近 5 日全部成交概览 (对照今天有没有任何交易) ===")
    for r in conn.execute(
            "SELECT ts, code, direction, price, qty, amount, strategy "
            "FROM trades WHERE ts >= '2026-08-30' ORDER BY ts"):
        print(dict(r))

    print("\n=== orders 表里 518880 的委托 (谁下的单) ===")
    try:
        for r in conn.execute(
                "SELECT * FROM orders WHERE code LIKE '%518880%' ORDER BY rowid DESC LIMIT 10"):
            print(json.dumps(dict(r), ensure_ascii=False))
    except Exception as e:
        print(f"(orders 查询失败: {e})")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
