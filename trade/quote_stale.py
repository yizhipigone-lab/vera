"""trade/quote_stale.py — 行情陈旧判定 (fail-closed 单一真相源)。

monitor / executor / rotation 三处原本各自写一套"行情是否陈旧"判定,
口径曾分歧 (见 docs/plan/2026-08-16_深模块接口债治理_计划书.md P2-2):
  ① ts 缺失语义 —— executor 判"不陈旧"继续卖 (fail-open), rotation 判陈旧;
  ② tick_ts_missing 标志 —— monitor/rotation 查, executor 不查。
本模块是唯一判定: 无 ts 键 / ts 为 None / tick_ts_missing / 超时,
任一成立一律判陈旧 (宁可不卖, 不可瞎卖)。三处只保留"判陈旧后做什么"
(monitor 跳过+审计 / executor fail-closed 不卖 / rotation 不调仓),
判定口径不再各自漂移。

独立小模块, 不 import monitor/executor/rotation, 避免三者互相 import
成环 —— 三者都只依赖本模块, 单向无环。
"""

from __future__ import annotations

# 判定原因码 (供调用方拼审计文案/日志; 不拼自然语言, 文案由调用方自定)
REASON_TICK_TS_MISSING = "tick_ts_missing"  # tick 原本没带时间戳 (on_quote 回填过 ts)
REASON_NO_TS = "no_ts"                      # 快照没有 ts 键
REASON_TS_NONE = "ts_none"                  # ts 值为 None (或不可解析)
REASON_STALE = "stale"                      # 距上次行情超过 stale_sec


def is_quote_stale(quote: dict, clock: float, stale_sec: float) -> tuple[bool, str]:
    """(是否陈旧, 原因)。fail-closed: 任一异常一律判陈旧。

    返回 (stale: bool, reason: str); 不陈旧时 reason 为 ""。

    判定顺序有意为之:
    - tick_ts_missing 必须最先查 —— monitor.on_quote 会把 ts 回填成
      event_ts/本地时钟 (数值上看着新鲜), 只有该标记能还原"tick 原本
      没带时间戳"这一事实, 迟于 ts 数值检查就会漏判;
    - 之后才查 ts 键缺失 / ts 为 None / 超时, 覆盖 executor 拿到的裸快照
      (未走 monitor 回填) 与轮询/测试裸 dict。
    """
    if not isinstance(quote, dict):
        return True, REASON_NO_TS
    if quote.get("tick_ts_missing"):
        return True, REASON_TICK_TS_MISSING
    if "ts" not in quote:
        return True, REASON_NO_TS
    ts = quote.get("ts")
    if ts is None:
        return True, REASON_TS_NONE
    try:
        age = clock - float(ts)
    except (TypeError, ValueError):
        # ts 不是可解析数值: 宁可判陈旧 (fail-closed), 不按"0 岁"放行
        return True, REASON_TS_NONE
    if age > stale_sec:
        return True, REASON_STALE
    return False, ""
