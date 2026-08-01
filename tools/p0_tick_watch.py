"""P0 盘中验证: RealGateway tick 推送接线 (审计H5修复后真机验证, 2026-07-26)。

用法 (盘中跑, 集合竞价/连续竞价时段最佳):
    set VERA_QMT_ACCOUNT=你的资金账号
    set VERA_QMT_PATH=D:\\QMT\\userdata_mini
    python tools/p0_tick_watch.py [秒数, 默认 60]

验证点 (对照修复包 B 任务 1):
    1. subscribe_quotes 后 60s 内能持续收到 quote 事件 (订阅腿不是哑的);
    2. quote 字段与 query_quotes 同构 (last/bid1/ask1/high/low/prev_close/ts);
    3. ts 新鲜 (与本地时钟偏差应 < 数秒 —— monitor 陈旧判定的上游);
    4. unsubscribe_all 后事件停止。

账号/路径走环境变量 (审计M13教训: 真实账号不进 git)。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade.gateway import RealGateway  # noqa: E402


def main() -> None:
    account = os.environ.get("VERA_QMT_ACCOUNT", "")
    qmt_path = os.environ.get("VERA_QMT_PATH", "")
    watch_sec = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    if not account:
        print("请先 set VERA_QMT_ACCOUNT=资金账号 (VERA_QMT_PATH 可选)")
        sys.exit(1)

    events: list[tuple[float, str, dict]] = []

    gw = RealGateway(
        account_id=account, mini_qmt_path=qmt_path,
        on_quote=lambda code, q: events.append((time.time(), code, q)),
        on_disconnected=lambda reason: print(f"[断线] {reason}"),
    )
    print("连接 QMT ...")
    gw.connect()
    print("已连接, 查询持仓 ...")
    positions = gw.query_positions()
    codes = [p["code"] for p in positions] + ["000001.SZ"]
    print(f"订阅 {codes} ...")
    gw.subscribe_quotes(codes)

    print(f"观察 {watch_sec}s (盘中才有推送, 收盘后可能静默) ...")
    time.sleep(watch_sec)

    print(f"\n收到 quote 事件: {len(events)} 条")
    by_code: dict[str, int] = {}
    for _, code, _ in events:
        by_code[code] = by_code.get(code, 0) + 1
    for code, n in sorted(by_code.items()):
        print(f"  {code}: {n} 条")
    if events:
        recv_ts, code, q = events[-1]
        print(f"\n最新样例 [{code}]:")
        for k, v in q.items():
            print(f"  {k}: {v}")
        drift = recv_ts - q["ts"]
        print(f"  ts 漂移: {drift:+.1f}s (接收时刻 - tick 自带 ts)")
        print("\n轮询同构对照 (query_quotes 键集合应与上面一致):")
        polled = gw.query_quotes([code]).get(code, {})
        print(f"  订阅键={sorted(q.keys())}")
        print(f"  轮询键={sorted(polled.keys())}")
        print(f"  同构: {set(q.keys()) == set(polled.keys())}")
    else:
        print("!! 零事件 — 若在盘中, 订阅腿仍有问题; 若在收盘后, 属预期静默")

    print("\n退订 ...")
    gw.unsubscribe_all()
    n_before = len(events)
    time.sleep(5)
    print(f"退订后 5s 新增事件: {len(events) - n_before} 条 (应为 0)")
    gw.disconnect()
    print("完成。")


if __name__ == "__main__":
    main()
