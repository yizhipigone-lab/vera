"""trade/analysis.py 纯函数单测 (2026-08-19 深模块治理)。

按计划书 Task 3: 先写失败测试 (模块尚不存在 → ModuleNotFoundError),
再写实现, 最后确认 PASS。
"""
import datetime

import pytest

from trade.analysis import calc_drawdowns, deep_merge, diff_dicts, hold_days
from trade.book import DIRECTION_BUY, DIRECTION_SELL


def test_calc_drawdowns():
    """从净值序列算逐日回撤 (peak 为历史最高)。"""
    assert calc_drawdowns([100.0, 110.0, 99.0]) == pytest.approx(
        [0.0, 0.0, 99.0 / 110.0 - 1.0])
    assert calc_drawdowns([]) == []
    assert calc_drawdowns([100.0]) == [0.0]


def test_diff_dicts_returns_dotted_paths():
    old = {"a": 1, "b": {"c": 2, "d": 3}, "e": 5}
    new = {"a": 1, "b": {"c": 2, "d": 4}, "f": 6}
    changed = diff_dicts(old, new)
    assert "b.d" in changed
    assert "e" in changed and "f" in changed
    assert "a" not in changed and "b.c" not in changed


def test_deep_merge_replaces_non_dict():
    base = {"a": 1, "b": {"c": 2}, "levels": [1, 2]}
    override = {"b": {"d": 3}, "levels": [9]}
    out = deep_merge(base, override)
    assert out["a"] == 1
    assert out["b"] == {"c": 2, "d": 3}   # dict 递归合并
    assert out["levels"] == [9]           # 非 dict 整体替换


def test_hold_days_with_fixed_calendar(monkeypatch):
    import trade.analysis as an
    # 日历 = 8/10(一)、8/11(二)、8/12(三) 三天
    monkeypatch.setattr(an, "_trading_days",
                        lambda: ["20260810", "20260811", "20260812"])
    entry = datetime.datetime(2026, 8, 10).timestamp()
    # 8/10 买入, 算到 8/12 → 2 天 (8/11, 8/12)
    assert hold_days(entry, datetime.datetime(2026, 8, 12).timestamp()) == 2
    assert hold_days(None) is None


# ---------- entry_and_closed: 已实现盈亏% 分母口径 (159290 事件) ----------


def _mk_store(tmp_path):
    from trade.store import TradeStore
    return TradeStore(str(tmp_path / "t.db"), str(tmp_path / "r.jsonl"))


def _rec(tid, code, direction, price, qty, ts, pnl=0.0):
    return {"traded_id": tid, "order_id": "0", "code": code,
            "direction": direction, "price": price, "qty": qty,
            "amount": round(price * qty, 2), "ts": ts, "pnl_amount": pnl}


def test_entry_and_closed_legacy_position_pct(tmp_path):
    """159290 事件 (2026-08-27): 表内买入只有两笔 100 股测试单 (222.6 元),
    08-27 手工卖出 229,200 股遗产仓 —— 买入早于系统上线, 成本来自 QMT
    灌仓 (≈1.3672), 不在 trades 表内。

    已实现盈亏% 的分母必须是"被卖股票的买入成本" (卖出额-盈亏, ≈31.3 万),
    不是表内买入额 (222.6 元)。修复前显示 -20925.22%, 修复后 ≈ -14.86%。
    数量/成本列同步改用被平仓口径 (229,200 股 @ ≈1.3672), 行内数字自洽。
    """
    from trade.analysis import entry_and_closed
    store = _mk_store(tmp_path)
    d1 = 1785131426.0          # 2026-07-27 13:50
    store.save_trade(_rec("L-S0a", "159290.SZ", DIRECTION_SELL, 1.11, 100, d1))
    store.save_trade(_rec("L-B1", "159290.SZ", DIRECTION_BUY, 1.113, 100, d1 + 1))
    store.save_trade(_rec("L-S0b", "159290.SZ", DIRECTION_SELL, 1.11, 100, d1 + 2))
    store.save_trade(_rec("L-B2", "159290.SZ", DIRECTION_BUY, 1.113, 100, d1 + 3))
    cost = 1.367226            # QMT 灌仓成本 (由真实库 pnl_pct=-14.86 反推)
    qtys = (48300, 32400, 9200, 100000, 2500, 36800)   # 08-27 六笔卖出
    d2 = 1787813031.0          # 2026-08-27 14:44
    for i, q in enumerate(qtys):
        store.save_trade(_rec(f"L-S{i}", "159290.SZ", DIRECTION_SELL,
                              1.164, q, d2 + i,
                              pnl=round((1.164 - cost) * q, 2)))
    _, closed, summary = entry_and_closed(store)
    s = summary["159290.SZ"]
    assert s["is_closed"]
    expected_pnl = sum(round((1.164 - cost) * q, 2) for q in qtys)
    expected_basis = sum(round(1.164 * q, 2) for q in qtys) - expected_pnl
    assert s["realized_pnl"] == pytest.approx(expected_pnl, abs=0.01)
    assert s["realized_pnl_pct"] == pytest.approx(
        expected_pnl / expected_basis * 100, abs=0.005)
    assert -14.87 < s["realized_pnl_pct"] < -14.85   # 人话: 约 -14.86%
    # 行内自洽: 数量=被平仓股数, 成本=被平仓股的买入成本均价
    assert s["closed_qty"] == 229200
    assert s["cost_avg"] == pytest.approx(cost, abs=1e-4)
    c = next(x for x in closed if x["code"] == "159290.SZ")
    assert c["qty"] == 229200
    assert c["buy_avg"] == pytest.approx(cost, abs=1e-4)


def test_entry_and_closed_in_table_pct_unchanged(tmp_path):
    """对照组: 全程表内的普通平仓, 新旧分母恒等 (卖出成本基数 = 表内买入额,
    账本移动加权成本守恒), 数值必须不变; closed_qty/cost_avg 退化为买入口径。"""
    from trade.analysis import entry_and_closed
    store = _mk_store(tmp_path)
    ts = 1787813031.0
    store.save_trade(_rec("N-B1", "600519.SH", DIRECTION_BUY, 10.0, 1000, ts))
    store.save_trade(_rec("N-S1", "600519.SH", DIRECTION_SELL, 12.0, 1000,
                          ts + 1, pnl=2000.0))
    _, closed, summary = entry_and_closed(store)
    s = summary["600519.SH"]
    assert s["is_closed"]
    assert s["realized_pnl"] == pytest.approx(2000.0)
    assert s["realized_pnl_pct"] == pytest.approx(20.0)   # 与旧口径一致
    assert s["closed_qty"] == 1000
    assert s["cost_avg"] == pytest.approx(10.0)
    c = next(x for x in closed if x["code"] == "600519.SH")
    assert c["qty"] == 1000
    assert c["buy_avg"] == pytest.approx(10.0)


def test_entry_and_closed_historical_no_pnl_keeps_none(tmp_path):
    """全历史行 (pnl 列=0, 2026-08-07 前) 的平仓: 无成本基数可算,
    pct 维持 None, closed_qty/cost_avg 回退买入口径 (realized_pnl=0.0
    是 is_closed 分支的既有行为, 本次不动)。"""
    from trade.analysis import entry_and_closed
    store = _mk_store(tmp_path)
    ts = 1785131426.0
    store.save_trade(_rec("H-B1", "000001.SZ", DIRECTION_BUY, 10.0, 100, ts))
    store.save_trade(_rec("H-S1", "000001.SZ", DIRECTION_SELL, 12.0, 100, ts + 1))
    _, closed, summary = entry_and_closed(store)
    s = summary["000001.SZ"]
    assert s["is_closed"]
    assert s["realized_pnl"] == 0.0
    assert s["realized_pnl_pct"] is None
    assert s["closed_qty"] == 100
    assert s["cost_avg"] is None
