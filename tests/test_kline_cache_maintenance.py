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
