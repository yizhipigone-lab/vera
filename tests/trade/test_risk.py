"""KillSwitch / RiskGate 单元测试 (P1 地基, 2026-07-26).

锁住: 急停三重态任一即激活、deactivate 清三源、文件异常 fail-safe、
MVP 5 道闸各自拒绝路径、每道拒绝写 audit。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.book import DIRECTION_BUY, DIRECTION_SELL, PositionView
from trade.config import PositionSizingConfig
from trade.risk import KillSwitch, OrderIntent, RiskContext, RiskGate
from trade.store import TradeStore


@pytest.fixture()
def store(tmp_path):
    s = TradeStore(tmp_path / "trade.db", tmp_path / "raw.jsonl")
    yield s
    s.close()


@pytest.fixture()
def kill(store, tmp_path):
    return KillSwitch(store, tmp_path / "KILL")


@pytest.fixture()
def gate(kill, store):
    """不挂 sizing 的闸 (各闸用例聚焦自身, 不被 sizing 前置拦截)。sizing
    有独立 fixture 与用例 (裁决②: 集中度闸已砍, sizing 接买入校验)。"""
    return RiskGate(kill, store, daily_loss_limit=0.05)


@pytest.fixture()
def sizing_gate(kill, store):
    return RiskGate(kill, store, daily_loss_limit=0.05,
                    sizing=PositionSizingConfig(
                        min_buy_amount=2000.0, max_buy_amount=20000.0,
                        lot_size=100, max_positions=2))


def _ctx(**over):
    """默认一个"世界一切正常"的风控上下文, 各测试按需破坏一项。"""
    base = dict(
        reconcile_passed=True,
        total_asset=1_000_000.0,
        positions={},
        day_baseline_equity=1_000_000.0,
        current_equity=1_000_000.0,
        is_trading_day=True,
    )
    base.update(over)
    return RiskContext(**base)


def _buy(code="600519.SH", price=10.0, qty=1000):
    # qty=1000: amount 10000 ∈ sizing 合法区, 各闸用例不被 sizing 误拦
    return OrderIntent(code=code, direction=DIRECTION_BUY, price=price, qty=qty)


def _sell(code="600519.SH", price=10.0, qty=100):
    return OrderIntent(code=code, direction=DIRECTION_SELL, price=price, qty=qty)


# ═══════════════════════════════════════════════════════════════
# 急停三重态
# ═══════════════════════════════════════════════════════════════

def test_kill_inactive_by_default(kill):
    assert not kill.is_active()


def test_kill_db_and_file_sources_activate_without_memory(kill, tmp_path):
    """activate 三源全落; 进程失忆 (内存清掉) 后 DB+文件仍生效。"""
    kill.activate("人工按钮")
    assert kill.is_active()
    # 三源都已落: 单独把内存清掉 (模拟重启后进程失忆), DB+文件仍在
    kill._memory = False
    assert kill.is_active()


def test_kill_db_read_error_failsafe_active(kill, store):
    """审计L4修复(补测试): kill_flag 表损坏 → DB 读异常 →
    fail-safe 视为激活 (DB 读不出来 ≠ 没激活)。"""
    store._conn.execute("DROP TABLE kill_flag")
    store._conn.commit()
    assert kill.is_active()


def test_kill_db_source_alone_activates(store, tmp_path):
    """另一个进程写的 DB 标志, 本进程重启后能读到。"""
    store.set_kill_flag(True, "另一进程")
    kill = KillSwitch(store, tmp_path / "KILL")
    assert kill.is_active()


def test_kill_file_source_alone_activates(store, tmp_path):
    """手工放的 KILL 文件 (连 DB 都没有) 也生效。"""
    (tmp_path / "KILL").write_text('{"active": true}', encoding="utf-8")
    kill = KillSwitch(store, tmp_path / "KILL")
    assert kill.is_active()


def test_deactivate_clears_all_three(kill, tmp_path):
    kill.activate("人工按钮")
    kill.deactivate()
    assert not kill.is_active()
    assert not (tmp_path / "KILL").exists()
    assert not kill._store.get_kill_flag()["active"]


def test_corrupt_flag_file_failsafe_active(store, tmp_path):
    """文件损坏 = 状态未知, fail-safe 视为激活 (宁可误拒不可漏放)。"""
    (tmp_path / "KILL").write_text("not-json{{{", encoding="utf-8")
    kill = KillSwitch(store, tmp_path / "KILL")
    assert kill.is_active()


# ═══════════════════════════════════════════════════════════════
# 风控闸门 (裁决②做减法后: 急停/启动对账/日亏/T+1 + sizing 买入校验)
# ═══════════════════════════════════════════════════════════════

def test_gate1_kill_switch_rejects_all(kill, gate):
    """闸1: 急停激活, 买卖全拒。"""
    kill.activate("测试")
    ok, reason = gate.check(_buy(), _ctx())
    assert not ok and "kill_switch" in reason
    ok, _ = gate.check(_sell(), _ctx(positions={
        "600519.SH": PositionView("600519.SH", 200, 200, 9.0)}))
    assert not ok


def test_gate2_reconcile_not_passed_rejects(gate):
    """闸2: 启动对账未通过, 买单拒。"""
    ok, reason = gate.check(_buy(), _ctx(reconcile_passed=False))
    assert not ok and "reconcile" in reason


def test_gate2_reconcile_not_passed_allows_sell(gate):
    """审计L8修复(产品裁决): 闸 2 只拦买单 —— 卖出是降风险动作,
    对账未通过仍放行 (执行链路有 QMT 实时 can_use 双校验兜底)。"""
    ctx = _ctx(reconcile_passed=False, positions={
        "600519.SH": PositionView("600519.SH", 200, 200, 9.0)})
    ok, _ = gate.check(_sell(), ctx)
    assert ok


def test_sizing_rejects_odd_lot(sizing_gate):
    """sizing: 非整手拒。"""
    ok, reason = sizing_gate.check(_buy(qty=150), _ctx())
    assert not ok and "sizing" in reason and "整手" in reason


def test_sizing_rejects_below_min_amount(sizing_gate):
    """sizing: 金额 1000 < 下限 2000, 拒。"""
    ok, reason = sizing_gate.check(_buy(qty=100), _ctx())
    assert not ok and "低于下限" in reason


def test_sizing_rejects_above_max_amount(sizing_gate):
    """sizing: 金额 30000 > 上限 20000, 拒。"""
    ok, reason = sizing_gate.check(_buy(qty=3000), _ctx())
    assert not ok and "高于上限" in reason


def test_sizing_rejects_max_positions(sizing_gate):
    """sizing: 新票持仓数达上限, 拒; 加仓已有票不受限。"""
    ctx = _ctx(positions={
        "600519.SH": PositionView("600519.SH", 100, 100, 9.0),
        "000001.SZ": PositionView("000001.SZ", 100, 100, 9.0),
    })
    ok, reason = sizing_gate.check(_buy(code="300750.SZ"), ctx)
    assert not ok and "上限" in reason
    ok, _ = sizing_gate.check(_buy(code="600519.SH"), ctx)  # 加仓已有票
    assert ok


def test_sizing_passes_legal_buy(sizing_gate):
    ok, _ = sizing_gate.check(_buy(), _ctx())
    assert ok


def test_gate4_daily_loss_breached_rejects_buy(gate):
    """日亏: 当前权益跌破基准 ×(1-5%), 禁买。"""
    ok, reason = gate.check(_buy(), _ctx(current_equity=940_000.0))
    assert not ok and "daily_loss" in reason


def test_gate4_missing_baseline_failsafe_rejects_buy(gate):
    """日亏: 盘前基准缺失 → fail-safe 禁买。"""
    ok, reason = gate.check(_buy(), _ctx(day_baseline_equity=None))
    assert not ok and "基准缺失" in reason


def test_gate5_sell_without_can_use_rejects(gate):
    """闸5: 无持仓/可用不足, 卖单拒。"""
    ok, reason = gate.check(_sell(), _ctx())
    assert not ok and "t1_sellable" in reason
    ctx = _ctx(positions={"600519.SH": PositionView("600519.SH", 200, 50, 9.0)})
    ok, _ = gate.check(_sell(qty=100), ctx)
    assert not ok


def test_gate5_non_trading_day_rejects_sell(gate):
    """闸5 双校验之交易日历: 非交易日卖单拒。"""
    ctx = _ctx(is_trading_day=False,
               positions={"600519.SH": PositionView("600519.SH", 200, 200, 9.0)})
    ok, reason = gate.check(_sell(), ctx)
    assert not ok and "非交易日" in reason


def test_sell_passes_all_gates(gate):
    """正常卖单放行。"""
    ctx = _ctx(positions={"600519.SH": PositionView("600519.SH", 200, 200, 9.0)})
    ok, reason = gate.check(_sell(), ctx)
    assert ok and reason == ""


# ═══════════════════════════════════════════════════════════════
# audit 落库
# ═══════════════════════════════════════════════════════════════

def test_every_rejection_writes_audit(gate, store):
    """每道拒绝都必须写 audit (盘后追责的依据)。"""
    gate.check(_buy(), _ctx(reconcile_passed=False))
    gate.check(_buy(), _ctx(day_baseline_equity=None))
    gate.check(_sell(), _ctx())
    rows = store._conn.execute(
        "SELECT kind, message, detail_json FROM audit ORDER BY id"
    ).fetchall()
    assert len(rows) == 3
    assert all(r[0] == "risk_reject" for r in rows)
    assert "reconcile" in rows[0][1]
    assert "daily_loss" in rows[1][1]
    assert "t1_sellable" in rows[2][1]
