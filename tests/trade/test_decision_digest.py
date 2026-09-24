"""`Monitor.daily_digest` 契约测试 (2026-09-18)。

这个方法是决策台账里「止盈止损那条腿今天为什么没卖」的唯一数据源。它守两条底线:

1. **只读** —— 永远不会下一笔单。卖出动作仍然是盘中 `scan_once` 的事;
2. **不重写判定** —— 命中与否一律复用盘中的 `_hit_cost_stop` / `_hit_trailing` /
   `_hit_ladder` / `_hit_time_stop` / `_hit_cond_time` / `_hit_first_day`,
   本方法只额外算「给人看的距离百分比」。

第 2 条是本方案最容易被写歪的地方: 一旦这里自己写一遍阈值判断, 改了阈值之后
就会出现「台账说没触发、规则其实已经触发了」—— 这是最伤信任的一类分叉, 而且
不会报错。下面用 monkeypatch 把 `_hit_*` 换掉来钉死「确实走的是那几个函数」。
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.book import DIRECTION_BUY, Book, PositionView, is_etf  # noqa: E402
from trade.config import (  # noqa: E402
    CostStopConfig, LadderTpConfig, StopConfig, TimeStopConfig,
    TradeConfig, TrailingStopConfig,
)
from trade.gateway import FakeGateway  # noqa: E402
from trade.monitor import Monitor  # noqa: E402
from trade.store import TradeStore  # noqa: E402

# 工作日盘中 (2024-01-02 周二 10:00), 与 test_monitor.py 同款时钟
_T0 = time.mktime(time.strptime("2024-01-02 10:00", "%Y-%m-%d %H:%M"))

CODE = "600519.SH"
ETF = "159949.SZ"   # 创业板50ETF — 默认配置里 ETF 不纳入自动管理


class StubExecutor:
    """记录调用的 executor 替身。`daily_digest` 期间必须**一次都不被调**。"""

    def __init__(self):
        self.exits: list[tuple] = []
        self.pending_calls = 0

    def execute_exit(self, code, reason, qty=None):
        self.exits.append((code, reason, qty))
        return True

    def pending_check(self, now_hhmm=None):
        self.pending_calls += 1


@pytest.fixture()
def store(tmp_path):
    s = TradeStore(tmp_path / "trade.db", tmp_path / "raw.jsonl")
    yield s
    s.close()


def _pos(code=CODE, volume=1000, avg_cost=10.0):
    return PositionView(code=code, volume=volume, can_use=volume, avg_cost=avg_cost)


def _make(store, stub=None, cfg=None, hold_days=1, peak=None, book=None):
    """造一个 monitor: 时钟固定盘中, 持仓天数/历史峰值可注入。"""
    stub = stub or StubExecutor()
    config = cfg or TradeConfig(
        account_id="TEST", tick_heartbeat_sec=15,
        stop=StopConfig(
            cost_stop=CostStopConfig(threshold=-0.12),
            trailing_stop=TrailingStopConfig(activation=0.035, drawdown=0.01),
            ladder_tp=LadderTpConfig(levels=((0.06, 0.30), (0.15, 0.30))),
            time_stop=TimeStopConfig(max_hold_days=20),
        ),
    )
    gw = FakeGateway()
    gw.connect()
    mon = Monitor(gw, book or Book(), stub, store, config,
                  hold_days=lambda code: hold_days,
                  peak_px=lambda code: peak,
                  clock=lambda: _T0)
    return mon, stub


def _feed(mon, code, last, high=None):
    """把行情灌进 monitor 的行情缓存 (与真实 tick 同一入口)。"""
    mon.on_quote(code, {"last": last, "high": high if high is not None else last})


def _only_trailing_cfg():
    """只留移动止盈、其余关掉的配置。

    为什么需要它: `_hit_ladder` 看的是**当日最高价**, 而测移动止盈时我们故意
    喂一个高点 (峰值要过激活线) —— 成本 10.0 时第 1 档在 10.6, 高点一到 11/12
    阶梯就先命中了, 挤掉移动止盈。要单独验移动止盈那条线, 就得把阶梯关掉。
    (这个"高点会先触发阶梯"本身就是真实行为, 另有 test_high_spike_triggers_ladder_*)
    """
    return TradeConfig(
        account_id="TEST",
        stop=StopConfig(
            cost_stop=CostStopConfig(enabled=False),
            trailing_stop=TrailingStopConfig(activation=0.035, drawdown=0.01),
            ladder_tp=LadderTpConfig(enabled=False),
            time_stop=TimeStopConfig(enabled=False)))


# ── 不产出任何行的情况 ──────────────────────────────────────────

def test_no_positions_returns_empty(store):
    mon, _ = _make(store)
    assert mon.daily_digest({}) == []


def test_zero_volume_is_skipped(store):
    """持仓量为 0 (已清仓的残留行) 不产生台账行 —— 没仓位的票谈不上"要不要卖"。"""
    mon, _ = _make(store)
    _feed(mon, CODE, 10.0)
    assert mon.daily_digest({CODE: _pos(volume=0)}) == []


def test_etf_is_skipped_when_excluded(store):
    """默认配置下 ETF 不纳入自动管理, 也就不给它编一句"没触发"。
    与盘中 `_evaluate` 的早退同口径 —— 两边不一致会让页面上出现一个
    系统根本没在管的票的"决策记录", 纯属误导。"""
    cfg = TradeConfig(account_id="TEST", exclude_etf=True)
    mon, _ = _make(store, cfg=cfg)
    assert is_etf(ETF), "测试前提: 这个代码得真的是 ETF"
    _feed(mon, ETF, 1.0)
    assert mon.daily_digest({ETF: _pos(code=ETF, avg_cost=1.0)}) == []


def test_etf_included_when_config_allows(store):
    """把 exclude_etf 关掉, ETF 就该有台账行 —— 说明上面那条跳过的确是配置驱动。"""
    cfg = TradeConfig(account_id="TEST", exclude_etf=False)
    mon, _ = _make(store, cfg=cfg)
    _feed(mon, ETF, 1.0)
    rows = mon.daily_digest({ETF: _pos(code=ETF, avg_cost=1.0)})
    assert len(rows) == 1


# ── 拿不到行情 ──────────────────────────────────────────────────

def test_empty_quote_cache_says_program_just_restarted(store):
    """行情缓存整个是空的 → 白话点明「程序刚重启过」。
    这两种"拿不到"的原因不一样, 混成一句用户会误以为数据源坏了。"""
    mon, _ = _make(store)
    rows = mon.daily_digest({CODE: _pos()})
    assert len(rows) == 1
    r = rows[0]
    assert (r["action"], r["reason_code"]) == ("FAIL", "EXIT_NO_QUOTE")
    assert "重启" in r["reason_text"]
    assert r["evidence"]["has_any_quote"] is False


def test_missing_code_in_quote_cache_says_not_found(store):
    """缓存里有别的票、唯独没有它 → 另一种说法, 且不说"重启"。"""
    mon, _ = _make(store)
    _feed(mon, "000001.SZ", 12.0)   # 缓存里有别的票
    rows = mon.daily_digest({CODE: _pos()})
    assert rows[0]["reason_code"] == "EXIT_NO_QUOTE"
    assert "重启" not in rows[0]["reason_text"]
    assert rows[0]["evidence"]["has_any_quote"] is True


def test_zero_price_treated_as_no_quote(store):
    """行情里 last=0 → 不能拿零价硬算距离 (会算出"距止损 -100%"这种鬼话)。"""
    mon, _ = _make(store)
    _feed(mon, CODE, 0.0)
    rows = mon.daily_digest({CODE: _pos()})
    assert rows[0]["reason_code"] == "EXIT_NO_QUOTE"


def test_no_quote_row_carries_no_distance_numbers(store):
    """拿不到行情的那行不该有任何"距离"数字 —— 有数字就等于在假装算过。"""
    mon, _ = _make(store)
    ev = mon.daily_digest({CODE: _pos()})[0]["evidence"]
    assert "cost_stop_price" not in ev
    assert "gap_to_cost_stop_pct" not in ev
    assert "trailing_stop_price" not in ev


# ── 规则命中但它还拿着仓 ────────────────────────────────────────

def test_cost_stop_hit_reports_arm_fail(store):
    """跌破硬止损线但还持仓 → FAIL + EXIT_ARM_FAIL, 白话里点名「触发的是硬止损」。"""
    mon, _ = _make(store)
    _feed(mon, CODE, 8.0)     # 成本 10.0, 阈值 -12% → 线 8.8, 8.0 已破
    r = mon.daily_digest({CODE: _pos(avg_cost=10.0)})[0]
    assert (r["action"], r["reason_code"]) == ("FAIL", "EXIT_ARM_FAIL")
    assert r["evidence"]["hit_rule"] == "硬止损"
    assert "没有查到它的卖出成交记录" in r["reason_text"]


def test_trailing_stop_hit_reports_arm_fail(store):
    """高点 12.0 后跌到 11.5: 激活线 10.35 过了, 回撤线 12.0×0.99=11.88 已破。"""
    mon, _ = _make(store)
    _feed(mon, CODE, 11.5, high=12.0)
    r = mon.daily_digest({CODE: _pos(avg_cost=10.0)})[0]
    assert (r["action"], r["reason_code"]) == ("FAIL", "EXIT_ARM_FAIL")
    assert r["evidence"]["hit_rule"] == "移动止盈"


def test_time_stop_hit_reports_arm_fail(store):
    """持仓到 20 天该走 → 没走就记 FAIL, 不是 HOLD。"""
    mon, _ = _make(store, hold_days=20)
    _feed(mon, CODE, 10.5)
    r = mon.daily_digest({CODE: _pos()})[0]
    assert r["evidence"]["hit_rule"] == "时间止损"


def test_ladder_hit_names_the_tier(store):
    """涨幅过第 1 档 (成本 10.0 +6% = 10.6) → 白话要说清是第几档。"""
    mon, _ = _make(store)
    _feed(mon, CODE, 10.7, high=10.7)
    r = mon.daily_digest({CODE: _pos(avg_cost=10.0)})[0]
    assert r["reason_code"] == "EXIT_ARM_FAIL"
    assert "阶梯止盈第 1 档" in r["evidence"]["hit_rule"]


def test_arm_fail_row_does_not_claim_distances(store):
    """命中规则的行使 FAIL —— 不该再附"距离"数字 (都已经到了, 还说什么距离)。"""
    mon, _ = _make(store)
    _feed(mon, CODE, 8.0)
    ev = mon.daily_digest({CODE: _pos(avg_cost=10.0)})[0]["evidence"]
    assert "gap_to_cost_stop_pct" not in ev


# ── 都没命中: 这一行才是「每天每票都写」的那条 ──────────────────

def test_no_trigger_hold_row(store):
    """10.5 元: 硬止损线 8.8 没破、移动止盈未激活、阶梯第 1 档 10.6 没到。"""
    mon, _ = _make(store)
    _feed(mon, CODE, 10.5, high=10.5)
    r = mon.daily_digest({CODE: _pos(avg_cost=10.0)})[0]
    assert (r["action"], r["reason_code"]) == ("HOLD", "EXIT_NO_TRIGGER")
    assert "没到任何一条卖出线" in r["reason_text"]


def test_no_trigger_row_gives_cost_stop_distance(store):
    """「凭什么」里必须有硬止损价与距它的百分比 —— 用户要看的就是"还差多少"。"""
    mon, _ = _make(store)
    _feed(mon, CODE, 10.5, high=10.5)
    ev = mon.daily_digest({CODE: _pos(avg_cost=10.0)})[0]["evidence"]
    assert ev["cost_stop_price"] == pytest.approx(8.8, abs=0.01)
    # (10.5 - 8.8) / 10.5 ≈ 16.19%
    assert ev["gap_to_cost_stop_pct"] == pytest.approx(16.19, abs=0.05)
    assert "还差" in mon.daily_digest({CODE: _pos(avg_cost=10.0)})[0]["reason_text"]


def test_trailing_not_activated_says_so_without_line(store):
    """涨幅还没过激活线 (3.5%) → 白话明说「还没激活」, 且不给移动止盈线数字
    (给了就等于暗示它已经在生效, 会让人误判风险)。"""
    mon, _ = _make(store)
    _feed(mon, CODE, 10.2, high=10.2)   # 峰值 10.2 < 激活线 10.35
    r = mon.daily_digest({CODE: _pos(avg_cost=10.0)})[0]
    assert "移动止盈还没激活" in r["reason_text"]
    assert "trailing_stop_price" not in r["evidence"]


def test_trailing_activated_gives_line_and_gap(store):
    """高点 12.0 (过了激活线) → 给移动止盈线与距离。
    只留移动止盈 (阶梯会先被那个高点触发, 见 _only_trailing_cfg 的说明)。"""
    mon, _ = _make(store, cfg=_only_trailing_cfg())
    _feed(mon, CODE, 11.95, high=12.0)
    r = mon.daily_digest({CODE: _pos(avg_cost=10.0)})[0]
    assert r["reason_code"] == "EXIT_NO_TRIGGER"      # 11.95 > 11.88, 差一点点但没破
    ev = r["evidence"]
    assert ev["trailing_stop_price"] == pytest.approx(11.88, abs=0.01)
    assert 0 < ev["gap_to_trailing_pct"] < 1


def test_high_spike_pullback_is_reported_as_trailing_not_ladder(store):
    """真实优先级 (`trailing_first`), 值得钉住: 当日最高 12.0 又回落到 10.5 ——
    阶梯止盈第 1 档 (10.6) 与移动止盈回撤线 (12.0×0.99=11.88) 都够格,
    但 `_hit_ladder` 排在判定链**最后**, 所以台账说的是「移动止盈」。

    写这条的目的: 判定顺序是恢复历史原因的关键。哪天有人把 `_hit_ladder`
    往前提, 这一天的原因就会从"移动止盈"变成"阶梯止盈", 回填出来的历史原因
    跟着全变 —— 而不会有任何报错。
    """
    mon, _ = _make(store)
    _feed(mon, CODE, 10.5, high=12.0)
    r = mon.daily_digest({CODE: _pos(avg_cost=10.0)})[0]
    assert r["reason_code"] == "EXIT_ARM_FAIL"
    assert r["evidence"]["hit_rule"] == "移动止盈"


def test_ladder_alone_can_be_the_hit(store):
    """阶梯真的可以单独命中: 最高与现价都是 10.7 时, 回撤线 10.59 没破 (现价
    在线上方), 只有第 1 档 10.6 够格 → 这时候才轮到阶梯出场。
    与上一条配对, 一起把「阶梯用当日最高价判」这件事钉死。"""
    mon, _ = _make(store)
    _feed(mon, CODE, 10.7, high=10.7)
    r = mon.daily_digest({CODE: _pos(avg_cost=10.0)})[0]
    assert r["evidence"]["hit_rule"] == "阶梯止盈第 1 档"


def test_ladder_next_tier_reported(store):
    """下一档位与价位要写进明细 —— 页面才能说"离下一档还差多少"。"""
    mon, _ = _make(store)
    _feed(mon, CODE, 10.5, high=10.5)
    ev = mon.daily_digest({CODE: _pos(avg_cost=10.0)})[0]["evidence"]
    assert ev["ladder_next_tier"] == 1
    assert ev["ladder_next_price"] == pytest.approx(10.6, abs=0.01)


def test_hold_days_always_in_evidence(store):
    """持有天数永远要有 (时间止损那条线的依据), 哪怕没触发。"""
    mon, _ = _make(store, hold_days=7)
    _feed(mon, CODE, 10.5)
    assert mon.daily_digest({CODE: _pos()})[0]["evidence"]["hold_days"] == 7


def test_peak_uses_largest_of_cost_high_and_history(store):
    """峰值取「成本 / 当日最高 / 历史峰值」三者最大 —— 少算一个就会把
    移动止盈线画低, 表现为"台账说没触发、实际早该卖了"。
    这里用"现价等于历史峰值"的不命中场景, 才能把 peak 这一格单独看到。
    关掉阶梯 (高点是 11.0, 第 1 档 10.6 会先命中)。"""
    mon, _ = _make(store, cfg=_only_trailing_cfg(), peak=13.0)
    _feed(mon, CODE, 13.0, high=11.0)
    r = mon.daily_digest({CODE: _pos(avg_cost=10.0)})[0]
    assert r["reason_code"] == "EXIT_NO_TRIGGER"
    assert r["evidence"]["peak"] == 13.0
    # 回撤线 = 13.0 × 0.99 = 12.87; 现价 13.0 在线上方 → 没破
    assert r["evidence"]["trailing_stop_price"] == pytest.approx(12.87, abs=0.01)


def test_historical_peak_raises_trailing_line_and_flags_sell(store):
    """历史峰值 13.0 而现价只有 12.0 → 回撤线 12.87 已被跌破, 台账要说"该卖"。
    这条验的是「历史峰值没被漏算」: 只看当日最高 (11.0) 的话回撤线是 10.89,
    现价 12.0 就"没触发" —— 一笔早该卖出的仓位会被台账说成安全。"""
    mon, _ = _make(store, cfg=_only_trailing_cfg(), peak=13.0)
    _feed(mon, CODE, 12.0, high=11.0)
    r = mon.daily_digest({CODE: _pos(avg_cost=10.0)})[0]
    assert r["reason_code"] == "EXIT_ARM_FAIL"
    assert r["evidence"]["hit_rule"] == "移动止盈"


def test_broken_peak_source_does_not_crash(store):
    """历史峰值取数抛异常 (数据源抽风) 不能炸整张卡 —— 退化成不算它。"""
    mon, _ = _make(store)

    def boom(code):
        raise RuntimeError("峰值源坏了")

    mon._peak_px = boom
    _feed(mon, CODE, 10.5)
    rows = mon.daily_digest({CODE: _pos()})
    assert len(rows) == 1
    assert rows[0]["evidence"]["peak"] >= 10.0


def test_rows_sorted_by_code(store):
    """多票时按代码排序 —— 页面顺序稳定, 不随 dict 插入顺序抖。"""
    mon, _ = _make(store)
    _feed(mon, "600519.SH", 10.5)
    _feed(mon, "000001.SZ", 12.0)
    rows = mon.daily_digest({"600519.SH": _pos(avg_cost=10.0),
                             "000001.SZ": _pos(code="000001.SZ", avg_cost=11.0)})
    assert [r["code"] for r in rows] == ["000001.SZ", "600519.SH"]


def test_every_row_has_the_six_fields(store):
    """每行都要有「谁/做了什么/为什么/凭什么」四件套 (日期与关联单号由调用方补)。"""
    mon, _ = _make(store)
    _feed(mon, CODE, 10.5)
    for r in mon.daily_digest({CODE: _pos()}):
        assert set(r) == {"code", "action", "reason_code", "reason_text", "evidence"}
        assert r["reason_text"].strip()
        assert isinstance(r["evidence"], dict)


# ── 只读: 绝不会下一笔单 ────────────────────────────────────────

def test_digest_never_places_any_order(store):
    """**最重要的一条**: 就算规则已经命中, daily_digest 也一单都不下。
    卖出动作是盘中 scan_once 的事; 这个方法是纯汇总。"""
    stub = StubExecutor()
    mon, stub = _make(store, stub=stub)
    _feed(mon, CODE, 8.0)   # 已破硬止损 —— 盘中这时早该卖了
    mon.daily_digest({CODE: _pos(avg_cost=10.0)})
    assert stub.exits == []
    assert stub.pending_calls == 0


def test_digest_does_not_write_audit(store):
    """纯汇总不该往审计表里塞东西 (审计是给"发生了什么"用的, 不是给"没发生什么")。"""
    mon, _ = _make(store)
    _feed(mon, CODE, 8.0)
    mon.daily_digest({CODE: _pos(avg_cost=10.0)})
    kinds = [r[0] for r in store._conn.execute("SELECT kind FROM audit").fetchall()]
    assert "exit_arm" not in kinds and "exit_trigger" not in kinds


# ── 防漂移: 命中判定必须走盘中的 _hit_* ─────────────────────────

@pytest.mark.parametrize("hook,rule", [
    ("_hit_cost_stop", "硬止损"),
    ("_hit_trailing", "移动止盈"),
    ("_hit_time_stop", "时间止损"),
    ("_hit_cond_time", "条件时间止损"),
    ("_hit_first_day", "首日未达标"),
])
def test_hit_judgement_is_delegated_to_scan_once_rules(store, monkeypatch, hook, rule):
    """把盘中的 `_hit_*` 换成"永远命中" → digest 必须跟着报那条规则。

    这条是防漂移的核心: 它证明 digest **没有**自己重写一遍阈值判断。
    哪天有人图省事在这里写 if last < avg_cost * 0.88, 这些用例会红。
    """
    mon, _ = _make(store)
    _feed(mon, CODE, 10.5)     # 真实阈值下一条都不命中
    monkeypatch.setattr(mon, hook, lambda *a, **kw: True)
    r = mon.daily_digest({CODE: _pos()})[0]
    assert r["evidence"]["hit_rule"] == rule


def test_ladder_hit_is_delegated_too(store, monkeypatch):
    """阶梯止盈走的是 `_hit_ladder` (返回档位号), 返回值要被用上 ——
    换成"永远命中第 2 档"后, 白话里就该出现第 2 档。"""
    mon, _ = _make(store)
    _feed(mon, CODE, 10.5)
    monkeypatch.setattr(mon, "_hit_ladder", lambda *a, **kw: 1)
    r = mon.daily_digest({CODE: _pos()})[0]
    assert "阶梯止盈第 2 档" in r["evidence"]["hit_rule"]


def test_threshold_change_flows_into_the_wording(store):
    """真改阈值 (而不是 monkeypatch): 阈值放松到 -50% 后, 8.0 元就不再算破线。"""
    loose = TradeConfig(
        account_id="TEST",
        stop=StopConfig(cost_stop=CostStopConfig(threshold=-0.50),
                        trailing_stop=TrailingStopConfig(enabled=False),
                        ladder_tp=LadderTpConfig(enabled=False),
                        time_stop=TimeStopConfig(enabled=False)))
    mon, _ = _make(store, cfg=loose)
    _feed(mon, CODE, 8.0)
    r = mon.daily_digest({CODE: _pos(avg_cost=10.0)})[0]
    assert r["reason_code"] == "EXIT_NO_TRIGGER"      # 不再是 EXIT_ARM_FAIL
    assert r["evidence"]["cost_stop_price"] == pytest.approx(5.0, abs=0.01)


def test_disabled_rules_are_not_mentioned(store):
    """关掉的规则不该出现在话术里 —— 否则用户会去等一条根本不会触发的线。"""
    cfg = TradeConfig(
        account_id="TEST",
        stop=StopConfig(cost_stop=CostStopConfig(enabled=False),
                        trailing_stop=TrailingStopConfig(enabled=False),
                        ladder_tp=LadderTpConfig(enabled=False),
                        time_stop=TimeStopConfig(enabled=False)))
    mon, _ = _make(store, cfg=cfg)
    _feed(mon, CODE, 10.5)
    r = mon.daily_digest({CODE: _pos(avg_cost=10.0)})[0]
    assert "硬止损" not in r["reason_text"]
    assert "移动止盈" not in r["reason_text"]
    assert "阶梯" not in r["reason_text"]
    assert "已持有" not in r["reason_text"]


def test_multi_position_mixed_states(store):
    """多票混合: 一只没触发、一只破线、一只没行情 —— 各说各的, 互不干扰。"""
    mon, _ = _make(store)
    _feed(mon, "600519.SH", 10.5)
    _feed(mon, "000001.SZ", 8.0)
    rows = mon.daily_digest({
        "600519.SH": _pos(avg_cost=10.0),
        "000001.SZ": _pos(code="000001.SZ", avg_cost=10.0),
        "300750.SZ": _pos(code="300750.SZ", avg_cost=20.0),   # 无行情
    })
    got = {r["code"]: r["reason_code"] for r in rows}
    assert got == {"600519.SH": "EXIT_NO_TRIGGER",
                   "000001.SZ": "EXIT_ARM_FAIL",
                   "300750.SZ": "EXIT_NO_QUOTE"}


def test_wording_is_plain_chinese_with_full_names(store):
    """大白话规则 (AGENTS.md 第 6/7 条): 话术里标的要写全名+代码, 日期写全,
    数字带单位。这里抽查「元」在不在 —— 光写 8.80 用户不知道是元还是百分比。"""
    mon, _ = _make(store)
    _feed(mon, CODE, 10.5)
    text = mon.daily_digest({CODE: _pos(avg_cost=10.0)})[0]["reason_text"]
    assert "元" in text
    assert "%" in text
