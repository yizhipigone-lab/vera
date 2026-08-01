# -*- coding: utf-8 -*-
"""P0 安全单验证:order_remark 回传 + 回报链路 + 撤单(一笔,零成交风险)。

标的安全逻辑:平安银行 000001.SZ,限价买入价=跌停价(昨收×0.9)。
- 价格在有效申报范围内(不会被判废单);
- 几乎不可能成交(除非平安银行单日跌停);
- 验证后立即撤单;若夜间撤单不受理,明日 9:15-9:20 可撤窗口处理。

用法: python tools/p0_safe_order_check.py
前提: miniQMT 已登录运行; 环境变量 VERA_QMT_ACCOUNT / VERA_QMT_PATH 已设置
(审计M13修复: 真实账号不进 git)。
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trade.gateway import RealGateway

QMT_PATH = os.environ.get("VERA_QMT_PATH", "")
ACCOUNT_ID = os.environ.get("VERA_QMT_ACCOUNT", "")

if not ACCOUNT_ID or not QMT_PATH:
    print("请先设置环境变量 (审计M13: 账号不进 git):")
    print('  set VERA_QMT_ACCOUNT=你的资金账号')
    print('  set VERA_QMT_PATH=D:\\path\\to\\userdata_mini')
    sys.exit(1)

CODE = "000001.SZ"
REMARK = "V0726-T01"  # ≤24 字符, 验证回传是否原样
QTY = 100


def main() -> int:
    events = []
    gw = RealGateway(
        ACCOUNT_ID,
        mini_qmt_path=QMT_PATH,
        on_order=lambda o: events.append(("on_order", o)),
        on_trade=lambda t: events.append(("on_trade", t)),
        on_disconnected=lambda: events.append(("on_disconnected",)),
    )
    if not gw.connect():
        print("连接失败")
        return 1

    # 1. 取昨收算跌停价(主板 10%)
    q = gw.query_quotes([CODE])[CODE]
    prev_close = q["prev_close"]
    limit_down = round(prev_close * 0.9 + 1e-9, 2)
    print(f"[1] {CODE} 昨收={prev_close} 现价={q['last']} → 安全买入价(跌停价)={limit_down}")

    # 2. 下安全单
    print(f"[2] 下单: 买入 {CODE} {QTY}股 @ {limit_down} remark={REMARK!r}")
    order_id = gw.order(CODE, 23, limit_down, QTY, remark=REMARK)
    print(f"    返回 order_id/seq: {order_id}")
    if order_id in ("-1", "None", ""):
        print("    下单被拒(可能非交易时间)。查看事件与当日委托…")

    # 3. 等回调 3 秒
    time.sleep(3)
    print(f"[3] 回调事件 {len(events)} 条:")
    for kind, payload in events:
        print(f"    {kind}: {payload}")

    # 4. 查当日委托, 验证 remark 回传
    print("[4] query_stock_orders 验证 remark 回传:")
    found = None
    for o in gw.query_orders():
        print(f"    {o}")
        if str(o.get("order_id")) == str(order_id) or o.get("remark") == REMARK:
            found = o
    if found is None:
        print("    当日委托中未找到该单(夜间被拒则属预期,remark 回传需盘中复验)")
    else:
        ok = found.get("remark") == REMARK
        print(f"    remark 回传: {found.get('remark')!r}  {'✓ 原样回传' if ok else '✗ 被改写/丢失'}")

    # 5. 撤单
    if order_id not in ("-1", "None", ""):
        print(f"[5] 撤单 {order_id}")
        rc = gw.cancel(order_id)
        print(f"    cancel 返回: {rc}")
        time.sleep(2)
        for o in gw.query_orders():
            if str(o.get("order_id")) == str(order_id):
                print(f"    撤单后状态: {o}")
        print(f"    全部回调事件: {events}")

    # 6. 资产确认(冻结是否释放)
    asset = gw.query_asset()
    print(f"[6] 资产: {asset}  (frozen_cash 应回到 0 或仅有该单冻结)")

    gw.disconnect()
    print("\n安全单验证完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
