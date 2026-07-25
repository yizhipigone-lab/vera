"""细粒度进度报告器 (2026-07-26) — 全局模块状态, 无人读取时零副作用。

动机: web 进度条此前只有 _cb 步骤边界点位 (10/15/20/30...), 选股占 ~92%
时间却挤在同一个点位区间, 进度条"停住→跳一下"。本模块让深层循环
(公式批次/ST过滤/取数/核心loop) 直接写进度状态, server /api/status 读取
融合, 前端按真实刻度平滑显示 + 速率法 ETA。

设计约束 (防回归铁律):
- 纯 stdlib, 无 server/pipeline 依赖; tools/CLI 调用方同样写状态但无人读,
  开销 = 一次锁内 dict 更新 (调用方已按 N 节流)
- 不改任何现有函数签名; progress_callback (pct, step) 契约不变, 本模块是
  旁路增量层, /api/status 兼容融合 (additive 字段 detail/eta_s)
- pct 单调不回退由 server 侧 guard 保证 (formula_sell 等嵌套调用可能
  乱序上报, 见 server.py /api/status)
"""
from __future__ import annotations

import threading
import time

# 阶段 → 全局百分比区间 (lo, hi)。总和 0-100, 与耗时占比对齐 (实测选股 ~92%)。
ANCHORS = {
    "universe_list": (10.0, 14.0),   # 拉股票列表/板块成份
    "st_filter":     (14.0, 26.0),   # ST/退市/港股过滤 (逐只)
    "formula":       (26.0, 45.0),   # 公式选股 (批次)
    "fetch":         (45.0, 78.0),   # 取数 (逐只/窗口批次)
    "matrix":        (78.0, 84.0),   # 矩阵准备
    "loop":          (84.0, 92.0),   # 核心回测循环 (逐 bar)
    "benchmark":     (92.0, 96.0),   # 基准对比
    "report":        (96.0, 100.0),  # 画图报告
}

# 阶段 → 中文名 (/api/status 融合时替换粗粒度 step, 2026-07-26)
STAGE_NAMES = {
    "universe_list": "解析股票池",
    "st_filter": "ST/退市过滤",
    "formula": "公式选股",
    "fetch": "取数",
    "matrix": "矩阵准备",
    "loop": "核心回测",
    "benchmark": "基准对比",
    "report": "生成报告",
}

_lock = threading.Lock()
_state = {
    "stage": "",          # ANCHORS key
    "frac": 0.0,          # 阶段内进度 0..1
    "detail": "",         # 展示文案 ("批次 23/51")
    "pct": 0.0,           # 映射后的全局百分比
    "eta_s": -1.0,        # 速率法预估剩余秒; <0 = 不可估
    "ts": 0.0,            # 最近一次上报时间
    # 速率追踪 (stage 变化时重置)
    "_stage": "", "_t0": 0.0, "_done0": 0,
}


def reset() -> None:
    """新回测开始时清空 (server /api/run 调用)。"""
    with _lock:
        _state.update({"stage": "", "frac": 0.0, "detail": "", "pct": 0.0,
                       "eta_s": -1.0, "ts": 0.0,
                       "_stage": "", "_t0": 0.0, "_done0": 0})


def report(stage: str, frac: float, detail: str = "",
           done: int | None = None, total: int | None = None) -> None:
    """上报阶段进度。frac 截断到 [0,1]; done/total 提供时算速率 ETA。

    无监听方也可安全调用 (开销 ~1µs); 调用方负责按 N 节流 (每 200 只/
    每批/每 100 bar 一级), 不要逐 item 调用。
    """
    rng = ANCHORS.get(stage)
    if rng is None:
        return
    frac = min(1.0, max(0.0, float(frac)))
    now = time.monotonic()
    with _lock:
        eta = -1.0
        if done is not None and total and total > 0:
            if _state["_stage"] != stage:
                _state["_stage"] = stage
                _state["_t0"] = now
                _state["_done0"] = done
            elapsed = now - _state["_t0"]
            dd = done - _state["_done0"]
            if dd > 0 and elapsed > 0.5 and done < total:
                rate = dd / elapsed
                if rate > 0:
                    eta = (total - done) / rate
        _state.update({
            "stage": stage, "frac": frac, "detail": detail,
            "pct": rng[0] + (rng[1] - rng[0]) * frac,
            "eta_s": eta, "ts": now,
        })


def snapshot() -> dict:
    """读当前状态 (浅拷贝)。ts 用 monotonic 时钟, 与 reset 后首次上报比较用。"""
    with _lock:
        return {k: v for k, v in _state.items() if not k.startswith("_")}
