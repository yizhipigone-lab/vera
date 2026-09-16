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


# ── 2026-09-16 审计修复收尾: 当日日线已在序列里时不再追加实时价 (不双计) ──

def _day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y%m%d")


def test_close_bar_present_no_realtime_append(tmp_path):
    """盘后当日日线已落地 → 不追加实时价 (否则当天计两次, 均线被拉偏)。

    构造: 日线 [10]×5 → MA5=10, 末根=10 恰好站上 → 放行。
    若仍追加实时价 9.0: MA5 变 9.8, 9.0 < 9.8 → 会被误判成弱市而禁买。
    """
    cfg = TradeConfig(account_id="AB", fake_sdk=True,
                      db_path=str(tmp_path / "t.db"),
                      raw_log_path=str(tmp_path / "r.jsonl"),
                      kill_flag_path=str(tmp_path / "KILL"),
                      regime_filter=RegimeFilterConfig(
                          enabled=True, index_code=IDX, ma_window=5))
    clock = [_ts("15:30")]                        # 盘后
    app = _make(cfg, clock, daily_closes={IDX: [10.0] * 5})
    app.gateway.daily_last_bar[IDX] = _day_of(clock[0])   # 网关: 末根日线就是今天
    _push(app, IDX, 9.0, 9.0)                     # 实时价 9.0 低于均线
    _push(app, CODE, 25.5, 25.51, prev_close=25.0)

    app._auto_buy.on_signals({"signals": [{"code": CODE, "select_date": "x"}],
                              "source": "test"})
    assert len(app.gateway.query_orders()) == 1   # 未被实时价拖成弱市
    assert "auto_buy_skip_regime" not in _audit_kinds(app)


def test_stale_daily_bar_still_appends_realtime(tmp_path):
    """对照: 末根日线不是今天 (数据源滞后/盘中) → 仍追加实时价判"现在"。"""
    cfg = TradeConfig(account_id="AB", fake_sdk=True,
                      db_path=str(tmp_path / "t.db"),
                      raw_log_path=str(tmp_path / "r.jsonl"),
                      kill_flag_path=str(tmp_path / "KILL"),
                      regime_filter=RegimeFilterConfig(
                          enabled=True, index_code=IDX, ma_window=5))
    clock = [_ts("15:30")]
    app = _make(cfg, clock, daily_closes={IDX: [10.0] * 5})
    app.gateway.daily_last_bar[IDX] = "20260915"   # 陈旧: 不是今天
    _push(app, IDX, 9.0, 9.0)
    _push(app, CODE, 25.5, 25.51, prev_close=25.0)

    app._auto_buy.on_signals({"signals": [{"code": CODE, "select_date": "x"}],
                              "source": "test"})
    assert app.gateway.query_orders() == []
    assert "auto_buy_skip_regime" in _audit_kinds(app)


def test_intraday_bar_trimmed_recorded_as_not_today():
    """网关记录的是**实际返回序列**的末根: 盘中裁掉当日 bar 后不应记成今天。

    直接锁 gateway 的这一行语义 (auto_buy 的判据就靠它):
    盘中 df 末根=今天且 <15:05 → 裁剪 → daily_last_bar 记的是前一根。
    """
    import pandas as pd

    import trade.gateway as gw_mod
    from trade.gateway import RealGateway

    today = datetime.now().date()
    prev = today - timedelta(days=1)
    idx = pd.to_datetime([prev.strftime("%Y%m%d"), today.strftime("%Y%m%d")],
                         format="%Y%m%d")
    frame = pd.DataFrame({"close": [2.0, 3.0]}, index=idx)

    class _Fake:
        def get_market_data_ex(self, *a, **kw):
            return {"399006.SZ": frame}

        def download_history_data(self, *a, **kw):
            return 0

    g = RealGateway(account_id="TEST")
    g._xtdata = lambda: _Fake()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(gw_mod, "_expected_last_bar_day",
                   lambda d, hm: prev.strftime("%Y%m%d"))   # 不该补下载
    monkey.setattr(gw_mod.time, "localtime", lambda *a: time.struct_time(
        (today.year, today.month, today.day, 10, 0, 0, 0, 0, -1)))
    try:
        out = g.query_daily_closes("399006.SZ", count=21)
    finally:
        monkey.undo()
    assert out == pytest.approx([2.0])                        # 当日盘中 bar 被裁
    assert g.last_daily_bar_day("399006.SZ") == prev.strftime("%Y%m%d")
