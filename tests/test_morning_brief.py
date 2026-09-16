"""tests/test_morning_brief.py — 隔夜简报（2026-09-17 M7 补做，计划书 §17.4/§17.5）。

全程离线：盘面快照与昨日位置都打桩，不联网、不推飞书、不碰生产落盘路径。
覆盖 §17.5 验收 1/3 的两条硬规则：**没有隔夜新信息就不发空卡**、
**昨天 15:55 没推成则附带补发**。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from notes_gen import morning as mb  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(mb, "STATE_PATH", tmp_path / "scheduler_state.json")
    monkeypatch.setattr(mb, "_ROOT", tmp_path)


class TestRules:
    def test_no_overnight_info_does_not_send(self, monkeypatch):
        """§17.5-3 硬规则 1：没有隔夜新信息 → **不发空卡**。"""
        monkeypatch.setattr(mb, "_market_snapshot", lambda: "")
        monkeypatch.setattr(mb, "_yesterday_line", lambda: "昨天收盘：上证在十年 90% 位置。")
        b = mb.build_brief()
        assert b["ok"] is False
        assert "不发空卡" in b["reason"]

    def test_yesterday_line_alone_is_not_enough(self, monkeypatch):
        """昨日位置是**背景**，单靠它不足以构成一封简报（否则每天都能发）。"""
        monkeypatch.setattr(mb, "_market_snapshot", lambda: "")
        monkeypatch.setattr(mb, "_yesterday_line", lambda: "昨天收盘：上证在十年 90% 位置。")
        assert mb.build_brief()["ok"] is False

    def test_with_overnight_info_it_sends(self, monkeypatch):
        monkeypatch.setattr(mb, "_market_snapshot", lambda: "美股三大指数隔夜收盘：…")
        monkeypatch.setattr(mb, "_yesterday_line", lambda: "昨天收盘：上证在十年 90% 位置。")
        b = mb.build_brief()
        assert b["ok"] is True
        md = mb.brief_md(b)
        assert "隔夜简报" in md and "美股三大指数隔夜收盘" in md
        assert "一句话背景" in md and "上证在十年 90% 位置" in md
        # 不许是体温表的复制
        assert "十年百分位" not in md and "照镜子" not in md

    def test_no_webhook_fails_soft(self, monkeypatch):
        monkeypatch.setattr(mb, "_market_snapshot", lambda: "隔夜有内容")
        monkeypatch.setattr(mb, "_yesterday_line", lambda: "")
        import tools.send_report_feishu as srf
        monkeypatch.setattr(srf, "load_webhook",
                            lambda: (_ for _ in ()).throw(SystemExit("no webhook")))
        out = mb.push_brief(mb.build_brief())
        assert out["ok"] is False and "FEISHU_WEBHOOK_URL" in out["reason"]

    def test_empty_brief_never_pushes(self, monkeypatch):
        monkeypatch.setattr(mb, "_market_snapshot", lambda: "")
        monkeypatch.setattr(mb, "_yesterday_line", lambda: "")
        out = mb.push_brief(mb.build_brief())
        assert out["ok"] is False and out["sent"] is False


class TestMissedPush:
    def test_no_state_file_means_no_makeup(self, monkeypatch):
        """状态文件不存在（调度器还没跑过）→ **不补发**（宁可漏，不多发）。"""
        assert mb._missed_evening_push() is None

    def test_last_run_ok_means_no_makeup(self, monkeypatch):
        mb.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        mb.STATE_PATH.write_text(json.dumps({
            "jobs": {mb.EVENING_JOB: {"last_run": "2026-09-17 15:55:01",
                                      "last_error": ""}}}), encoding="utf-8")
        assert mb._missed_evening_push() is None

    def test_last_run_failed_means_makeup(self, monkeypatch):
        mb.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        mb.STATE_PATH.write_text(json.dumps({
            "jobs": {mb.EVENING_JOB: {"last_run": "2026-09-17 15:55:01",
                                      "last_error": "飞书 webhook 未配置"}}}),
            encoding="utf-8")
        m = mb._missed_evening_push()
        assert m and m["date"] == "2026-09-17" and "webhook" in m["reason"]

    def test_broken_state_file_is_treated_as_not_missed(self):
        mb.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        mb.STATE_PATH.write_text("{ 这不是 json", encoding="utf-8")
        assert mb._missed_evening_push() is None

    def test_makeup_section_appears_in_markdown(self, monkeypatch):
        monkeypatch.setattr(mb, "_market_snapshot", lambda: "隔夜有内容")
        monkeypatch.setattr(mb, "_yesterday_line", lambda: "")
        monkeypatch.setattr(mb, "_missed_evening_push",
                            lambda: {"date": "2026-09-17", "reason": "推送失败"})
        md = mb.brief_md(mb.build_brief())
        assert "补发" in md and "2026-09-17" in md


class TestSchedulerWiring:
    def test_morning_job_now_runs_the_brief(self):
        """§17.5-1：09:05 的 job 内容**不得是体温表的复制**，改为隔夜简报。"""
        import ast
        import inspect

        from scheduler import __main__ as sm
        src = inspect.getsource(sm._job_market_position_morning)
        called = {n.attr for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Attribute)}
        called |= {n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)}
        assert "run_morning_brief" in called, "09:05 的 job 没有推隔夜简报"
        assert "push_thermometer" not in called, "09:05 又推体温表了（重复推送）"
        assert "collect" in called, "09:05 仍要兜底补采"

    def test_both_morning_and_evening_registered(self):
        import inspect

        from scheduler import __main__ as sm
        src = inspect.getsource(sm._register_market_position)
        assert '"09:05"' in src and '"15:55"' in src
