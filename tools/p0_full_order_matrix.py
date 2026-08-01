# -*- coding: utf-8 -*-
"""159290 实盘全链路通测:限价卖→限价买→对手最优卖→对手最优买(各 100 股)。

每腿等终态再下一腿;全部状态以 query_orders/query_trades 查询为准
(P0 教训: 回调不可靠, cancel/返回值不可靠)。净持仓不变。
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trade.gateway import RealGateway

CODE = "159290.SZ"
QTY = 100
cb = []

# 审计M13: 账号/路径不进 git, 走环境变量(同其余 p0/spike 脚本)
ACCOUNT_ID = os.environ.get("VERA_QMT_ACCOUNT", "")
QMT_PATH = os.environ.get("VERA_QMT_PATH", "")
if not ACCOUNT_ID or not QMT_PATH:
    print("请先设置环境变量 (审计M13: 账号不进 git):")
    print('  set VERA_QMT_ACCOUNT=你的资金账号')
    print('  set VERA_QMT_PATH=D:\\path\\to\\userdata_mini')
    sys.exit(1)


def wait_terminal(gw, oid, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        for o in gw.query_orders():
            if str(o["order_id"]) == str(oid):
                if o["status"] in (53, 54, 56, 57):
                    return o
        time.sleep(1)
    return None


def leg(gw, name, direction, price_type, price, remark):
    q = gw.query_quotes([CODE])[CODE]
    px = price if price is not None else 0.0
    print(f"\n=== {name}: {'买' if direction == 23 else '卖'} {QTY}股 @ {px or '对手最优'} (行情 bid1={q['bid1']} ask1={q['ask1']})")
    oid = gw.order(CODE, direction, px, QTY, price_type=price_type, remark=remark)
    print(f"  order_id={oid}")
    final = wait_terminal(gw, oid)
    if final is None:
        print("  ✗ 30s 未到终态(挂单中), 撤单...")
        gw.cancel(oid)
        time.sleep(3)
        return False
    filled = final.get("filled_qty", 0)
    print(f"  状态={final['status']} (56=已成 57=废单) 成交={filled}")
    if final["status"] == 56 and filled == QTY:
        tr = [t for t in gw.query_trades() if str(t.get("order_id")) == str(oid)]
        for t in tr:
            print(f"  ✓ 成交回报: {t['price']}x{t['qty']}")
        return True
    print("  ✗ 未成交")
    return False


def main():
    gw = RealGateway(ACCOUNT_ID, mini_qmt_path=QMT_PATH,
                     on_order=lambda o: cb.append(("order", o)),
                     on_trade=lambda t: cb.append(("trade", t)))
    assert gw.connect()
    p0 = {p["code"]: p["volume"] for p in gw.query_positions()}
    print(f"起始持仓: {p0}")

    results = {}
    q = gw.query_quotes([CODE])[CODE]
    results["限价卖@bid1"] = leg(gw, "腿1 限价卖出", 24, 11, round(q["bid1"], 3), "V0727-Q01")
    q = gw.query_quotes([CODE])[CODE]
    results["限价买@ask1"] = leg(gw, "腿2 限价买入", 23, 11, round(q["ask1"], 3), "V0727-Q02")
    results["对手最优卖"] = leg(gw, "腿3 对手最优卖出", 24, "MARKET_PEER_FIRST", None, "V0727-Q03")
    results["对手最优买"] = leg(gw, "腿4 对手最优买入", 23, "MARKET_PEER_FIRST", None, "V0727-Q04")

    p1 = {p["code"]: p["volume"] for p in gw.query_positions()}
    print(f"\n结束持仓: {p1}")
    print(f"回调条数: {len(cb)}")
    print("\n===== 通测结果 =====")
    for k, v in results.items():
        print(f"  {'✓' if v else '✗'} {k}")
    asset = gw.query_asset()
    print(f"  现金: {asset['cash']}")
    gw.disconnect()
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
