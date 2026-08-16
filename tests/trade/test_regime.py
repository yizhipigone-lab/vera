"""弱市择时闸门测试 (2026-08-16)。

锁住: 纯函数 index_above_ma 边界 (站上/跌破/数据不足/NaN)、
regime_filter 配置校验 (默认关/代码格式/窗口)、
auto_buy 闸门 fail-closed (跌破不买、站上照买、数据缺失不买)。
"""
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.config import TradeConfig, load_trade_config, RegimeFilterConfig
from trade.regime import index_above_ma
from trade_main import TradeApp

CODE = "000001.SZ"
IDX = "399006.SZ"


# ── 纯函数 ──────────────────────────────────────────────────────

def test_above_ma_returns_true():
    assert index_above_ma([10.0] * 200, 200) is True


def test_below_ma_returns_false():
    closes = [10.0] * 199 + [9.0]           # 最新价跌破均线
    assert index_above_ma(closes, 200) is False


def test_last_price_used_as_judgment():
    # 前 199 根全是 10, 最新一根 11 → 站上
    assert index_above_ma([10.0] * 199 + [11.0], 200) is True


def test_insufficient_data_returns_none():
    assert index_above_ma([10.0] * 5, 200) is None
    assert index_above_ma([], 200) is None


def test_nan_and_nonpositive_filtered():
    # NaN/非正数剔除后不足窗口 → None (fail-closed)
    assert index_above_ma([10.0] * 10 + [float("nan"), 0.0], 200) is None


# ── 配置 ────────────────────────────────────────────────────────

def _write(tmp_path, text):
    p = tmp_path / "c.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_regime_default_disabled(tmp_path):
    cfg = load_trade_config(_write(tmp_path, ""))
    assert cfg.regime_filter.enabled is False
    assert cfg.regime_filter.index_code == "399006.SZ"
    assert cfg.regime_filter.ma_window == 200


def test_regime_override(tmp_path):
    cfg = load_trade_config(_write(tmp_path, """
regime_filter:
  enabled: true
  index_code: 399006.SZ
  ma_window: 120
"""))
    assert cfg.regime_filter.enabled is True
    assert cfg.regime_filter.index_code == "399006.SZ"
    assert cfg.regime_filter.ma_window == 120


def test_regime_bad_code_rejected(tmp_path):
    with pytest.raises(ValueError, match="index_code"):
        load_trade_config(_write(tmp_path, "regime_filter:\n  index_code: 399006\n"))


def test_regime_bad_window_rejected(tmp_path):
    with pytest.raises(ValueError, match="ma_window"):
        load_trade_config(_write(tmp_path, "regime_filter:\n  ma_window: 1\n"))


def test_regime_unknown_field_rejected(tmp_path):
    with pytest.raises(ValueError, match="未知字段"):
        load_trade_config(_write(tmp_path, "regime_filter:\n  magic: 1\n"))


# ── auto_buy 闸门 ───────────────────────────────────────────────

def _ts(hhmm):
    d = datetime.now().replace(
        hour=int(hhmm[:2]), minute=int(hhmm[3:]), second=0, microsecond=0)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.timestamp()


def _make(cfg, clock, daily_closes=None):
    app = TradeApp(cfg, fake=True, clock=lambda: clock[0],
                   fake_gateway_kwargs={"cash": 1_000_000.0,
                                        "positions": {},
                                        "daily_closes": daily_closes or {}},
                   selection_runner=lambda f, a, u: [])
    app._auto_buy._await_interval = 0.01
    app._auto_buy._await_timeout = 0.05
    assert app.start(start_timers=False)
    return app


def _push(app, code, last, ask1, prev_close=10.0):
    app.gateway.push_quote(code, {"last": last, "bid1": last - 0.01,
                                  "ask1": ask1, "high": last,
                                  "prev_close": prev_close})
    deadline = time.time() + 4.0
    while time.time() < deadline and app.monitor.quote_of(code) is None:
        time.sleep(0.01)


def _audit_kinds(app):
    return {r[0] for r in app.store._conn.execute(
        "SELECT kind FROM audit").fetchall()}


def test_bear_index_blocks_buy(tmp_path):
    """指数跌破 MA → 当日不买, 留 auto_buy_skip_regime 审计。"""
    cfg = TradeConfig(account_id="AB", fake_sdk=True,
                      db_path=str(tmp_path / "t.db"),
                      raw_log_path=str(tmp_path / "r.jsonl"),
                      kill_flag_path=str(tmp_path / "KILL"),
                      regime_filter=RegimeFilterConfig(
                          enabled=True, index_code=IDX, ma_window=5))
    clock = [_ts("14:55")]
    app = _make(cfg, clock, daily_closes={IDX: [10.0] * 5})
    _push(app, IDX, 9.0, 9.0)                    # 9.0 < MA5(=9.8) → 熊市
    _push(app, CODE, 25.5, 25.51, prev_close=25.0)
    app._auto_buy.on_signals({"signals": [{"code": CODE, "select_date": "x"}],
                              "source": "test"})
    assert app.gateway.query_orders() == []
    assert "auto_buy_skip_regime" in _audit_kinds(app)
    assert app._auto_buy.last["error"].startswith("弱市闸门")


def test_bull_index_allows_buy(tmp_path):
    """指数站上 MA → 照常买入。"""
    cfg = TradeConfig(account_id="AB", fake_sdk=True,
                      db_path=str(tmp_path / "t.db"),
                      raw_log_path=str(tmp_path / "r.jsonl"),
                      kill_flag_path=str(tmp_path / "KILL"),
                      regime_filter=RegimeFilterConfig(
                          enabled=True, index_code=IDX, ma_window=5))
    clock = [_ts("14:55")]
    app = _make(cfg, clock, daily_closes={IDX: [10.0] * 5})
    _push(app, IDX, 11.0, 11.0)                  # 11.0 > MA5(=10.2) → 牛市
    _push(app, CODE, 25.5, 25.51, prev_close=25.0)
    app._auto_buy.on_signals({"signals": [{"code": CODE, "select_date": "x"}],
                              "source": "test"})
    assert len(app.gateway.query_orders()) == 1


def test_missing_index_data_blocks_buy(tmp_path):
    """指数数据缺失 → fail-closed 不买 (宁可不买)。"""
    cfg = TradeConfig(account_id="AB", fake_sdk=True,
                      db_path=str(tmp_path / "t.db"),
                      raw_log_path=str(tmp_path / "r.jsonl"),
                      kill_flag_path=str(tmp_path / "KILL"),
                      regime_filter=RegimeFilterConfig(
                          enabled=True, index_code=IDX, ma_window=5))
    clock = [_ts("14:55")]
    app = _make(cfg, clock, daily_closes={})      # 无日线
    _push(app, CODE, 25.5, 25.51, prev_close=25.0)
    app._auto_buy.on_signals({"signals": [{"code": CODE, "select_date": "x"}],
                              "source": "test"})
    assert app.gateway.query_orders() == []
    assert "auto_buy_skip_regime" in _audit_kinds(app)


def test_regime_disabled_no_gate(tmp_path):
    """开关关 → 无闸门, 照常买 (不查指数)。"""
    cfg = TradeConfig(account_id="AB", fake_sdk=True,
                      db_path=str(tmp_path / "t.db"),
                      raw_log_path=str(tmp_path / "r.jsonl"),
                      kill_flag_path=str(tmp_path / "KILL"))
    clock = [_ts("14:55")]
    app = _make(cfg, clock, daily_closes={})
    _push(app, CODE, 25.5, 25.51, prev_close=25.0)
    app._auto_buy.on_signals({"signals": [{"code": CODE, "select_date": "x"}],
                              "source": "test"})
    assert len(app.gateway.query_orders()) == 1
