# -*- coding: utf-8 -*-
"""P0-③ 验证:on_disconnected 触发条件(交互式真机实验)。

流程: 循环等 QMT 上线 → 连接成功 → 挂监听 → 等用户断开 QMT →
记录 on_disconnected 是否触发、触发延迟。超时 10 分钟自动退出。

用法: python tools/p0_disconnect_watch.py
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trade.gateway import RealGateway

ACCOUNT = os.environ.get("VERA_QMT_ACCOUNT", "")
QMT_PATH = os.environ.get("VERA_QMT_PATH", "")
if not ACCOUNT or not QMT_PATH:
    print("请先设 VERA_QMT_ACCOUNT / VERA_QMT_PATH")
    sys.exit(1)

DEADLINE = time.time() + 600
events = []


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)


gw = RealGateway(
    ACCOUNT, mini_qmt_path=QMT_PATH,
    on_disconnected=lambda: events.append(("on_disconnected", time.time())),
)

# 1. 等 QMT 上线
log("等待 miniQMT 上线 (每 10s 重试, 总限 10 分钟) ...")
connected = False
while time.time() < DEADLINE and not connected:
    try:
        connected = gw.connect()
    except Exception as e:
        log(f"connect 异常: {e}")
    if not connected:
        time.sleep(10)
if not connected:
    log("超时未连上, 退出")
    sys.exit(1)
log("已连接 ✓  —— 现在请断开 miniQMT (关客户端或断网)")

# 2. 挂监听等断线
t0 = time.time()
while time.time() < DEADLINE:
    if events:
        dt = events[0][1] - t0
        log(f"*** on_disconnected 触发! 延迟 {dt:.1f}s ***")
        break
    time.sleep(2)
else:
    log("监听超时: on_disconnected 未触发")

# 3. 顺便验证: 断线状态下查询接口行为
try:
    ps = gw.query_positions()
    log(f"断线后 query_positions 返回 {len(ps)} 条 (本地缓存仍可读)")
except Exception as e:
    log(f"断线后 query_positions 异常: {type(e).__name__}: {e}")

log(f"事件清单: {events}")
print("完成", flush=True)
