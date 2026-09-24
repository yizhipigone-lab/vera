"""trade/quote_stale.py 单元测试 (2026-08-16).

锁住 fail-closed 单一真相源: 无 ts 键 / ts 为 None / tick_ts_missing /
超时 任一即陈旧; 其余情况不陈旧。monitor/executor/rotation 三处共用,
判定口径不再各自漂移 (旧 executor 无 ts 判"不陈旧"是 fail-open 隐患)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.quote_stale import (
    REASON_NO_TS,
    REASON_STALE,
    REASON_TICK_TS_MISSING,
    REASON_TS_NONE,
    is_quote_stale,
)

CLOCK = 1000.0
STALE_SEC = 60.0


def test_fresh_quote_not_stale():
    assert is_quote_stale({"last": 10.8, "ts": CLOCK}, CLOCK, STALE_SEC) == (False, "")


def test_no_ts_key_is_stale():
    """无 ts 键 → 陈旧 (fail-closed; 旧 executor 在此处判"不陈旧"卖, fail-open)。"""
    assert is_quote_stale({"last": 10.8}, CLOCK, STALE_SEC) == (True, REASON_NO_TS)


def test_ts_none_is_stale():
    assert is_quote_stale({"last": 10.8, "ts": None}, CLOCK, STALE_SEC) \
        == (True, REASON_TS_NONE)


def test_ts_not_numeric_is_stale():
    assert is_quote_stale({"last": 10.8, "ts": "not-a-ts"}, CLOCK, STALE_SEC) \
        == (True, REASON_TS_NONE)


def test_tick_ts_missing_wins_over_fresh_backfill():
    """tick_ts_missing 优先于 ts 数值: on_quote 已把 ts 回填成新鲜值,
    只有该标记能还原"tick 原本没带时间戳"。"""
    q = {"last": 10.8, "ts": CLOCK, "tick_ts_missing": True}
    assert is_quote_stale(q, CLOCK, STALE_SEC) == (True, REASON_TICK_TS_MISSING)


def test_stale_beyond_threshold():
    q = {"last": 10.8, "ts": CLOCK - 120}
    assert is_quote_stale(q, CLOCK, STALE_SEC) == (True, REASON_STALE)


def test_at_threshold_not_stale():
    """恰好等于 stale_sec 不算陈旧 (边界: 只用严格 > )。"""
    q = {"last": 10.8, "ts": CLOCK - STALE_SEC}
    assert is_quote_stale(q, CLOCK, STALE_SEC) == (False, "")


def test_none_quote_is_stale():
    """rotation 的 _quote_stale 可能收到 None (无快照)。"""
    assert is_quote_stale(None, CLOCK, STALE_SEC) == (True, REASON_NO_TS)
