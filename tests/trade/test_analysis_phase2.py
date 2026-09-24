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
    import trade.analysis as an
    monkeypatch.setattr(bsi, "build_stock_sector_index",
                        lambda force_refresh=False: {SH: "S1", SZ: "S2"})
    monkeypatch.setattr(dfm.DataFetcher, "get_sector_list",
                        classmethod(lambda cls: [{"code": "S1", "name": "白酒"},
                                                 {"code": "S2", "name": "银行"}]))
    monkeypatch.setattr(dfm.DataFetcher, "get_name_map",
                        classmethod(lambda cls: {SH: "贵州茅台", SZ: "平安银行"}))
    monkeypatch.setattr(an, "_name_map", None)  # 清惰性缓存, 防跨测试污染


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


# ---------- /api/trade/analysis/daily_pnl: 首日基准修复 + _month 月度汇总 (2026-09-04) ----------

def _ts(date_time: str) -> float:
    """本地时区时间串 → epoch 秒 (与 daily_pnl 月窗构造同款口径)。
    支持带/不带秒两种格式。"""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return time.mktime(time.strptime(date_time, fmt))
        except ValueError:
            continue
    raise ValueError(date_time)


def _save_at(app, traded_id, code, direction, ts):
    """带显式 ts 的成交落库 (现有 _save 用墙钟 now, 进不了测试月份)。"""
    rec = {"traded_id": traded_id, "order_id": f"O-{traded_id}", "code": code,
           "direction": direction, "price": 10.0, "qty": 100,
           "amount": 1000.0, "ts": ts}
    app.store.save_trade(rec)


def test_daily_pnl_first_day_uses_prev_month_baseline(client):
    """2026-09-04 回归: 每月首日盈亏基准 = 前月最后交易日资产。

    旧 bug: 前月基准行被 rows_all[first_month_idx:] 切掉、且仅 i>0
    才算盈亏 → 每月首个交易日恒显 0.00%, 按日聚合月收益系统性漏首日。"""
    c, app = client
    app.store.daily_asset.save("2026-07-31", 1_000_000.0, 0.0, 0.0)
    app.store.daily_asset.save("2026-08-03", 1_010_000.0, 0.0, 0.0)
    app.store.daily_asset.save("2026-08-04", 990_000.0, 0.0, 0.0)
    d = c.get("/api/trade/analysis/daily_pnl?year=2026&month=8").json()
    assert d["2026-08-03"]["pnl_rate"] == pytest.approx(0.01)
    assert d["2026-08-03"]["pnl_amount"] == pytest.approx(10_000.0)
    assert d["2026-08-04"]["pnl_rate"] == round(-20_000 / 1_010_000, 6)
    # 前月行不泄漏进当月结果
    assert "2026-07-31" not in d


def test_daily_pnl_month_aggregate(client):
    """_month 月度汇总: 期末资产÷基准资产-1 (基准=前月最后交易日),
    盈亏天数/买卖笔数按当月逐日累加, 当月首日计入月度链。"""
    c, app = client
    app.store.daily_asset.save("2026-07-31", 1_000_000.0, 0.0, 0.0)
    app.store.daily_asset.save("2026-08-03", 1_010_000.0, 0.0, 0.0)
    app.store.daily_asset.save("2026-08-04", 990_000.0, 0.0, 0.0)
    _save_at(app, "T-B1", SH, DIRECTION_BUY, _ts("2026-08-03 09:35"))
    _save_at(app, "T-S1", SH, DIRECTION_SELL, _ts("2026-08-04 09:40"))
    d = c.get("/api/trade/analysis/daily_pnl?year=2026&month=8").json()
    m = d["_month"]
    assert m["pnl_rate"] == pytest.approx(-0.01)
    assert m["pnl_amount"] == pytest.approx(-10_000.0)
    assert m["baseline_date"] == "2026-07-31"
    assert m["baseline_is_prev_month"] is True
    assert m["trading_days"] == 2
    assert m["win_days"] == 1
    assert m["loss_days"] == 1
    assert m["buy_count"] == 1
    assert m["sell_count"] == 1


def test_daily_pnl_first_month_no_prev_baseline(client):
    """账户首月 (前月无行): 基准回退当月首行, 首日盈亏如实为 0,
    汇总自次个交易日起算, baseline_is_prev_month=False 供前端区分。"""
    c, app = client
    app.store.daily_asset.save("2026-08-03", 1_000_000.0, 0.0, 0.0)
    app.store.daily_asset.save("2026-08-04", 1_020_000.0, 0.0, 0.0)
    d = c.get("/api/trade/analysis/daily_pnl?year=2026&month=8").json()
    assert d["2026-08-03"]["pnl_rate"] == 0.0
    m = d["_month"]
    assert m["baseline_is_prev_month"] is False
    assert m["baseline_date"] == "2026-08-03"
    assert m["pnl_rate"] == pytest.approx(0.02)
    assert m["pnl_amount"] == pytest.approx(20_000.0)


def test_daily_pnl_empty_month(client):
    """无数据月份: _month 为 None, 无日期键。

    2026-09-10 停机日补算: 响应追加 _missing / _gapfill 两个 "_" 键
    (与 _month 同款加法演进, 按日期键取值的老消费方自动忽略), 故此处
    不再整体等值比较, 改为断言"没有日期键 + _month 为 None"。"""
    c, _ = client
    d = c.get("/api/trade/analysis/daily_pnl?year=2025&month=1").json()
    assert d["_month"] is None
    assert [k for k in d if not k.startswith("_")] == []


# ---------- 2026-09-04 对手审计修复回归 (独立复审 FAIL 裁决后的补测) ----------

def test_daily_pnl_prev_month_row_before_25th(client):
    """对手审计 MEDIUM 回归: 前月最后资产行早于 25 号 (月末停机场景)。

    旧取数窗起点=前月25日, 抓不到前月基准行 → 误判"账户首月":
    首日复显 0.00%、月收益错按当月首行起算 (实测 -1.98% vs 应 -1.00%)。
    窗口改前月 1 号后应抓到 7/10 行; 且前月多行时取最后一行 (非首行)。"""
    c, app = client
    app.store.daily_asset.save("2026-07-01", 950_000.0, 0.0, 0.0)
    app.store.daily_asset.save("2026-07-10", 1_000_000.0, 0.0, 0.0)  # 前月最后行
    app.store.daily_asset.save("2026-08-03", 1_010_000.0, 0.0, 0.0)
    app.store.daily_asset.save("2026-08-04", 990_000.0, 0.0, 0.0)
    d = c.get("/api/trade/analysis/daily_pnl?year=2026&month=8").json()
    assert d["2026-08-03"]["pnl_rate"] == pytest.approx(0.01)
    m = d["_month"]
    assert m["baseline_date"] == "2026-07-10"
    assert m["baseline_is_prev_month"] is True
    assert m["pnl_rate"] == pytest.approx(-0.01)


def test_daily_pnl_month_counts_cover_hole_days(client):
    """对手审计 LOW 回归: 月内某交易日缺资产快照行 (停机洞日) 时,
    当天成交仍须计入月买卖笔数。旧实现只随 rows (快照行) 循环累加,
    洞日成交静默丢失 (buy_count=0)。"""
    c, app = client
    app.store.daily_asset.save("2026-07-31", 1_000_000.0, 0.0, 0.0)
    app.store.daily_asset.save("2026-08-03", 1_000_000.0, 0.0, 0.0)
    app.store.daily_asset.save("2026-08-05", 1_000_000.0, 0.0, 0.0)  # 08-04 缺快照行
    _save_at(app, "T-B1", SH, DIRECTION_BUY, _ts("2026-08-04 09:35"))  # 洞日成交
    d = c.get("/api/trade/analysis/daily_pnl?year=2026&month=8").json()
    m = d["_month"]
    assert m["buy_count"] == 1
    assert m["trading_days"] == 2  # 快照口径: 缺行日涨跌并入下一有行日


def test_daily_pnl_month_param_bounds(client):
    """对手审计 存量LOW 回归: month=13 手输原为 500 (datetime ValueError),
    参数上界收紧后应 422; 0=缺省当月哨兵保留 (200)。"""
    c, _ = client
    assert c.get("/api/trade/analysis/daily_pnl?year=2026&month=13").status_code == 422
    assert c.get("/api/trade/analysis/daily_pnl?month=0").status_code == 200


def test_daily_pnl_cross_year_baseline_pinned(client):
    """钉死对手复审实测结论: 12月→1月跨年取数窗正确。
    start 应落在 {year-1}-12-01, 基准=12月最后行, is_prev_month=True。
    (钉死型用例: 复审已实测正确, 此处固化防回归。)"""
    c, app = client
    app.store.daily_asset.save("2025-12-30", 1_000_000.0, 0.0, 0.0)
    app.store.daily_asset.save("2026-01-05", 1_010_000.0, 0.0, 0.0)
    d = c.get("/api/trade/analysis/daily_pnl?year=2026&month=1").json()
    assert d["2026-01-05"]["pnl_rate"] == pytest.approx(0.01)
    m = d["_month"]
    assert m["baseline_date"] == "2025-12-30"
    assert m["baseline_is_prev_month"] is True
    assert m["pnl_rate"] == pytest.approx(0.01)


def test_daily_pnl_trades_month_window_boundaries(client):
    """钉死对手审计发现6盲区: trades 月窗边界。前月末日 23:59 的成交
    不计入当月; 当月末日 23:59:59.5 (小数秒) 计入 (月窗 +1s 缓冲)。"""
    c, app = client
    app.store.daily_asset.save("2026-07-31", 1_000_000.0, 0.0, 0.0)
    app.store.daily_asset.save("2026-08-31", 1_000_000.0, 0.0, 0.0)
    _save_at(app, "T-B-OUT", SH, DIRECTION_BUY, _ts("2026-07-31 23:59:00"))   # 前月末 → 排除
    _save_at(app, "T-B-FRAC", SZ, DIRECTION_BUY, _ts("2026-08-31 23:59:59") + 0.5)  # 小数秒 → 纳入
    d = c.get("/api/trade/analysis/daily_pnl?year=2026&month=8").json()
    m = d["_month"]
    assert m["buy_count"] == 1


# ---------- 2026-09-04 复验轮遗留三项 (复审 PASS 后顺手闭洞) ----------

def test_daily_pnl_prev_month_fully_dark(client):
    """复验遗留①: 前月整月停机 (无任何资产行)。

    窗口法 (无论起点放宽到几号) 抓不到基准 → 误判"账户首月"
    (实测 baseline=当月首行、m_rate=0)。根治 = store.load_prev 单查
    「当月1号前最后一行」, 无窗口概念 → 基准应退到 8/31 (两个月前)。"""
    c, app = client
    app.store.daily_asset.save("2026-08-31", 1_000_000.0, 0.0, 0.0)
    # 2026-09 整月无行 (停机一个月)
    app.store.daily_asset.save("2026-10-05", 1_010_000.0, 0.0, 0.0)
    d = c.get("/api/trade/analysis/daily_pnl?year=2026&month=10").json()
    assert d["2026-10-05"]["pnl_rate"] == pytest.approx(0.01)
    m = d["_month"]
    assert m["baseline_date"] == "2026-08-31"
    assert m["baseline_is_prev_month"] is True
    assert m["pnl_rate"] == pytest.approx(0.01)


def test_daily_pnl_year_param_upper_bound(client):
    """复验遗留②: year 无上界 → 9999/10000 手输 500 (Windows timestamp
    溢出 / datetime ValueError), 与 month=13 同性质。收紧 le=3000
    (Windows mktime64 平台上限 3000-12-31) 后 422, 合法年份不受影响。"""
    c, _ = client
    assert c.get("/api/trade/analysis/daily_pnl?year=3001&month=8").status_code == 422
    assert c.get("/api/trade/analysis/daily_pnl?year=3000&month=8").status_code == 200
