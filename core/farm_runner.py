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
import re
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
    log_file: str = ""               # 完整日志路径 (runs/日期/闸门.log, 2026-09-16)


# ── 失败病因提取 (纯函数, 有测试) ────────────────────────────

def _extract_error(lines):
    """从闸门输出尾部提取真病因。

    优先级: ① 最后一个 Traceback 块的最后一个非空行
               (ModuleNotFoundError: No module named 'psutil' 这类, 藏在
               "子进程退出码 1" 背后的真凶);
            ② 最后的 XxxError/XxxException/SystemExit 行;
            ③ 最后一条非空行 (脚本 raise SystemExit 前自己打印的人话原因,
               如「没有待入库清单 — 先点『检查增量』」)。
    """
    tb = None
    for i, l in enumerate(lines):
        if "Traceback (most recent call last)" in l:
            tb = i
    if tb is not None:
        for l in reversed(lines[tb + 1:]):
            if l.strip():
                return l.strip()[:300]
    for l in reversed(lines):
        s = l.strip()
        if re.match(r"^[\w.]*(Error|Exception|SystemExit|KeyboardInterrupt)\b", s):
            return s[:300]
    for l in reversed(lines):
        if l.strip():
            return l.strip()[:300]
    return ""


def _read_log_tail(path, n=200):
    """读日志文件最后 n 行 (失败时文件可能很大, 不全读)。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.readlines()[-n:]
    except Exception:
        return []


class FarmRunner:
    def __init__(self, runner=None):
        self._lock = threading.Lock()
        self._current: GateRun | None = None
        self._proc = None
        self._stop_requested = False   # 人工停止标记 (停止与真失败分开记)
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
            self._stop_requested = False
            self._current = run
        threading.Thread(target=self._work, args=(run,), daemon=True).start()
        return run, ""

    def stop(self):
        with self._lock:
            self._stop_requested = True
            p = self._proc
        if p and p.poll() is None:
            p.terminate()

    def _work(self, run: GateRun):
        try:
            rc = self._runner(run)
            run.status = "done" if rc == 0 else "failed"
            if rc != 0:
                # 2026-09-16: 失败病因结构化 —— 从完整日志尾部抓真错误行,
                # 不再只给一句「子进程退出码 1」让人翻文件
                reason = _extract_error(
                    _read_log_tail(run.log_file) or run.log_tail) \
                    if (run.log_file or run.log_tail) else ""
                run.error = "子进程退出码 %s" % rc + (" — %s" % reason if reason else "")
        except Exception as e:
            run.status = "failed"
            run.error = repr(e)
        finally:
            if self._stop_requested and run.status != "done":
                # 人工停止 ≠ 子进程真失败 (此前页面都显示 ❌ 失败+退出码, 分不清)
                run.status = "stopped"
                run.error = ""
            run.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
            with self._lock:
                self._last[run.gate] = {
                    "status": run.status, "finished_at": run.finished_at,
                    "error": run.error, "log_tail": run.log_tail[-30:],
                    "log_file": run.log_file}
                self._proc = None
                self._current = run  # 留最后一次供页面读
            self._save_last()

    def _run_gate(self, run: GateRun):
        label, script = GATES[run.gate]
        run.stage = label
        # 2026-09-16: 完整输出边跑边落盘 runs/日期/闸门.log —— 此前只留内存
        # 最后 30 行, 失败的真病因 (Traceback) 经常被截掉, 只能翻文件猜
        date_str = (run.started_at or time.strftime("%Y-%m-%d %H:%M:%S"))[:10]
        fh = None
        try:
            log_dir = DATA / "runs" / date_str
            log_dir.mkdir(parents=True, exist_ok=True)
            fh = open(DATA / "runs" / date_str / ("%s.log" % run.gate),
                      "w", encoding="utf-8")
            run.log_file = str(fh.name)
        except Exception:
            _logger.warning("farm_runner: 闸门日志文件创建失败", exc_info=True)
        env = dict(os.environ, PYTHONUTF8="1")
        proc = subprocess.Popen(
            [PY, "-X", "utf8", str(script)], cwd=str(ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", env=env)
        with self._lock:
            self._proc = proc
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                if fh:
                    try:
                        fh.write(line + "\n")
                        fh.flush()
                    except Exception:
                        pass
                if not line.strip():
                    continue
                run.log_tail.append(line)
                if len(run.log_tail) > 200:
                    run.log_tail = run.log_tail[-200:]
                run.stage = line[:60]
        finally:
            if fh:
                fh.close()
        return proc.wait()
