"""补项 (2026-08-06 审计复核): TradeApp 级两个实盘守护的守卫测试.

背景: 第 2 批 (commit 8c8a79d) 只锁了 monitor._peak_px 注入分支,
本文件补 TradeApp 侧剩余裸奔路径:

1. HIGH#1 guard 本体: _on_trade 跨日 ts 拦截 —— 昨日 ts 不入账
   (book/store 均不变), 今日 ts / 缺 ts 放行 (缺 ts 属"无法验证",
   显式 debug 留痕, 不拦)。
2. HIGH#6 剩余半段: _peak_for_code / _episode_entry_ts 胶水 ——
   本轮持仓起点倒推 (最新成交往回, 买扣卖加, 归零处即起点)、
   query_daily_highs 聚合取 max、异常回退 None、失败只缓存 60s。
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.book import DIRECTION_BUY, DIRECTION_SELL
from trade.config import TradeConfig
from trade_main import TradeApp

CODE = "600519.SH"


@pytest.fixture()
def clock():
    # 与 test_e2e_trade 同款: 锚"最近工作日 10:00" (时段感知后自动规则
    # 只在连续竞价评估; 本文件不跑 monitor, 但保持时钟纪律一致)
    from datetime import datetime, timedelta
    d = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return [d.timestamp()]


@pytest.fixture()
def app(tmp_path, clock):
    cfg = TradeConfig(
        account_id="GUARD", fake_sdk=True,
        db_path=str(tmp_path / "trade.db"),
        raw_log_path=str(tmp_path / "raw.jsonl"),
        kill_flag_path=str(tmp_path / "KILL"),
    )
    a = TradeApp(cfg, fake=True, clock=lambda: clock[0],
                 fake_gateway_kwargs={"cash": 1_000_000.0, "positions": {}})
    yield a
    a.stop()


def _trade_rec(tid, ts, direction=DIRECTION_BUY, qty=500):
    return {"traded_id": tid, "order_id": f"O-{tid}", "code": CODE,
            "direction": direction, "price": 10.0, "qty": qty,
            "amount": 10.0 * qty, "ts": ts}


def _day(ts) -> str:
    return time.strftime("%Y%m%d", time.localtime(ts))


def _store_trade_count(app) -> int:
    ro = app.store.open_readonly()
    try:
        return ro.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    finally:
        ro.close()


# ═══════════════════════════════════════════════════════════
# 1. HIGH#1: _on_trade 跨日 ts guard
# ═══════════════════════════════════════════════════════════

def test_cross_day_trade_blocked(app, clock):
    """昨日 ts 的成交回报 (断线重连重推) → 拦截: book 无仓位, store 无记录。"""
    app._on_trade(_trade_rec("T-OLD", clock[0] - 86400))
    assert app.book.snapshot()["positions"].get(CODE) is None
    assert _store_trade_count(app) == 0


def test_today_trade_passes(app, clock):
    """今日 ts 正常放行 (对照组: 证明上一用例的拦截是跨日判断所致)。"""
    app._on_trade(_trade_rec("T-NOW", clock[0]))
    pos = app.book.snapshot()["positions"].get(CODE)
    assert pos is not None and pos.volume == 500
    assert _store_trade_count(app) == 1


def test_missing_ts_passes(app, clock):
    """ts=None (xtquant 字段缺失/为 0, 见 gateway `or None`) → 无法验证,
    放行 (debug 留痕), 不误拦合法成交。"""
    app._on_trade(_trade_rec("T-NOTS", None))
    pos = app.book.snapshot()["positions"].get(CODE)
    assert pos is not None and pos.volume == 500


# ═══════════════════════════════════════════════════════════
# 2. HIGH#6 剩余半段: _peak_for_code / _episode_entry_ts
# ═══════════════════════════════════════════════════════════

def _seed_store(app, rows):
    """直接写 store 铺历史成交 (绕过 _on_trade 的跨日 guard —— 这些
    成交"发生在过去各自的日子", 不是今天重推的)。"""
    for i, (ts, direction, qty) in enumerate(rows):
        app.store.save_trade({
            "traded_id": f"T-H{i}", "order_id": f"O-H{i}", "code": CODE,
            "direction": direction, "price": 10.0, "qty": qty, "ts": ts})


def test_episode_entry_backtracks_to_current_holding(app, clock):
    """买 1000 → 卖 1000 (清仓) → 昨日再买 500 (当前持仓): 本轮起点应倒推
    到昨日这笔, 而不是"有史以来第一笔" (旧 MIN(ts) 口径会把空仓期高点
    算进峰值 → 买入即误触发移动止盈)。"""
    t1, t2, t3 = clock[0] - 3 * 86400, clock[0] - 2 * 86400, clock[0] - 86400
    _seed_store(app, [(t1, DIRECTION_BUY, 1000),
                      (t2, DIRECTION_SELL, 1000),
                      (t3, DIRECTION_BUY, 500)])
    app.book.apply_trade("T-CUR", "O-CUR", CODE, DIRECTION_BUY, 10.0, 500)

    calls = []
    app.gateway.query_daily_highs = (
        lambda code, start, end="": calls.append((code, start, end)) or [11.0, 12.5])
    peak = app.monitor._peak_px(CODE)

    assert peak == 12.5                          # max(历史日高)
    assert calls[0][1] == _day(t3)               # 起点 = 本轮首买日, 非 t1


def test_episode_entry_fallback_to_earliest(app, clock):
    """成交数据不全 (倒推不完): 回退最早一笔买入 —— 保守方向 (峰值取大,
    保护更紧)。"""
    t1 = clock[0] - 3 * 86400
    _seed_store(app, [(t1, DIRECTION_BUY, 300)])   # 只有 300, 持仓 500 倒推不完
    app.book.apply_trade("T-CUR", "O-CUR", CODE, DIRECTION_BUY, 10.0, 500)

    calls = []
    app.gateway.query_daily_highs = (
        lambda code, start, end="": calls.append((code, start, end)) or [12.0])
    app.monitor._peak_px(CODE)
    assert calls[0][1] == _day(t1)


def test_peak_px_gateway_exception_falls_back_none(app, clock):
    """query_daily_highs 抛异常 (xtdata 挂) → 回退 None (monitor 侧再
    or 0.0 回退当日口径), 不崩。"""
    _seed_store(app, [(clock[0], DIRECTION_BUY, 500)])  # 无 store 行则起点
    app.book.apply_trade("T-CUR", "O-CUR", CODE, DIRECTION_BUY, 10.0, 500)
    calls = []                                          # 倒推为 None, 根本不查

    def boom(code, start, end=""):
        calls.append(1)
        raise RuntimeError("xtdata down")
    app.gateway.query_daily_highs = boom
    assert app.monitor._peak_px(CODE) is None
    assert calls == [1]  # 确实查过且炸了, 不是起点缺失的空转


def test_peak_px_failure_cached_60s_then_retry(app, clock):
    """失败结果只缓存 60s (防瞬时故障让历史峰值保护整天缺席);
    成功值按 (code, 当日) 缓存, 当日不再查。"""
    _seed_store(app, [(clock[0], DIRECTION_BUY, 500)])
    app.book.apply_trade("T-CUR", "O-CUR", CODE, DIRECTION_BUY, 10.0, 500)
    calls = []

    def boom(code, start, end=""):
        calls.append(1)
        raise RuntimeError("xtdata down")
    app.gateway.query_daily_highs = boom

    app.monitor._peak_px(CODE)
    app.monitor._peak_px(CODE)                   # 60s 内 → 不重复查
    assert len(calls) == 1
    clock[0] += 61.0                             # 超过失败缓存窗口 → 重试
    app.monitor._peak_px(CODE)
    assert len(calls) == 2

    clock[0] += 61.0                             # 第二次失败也进 60s 缓存, 再越过
    app.gateway.query_daily_highs = (
        lambda code, start, end="": calls.append(1) or [12.0])
    assert app.monitor._peak_px(CODE) == 12.0    # 恢复后取到
    app.monitor._peak_px(CODE)                   # 成功值当日缓存 → 不再查
    assert len(calls) == 3
