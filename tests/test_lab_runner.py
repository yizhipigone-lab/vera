# -*- coding: utf-8 -*-
"""core/lab_runner — 提交校验 / 去重 / 进度解析 / baseline 串联 / 互斥等待 测试"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.lab_runner import FORMULA_RE, LabQueue  # noqa: E402


def _wait(task_id, q, timeout=5):
    t0 = time.time()
    while time.time() - t0 < timeout:
        snap = q.snapshot()
        for t in snap["queue"]:
            if t["id"] == task_id and t["status"] in ("done", "failed", "cancelled"):
                return t
        time.sleep(0.05)
    raise TimeoutError("任务未在超时内完成")


def _fake_runner_ok(calls):
    def run(cmd, on_line=None):
        calls.append(cmd)
        if on_line:
            for ln in ["[S0] x: OK", "[S2] y: 复用", "[S4 A/B z] done", "[S5] ok"]:
                on_line(ln)
        return 0, "ok"
    return run


def test_formula_name_whitelist():
    assert FORMULA_RE.match("QUANTQQ") and FORMULA_RE.match("公式甲") and FORMULA_RE.match("UP-N2")
    assert not FORMULA_RE.match("../etc") and not FORMULA_RE.match("a b") and not FORMULA_RE.match("x;rm")


def test_submit_rejects_bad_formula():
    q = LabQueue(runner=_fake_runner_ok([]))
    ids, err = q.submit(["../evil"], "20250719_20260718", None)
    assert ids == [] and "非法字符" in err


def test_submit_dedupes_pending():
    calls = []
    def slow(cmd, on_line=None):
        time.sleep(0.3)
        calls.append(cmd)
        return 0, "ok"
    q = LabQueue(runner=slow)
    ids1, _ = q.submit(["FAKEA"], "20250719_20260718", None)
    ids2, err2 = q.submit(["FAKEA"], "20250719_20260718", None)
    assert ids1 and ids2 == [] and "同公式" in err2


def test_baseline_chained_when_missing_then_lab():
    """基线 parquet 不存在的公式 → 先 run_upn_baseline 后 formula_lab。"""
    calls = []
    q = LabQueue(runner=_fake_runner_ok(calls))
    ids, err = q.submit(["NOSUCHFX"], "20250719_20260718", "20230719_20260718")
    assert not err
    t = _wait(ids[0], q)
    assert t["status"] == "done", t
    baselines = [c for c in calls if "run_upn_baseline" in c[0]]
    labs = [c for c in calls if "formula_lab" in c[0]]
    assert len(baselines) == 2 and len(labs) == 1          # 两个窗口各补一次
    assert calls.index(baselines[0]) < calls.index(labs[0])
    # 2026-07-25: 体检基线固定全市场 type=50 (不随 default.yaml universe 漂移)
    for c in baselines:
        assert "--universe-type" in c and "50" in c


def test_baseline_skipped_when_present():
    calls = []
    q = LabQueue(runner=_fake_runner_ok(calls))
    ids, _ = q.submit(["QUANTQQ"], "20250719_20260718", "20230719_20260718")
    t = _wait(ids[0], q, timeout=10)
    assert t["status"] == "done", t
    assert not any("run_upn_baseline" in c[0] for c in calls)  # 基线已在, 不重复补


def test_failed_baseline_marks_task_failed():
    def bad(cmd, on_line=None):
        return 1, "boom"
    q = LabQueue(runner=bad)
    ids, _ = q.submit(["NOSUCHFX2"], "20250719_20260718", None)
    t = _wait(ids[0], q)
    assert t["status"] == "failed" and "基线生成失败" in t["error"]


def test_worker_waits_while_pipeline_busy():
    calls = []
    busy = {"v": True}
    q = LabQueue(pipeline_busy=lambda: busy["v"], runner=_fake_runner_ok(calls))
    ids, _ = q.submit(["QUANTQQ"], "20250719_20260718", None)
    time.sleep(0.3)
    assert not calls                                  # 回测在跑 → 任务等待
    busy["v"] = False
    t = _wait(ids[0], q, timeout=10)
    assert t["status"] == "done" and calls


def test_progress_parsing_updates_stage():
    q = LabQueue(runner=_fake_runner_ok([]))
    task_seen = {}
    # 直接调 _on_line 验证解析
    from core.lab_runner import LabTask
    t = LabTask(formulas=["X"], tag="t", tag2=None)
    q._on_line(t, "[S4 A/B 20250719] xxx")
    assert t.stage == "S4"
    q._on_line(t, "garbage line")
    assert t.stage == "S4"                              # 非标记行不覆盖


# ── 2026-07-25: 停止体检 (stop_current) ─────────────────────────

def test_stop_current_idle_returns_false():
    q = LabQueue(runner=_fake_runner_ok([]))
    ok, msg = q.stop_current()
    assert not ok and "无运行中" in msg


def test_stop_running_task_marks_cancelled():
    """运行中的任务被 stop → cancelled (不记 error), 队列继续跑下一个。"""
    calls = []
    q = None
    def runner(cmd, on_line=None):
        calls.append(cmd)
        if "formula_lab" in cmd[0] and "STOPME" in cmd:
            q.stop_current()          # 模拟用户在体检阶段点停止
            return -15, "killed"      # 子进程被杀后的非零返回
        return 0, "ok"
    q = LabQueue(runner=runner)
    ids1, _ = q.submit(["STOPME"], "20250719_20260718", None)
    ids2, _ = q.submit(["NEXTFX"], "20250719_20260718", None)
    t1 = _wait(ids1[0], q)
    t2 = _wait(ids2[0], q)
    assert t1["status"] == "cancelled" and t1["error"] == ""
    assert t2["status"] == "done"                      # 队列不被取消卡死


def test_stop_kills_real_subprocess():
    """真实 Popen 路径: 长眠子进程被 stop_current 杀掉, 任务秒级变 cancelled。"""
    q = LabQueue()
    def sleeper(cmd, on_line=None):
        # 忽略业务命令, 跑一个真实 60s 长眠进程验证 kill
        return q._subprocess_run(["-c", "import time; time.sleep(60)"])
    q._runner = sleeper
    ids, err = q.submit(["SLEEPFX"], "20250101_20260101", None)
    assert not err
    t0 = time.time()
    while q._current_proc is None:                     # 等子进程起来
        assert time.time() - t0 < 5, "子进程未启动"
        time.sleep(0.05)
    ok, msg = q.stop_current()
    assert ok
    t = _wait(ids[0], q, timeout=10)                   # 被杀应秒级结束 (不等 60s)
    assert t["status"] == "cancelled" and t["error"] == ""
