"""tests/test_daily_review.py — 盘后复盘报告（2026-09-17, M6）。

全程离线：自己造一个临时 trade.db + 临时日线缓存，不碰生产库、不推任何东西。
覆盖计划书 §9 / §12.5 / §13.6 的验收条目。
"""
from __future__ import annotations

import ast
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from notes_gen import daily as drev  # noqa: E402
# 方向码**必须**从生产库的唯一真相源引，不许在测试里自己编一套（审计 F-01 的根因：
# 测试夹具曾用 0/1 造数，而生产库只存 23/24，于是测试全绿、报告全错）。
from trade.book import DIRECTION_BUY, DIRECTION_SELL  # noqa: E402


def _ts(date: str, hh: int = 14, mm: int = 30) -> float:
    """某天某时刻的 Unix 秒（成交记录的时间戳必须落在目标日, 否则查不到）。"""
    import datetime as _dt
    d = _dt.date.fromisoformat(date)
    return _dt.datetime.combine(d, _dt.time(hh, mm)).timestamp()


def _make_db(path: Path, *, report_date: str | None, positions=None, trades=None,
             assets=None) -> None:
    """造一个最小 trade.db（列名与真实库一致，实测核验过）。"""
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE daily_report (date TEXT, payload_json TEXT, ts REAL);
        CREATE TABLE daily_asset (date TEXT, total_asset REAL, available REAL,
                                  market_value REAL, ts REAL, source TEXT);
        CREATE TABLE trades (traded_id TEXT, order_id TEXT, code TEXT, direction INTEGER,
                             price REAL, qty INTEGER, amount REAL, ts REAL, source TEXT,
                             reason TEXT, pnl_amount REAL, pnl_pct REAL);
        CREATE TABLE position_snapshot (code TEXT, volume INTEGER, can_use INTEGER,
                                        avg_cost REAL, ts REAL);
    """)
    if report_date:
        # 单位照**真实写方**来（2026-09-17 核实）:
        #   day_pnl_pct = 百分数(0.5 = +0.5%)、turnover = 成交额(元)、win_rate = 比率
        payload = {"total_asset": 1_000_000.0, "cash": 400_000.0,
                   "market_value": 600_000.0, "day_pnl": 5_000.0,
                   "day_pnl_pct": 0.5, "position_count": len(positions or []),
                   "buy_count": sum(1 for t in (trades or []) if t[1] == DIRECTION_BUY),
                   "sell_count": sum(1 for t in (trades or []) if t[1] == DIRECTION_SELL),
                   "turnover": 120_000.0, "realized_pnl": 1_200.0, "win_rate": 0.5,
                   "floating_pnl": -3_000.0, "rotation": {"target": "159949.SZ"},
                   "ts": 0}
        con.execute("INSERT INTO daily_report VALUES (?,?,?)",
                    (report_date, json.dumps(payload, ensure_ascii=False), 0.0))
    for p in (positions or []):
        con.execute("INSERT INTO position_snapshot VALUES (?,?,?,?,?)",
                    (p["code"], p.get("volume", 1000), p.get("can_use", 1000),
                     p["avg_cost"], 0.0))
    for t in (trades or []):
        # (code, direction, price, ts, reason, pnl_amount, pnl_pct)
        con.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"T{t[3]}", "O1", t[0], t[1], t[2], 100, t[2] * 100, t[3],
                     "manual", t[4], t[5], t[6]))
    for a in (assets or []):
        con.execute("INSERT INTO daily_asset VALUES (?,?,?,?,?,?)",
                    (a[0], a[1], 0.0, a[1], 0.0, "eod"))
    con.commit()
    con.close()


def _make_kline(code: str, date: str, *, high=10.0, low=8.0, close=9.0, volume=1000.0):
    import pandas as pd
    d = drev.KLINE_1D_DIR
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"date": [date], "open": [9.0], "high": [high], "low": [low],
                  "close": [close], "volume": [volume]}).to_parquet(
        d / f"{code}.parquet", index=False)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """隔离 db 路径、日线缓存目录、复盘落盘目录 —— 绝不碰生产数据。"""
    monkeypatch.setattr(drev, "TRADE_DB", tmp_path / "trade.db")
    monkeypatch.setattr(drev, "KLINE_1D_DIR", tmp_path / "kline" / "1d")
    monkeypatch.setattr(drev, "REVIEW_DIR", tmp_path / "daily_review")


class TestBuildReview:
    def test_no_report_row_says_so_and_does_not_fake(self, monkeypatch, tmp_path):
        """停机场景（§9 验收 3）：没有当日归档 → 明写，绝不拿昨天数字冒充。"""
        _make_db(tmp_path / "trade.db", report_date="2026-09-16",
                 assets=[("2026-09-16", 1_000_000.0)])
        r = drev.build_review(asof="2026-09-17", with_market=False)
        text = "\n".join(r["account"])
        assert "今日没有交易归档" in text
        assert "1,000,000" not in text, "不许把昨天(9月16日)的数字拿来当今天"
        md = drev.review_md(r)
        assert "没有交易归档" in md

    def test_with_report_row_uses_payload_verbatim(self, monkeypatch, tmp_path):
        """账户段必须**复用** payload 的数字，不重算（§9 验收 2）。"""
        _make_db(tmp_path / "trade.db", report_date="2026-09-17")
        r = drev.build_review(asof="2026-09-17", with_market=False)
        text = "\n".join(r["account"])
        assert "1,000,000 元" in text and "+0.50%" in text
        # turnover 是**成交额(元)**, 不是百分比 —— 打错了会把 12 万元印成 12%
        assert "成交额 120,000 元" in text

    def test_market_section_can_be_switched_off(self, tmp_path):
        r = drev.build_review(asof="2026-09-17", with_market=False)
        assert r["market_md"] == ""
        assert "大盘体温表" not in drev.review_md(r)


class TestStopWatch:
    def test_hard_stop_distance_is_reported(self, monkeypatch, tmp_path):
        _make_db(tmp_path / "trade.db", report_date="2026-09-17",
                 positions=[{"code": "159949.SZ", "avg_cost": 10.0}])
        _make_kline("159949.SZ", "2026-09-17", close=8.9, high=9.0, low=8.8)
        r = drev.build_review(asof="2026-09-17", with_market=False)
        text = "\n".join(r["watch"])
        assert "距硬止损线" in text and "159949.SZ" in text

    def test_stop_threshold_is_really_read_from_trade_config(self, monkeypatch, tmp_path):
        """**审计 F6**：原测试只断言 `CostStopConfig().threshold < 0` —— 那**不能证明**
        `notes_gen/daily` 真的读了配置（它自己写一份 -0.12 兜底也能过这个断言）。
        这里**把配置换掉再跑**：止损线必须跟着变，才算真的"复用"。
        """
        _make_db(tmp_path / "trade.db", report_date="2026-09-17",
                 positions=[{"code": "159949.SZ", "avg_cost": 10.0}])
        _make_kline("159949.SZ", "2026-09-17", close=9.0, high=9.1, low=8.9)
        import trade.config as tc
        from dataclasses import replace

        def _text_with(thr):
            monkeypatch.setattr(tc, "CostStopConfig",
                                lambda: replace(_Real(), threshold=thr))
            r = drev.build_review(asof="2026-09-17", with_market=False)
            return "\n".join(r["watch"])

        _Real = tc.CostStopConfig
        tight = _text_with(-0.05)     # 止损线 = 成本 × 0.95 = 9.50
        loose = _text_with(-0.30)     # 止损线 = 成本 × 0.70 = 7.00
        assert "9.50" in tight and "7.00" in loose, (tight[-140:], loose[-140:])
        assert tight != loose, "换了配置报告却没变 = 止损阈值没有真的在读配置"

    def test_missing_config_does_not_fabricate_a_threshold(self, monkeypatch, tmp_path):
        """读不到配置就**不报**「距止损线还有多少」，绝不用编的默认值糊过去。"""
        _make_db(tmp_path / "trade.db", report_date="2026-09-17",
                 positions=[{"code": "159949.SZ", "avg_cost": 10.0}])
        _make_kline("159949.SZ", "2026-09-17", close=9.0)
        import trade.config as tc

        def _boom():
            raise RuntimeError("配置坏了")
        monkeypatch.setattr(tc, "CostStopConfig", _boom)
        r = drev.build_review(asof="2026-09-17", with_market=False)
        text = "\n".join(r["watch"])
        assert "距硬止损线" not in text
        assert any("读不到交易配置里的硬止损阈值" in n for n in r["notes"])

    def test_no_quote_says_so_instead_of_guessing(self, monkeypatch, tmp_path):
        _make_db(tmp_path / "trade.db", report_date="2026-09-17",
                 positions=[{"code": "000001.SZ", "avg_cost": 10.0}])
        r = drev.build_review(asof="2026-09-17", with_market=False)
        assert "拿不到当日收盘价" in "\n".join(r["watch"])

    def test_watch_section_has_no_imperative_language(self, monkeypatch, tmp_path):
        """§12.5 硬条款：留意清单**不得出现评价性措辞**（只陈述事实）。"""
        _make_db(tmp_path / "trade.db", report_date="2026-09-17",
                 positions=[{"code": "159949.SZ", "avg_cost": 10.0}])
        _make_kline("159949.SZ", "2026-09-17", close=8.9)
        md = drev.review_md(drev.build_review(asof="2026-09-17", with_market=False))
        for bad in ("过于", "太频繁", "你应该", "建议你", "必须马上"):
            assert bad not in md, f"留意清单里出现了评价性措辞: {bad}"


class TestHighLowZero:
    """§13.6 验收 1：空壳 bar（high == low / volume == 0）不许除零、不许编数。"""

    def test_stub_bar_is_treated_as_no_quote(self, monkeypatch, tmp_path):
        _make_db(tmp_path / "trade.db", report_date="2026-09-17")
        # open=high=low=close 且 volume=0 —— 这正是本项目实测过的空壳 bar
        _make_kline("600000.SH", "2026-09-17", high=11.81, low=11.81,
                    close=11.81, volume=0.0)
        assert drev._ohlc_on("600000.SH", "2026-09-17") is None

    def test_flat_but_traded_bar_gives_no_amplitude(self, monkeypatch, tmp_path):
        """有成交但一字板（high == low）→ 输出「当日无有效振幅」，不产生 inf。"""
        _make_db(tmp_path / "trade.db", report_date="2026-09-17",
                 trades=[("600000.SH", DIRECTION_BUY, 11.81, _ts("2026-09-17"),
                          "manual", None, None)])
        _make_kline("600000.SH", "2026-09-17", high=11.81, low=11.81,
                    close=11.81, volume=1000.0)
        r = drev.build_review(asof="2026-09-17", with_market=False)
        text = "\n".join(r["behavior"])
        assert "当日无有效振幅" in text
        assert "inf" not in text and "nan" not in text.lower()

    def test_normal_bar_computes_position(self, monkeypatch, tmp_path):
        _make_db(tmp_path / "trade.db", report_date="2026-09-17",
                 trades=[("600000.SH", DIRECTION_BUY, 9.8, _ts("2026-09-17"),
                          "manual", None, None)])
        _make_kline("600000.SH", "2026-09-17", high=10.0, low=8.0, close=9.0)
        r = drev.build_review(asof="2026-09-17", with_market=False)
        text = "\n".join(r["behavior"])
        assert "90%" in text and "买在当日偏高位置" in text


class TestCumulative:
    def test_reuses_trade_analysis_month_view(self, monkeypatch, tmp_path):
        """§13.2/§13.6 验收 2：月度口径**复用** `trade.analysis.build_daily_pnl_view`。

        用"偷梁换柱"的方式证明它真的被调用了（而不是自己又写了一份聚合）。
        """
        _make_db(tmp_path / "trade.db", report_date="2026-09-17",
                 assets=[("2026-08-29", 1_000_000.0), ("2026-09-01", 1_010_000.0),
                         ("2026-09-17", 1_020_000.0)])
        import trade.analysis as ta
        calls = []
        orig = ta.build_daily_pnl_view

        def spy(*a, **k):
            calls.append(1)
            return orig(*a, **k)
        monkeypatch.setattr(ta, "build_daily_pnl_view", spy)
        r = drev.build_review(asof="2026-09-17", with_market=False)
        assert calls, "必须复用 trade.analysis.build_daily_pnl_view, 不许另写月度聚合"
        assert "本月至今" in "\n".join(r["cumulative"])

    def test_recent_window_summary(self, monkeypatch, tmp_path):
        trades = [("600000.SH", DIRECTION_SELL, 10.0, _ts("2026-09-17"),
                   "cost_stop", -500.0, -5.0),
                  ("600001.SH", DIRECTION_SELL, 20.0, _ts("2026-09-17") + 100,
                   "ladder_tp", 800.0, 4.0)]
        _make_db(tmp_path / "trade.db", report_date="2026-09-17", trades=trades)
        r = drev.build_review(asof="2026-09-17", with_market=False)
        text = "\n".join(r["cumulative"])
        assert "胜率" in text and "最大单笔亏损" in text
        assert "最近 2 笔卖出" in text, f"分母只该数卖出：{text}"

    def test_win_rate_denominator_excludes_buys(self, monkeypatch, tmp_path):
        """审计 F-05 回归锁：**买入不许进胜率分母**。

        买入那行的 `pnl_amount` 是 `0`（`trade/store.py` 里该列 `REAL NOT NULL DEFAULT 0`），
        所以按「`pnl_amount` 不为空」筛卖出等于没筛 —— 生产库实测最近 200 行里有 58 行是买入，
        胜率被摊薄成 20%，而真实（只算卖出且非零）是 38%。
        这里 2 笔卖出（1 赚 1 亏）+ 3 笔买入 → 必须报 50%、且明写「最近 2 笔卖出」。
        """
        trades = [("600000.SH", DIRECTION_SELL, 10.0, _ts("2026-09-17"),
                   "cost_stop", -500.0, -5.0),
                  ("600001.SH", DIRECTION_SELL, 20.0, _ts("2026-09-17") + 100,
                   "ladder_tp", 800.0, 4.0)]
        trades += [("60010%d.SH" % i, DIRECTION_BUY, 10.0, _ts("2026-09-17") + 200 + i,
                    "manual", 0.0, 0.0) for i in range(3)]
        _make_db(tmp_path / "trade.db", report_date="2026-09-17", trades=trades)
        r = drev.build_review(asof="2026-09-17", with_market=False)
        text = "\n".join(r["cumulative"])
        assert "最近 2 笔卖出" in text, f"买入混进来了：{text}"
        assert "胜率 50%" in text, f"胜率该是 1/2 = 50%：{text}"
        # 今天那段也一样：卖出只数卖出
        assert "卖出 2 笔" in text, f"今日卖出笔数被买入污染：{text}"

    def test_unrecorded_pnl_sells_leave_the_denominator(self, monkeypatch, tmp_path):
        """审计 F-05 第二半：**没有盈亏记录的卖出既不算赚也不算亏**，且必须写明笔数。

        生产库 212 笔卖出里有 105 笔 `pnl_amount`/`pnl_pct` 恰恰都是 0，日期全落在
        2026-07-30~2026-08-07 —— 那是 `trades` 表还没有这两列的时候（2026-08-07 才加），
        它们是「没记」不是「打平」。把它们当没赚钱 → 胜率 20%；只算有记录的 → 38%。
        """
        trades = [("600000.SH", DIRECTION_SELL, 10.0, _ts("2026-09-17"),
                   "cost_stop", -500.0, -5.0),
                  ("600001.SH", DIRECTION_SELL, 20.0, _ts("2026-09-17") + 100,
                   "ladder_tp", 800.0, 4.0),
                  ("600002.SH", DIRECTION_SELL, 15.0, _ts("2026-09-17") + 200,
                   "manual", 0.0, 0.0)]      # 没有盈亏记录的那种
        _make_db(tmp_path / "trade.db", report_date="2026-09-17", trades=trades)
        r = drev.build_review(asof="2026-09-17", with_market=False)
        text = "\n".join(r["cumulative"])
        assert "胜率 50%" in text, f"没记录的那笔不该进分母：{text}"
        assert "按能算出盈亏的 2 笔算" in text, f"必须说明分母是几笔：{text}"
        assert "其中 1 笔没有盈亏记录" in text, f"必须披露被排除的笔数：{text}"

    def test_direction_caliber_is_imported_not_reinvented(self):
        """审计 F-01 回归锁：方向判据必须是 `trade.book` 那一份（23/24）。

        原来本文件自己写了「0=买 / 1=卖」，与生产库不符 → 每笔买入都判不出来，
        报告会印「今日无买入成交」并把买入当卖出。这里锁死「只认 23/24」。
        """
        assert drev._DIR_BUY == DIRECTION_BUY == 23
        assert drev._DIR_SELL == DIRECTION_SELL == 24
        assert drev._is_buy({"direction": DIRECTION_BUY}) is True
        assert drev._is_sell({"direction": DIRECTION_SELL}) is True
        assert drev._is_buy({"direction": DIRECTION_SELL}) is False
        assert drev._is_sell({"direction": DIRECTION_BUY}) is False
        # 那个害人的旧口径必须**判不出来**（宁可漏判并告警，也不许猜反方向）
        assert drev._is_buy({"direction": 0}) is False
        assert drev._is_sell({"direction": 1}) is False


class TestDidWell:
    def test_no_trades_says_so(self, monkeypatch, tmp_path):
        _make_db(tmp_path / "trade.db", report_date="2026-09-17")
        r = drev.build_review(asof="2026-09-17", with_market=False)
        assert "没有可算的亮点" in "\n".join(r["did_well"])

    def test_never_empty(self, monkeypatch, tmp_path):
        """§12.5 验收 1：无亮点时明写「没有特别值得点出来的亮点」，不许略过。"""
        _make_db(tmp_path / "trade.db", report_date="2026-09-17",
                 trades=[("600000.SH", DIRECTION_SELL, 10.0, _ts("2026-09-17"),
                          "manual", -100.0, -1.0)])
        r = drev.build_review(asof="2026-09-17", with_market=False)
        assert r["did_well"]


class TestRunAndWrite:
    def test_run_writes_review_file(self, monkeypatch, tmp_path):
        _make_db(tmp_path / "trade.db", report_date="2026-09-17")
        res = drev.run_daily_review(asof="2026-09-17", write=True, push=False)
        assert res["ok"] and res["path"]
        p = Path(res["path"])
        assert p.exists() and p.parent == drev.REVIEW_DIR
        assert p.name == "2026-09-17.md"
        assert "盘后复盘 2026-09-17" in p.read_text(encoding="utf-8")

    def test_review_dir_is_not_a_rag_corpus_root(self):
        """§13.6 验收 4（与 RAG §12.6 联验）：复盘落盘路径**不在 RAG 语料范围内**。

        注意 fixture 会把 REVIEW_DIR 指到 tmp，所以这里断言的是**源码里的生产常量**
        （tmp 路径当然也可能在 data 下，那样的断言没有意义）。
        """
        import re

        from brain.search_engine import _index_config, _is_excluded
        src = (ROOT / "notes_gen" / "daily.py").read_text(encoding="utf-8")
        m = re.search(r'REVIEW_DIR = _ROOT / "([^"]+)" / "([^"]+)"', src)
        assert m, "没找到 REVIEW_DIR 的定义"
        rel = f"{m.group(1)}/{m.group(2)}"
        assert rel.startswith("data/"), f"复盘应落 data/ 下, 实际 {rel}"
        assert _is_excluded(ROOT / rel / "2026-09-17.md",
                            tuple(_index_config()["exclude_dirs"])), \
            "复盘报告落进了 RAG 语料范围 —— 系统每天生成一份会污染自己的检索库"

    def test_push_fails_soft_without_webhook(self, monkeypatch, tmp_path):
        _make_db(tmp_path / "trade.db", report_date="2026-09-17")
        import tools.send_report_feishu as srf
        monkeypatch.setattr(srf, "load_webhook",
                            lambda: (_ for _ in ()).throw(SystemExit("no webhook")))
        r = drev.build_review(asof="2026-09-17", with_market=False)
        out = drev.push_review(r)
        assert out["ok"] is False and "FEISHU_WEBHOOK_URL" in out["feishu"]["reason"]


class TestAstGuards:
    """铁律 1 的守护是**双向**的（§13.3 M8）。"""

    def test_forward_no_trade_import_in_market_position(self):
        """正向：大盘位置模块绝不 import trade（既有守护，这里再确认一次）。"""
        for rel in ("core/market_position.py", "core/market_position_runner.py",
                    "market_position_api.py"):
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    assert not any(a.name.startswith("trade") for a in node.names), rel
                elif isinstance(node, ast.ImportFrom):
                    assert not (node.module or "").startswith("trade"), rel

    def test_reverse_no_trade_module_imports_daily_review(self):
        """**反向**：`trade_main.py` 与 `trade/` 下不许 import `notes_gen.daily`。

        否则"报告 → 交易"的反馈环就通了：报告线一旦被交易线引用，
        它就不再是"只读参考"，违反业务铁律 1。
        """
        targets = [ROOT / "trade_main.py"] + sorted((ROOT / "trade").glob("*.py"))
        assert len(targets) > 3, "没扫到 trade 模块，守护失效"
        for p in targets:
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                mod = ""
                if isinstance(node, ast.Import):
                    mod = ",".join(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                assert "notes_gen" not in mod, f"{p.name} 引用了 notes_gen —— 反馈环通了"

    def test_daily_review_imports_market_position_one_way(self):
        """复盘编排器**可以**同时 import 两边（隔离靠它在更外层实现）。"""
        src = (ROOT / "notes_gen" / "daily.py").read_text(encoding="utf-8")
        assert "market_position_runner" in src and "trade.config" in src
