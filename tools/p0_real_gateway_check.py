# -*- coding: utf-8 -*-
"""P0 真机联调:RealGateway 连接 → 灌仓 → 三方对账(只读,绝不下单)。

走 trade/ 真实模块(不是裸 xtquant),验证 RealGateway 的回调封装、
查询适配、Book 冷启动灌仓、Reconciler 对账在真机上的行为。

用法: python tools/p0_real_gateway_check.py
前提: miniQMT 已登录运行; 环境变量 VERA_QMT_ACCOUNT / VERA_QMT_PATH 已设置
(审计M13修复: 真实账号不进 git)。安全: 脚本不含任何下单/撤单调用。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trade.book import Book
from trade.gateway import RealGateway
from trade.reconciler import Reconciler
from trade.risk import KillSwitch
from trade.store import TradeStore

QMT_PATH = os.environ.get("VERA_QMT_PATH", "")
ACCOUNT_ID = os.environ.get("VERA_QMT_ACCOUNT", "")

if not ACCOUNT_ID or not QMT_PATH:
    print("请先设置环境变量 (审计M13: 账号不进 git):")
    print('  set VERA_QMT_ACCOUNT=你的资金账号')
    print('  set VERA_QMT_PATH=D:\\path\\to\\userdata_mini')
    sys.exit(1)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="vera_p0_"))
    store = TradeStore(tmp / "t.db", tmp / "raw.jsonl")
    kill = KillSwitch(store, tmp / "KILL")

    events = []
    gw = RealGateway(
        ACCOUNT_ID,
        mini_qmt_path=QMT_PATH,
        on_order=lambda o: events.append(("order", o)),
        on_trade=lambda t: events.append(("trade", t)),
        on_disconnected=lambda: events.append(("disconnected",)),
    )

    print("[1] RealGateway.connect()")
    ok = gw.connect()
    print(f"    -> {ok}")
    if not ok:
        print("连接失败")
        return 1

    print("[2] query_asset / query_positions")
    asset = gw.query_asset()
    print(f"    资产: {asset}")
    positions = gw.query_positions()
    for p in positions:
        print(f"    {p}")

    print("[3] Book 冷启动灌仓(restore_positions)")
    book = Book()
    book.restore_positions(positions)
    snap = book.snapshot()
    for code, pos in snap["positions"].items():
        print(f"    {code}: volume={pos.volume} can_use={pos.can_use} avg_cost={pos.avg_cost}")

    print("[4] query_quotes(买一/卖一)")
    codes = [p["code"] for p in positions] or ["600519.SH"]
    quotes = gw.query_quotes(codes)
    for code, q in quotes.items():
        print(f"    {code}: {q}")

    print("[5] Reconciler 三方对账(灌仓后应 NONE 通过)")
    rec = Reconciler(gw, book, store, kill, quote_price=lambda c: (quotes.get(c) or {}).get("last"))
    report = rec.reconcile()
    print(f"    level={report.level} passed={report.passed} diffs={len(report.diffs)}")
    for d in report.diffs:
        print(f"    {d}")
    print(f"    急停状态(应为 False): {kill.is_active()}")

    print(f"[6] 回调事件: {events if events else '(无,正常)'}")
    gw.disconnect()
    print("\n真机联调完成(只读,未下任何单)。")
    return 0 if report.passed and not kill.is_active() else 2


if __name__ == "__main__":
    sys.exit(main())
