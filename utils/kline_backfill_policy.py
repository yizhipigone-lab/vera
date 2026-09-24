# -*- coding: utf-8 -*-
"""utils/kline_backfill_policy.py — K 线回填的失败治理纯函数 (2026-09-05 体检 P1)。

背景: 8/24~9/4 refresh.log 累计 7250 条 "获取K线数据失败: 未知错误" —
 ① 有旧缓存的公司尾段延伸拉不到时被当成成功 (manifest 存在即算), 永不进
    no_data → 每轮调度刷新重复打同一批失败;
 ② 源站整体故障时 (8/26 03:20 一轮 5303 次) 无熔断, 硬刷到底。
本模块只放可单测的判定逻辑, 主循环在 tools/backfill_kline_cache.py 消费。

两个机制:
 - stall 冷却: 尾段/前段延伸失败 ≠ "整段无数据" (那是 no_data), 记 {code:
   ts}, 冷却期内跳过重试, 既停重复噪音又保留后续重试机会。
 - 熔断: 最近 _BREAK_MIN_TRIED 个有进展判定里失败占比 ≥ _BREAK_FAIL_RATIO
   → 判定源站整体故障, 提前中止本轮。
"""
from __future__ import annotations

import datetime as dt

# 延伸失败冷却时长: 源站小故障/停牌 72h 后自动再试一次; --retry-no-data 强制
STALL_COOLDOWN = dt.timedelta(hours=72)
# 熔断阈值: 最近 80 次有判定里 ≥60% 失败 → 中止 (避免 5303 式硬刷)
BREAK_MIN_TRIED = 80
BREAK_FAIL_RATIO = 0.6


def manifest_advanced(before: tuple | None, after: tuple | None) -> bool:
    """一次拉取后 manifest 是否真的前进过 (first 变早 / last 变晚 / 新建档)。

    before/after 为 _manifest_get 的原始行 (None=无记录); after 与 before
    比较只看 first_date/last_date 两位 (兼容 record[2:] 为其它元数据)。
    """
    if before is None:
        return after is not None
    if after is None:
        return False
    return str(after[0]) < str(before[0]) or str(after[1]) > str(before[1])


def stall_due(entry: dict | None, now: dt.datetime | None = None) -> bool:
    """stall 记录 (json 反序列化后的 dict) 是否已过冷却期、值得再试。

    无记录 / ts 缺失 / 已超冷却 → True (再试); 冷却期内 → False (跳过)。
    """
    if not entry:
        return True
    ts = entry.get("ts")
    if not ts:
        return True
    try:
        recorded = dt.datetime.fromisoformat(str(ts))
    except ValueError:
        return True
    now = now or dt.datetime.now()
    return (now - recorded) > STALL_COOLDOWN


def breaker_tripped(window: list[bool], min_tried: int = BREAK_MIN_TRIED,
                    ratio: float = BREAK_FAIL_RATIO) -> bool:
    """最近 window 长度个"有判定"结果中失败占比过高 → 熔断。

    window 元素 True=失败 / False=成功; 判定样本不足 min_tried 不熔断
    (避免刚起步遇一串次新股误伤)。"""
    if len(window) < min_tried:
        return False
    return sum(1 for x in window if x) / len(window) >= ratio
