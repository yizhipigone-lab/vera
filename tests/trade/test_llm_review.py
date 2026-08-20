"""trade/llm_review.py 盘后 AI 复盘测试 (2026-08-15).

锁住: format_daily_data 纯函数 (字段格式化/空 payload/轮动信号/买卖方向)、
build_daily_summary 松耦合 (mock 成功 / 失败返 None / 空数据不调 LLM)。
不调真实 LLM (慢且贵), 全程 mock get_client。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade import llm_review
from trade.llm_review import build_daily_summary, format_daily_data


def _payload(**kw):
    base = {
        "total_asset": 1032000.0,
        "cash": 200000.0,
        "market_value": 832000.0,
        "day_pnl": 3200.0,
        "day_pnl_pct": 0.31,
        "position_count": 12,
        "floating_pnl": 5000.0,
        "buy_count": 3, "sell_count": 2,
        "turnover": 150000.0,
        "realized_pnl": 7200.0, "win_rate": 0.5,
        "position_changes": {"new": [{"code": "159949.SZ", "delta": 50000}],
                             "closed": [{"code": "600519.SH", "delta": -100}]},
        "trade_details": [
            {"code": "600519.SH", "direction": 24, "pnl_amount": 8000.0,
             "reason": "移动止盈"},
            {"code": "300750.SZ", "direction": 24, "pnl_amount": -800.0,
             "reason": "硬止损"},
        ],
        "rotation": {"target": "513100.SH",
                     "momentum": {"159949.SZ": 0.03, "513100.SH": 0.08},
                     "entry_high": {"513100.SH": 1.05}},
    }
    base.update(kw)
    return base


def test_format_daily_data_includes_key_fields():
    s = format_daily_data(_payload())
    assert "1,032,000.00" in s          # 总资产
    assert "+3,200.00" in s             # 当日盈亏
    assert "513100.SH" in s             # 轮动目标代码
    assert "159949.SZ +3.0%" in s       # 各腿动量百分比
    assert "513100.SH +8.0%" in s
    assert "移动止损基准" in s          # 移动止损基准
    assert "移动止盈" in s              # 卖出原因
    assert "硬止损" in s
    assert "卖 600519.SH" in s          # 卖出方向
    assert "新进 159949.SZ" in s        # 仓位变动


def test_format_daily_data_empty():
    assert format_daily_data({}) == ""


def test_format_daily_data_sell_fly_fields():
    """卖飞信号: 卖单带盘中最高涨幅/卖出时点涨幅时进文本, 缺字段则跳过。"""
    s = format_daily_data(_payload(trade_details=[
        {"code": "301072.SZ", "direction": 24, "pnl_amount": 546.0,
         "reason": "移动止盈", "intraday_high_pct": 9.64,
         "sell_pct_vs_prev": 2.10},
        {"code": "600519.SH", "direction": 24, "pnl_amount": 8000.0,
         "reason": "移动止盈"},
    ]))
    assert "盘中最高+9.64%" in s      # 有字段 → 进文本
    assert "卖在+2.10%" in s
    assert "卖 600519.SH" in s        # 无字段的卖单照常, 不误加


def test_format_daily_data_no_rotation():
    s = format_daily_data(_payload(rotation=None))
    assert "轮动信号" not in s           # 无轮动不出现该行
    assert "总资产" in s                # 其他字段照常


def test_build_daily_summary_ok(monkeypatch):
    class FakeClient:
        def chat(self, messages, **kw):
            assert messages[0]["role"] == "system"
            assert "总资产" in messages[1]["content"]
            return "今天赚了 3200 元"
    monkeypatch.setattr(llm_review, "get_client", lambda: FakeClient())
    assert build_daily_summary(_payload()) == "今天赚了 3200 元"


def test_build_daily_summary_llm_returns_none(monkeypatch):
    class FakeClient:
        def chat(self, messages, **kw):
            return None
    monkeypatch.setattr(llm_review, "get_client", lambda: FakeClient())
    assert build_daily_summary(_payload()) is None


def test_build_daily_summary_empty_no_llm(monkeypatch):
    called = []
    class FakeClient:
        def chat(self, messages, **kw):
            called.append(1)
            return "x"
    monkeypatch.setattr(llm_review, "get_client", lambda: FakeClient())
    assert build_daily_summary({}) is None
    assert called == []                 # 空数据不调 LLM
