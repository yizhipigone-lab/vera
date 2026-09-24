# -*- coding: utf-8 -*-
"""scheduler/health.py — 调度器存活判读 (2026-09-20)。

**只回答一个问题**：调度器现在*应该*活着吗？*实际*活着吗？

三态（这是本模块存在的全部理由 —— 少任何一态都会误报）：

| 状态 | 判据 | 含义 |
|---|---|---|
| `running` | 心跳新鲜 | 正常 |
| `stopped` | 存在人工停止标记 | 你主动 `stop_vera.bat` 停的，**不该报警** |
| `down`    | 无标记 且 心跳过期/缺失 | **这才是要你去看一眼的情况** |

为什么必须分 `stopped` 与 `down`：没有停止标记时，每次你正常停机，页面都会
喊"调度器死了" —— 假警报多了就没人看了，等于没做。**可见的前提是不误报。**

背景（2026-09-20 调查）：调度器至少两个静默停机窗口（09-11~09-14、09-19 至今），
无服务、无自启、无看护；`stop_vera.bat` 用 `taskkill /WINDOWTITLE /F` 强杀，
不留任何痕迹。方案经用户 2026-09-20 拍板：**可见优先于自愈** ——
先让"停了"看得见，不赌死因（强杀 vs 误关窗的正确反应是相反的）。

铁律 1：本模块不 import trade，不碰 data/trade/。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from utils.sysutil import project_root

# 心跳由 `scheduler/vera_scheduler.VeraScheduler._write_heartbeat` 每 ~30s 覆写
HEARTBEAT_PATH = project_root() / "data" / "scheduler_heartbeat.json"
# 人工停止标记：`stop_vera.bat` 创建、`start_vera.bat` 删除
STOP_MARKER_PATH = project_root() / "data" / ".vera_stopped"

# 判活阈值必须**显著大于**心跳写间隔（30s）：取 5 分钟，
# 容忍几次写盘失败与系统繁忙，又不至于让"停机"藏太久。
STALE_AFTER_S = 300.0


def read_heartbeat(path: Path | str | None = None) -> dict | None:
    """读心跳。不存在/损坏返 None（fail-soft，不抛）。"""
    p = Path(path) if path else HEARTBEAT_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def stopped_on_purpose(marker: Path | str | None = None) -> bool:
    """是否存在人工停止标记。"""
    p = Path(marker) if marker else STOP_MARKER_PATH
    try:
        return p.exists()
    except Exception:
        return False


def status(*, heartbeat_path: Path | str | None = None,
           marker_path: Path | str | None = None,
           now: float | None = None,
           stale_after_s: float = STALE_AFTER_S) -> dict:
    """调度器存活状态（三态）。纯读，fail-soft，永不抛。

    返 {state, age_s, heartbeat:{iso,pid,jobs}|None, note}
    state ∈ {"running", "stopped", "down"}
    """
    now_ts = now if now is not None else dt.datetime.now().timestamp()
    hb = read_heartbeat(heartbeat_path)
    if stopped_on_purpose(marker_path):
        return {"state": "stopped", "age_s": None, "heartbeat": None,
                "note": "已人工停止（存在停止标记）"}

    if not hb or not isinstance(hb.get("ts"), (int, float)):
        return {"state": "down", "age_s": None, "heartbeat": None,
                "note": "没有心跳文件 —— 调度器可能从未启动，或启动后立即退出"}

    age = max(0.0, now_ts - float(hb["ts"]))
    brief = {"iso": hb.get("iso"), "pid": hb.get("pid"), "jobs": hb.get("jobs")}
    if age > stale_after_s:
        return {"state": "down", "age_s": round(age, 1), "heartbeat": brief,
                "note": f"心跳已停 {age / 60:.1f} 分钟 —— 调度器很可能已经死了"}
    return {"state": "running", "age_s": round(age, 1), "heartbeat": brief,
            "note": ""}


# ── 交易进程看门狗判定 (2026-09-20 脆弱期 P0 修复 item 4) ──────────────────
#
# 看门狗 job 本体在 scheduler/__main__.py (1 分钟 interval, 仅交易日+交易时段);
# 本函数是**纯判定**, 只消费探测快照 dict, 不 import trade、不碰网络 ——
# 单测直接喂 dict。
#
# 三态防御 (逐行审查 P1-2): monitor_healthy 可为 None (非连续竞价, "不适用"),
# 绝不当 False 告警。last_tick_age_s 可为 None (滚动期 trade_main 未重启、
# 字段缺失), 此时回退到 monitor_healthy 判定。
#
# state 由调用方持有 (模块级 dict), 本函数原地更新它:
#   fails / last_alert_key / last_alert_ts / was_down

def evaluate_watchdog(snapshot: dict, state: dict, *, now: float,
                      cooldown_s: float = 900.0,
                      tick_stale_s: float = 120.0) -> dict:
    """看门狗纯判定。

    snapshot: {"trade_ok": bool, "web_ok": bool,
               "monitor_healthy": bool|None, "last_tick_age_s": float|None}
    返回 {"action": "none"|"alert"|"recover", "key": str, "reason": str}
    """
    fails: list[tuple[str, str]] = []
    if not snapshot.get("trade_ok"):
        fails.append(("trade_down", "交易进程不可达(8081)"))
    else:
        tick_age = snapshot.get("last_tick_age_s")
        if tick_age is not None:
            if float(tick_age) > tick_stale_s:
                fails.append(("quote_stale",
                              f"行情断流: 最后 tick 距今 {float(tick_age):.0f}s"))
        else:
            # 回退路径: 字段缺失 (滚动期) 只能用 monitor_healthy
            mh = snapshot.get("monitor_healthy")
            if mh is False:
                fails.append(("quote_unhealthy", "行情订阅不健康(降级轮询)"))
            # mh is None = 不适用, 跳过 (三态防御)
    if not snapshot.get("web_ok"):
        fails.append(("web_down", "Web 服务不可达(8080)"))

    if fails:
        state["fails"] = state.get("fails", 0) + 1
        key = "+".join(k for k, _ in fails)
        reason = "；".join(r for _, r in fails)
        if state["fails"] < 2:
            return {"action": "none", "key": key,
                    "reason": f"首次失败只计数: {reason}"}
        if (key == state.get("last_alert_key")
                and now - state.get("last_alert_ts", 0.0) < cooldown_s):
            return {"action": "none", "key": key, "reason": f"冷却中: {reason}"}
        state["last_alert_key"] = key
        state["last_alert_ts"] = now
        state["was_down"] = True
        return {"action": "alert", "key": key, "reason": reason}

    state["fails"] = 0
    if state.get("was_down"):
        state["was_down"] = False
        return {"action": "recover", "key": "", "reason": "交易/Web 服务已恢复"}
    return {"action": "none", "key": "", "reason": ""}
