"""Book / 订单状态机单元测试 (P1 地基, 2026-07-26).

锁住: 状态机合法/非法转换与终态不可逆、apply_trade 按 traded_id
幂等 (重复回报不双扣)、avg_cost 本地自算、snapshot 不可变。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.book import (
    DIRECTION_BUY,
    DIRECTION_SELL,
    OS_CANCELED,
    OS_PART_SUCC,
    OS_REPORTED,
    OS_SUCCEEDED,
    OS_UNREPORTED,
    TERMINAL_STATUSES,
    Book,
    transition,
)

# ═══════════════════════════════════════════════════════════════
# transition 状态机 (纯函数)
# ═══════════════════════════════════════════════════════════════

def test_terminal_status_set_locked():
    """终态集合 = {部撤53, 已撤54, 已成56, 废单57}, 锁死防漂移。"""
    assert frozenset({53, 54, 56, 57}) == TERMINAL_STATUSES


def test_transition_forward_path_valid():
    """常态推进 48→50→55→56 每步都合法。"""
    assert transition(OS_UNREPORTED, OS_REPORTED)
    assert transition(OS_REPORTED, OS_PART_SUCC)
    assert transition(OS_PART_SUCC, OS_SUCCEEDED)


def test_transition_terminal_irreversible():
    """终态 → 任何其他状态一律拒绝。"""
    for terminal in TERMINAL_STATUSES:
        assert not transition(terminal, OS_REPORTED)
        assert not transition(terminal, OS_PART_SUCC)


def test_transition_terminal_same_status_allowed():
    """终态 → 同态重复回报放行 (券商重复推送是常态, 幂等)。"""
    for terminal in TERMINAL_STATUSES:
        assert transition(terminal, terminal)


# ═══════════════════════════════════════════════════════════════
# apply_order_update
# ═══════════════════════════════════════════════════════════════

def test_apply_order_update_accepts_valid_path():
    book = Book()
    assert book.apply_order_update("O1", OS_REPORTED, code="600519.SH",
                                   direction=DIRECTION_BUY, price=10.0, qty=100)
    assert book.apply_order_update("O1", OS_SUCCEEDED, filled_qty=100)
    rec = book.snapshot()["orders"]["O1"]
    assert rec.status == OS_SUCCEEDED and rec.filled_qty == 100


def test_apply_order_update_rejects_terminal_reversal():
    """已撤之后再来"已报"回报必须被拒, 原状态不被污染。"""
    book = Book()
    book.apply_order_update("O1", OS_REPORTED, code="600519.SH",
                            direction=DIRECTION_BUY, price=10.0, qty=100)
    book.apply_order_update("O1", OS_CANCELED)
    assert not book.apply_order_update("O1", OS_REPORTED)
    assert book.snapshot()["orders"]["O1"].status == OS_CANCELED


# ═══════════════════════════════════════════════════════════════
# apply_trade 幂等 + avg_cost
# ═══════════════════════════════════════════════════════════════

def test_apply_trade_idempotent_by_traded_id():
    """同一 traded_id 重复回报: 第二次拒绝, 持仓不双扣。"""
    book = Book()
    kw = dict(traded_id="T1", order_id="O1", code="600519.SH",
              direction=DIRECTION_BUY, price=10.0, qty=100)
    assert book.apply_trade(**kw)
    assert not book.apply_trade(**kw)
    pos = book.snapshot()["positions"]["600519.SH"]
    assert pos.volume == 100  # 不是 200


def test_avg_cost_weighted_average():
    """两笔买入加权平均成本。"""
    book = Book()
    book.apply_trade("T1", "O1", "600519.SH", DIRECTION_BUY, 10.0, 100)
    book.apply_trade("T2", "O2", "600519.SH", DIRECTION_BUY, 20.0, 100)
    pos = book.snapshot()["positions"]["600519.SH"]
    assert pos.volume == 200
    assert pos.avg_cost == pytest.approx(15.0)


def test_sell_reduces_volume_keeps_cost_then_resets():
    """卖出减持仓、成本不变; 清仓后 avg_cost 归零。"""
    book = Book()
    book.apply_trade("T1", "O1", "600519.SH", DIRECTION_BUY, 10.0, 200)
    book.apply_trade("T2", "O2", "600519.SH", DIRECTION_SELL, 12.0, 100)
    pos = book.snapshot()["positions"]["600519.SH"]
    assert pos.volume == 100 and pos.avg_cost == pytest.approx(10.0)
    book.apply_trade("T3", "O3", "600519.SH", DIRECTION_SELL, 12.0, 100)
    pos = book.snapshot()["positions"]["600519.SH"]
    assert pos.volume == 0 and pos.avg_cost == 0.0


def test_buy_fill_does_not_add_can_use():
    """T+1: 当日买入不进可用 (可用数量真相由 QMT 对账回填)。"""
    book = Book()
    book.apply_trade("T1", "O1", "600519.SH", DIRECTION_BUY, 10.0, 100)
    pos = book.snapshot()["positions"]["600519.SH"]
    assert pos.volume == 100 and pos.can_use == 0


def test_apply_trade_advances_order_filled_qty():
    """成交回报同步推进订单已成交量, 满量置已成。"""
    book = Book()
    book.apply_order_update("O1", OS_REPORTED, code="600519.SH",
                            direction=DIRECTION_BUY, price=10.0, qty=200)
    book.apply_trade("T1", "O1", "600519.SH", DIRECTION_BUY, 10.0, 100)
    rec = book.snapshot()["orders"]["O1"]
    assert rec.filled_qty == 100 and rec.status == OS_PART_SUCC
    book.apply_trade("T2", "O1", "600519.SH", DIRECTION_BUY, 10.0, 100)
    rec = book.snapshot()["orders"]["O1"]
    assert rec.filled_qty == 200 and rec.status == OS_SUCCEEDED


# ═══════════════════════════════════════════════════════════════
# 档位状态 + snapshot 不可变
# ═══════════════════════════════════════════════════════════════

def test_tier_mark_query_restore():
    """审计C1修复: 档位标记按 (code, 日期) 二维 —— 当日独立, 昨日不混入。"""
    book = Book()
    book.mark_tier("600519.SH", 0, "20260726")
    book.mark_tier("600519.SH", 2, "20260726")
    assert book.tier_done("600519.SH", "20260726") == frozenset({0, 2})
    assert book.tier_done("600519.SH", "20260727") == frozenset()  # 次日独立
    assert book.tier_done("000001.SZ", "20260726") == frozenset()
    book.restore(tiers={("000001.SZ", "20260726"): [1]})
    assert book.tier_done("000001.SZ", "20260726") == frozenset({1})


def test_restore_seen_trades_prevents_double_count():
    """审计H4修复: 落库当日成交 → 新 Book 回填幂等集合 → 重推不双扣。"""
    book = Book()
    book.restore(seen_trades={"T1"})
    # 重启后 QMT 重推当日成交回报: 判重拒绝, 持仓不动
    assert not book.apply_trade("T1", "O1", "600519.SH", DIRECTION_BUY, 10.0, 100)
    assert "600519.SH" not in book.snapshot()["positions"]


def test_snapshot_is_immutable():
    """snapshot 的映射与记录都改不动, 读侧拿不到写后门。"""
    book = Book()
    book.apply_trade("T1", "O1", "600519.SH", DIRECTION_BUY, 10.0, 100)
    snap = book.snapshot()
    with pytest.raises(TypeError):
        snap["positions"] = {}
    with pytest.raises(TypeError):
        snap["positions"]["X"] = None
    with pytest.raises(Exception):
        snap["positions"]["600519.SH"].volume = 999  # frozen dataclass
