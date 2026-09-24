# -*- coding: utf-8 -*-
"""tests/test_scheduler_health.py — 调度器存活判读 (2026-09-20)。

为什么要有这个文件：

1. **三态必须真的分得开**。若 `stopped` 与 `down` 不分，用户每次正常
   `stop_vera.bat` 停机，首页都会喊"调度器死了"—— 假警报多了就没人看了，
   等于没做可见性。这里逐态锁住判据。

2. **停止标记的路径是跨语言契约**：`.bat`(GBK) 写、Python 读。
   两处路径写错一个，标记就永远读不到，而**症状是"静默不生效"**
   （页面永远显示 running）—— 正是本项目反复踩的那类坑。
   故有用例**直接读 .bat 源文件**比对路径常量。

3. **心跳不能拿 state_path 的 mtime 顶替**：那个只在 job 真触发时才写，
   daily job 一天可能只触发一两次 —— 拿它当心跳会把"正常空闲"误报成
   "已停机"。这正是本项目当前的真实状态（`scheduler_state.json` 停在
   09-18 17:30，而调度器当时是活的）。
"""
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scheduler import health as H  # noqa: E402
from scheduler.vera_scheduler import VeraScheduler  # noqa: E402


# ── 三态判读 ────────────────────────────────────────────────────────

class TestStatus:
    def _hb(self, tmp_path, ts, name="hb.json"):
        import json
        p = tmp_path / name
        p.write_text(json.dumps({"ts": ts, "iso": "2026-09-20 02:00:00",
                                 "pid": 1234, "jobs": 14}), encoding="utf-8")
        return p

    def test_心跳新鲜_running(self, tmp_path):
        now = 1_000_000.0
        hb = self._hb(tmp_path, now - 10)
        st = H.status(heartbeat_path=hb, marker_path=tmp_path / "none",
                      now=now)
        assert st["state"] == "running" and st["age_s"] == pytest.approx(10.0)

    def test_心跳过期_down(self, tmp_path):
        now = 1_000_000.0
        hb = self._hb(tmp_path, now - 3600)
        st = H.status(heartbeat_path=hb, marker_path=tmp_path / "none", now=now)
        assert st["state"] == "down" and "已经死" in st["note"]

    def test_无心跳文件_down(self, tmp_path):
        st = H.status(heartbeat_path=tmp_path / "missing",
                      marker_path=tmp_path / "none", now=1.0)
        assert st["state"] == "down"
        assert "从未启动" in st["note"] or "立即退出" in st["note"]

    def test_心跳损坏_down不抛(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{不是 json", encoding="utf-8")
        st = H.status(heartbeat_path=bad, marker_path=tmp_path / "none", now=1.0)
        assert st["state"] == "down"

    def test_有停止标记一律_stopped_哪怕心跳很旧(self, tmp_path):
        """标记优先于心跳 —— 人工停了就是停了，不许再报 down。"""
        now = 1_000_000.0
        hb = self._hb(tmp_path, now - 99999)
        mk = tmp_path / ".vera_stopped"
        mk.write_text("停止于 ...", encoding="utf-8")
        st = H.status(heartbeat_path=hb, marker_path=mk, now=now)
        assert st["state"] == "stopped"
        assert "人工停止" in st["note"]

    def test_有停止标记且无心跳也是_stopped(self, tmp_path):
        mk = tmp_path / ".vera_stopped"
        mk.write_text("x", encoding="utf-8")
        st = H.status(heartbeat_path=tmp_path / "missing", marker_path=mk, now=1.0)
        assert st["state"] == "stopped", "刚停完还没清标记时不许报 down"

    def test_阈值边界(self, tmp_path):
        now = 1_000_000.0
        just_under = self._hb(tmp_path, now - (H.STALE_AFTER_S - 1), "a.json")
        just_over = self._hb(tmp_path, now - (H.STALE_AFTER_S + 1), "b.json")
        assert H.status(heartbeat_path=just_under, marker_path=tmp_path / "n",
                        now=now)["state"] == "running"
        assert H.status(heartbeat_path=just_over, marker_path=tmp_path / "n",
                        now=now)["state"] == "down"

    def test_判活阈值必须显著大于心跳写间隔(self):
        """否则正常写入间隙就会被判死 —— 假警报。"""
        from scheduler.vera_scheduler import _HEARTBEAT_MIN_INTERVAL_S
        assert H.STALE_AFTER_S >= _HEARTBEAT_MIN_INTERVAL_S * 3


# ── 心跳真的被写出来 ────────────────────────────────────────────────

class TestHeartbeatWritten:
    def test_调度器确实落心跳文件(self, tmp_path):
        hb = tmp_path / "hb.json"
        s = VeraScheduler(tick_seconds=0.01, heartbeat_path=str(hb))
        s.start(block=False)
        try:
            deadline = time.time() + 5
            while time.time() < deadline and not hb.exists():
                time.sleep(0.05)
        finally:
            s.stop()
        assert hb.exists(), "调度器没写心跳 —— 首页判活会永远显示 down"
        data = H.read_heartbeat(hb)
        assert isinstance(data, dict) and data["ts"] > 0
        assert data["jobs"] == 0 and data["pid"] > 0

    def test_不给_heartbeat_path_就不写文件(self, tmp_path):
        """旧行为不变：不传参 = 零副作用（既有测试与批量脚本不受影响）。"""
        s = VeraScheduler(tick_seconds=0.01)
        s.start(block=False)
        try:
            time.sleep(0.2)
        finally:
            s.stop()
        assert not list(tmp_path.glob("*.json"))

    def test_心跳写入失败不影响调度(self, tmp_path):
        """路径不可写(拿目录当文件) → 只记 warning, 调度照跑。"""
        bad = tmp_path / "adir"
        bad.mkdir()
        s = VeraScheduler(tick_seconds=0.01, heartbeat_path=str(bad))
        s.start(block=False)
        try:
            time.sleep(0.2)
            assert s._thread is not None and s._thread.is_alive()
        finally:
            s.stop()


# ── 跨语言路径契约（.bat ↔ Python）────────────────────────────────

class TestStopMarkerContract:
    """`.bat` 用 GBK 写, Python 读 —— 路径写错就静默不生效, 故直接读源文件比。"""

    @staticmethod
    def _bat_text(name):
        return Path(ROOT / name).read_bytes().decode("gbk")

    def test_stop_vera_写的路径与常量一致(self):
        txt = self._bat_text("stop_vera.bat")
        assert "data\\.vera_stopped" in txt, \
            "stop_vera.bat 没有写停止标记 —— '已人工停止' 状态永远出不来"
        assert H.STOP_MARKER_PATH.name == ".vera_stopped"

    def test_start_vera_清的是同一个路径(self):
        txt = self._bat_text("start_vera.bat")
        assert "del \"data\\.vera_stopped\"" in txt, \
            "start_vera.bat 没有清除停止标记 —— 重启后页面会一直显示'已人工停止'"

    def test_两个bat的路径字面一致(self):
        a = "data\\.vera_stopped"
        assert a in self._bat_text("stop_vera.bat")
        assert a in self._bat_text("start_vera.bat")

    def test_常量落在_data_下(self):
        """不该落到 data/trade/（铁律 1 的边界）。"""
        s = str(H.STOP_MARKER_PATH).replace("\\", "/")
        assert "/data/" in s and "data/trade" not in s
        assert "data/trade" not in str(H.HEARTBEAT_PATH).replace("\\", "/")


# ── 铁律 1 ──────────────────────────────────────────────────────────

def test_ast_health_no_trade_import():
    import ast
    with open(H.__file__, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert not a.name.startswith("trade")
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("trade")
