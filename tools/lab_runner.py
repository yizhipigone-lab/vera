# -*- coding: utf-8 -*-
"""体检任务队列(LabQueue) — 公式体检页面的后端执行器。

计划书: docs/plan/2026-07-20_公式体检页面_计划书.md(v2, 审计修订版)

设计要点(v2 审计规则):
- 严格串行: 内存队列 + 单工作线程, 同一时间只跑一个任务;
- 不对称互斥: 体检永远 FIFO 排队(回测在跑也排); 回测提交在体检运行中 → 409
  (由 server.py 的 /api/run guard 检查 lab_status.running 实现);
- baseline 自动串联: 缺 data/baseline/{formula}_selections_{tag}.parquet 时,
  先 subprocess run_upn_baseline --formula X(universe 来自 default.yaml — 队列卡片显示);
- 进度解析: formula_lab stdout 的 [S0]/[S2]/[S3]/[S4]/[S5] 标记, 失败降级"运行中";
- 公式名白名单 ^[\w一-龥\-]+$, 防路径穿越。
"""
from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FORMULA_RE = re.compile(r"^[\w一-龥\-]+$")
STAGE_RE = re.compile(r"\[(S[0-9][^\]]*)\]")
# 阶段 → 进度权重(S2 30%, S4 60%, 其余 10%, 计划书 §2.1)
STAGE_PROGRESS = {"S0": 5, "S2": 30, "S3": 40, "S4": 95, "S5": 100}


@dataclass
class LabTask:
    formulas: list[str]
    tag: str
    tag2: str | None
    strategy_yaml: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    status: str = "queued"          # queued/baseline/lab/done/failed
    stage: str = "排队中"
    error: str = ""
    universe_note: str = ""
    queued_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0


class LabQueue:
    """严格串行体检队列。pipeline_busy 回调由 server 注入(返回 True 表示回测在跑)。"""

    def __init__(self, pipeline_busy=lambda: False, runner=None):
        self._tasks: list[LabTask] = []
        self._lock = threading.Lock()
        self._pipeline_busy = pipeline_busy
        self._runner = runner or self._subprocess_run   # 测试可注入假 runner
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    # ── 提交/查询 ─────────────────────────────────────────────

    def submit(self, formulas: list[str], tag: str, tag2: str | None,
               strategy_yaml: str | None = None) -> tuple[list[str], str]:
        """返回 (task_ids, 拒绝原因)。公式名校验 + 同公式 pending 去重。"""
        if not formulas:
            return [], "公式名不能为空"
        for f in formulas:
            if not FORMULA_RE.match(f):
                return [], f"公式名含非法字符: {f}"
        with self._lock:
            pending = {f for t in self._tasks if t.status in ("queued", "baseline", "lab")
                       for f in t.formulas}
            dup = [f for f in formulas if f in pending]
            if dup:
                return [], f"已有排队/运行中的同公式任务: {', '.join(dup)}"
            task = LabTask(formulas=list(formulas), tag=tag, tag2=tag2,
                           strategy_yaml=strategy_yaml,
                           universe_note=self._universe_note())
            self._tasks.append(task)
        return [task.id], ""

    def snapshot(self) -> dict:
        with self._lock:
            tasks = list(self._tasks)
        def _fmt(t: LabTask) -> dict:
            return {"id": t.id, "formulas": t.formulas, "tag": t.tag, "tag2": t.tag2,
                    "status": t.status, "stage": t.stage, "error": t.error,
                    "universe_note": t.universe_note,
                    "elapsed_s": int((t.finished_at or time.time()) - t.started_at) if t.started_at else 0}
        cur = next((t for t in tasks if t.status in ("baseline", "lab")), None)
        return {"running": cur is not None,
                "current": _fmt(cur) if cur else None,
                "queue": [_fmt(t) for t in tasks[-10:]]}

    @property
    def running(self) -> bool:
        with self._lock:
            return any(t.status in ("baseline", "lab") for t in self._tasks)

    # ── 工作线程 ─────────────────────────────────────────────

    def _loop(self):
        while True:
            task = None
            with self._lock:
                task = next((t for t in self._tasks if t.status == "queued"), None)
                if task:
                    task.status = "baseline"
                    task.started_at = time.time()
            if not task:
                time.sleep(0.5)
                continue
            try:
                # 不对称互斥: 回测在跑则等待(体检永远排队)
                while self._pipeline_busy():
                    task.stage = "回测运行中,体检排队中"
                    time.sleep(2)
                self._run_task(task)
                task.status = "done"
                task.stage = "完成"
            except Exception as e:
                task.status = "failed"
                task.error = str(e)
                task.stage = "失败"
            task.finished_at = time.time()

    def _run_task(self, task: LabTask):
        for formula in task.formulas:
            # baseline 自动串联(两窗口都查)
            for tag in [task.tag, task.tag2]:
                if not tag:
                    continue
                bp = ROOT / "data" / "baseline" / f"{formula}_selections_{tag}.parquet"
                if not bp.exists():
                    start, end = tag.split("_")
                    task.stage = f"补基线 {formula} {tag}(股票池: {task.universe_note})"
                    rc, out = self._runner(
                        ["tools/run_upn_baseline.py", "--formula", formula,
                         "--start", start, "--end", end])
                    if rc != 0:
                        raise RuntimeError(f"基线生成失败 {formula} {tag}(需 TDX 在线): {out[-500:]}")
            # 体检
            task.status = "lab"
            cmd = ["tools/formula_lab.py", "--formula", formula, "--tag", task.tag]
            if task.tag2:
                cmd += ["--tag2", task.tag2]
            if task.strategy_yaml:
                cmd += ["--strategy-yaml", task.strategy_yaml]
            rc, out = self._runner(cmd, on_line=lambda ln: self._on_line(task, ln))
            if rc != 0:
                raise RuntimeError(f"体检失败 {formula}: {out[-500:]}")

    def _on_line(self, task: LabTask, line: str):
        m = STAGE_RE.search(line)
        if m:
            key = m.group(1)[:2]
            if key in STAGE_PROGRESS:
                task.stage = key

    # ── 执行器(可注入) ───────────────────────────────────────

    @staticmethod
    def _subprocess_run(cmd: list[str], on_line=None) -> tuple[int, str]:
        """跑子进程, 逐行回调(进度解析), 返回 (rc, 全量输出尾部)。"""
        full = subprocess.run([sys.executable] + cmd, cwd=ROOT,
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        out = (full.stdout or "") + (full.stderr or "")
        if on_line:
            for ln in out.splitlines():
                try:
                    on_line(ln)
                except Exception:
                    pass
        return full.returncode, out

    @staticmethod
    def _universe_note() -> str:
        try:
            from utils.config_loader import ConfigLoader
            u = ConfigLoader.load_defaults().get("selection", {}).get("universe", {})
            return f"default.yaml type={u.get('type', '?')}"
        except Exception:
            return "default.yaml(读取失败)"
