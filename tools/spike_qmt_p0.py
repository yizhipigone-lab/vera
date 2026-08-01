# -*- coding: utf-8 -*-
"""P0 spike:QMT 实盘连接与查询验证(只读,绝不下单)。

验证项(计划书 §九 P0):
  ① miniQMT 查询接口流控/延迟实测(定对账轮询频率)
  ③ on_disconnected 回调注册是否成功(触发条件需断网实测,此处只验证机制)
  附:xtdata 行情通道是否可用(get_full_tick)

不下单,因此 ② order_remark 回传 与 ④ 集合竞价预埋行为 不在本脚本范围。

用法: python tools/spike_qmt_p0.py
前提: miniQMT 已登录运行; 环境变量 VERA_QMT_ACCOUNT / VERA_QMT_PATH 已设置
(审计M13修复: 真实账号不进 git, 一律走环境变量)。
"""
import os
import sys
import time

SESSION_ID = int(time.time()) % 2_000_000_000
QMT_PATH = os.environ.get("VERA_QMT_PATH", "")
ACCOUNT_ID = os.environ.get("VERA_QMT_ACCOUNT", "")

if not ACCOUNT_ID or not QMT_PATH:
    print("请先设置环境变量 (审计M13: 账号不进 git):")
    print('  set VERA_QMT_ACCOUNT=你的资金账号')
    print('  set VERA_QMT_PATH=D:\\path\\to\\userdata_mini')
    sys.exit(1)


def main() -> int:
    from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
    from xtquant.xttype import StockAccount

    events = []

    class CB(XtQuantTraderCallback):
        def on_connected(self):
            events.append("on_connected")

        def on_disconnected(self):
            events.append("on_disconnected")

        def on_stock_order(self, order):
            events.append(f"on_stock_order:{order}")

        def on_stock_trade(self, trade):
            events.append(f"on_stock_trade:{trade}")

        def on_order_error(self, err):
            events.append(f"on_order_error:{err}")

        def on_account_status(self, status):
            events.append(f"on_account_status:{getattr(status, 'status', status)}")

    trader = XtQuantTrader(QMT_PATH, SESSION_ID)
    trader.register_callback(CB())

    print(f"[1] start() session={SESSION_ID}")
    trader.start()

    print("[2] connect() ...")
    rc = trader.connect()
    print(f"    connect 返回: {rc}  (0=成功, -1=失败)")
    if rc != 0:
        print("连接失败:请确认 miniQMT 已登录且路径正确")
        return 1

    acc = StockAccount(ACCOUNT_ID)
    print(f"[3] subscribe({ACCOUNT_ID})")
    sub_rc = trader.subscribe(acc)
    print(f"    subscribe 返回: {sub_rc}  (0=成功)")

    print("[4] query_stock_asset()")
    asset = trader.query_stock_asset(acc)
    if asset:
        print(f"    总资产={asset.total_asset}  现金={asset.cash}  冻结={asset.frozen_cash}  市值={asset.market_value}")
    else:
        print("    返回 None(账户号可能不对或未订阅成功)")

    print("[5] query_stock_positions()")
    positions = trader.query_stock_positions(acc)
    print(f"    持仓 {len(positions)} 只")
    for p in positions:
        print(f"    {p.stock_code}  总量={p.volume}  可用={p.can_use_volume}  成本={p.avg_price}  市值={p.market_value}")

    print("[6] query_stock_orders() 当日委托")
    orders = trader.query_stock_orders(acc)
    print(f"    {len(orders)} 笔")
    for o in orders[:5]:
        print(f"    id={o.order_id} {o.stock_code} 状态={o.order_status} 价={o.price} 量={o.order_volume} remark={getattr(o, 'order_remark', '<无字段>')!r}")

    print("[7] query_stock_trades() 当日成交")
    trades = trader.query_stock_trades(acc)
    print(f"    {len(trades)} 笔")

    # ① 查询延迟/流控实测(本地 IPC,不打券商网络)
    print("[8] 查询延迟实测:连续 20 次 query_stock_positions")
    t0 = time.perf_counter()
    for _ in range(20):
        trader.query_stock_positions(acc)
    dt = (time.perf_counter() - t0) / 20
    print(f"    平均 {dt*1000:.1f} ms/次  → 对账轮询频率建议:≥{max(1, int(10*dt))}s 间隔无压力")

    # 行情通道
    print("[9] xtdata 行情通道 get_full_tick")
    try:
        from xtquant import xtdata
        tick = xtdata.get_full_tick(["600519.SH", "000001.SZ"])
        for code, t in tick.items():
            print(f"    {code}  last={t.get('lastPrice')}  bid1={t.get('bidPrice', [None])[0]}  ask1={t.get('askPrice', [None])[0]}")
    except Exception as e:  # noqa: BLE001 - spike 脚本,打印一切
        print(f"    xtdata 失败: {e}")

    print(f"[10] 回调事件: {events if events else '(无,正常——无下单无断线)'}")
    print("\nspike 完成。② order_remark 回传、④ 集合竞价预埋 需真实下单,待用户确认后另行验证。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
