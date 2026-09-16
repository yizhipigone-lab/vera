# -*- coding: utf-8 -*-
"""FarmRunner — 公式农场三段闸门的串行执行器(页面按钮驱动)。

- 严格串行: 同一时刻只有一个闸门在跑, 再点返回 busy;
- 每闸门独立子进程跑 tools/formula_farm/farm_*.py, stdout 逐行进 log_tail;
- 状态持久化 data/formula_farm/last_status.json(页面重启后能恢复"上次结果");
- stop() 终止当前子进程; runner 可注入假实现供测试。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from utils.logger import get_logger

_logger = get_logger("core.farm_runner")

ROOT = Path(__file__).resolve().parent.parent
FARM = ROOT / "tools" / "formula_farm"
DATA = ROOT / "data" / "formula_farm"
LAST = DATA / "last_status.json"
PY = sys.executable

GATES = {
    "check": ("检查增量", FARM / "farm_check.py"),
    "onboard": ("一键入库", FARM / "farm_onboard.py"),
    # 2026-09-11: 第四闸门 —— 计划书 §4 步骤 6 的"上架后定量复核"此前只在文档里。
    # 位置在入库之后、粗扫之前: 重画/未来函数的公式不该浪费粗扫算力。
    "verify": ("定量复核", FARM / "farm_verify.py"),
    "backtest": ("开始回测", FARM / "farm_backtest.py"),
}


@dataclass
class GateRun:
    gate: str
    status: str = "running"          # running/done/failed/stopped
    stage: str = "启动"
    log_tail: list = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    error: str = ""


class FarmRunner:
    def __init__(self, runner=None):
        self._lock = threading.Lock()
        self._current: GateRun | None = None
        self._proc = None
        self._runner = runner or self._run_gate
        self._last = self._load_last()

    # ── 状态 ────────────────────────────────────────────────
    def _load_last(self):
        try:
            return json.loads(LAST.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_last(self):
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            tmp = LAST.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._last, ensure_ascii=False), "utf-8")
            os.replace(tmp, LAST)
        except Exception:
            # 2026-09-16 P2: 裸 except 静默吞 → 留痕 (状态丢失曾无迹可查)
            _logger.warning("farm_runner: last_status.json 落盘失败", exc_info=True)

    @property
    def running(self):
        return self._current is not None and self._current.status == "running"

    def status(self):
        with self._lock:
            cur = asdict(self._current) if self._current else None
        return {"running": self.running, "current": cur,
                "gates": {k: v[0] for k, v in GATES.items()},
                "last": self._last}

    # ── 执行 ────────────────────────────────────────────────
    def start(self, gate):
        with self._lock:
            if self.running:
                return None, "有闸门正在运行"
            if gate not in GATES:
                return None, "未知闸门"
            run = GateRun(gate=gate, started_at=time.strftime("%Y-%m-%d %H:%M:%S"))
            self._current = run
        threading.Thread(target=self._work, args=(run,), daemon=True).start()
        return run, ""

    def stop(self):
        with self._lock:
            p = self._proc
        if p and p.poll() is None:
            p.terminate()

    def _work(self, run: GateRun):
        try:
            rc = self._runner(run)
            run.status = "done" if rc == 0 else "failed"
            if rc != 0:
                run.error = "子进程退出码 %s" % rc
        except Exception as e:
            run.status = "failed"
            run.error = repr(e)
        finally:
            run.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
            with self._lock:
                self._last[run.gate] = {
                    "status": run.status, "finished_at": run.finished_at,
                    "error": run.error, "log_tail": run.log_tail[-30:]}
                self._proc = None
                self._current = run  # 留最后一次供页面读
            self._save_last()

    def _run_gate(self, run: GateRun):
        label, script = GATES[run.gate]
        run.stage = label
        env = dict(os.environ, PYTHONUTF8="1")
        proc = subprocess.Popen(
            [PY, "-X", "utf8", str(script)], cwd=str(ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", env=env)
        with self._lock:
            self._proc = proc
        for line in proc.stdout:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            run.log_tail.append(line)
            if len(run.log_tail) > 200:
                run.log_tail = run.log_tail[-200:]
            run.stage = line[:60]
        return proc.wait()
