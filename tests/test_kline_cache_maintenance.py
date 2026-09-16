"""tests/test_kline_cache_maintenance.py — 缓存新鲜度检查 + 后台补拉护栏。

不碰 TDX/网络：子进程调用与线程全程 mock，manifest 用 tmp sqlite。
锁定：最近交易日判定 / 新鲜度比对 / 新鲜秒回 / 不新鲜起后台补拉 /
锁文件跨进程防重入 / 异常 fail-soft 不挡主流程。
"""
from __future__ import annotations

import datetime as dt
import sqlite3

import core.kline_cache_maintenance as m


def _make_manifest(db_path, rows):
    """rows: [(stock_code, period, last_date), ...]"""
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE manifest (stock_code TEXT, period TEXT, "
                 "first_date TEXT, last_date TEXT, last_close REAL, "
                 "rows INTEGER, fetched_at TEXT, intact INTEGER)")
    conn.executemany(
        "INSERT INTO manifest VALUES (?,?,?,?,NULL,0,'',1)",
        [(c, p, "20200101", d) for c, p, d in rows])
    conn.commit()
    conn.close()


class TestExpectedLastTradingDay:
    def test_交易日收盘后等于今天(self):
        assert m.expected_last_trading_day(
            dt.datetime(2026, 8, 14, 15, 30)) == dt.date(2026, 8, 14)  # 周五

    def test_交易日盘中算昨天(self):
        assert m.expected_last_trading_day(
            dt.datetime(2026, 8, 14, 10, 0)) == dt.date(2026, 8, 13)  # 周四

    def test_周末回退到周五(self):
        assert m.expected_last_trading_day(
            dt.datetime(2026, 8, 16, 12, 0)) == dt.date(2026, 8, 14)  # 周日→周五


class TestStalePeriods:
    def test_全新鲜(self, tmp_path):
        _make_manifest(tmp_path / "manifest.db", [
            ("000001.SZ", "5m", "20260813"), ("000001.SZ", "1d", "20260813"),
            ("000001.SZ", "1m", "20260813")])
        got = m.stale_periods(dt.datetime(2026, 8, 14, 10, 0), tmp_path)
        assert got == []

    def test_5m过期1d新鲜(self, tmp_path):
        _make_manifest(tmp_path / "manifest.db", [
            ("000001.SZ", "5m", "20260731"), ("000001.SZ", "1d", "20260813"),
            ("000001.SZ", "1m", "20260813")])
        got = m.stale_periods(dt.datetime(2026, 8, 14, 10, 0), tmp_path)
        assert got == ["5m"]

    def test_无manifest按全缺(self, tmp_path):
        got = m.stale_periods(dt.datetime(2026, 8, 14, 10, 0), tmp_path)
        assert got == ["5m", "1d", "1m"]


# ── 空壳 bar 识别 (2026-09-17) ────────────────────────────────────
# 背景: 盘前抓数会造出"有日期、无成交"的空壳 bar。实测 2026-09-16 沪深
# 5204 只票都有该日行, 但只有 10 只有成交 (0.2%)。只比 MAX(last_date)
# 会把它判成"新鲜" → 补拉永不触发 → 空壳永远卡住、体温表永远显示"数据滞后"。
# 计划书: docs/plan/2026-09-17_大盘位置与趋势研判_计划书.md §18


def _make_daily_cache(cache_dir, rows):
    """造 <cache_dir>/1d/<code>.parquet + manifest。

    rows: [(code, [(date_str, volume), ...]), ...] —— 末条即"最后一根 bar"。
    """
    import pandas as pd

    d = cache_dir / "1d"
    d.mkdir(parents=True, exist_ok=True)
    for code, bars in rows:
        pd.DataFrame({
            "date": pd.to_datetime([b[0] for b in bars]),
            "open": [10.0] * len(bars), "high": [10.0] * len(bars),
            "low": [10.0] * len(bars), "close": [10.0] * len(bars),
            "volume": [float(b[1]) for b in bars],
            "amount": [1000.0] * len(bars),
        }).to_parquet(d / f"{code}.parquet", index=False)
    conn = sqlite3.connect(str(cache_dir / "manifest.db"))
    conn.execute("CREATE TABLE manifest (stock_code TEXT, period TEXT, "
                 "first_date TEXT, last_date TEXT, last_close REAL, "
                 "rows INTEGER, fetched_at TEXT, intact INTEGER)")
    conn.executemany(
        "INSERT INTO manifest VALUES (?,?,?,?,NULL,0,'',1)",
        [(c, "1d", "20260801", bars[-1][0].replace("-", ""))
         for c, bars in rows])
    conn.commit()
    conn.close()


class TestStubBarDetection:
    def test_末根全无成交判为陈旧(self, tmp_path):
        _make_daily_cache(tmp_path, [
            (f"60000{i}.SH", [("2026-08-13", 1000), ("2026-08-14", 0)])
            for i in range(6)])
        got = m.stale_periods(dt.datetime(2026, 8, 14, 16, 0), tmp_path)
        assert "1d" in got, "末根无成交却没判陈旧 —— 空壳 bar 会永远卡在缓存里"

    def test_末根有成交不误报(self, tmp_path):
        _make_daily_cache(tmp_path, [
            (f"60000{i}.SH", [("2026-08-13", 1000), ("2026-08-14", 1000)])
            for i in range(6)])
        got = m.stale_periods(dt.datetime(2026, 8, 14, 16, 0), tmp_path)
        assert "1d" not in got

    def test_抽检比例算得对(self, tmp_path):
        """4 只里 1 只有成交 → 0.25。"""
        _make_daily_cache(tmp_path, [
            ("600000.SH", [("2026-08-14", 1000)]),
            ("600001.SH", [("2026-08-14", 0)]),
            ("600002.SH", [("2026-08-14", 0)]),
            ("600003.SH", [("2026-08-14", 0)]),
        ])
        assert m.last_bar_traded_ratio("1d", tmp_path) == 0.25

    def test_查不了返None且维持旧行为(self, tmp_path):
        """没有 parquet 文件 → 抽检"查不了" → **不得**据此判陈旧。

        这条是安全阀: 读盘抖动若被判成陈旧, 会误触发小时级的全量补拉。
        """
        _make_manifest(tmp_path / "manifest.db", [("000001.SZ", "1d", "20260814")])
        assert m.last_bar_traded_ratio("1d", tmp_path) is None
        got = m.stale_periods(dt.datetime(2026, 8, 14, 16, 0), tmp_path)
        assert "1d" not in got

    def test_5m不做末根抽检(self, tmp_path):
        """5m/1m 不抽检 —— 盘中当前日 bar 天然可能零成交, 抽检会误报一整天陈旧。"""
        import pandas as pd

        d = tmp_path / "5m"
        d.mkdir(parents=True, exist_ok=True)
        for i in range(4):
            pd.DataFrame({
                "date": pd.to_datetime(["2026-08-14 09:35", "2026-08-14 14:55"]),
                "open": [10.0, 10.0], "high": [10.0, 10.0],
                "low": [10.0, 10.0], "close": [10.0, 10.0],
                "volume": [0.0, 0.0], "amount": [0.0, 0.0],
            }).to_parquet(d / f"60000{i}.SH.parquet", index=False)
        conn = sqlite3.connect(str(tmp_path / "manifest.db"))
        conn.execute("CREATE TABLE manifest (stock_code TEXT, period TEXT, "
                     "first_date TEXT, last_date TEXT, last_close REAL, "
                     "rows INTEGER, fetched_at TEXT, intact INTEGER)")
        conn.executemany("INSERT INTO manifest VALUES (?,?,?,?,NULL,0,'',1)",
                         [(f"60000{i}.SH", "5m", "20260801", "20260814")
                          for i in range(4)])
        conn.commit()
        conn.close()
        got = m.stale_periods(dt.datetime(2026, 8, 14, 16, 0), tmp_path)
        assert "5m" not in got, "5m 不该做末根抽检"


class TestEnsureCacheFresh:
    def _patch_paths(self, monkeypatch, tmp_path):
        monkeypatch.setattr(m, "_CACHE_DIR", tmp_path)
        monkeypatch.setattr(m, "_LOCK", tmp_path / "refresh.lock")
        monkeypatch.setattr(m, "_REFRESH_LOG", tmp_path / "refresh.log")
        monkeypatch.setattr(m, "_thread", None)

    def test_新鲜不起线程(self, monkeypatch, tmp_path):
        self._patch_paths(monkeypatch, tmp_path)
        _make_manifest(tmp_path / "manifest.db", [
            ("000001.SZ", "5m", "20990101"), ("000001.SZ", "1d", "20990101"),
            ("000001.SZ", "1m", "20990101")])
        r = m.ensure_cache_fresh("test")
        assert r == {"fresh": True, "stale": [], "refreshing": False}
        assert m._thread is None

    def test_不新鲜起后台线程(self, monkeypatch, tmp_path):
        self._patch_paths(monkeypatch, tmp_path)
        _make_manifest(tmp_path / "manifest.db", [
            ("000001.SZ", "5m", "20200101"), ("000001.SZ", "1d", "20200101"),
            ("000001.SZ", "1m", "20200101")])
        started = []

        class FakeThread:
            def __init__(self, target=None, args=(), name=None, daemon=None):
                self.kw = {"target": target, "args": args, "daemon": daemon}
            def start(self):
                started.append(self.kw)
            def is_alive(self):
                return False
        monkeypatch.setattr(m.threading, "Thread", FakeThread)
        r = m.ensure_cache_fresh("test")
        assert r["fresh"] is False and r["refreshing"] is True
        assert r["stale"] == ["5m", "1d", "1m"]
        assert len(started) == 1 and started[0]["daemon"] is True
        assert (tmp_path / "refresh.lock").exists()   # 锁已落

    def test_锁文件占用则跳过(self, monkeypatch, tmp_path):
        self._patch_paths(monkeypatch, tmp_path)
        _make_manifest(tmp_path / "manifest.db", [
            ("000001.SZ", "5m", "20200101")])
        (tmp_path / "refresh.lock").write_text("other process", encoding="utf-8")
        monkeypatch.setattr(m.threading, "Thread",
                            lambda **kw: (_ for _ in ()).throw(
                                AssertionError("不应起线程")))
        r = m.ensure_cache_fresh("test")
        assert r["refreshing"] is True and r["fresh"] is False

    def test_超时锁自动收尸(self, monkeypatch, tmp_path):
        self._patch_paths(monkeypatch, tmp_path)
        _make_manifest(tmp_path / "manifest.db", [
            ("000001.SZ", "5m", "20200101")])
        lock = tmp_path / "refresh.lock"
        lock.write_text("dead process", encoding="utf-8")
        old = dt.datetime.now() - dt.timedelta(hours=5)  # 超过 4h TTL
        import os
        os.utime(lock, (old.timestamp(), old.timestamp()))
        assert m._lock_held() is False
        assert not lock.exists()   # 收尸删掉

    def test_异常fail_soft(self, monkeypatch, tmp_path):
        self._patch_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(m, "stale_periods",
                            lambda *a, **kw: (_ for _ in ()).throw(
                                RuntimeError("炸了")))
        r = m.ensure_cache_fresh("test")
        assert r["fresh"] is True   # 异常不挡主流程

    def test_默认股票池是沪深A股(self, monkeypatch, tmp_path):
        """2026-08-14 拍板：不拉北交所。start_backfill 缺省 universe 必须是 "50"。"""
        self._patch_paths(monkeypatch, tmp_path)
        started = []

        class FakeThread:
            def __init__(self, target=None, args=(), name=None, daemon=None):
                started.append(args)   # (trigger, segs, universe, end, limit)
            def start(self):
                pass
            def is_alive(self):
                return False
        monkeypatch.setattr(m.threading, "Thread", FakeThread)
        monkeypatch.setattr(m, "_lock_held", lambda: False)
        m.start_backfill(trigger="test")
        assert started[0][2] == "50", "缺省股票池必须沪深A股（不含 .BJ）"

    def test_补拉命令带universe(self, monkeypatch, tmp_path):
        """_run_refresh 拼给 backfill 工具的命令必须带 --universe。"""
        self._patch_paths(monkeypatch, tmp_path)
        calls = []
        monkeypatch.setattr("subprocess.call",
                            lambda cmd, **kw: calls.append(cmd) or 0)
        m._run_refresh("test", [("5m", "20240627")], "50", "", 0)
        assert "--universe" in calls[0]
        assert calls[0][calls[0].index("--universe") + 1] == "50"


class TestLockOwnership:
    """锁 JSON 归属 (2026-09-05 体检 P1): 谁在拉/何时开始, 不再只留时间戳。"""

    def _patch(self, monkeypatch, tmp_path):
        monkeypatch.setattr(m, "_CACHE_DIR", tmp_path)
        monkeypatch.setattr(m, "_LOCK", tmp_path / "refresh.lock")

    def test_start_backfill写JSON归属(self, monkeypatch, tmp_path):
        self._patch(monkeypatch, tmp_path)
        started = []

        class FakeThread:
            def __init__(self, target=None, args=(), name=None, daemon=None):
                started.append(args)
            def start(self):
                pass
            def is_alive(self):
                return False
        monkeypatch.setattr(m.threading, "Thread", FakeThread)
        monkeypatch.setattr(m, "_lock_held", lambda: False)
        m.start_backfill(trigger="scheduler_daily")
        owner = m._lock_owner()
        assert owner is not None
        assert owner["trigger"] == "scheduler_daily"
        assert owner["pid"] == __import__("os").getpid()
        assert owner["ts"]

    def test_held_by返回持有者描述(self, monkeypatch, tmp_path):
        self._patch(monkeypatch, tmp_path)
        (tmp_path / "refresh.lock").write_text(
            __import__("json").dumps(
                {"pid": 123, "trigger": "server_startup", "ts": "2026-09-05T10:00:00"}),
            encoding="utf-8")
        desc = m._lock_held_by()
        assert "123" in desc and "server_startup" in desc and "10:00" in desc

    def test_无锁时描述为空(self, monkeypatch, tmp_path):
        self._patch(monkeypatch, tmp_path)
        assert m._lock_held_by() == ""

    def test_坏内容归属可读不炸(self, monkeypatch, tmp_path):
        self._patch(monkeypatch, tmp_path)
        (tmp_path / "refresh.lock").write_text("old style raw text",
                                               encoding="utf-8")
        assert m._lock_owner() is None      # 旧版纯文本锁 → 归属未知
        assert m._lock_held_by() == ""      # 但读锁不炸
