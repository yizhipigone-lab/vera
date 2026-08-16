"""trade/api.py 分析端点 Phase 2 测试 (2026-08-13): equity 追加 rolling + 新增 attribution。

契约:
- GET /api/trade/analysis/equity: 既有 "equity" 不动, 追加 "rolling"
  (形状同 backtest rolling_metrics; 空数据不加 key)
- GET /api/trade/analysis/attribution: {"by_sector", "by_stock_top",
  "meta": {"skipped_no_pnl": N}}; 只统计 direction==SELL 且 pnl_amount
  非空的行, 无 pnl 的卖出计入 skipped_no_pnl
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient

from trade.api import create_api_app
from trade.book import DIRECTION_BUY, DIRECTION_SELL
from trade.config import TradeConfig
from trade_main import TradeApp

SH = "600519.SH"
SZ = "000001.SZ"


@pytest.fixture()
def client(tmp_path):
    cfg = TradeConfig(
        account_id="API", fake_sdk=True,
        db_path=str(tmp_path / "t.db"),
        raw_log_path=str(tmp_path / "r.jsonl"),
        kill_flag_path=str(tmp_path / "KILL"),
    )
    # 固定时钟到开盘前 (08:00) —— TradeApp.start() 里的 _startup_catchup 有
    # "09:15-15:00 补预埋 / 15:05 后补 EOD" 的时段判断，用真时钟会让测试在
    # 15:05 后跑时多写一条 daily_asset → equity 断言漂移（flaky）。固定时钟使
    # 该判断恒为"不触发"，测试确定性。与其他 trade 测试的 clock 注入同款。
    fixed = time.mktime(time.strptime("2026-08-10 08:00", "%Y-%m-%d %H:%M"))
    app = TradeApp(cfg, fake=True, clock=lambda: fixed, fake_gateway_kwargs={
        "cash": 1_000_000.0,
        "positions": {SH: {"volume": 1000, "can_use": 1000, "avg_cost": 10.0}}},
        config_path=str(tmp_path / "trade.yaml"))
    app.start(start_timers=False)
    yield TestClient(create_api_app(app)), app
    app.stop()


@pytest.fixture()
def mock_sector(monkeypatch):
    """mock 行业映射, 避免真拉 TDX; 同时固定股票名表。"""
    import core.data_fetcher as dfm
    import policy_kb.build_sector_index as bsi
    import trade.api as tapi
    monkeypatch.setattr(bsi, "build_stock_sector_index",
                        lambda force_refresh=False: {SH: "S1", SZ: "S2"})
    monkeypatch.setattr(dfm.DataFetcher, "get_sector_list",
                        classmethod(lambda cls: [{"code": "S1", "name": "白酒"},
                                                 {"code": "S2", "name": "银行"}]))
    monkeypatch.setattr(dfm.DataFetcher, "get_name_map",
                        classmethod(lambda cls: {SH: "贵州茅台", SZ: "平安银行"}))
    monkeypatch.setattr(tapi, "_name_map", None)  # 清惰性缓存, 防跨测试污染


# ---------- /api/trade/analysis/equity 追加 rolling ----------

def test_equity_appends_rolling(client):
    c, app = client
    for i, asset in enumerate((1_000_000.0, 1_010_000.0, 990_000.0)):
        app.store.daily_asset.save(f"2026-08-1{i}", asset, asset, 0.0)
    d = c.get("/api/trade/analysis/equity").json()
    assert len(d["equity"]) == 3  # 既有字段不动
    rolling = d["rolling"]
    assert rolling["window"] == 60
    assert rolling["dates"] == ["2026-08-10", "2026-08-11", "2026-08-12"]
    # 窗口不足 → 全 None (不能是 NaN, allow_nan=False)
    assert rolling["rolling_sharpe"] == [None, None, None]
    assert rolling["rolling_return"] == [None, None, None]
    assert rolling["rolling_vol"] == [None, None, None]


def test_equity_empty_no_rolling_key(client):
    c, _ = client
    d = c.get("/api/trade/analysis/equity").json()
    assert d == {"equity": []}  # 空数据不加 rolling key


# ---------- /api/trade/analysis/attribution ----------

def _save(app, traded_id, code, direction, pnl_amount=None):
    rec = {"traded_id": traded_id, "order_id": f"O-{traded_id}", "code": code,
           "direction": direction, "price": 10.0, "qty": 100, "amount": 1000.0,
           "ts": time.time()}
    if pnl_amount is not None:
        rec["pnl_amount"] = pnl_amount
    app.store.save_trade(rec)


def test_attribution_aggregates_sells(client, mock_sector):
    """卖出行按 code 归一 → 行业聚合; 买入行不参与。"""
    c, app = client
    _save(app, "T-B1", SH, DIRECTION_BUY)                    # 买入, 忽略
    _save(app, "T-S1", SH, DIRECTION_SELL, pnl_amount=100.0)
    _save(app, "T-S2", SH, DIRECTION_SELL, pnl_amount=-20.0)  # 同票合并 → 80
    _save(app, "T-S3", SZ, DIRECTION_SELL, pnl_amount=-50.0)
    d = c.get("/api/trade/analysis/attribution").json()
    sectors = {s["name"]: s for s in d["by_sector"]}
    assert sectors["白酒"]["pnl"] == pytest.approx(80.0)
    assert sectors["银行"]["pnl"] == pytest.approx(-50.0)
    # 按 pnl 降序
    assert [s["name"] for s in d["by_sector"]] == ["白酒", "银行"]
    top = {t["code"]: t for t in d["by_stock_top"]}
    assert top[SH]["pnl"] == pytest.approx(80.0)
    assert top[SH]["name"] == "贵州茅台"
    # 总净盈亏 = 30; pct
    assert sectors["白酒"]["pct"] == pytest.approx(80.0 / 30.0)
    assert d["meta"]["skipped_no_pnl"] == 0


def test_attribution_counts_skipped_no_pnl(client, mock_sector):
    """无 pnl_amount 的卖出 (含历史行=0) 计入 skipped_no_pnl, 不参与聚合。"""
    c, app = client
    _save(app, "T-S1", SH, DIRECTION_SELL, pnl_amount=100.0)
    _save(app, "T-S2", SH, DIRECTION_SELL)                   # 无 pnl → skipped
    _save(app, "T-S3", SZ, DIRECTION_SELL, pnl_amount=0.0)   # 历史行 0 → skipped
    d = c.get("/api/trade/analysis/attribution").json()
    assert d["meta"]["skipped_no_pnl"] == 2
    assert len(d["by_stock_top"]) == 1
    assert d["by_stock_top"][0]["code"] == SH


def test_attribution_unmapped_sector(client, mock_sector):
    """未映射行业的票归入「未标」。"""
    c, app = client
    _save(app, "T-S1", "300999.SZ", DIRECTION_SELL, pnl_amount=7.0)
    d = c.get("/api/trade/analysis/attribution").json()
    assert d["by_sector"][0]["name"] == "未标"
    assert d["by_sector"][0]["pnl"] == pytest.approx(7.0)


def test_attribution_empty(client, mock_sector):
    """无成交 → 空结构 + skipped=0, 不抛。"""
    c, _ = client
    d = c.get("/api/trade/analysis/attribution").json()
    assert d == {"by_sector": [], "by_stock_top": [],
                 "meta": {"skipped_no_pnl": 0}}
