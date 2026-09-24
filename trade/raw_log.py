"""trade/raw_log.py — JSONL 原始回报异步写盘 (治理III W4-f 自 trade/store.py 端出)。

2026-08-06 审计 HIGH#3 异步化落盘: 回调线程只 put_nowait (微秒级, 无锁无磁盘
IO) —— 修掉"回调线程持 store._lock 同步 write+flush"的铁律 2 违反 (tick 暴雨
或磁盘抖动不再堵住网关回调与消费者记账)。本模块是 TradeStore 内的独立小深模块
(3 方法藏一整条写盘流水线), 治理时整体迁出为独立文件; store.py 顶部 re-export
`_RawLogWriter` 保持既有 `from trade.store import _RawLogWriter` 引用零改动。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import queue
import threading
import time
from pathlib import Path

from utils.logger import get_logger

_logger = get_logger("trade.raw_log")


def rotate_raw_log_monthly(raw_log_path, today: dt.date | None = None) -> dict:
    """raw 审计日志按月轮转 (2026-09-19 架构修订批次 1.3)。

    背景: raw_reports.jsonl 只增不删, 2026-09-19 实测 752MB (主体是 tick)。
    它是铁律 5 的审计底账 —— **不能删, 只能归档**: 把 live 文件里非当月的行
    切到同目录 `raw_reports_YYYYMM.jsonl` (追加写, 已存在不覆盖), live 文件
    只留当月行。

    安全顺序 (fail-closed, 宁可不动不可弄丢):
      1. 先写归档文件 (append) 并 flush;
      2. 校验 总行数 == 归档行数 + 留存行数, 不等则中止 (live 文件原样);
      3. 最后才 tmp+replace 原子重写 live 文件。
    解析不出 ts 的行一律留在 live (不猜、不丢)。

    **调用时机铁律**: 必须在 TradeStore 打开 live 文件**之前**调用
    (Windows 下被占用的文件无法 os.replace; 且轮转中途的新写入会丢)。
    进程运行中跨月不轮转, 下次启动时自然收拾 —— 启动钩子在 trade_main。

    返回 {"scanned", "archived": {YYYYMM: n}, "kept", "noop"}。
    """
    p = Path(raw_log_path)
    today = today or dt.date.today()
    cur_key = today.strftime("%Y%m")
    if not p.exists():
        return {"scanned": 0, "archived": {}, "kept": 0, "noop": True}

    # 快路径: live 是 append-only 且按写入时间单调 —— 首行(最老一条)已是
    # 当月, 则全文件都是当月, 直接 no-op, 不必每次启动都全量扫几百 MB。
    # 首行解析不出不做此判断, 落到全量扫 (防御: 坏行挡路时宁慢勿错)。
    with open(p, encoding="utf-8") as f:
        first = f.readline()
    if first.strip():
        m0 = _line_month(first)
        if m0 is not None and m0 >= cur_key:
            return {"scanned": 0, "archived": {}, "kept": -1, "noop": True}

    archives: dict[str, list[str]] = {}
    kept: list[str] = []
    scanned = 0
    with open(p, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            scanned += 1
            month = _line_month(line)
            if month is None or month >= cur_key:
                kept.append(line)          # 当月/未来/解析不出 → 留 live
            else:
                archives.setdefault(month, []).append(line)

    if not archives:
        return {"scanned": scanned, "archived": {}, "kept": len(kept),
                "noop": True}

    # 1) 先写归档 (append —— 同一归档文件可能已存在, 例如停机跨月后重启两次)
    archived_counts: dict[str, int] = {}
    for month, lines in sorted(archives.items()):
        ap = p.with_name(f"{p.stem}_{month}{p.suffix}")
        with open(ap, "a", encoding="utf-8") as af:
            af.writelines(lines)
            af.flush()
            os.fsync(af.fileno())
        archived_counts[month] = len(lines)

    # 2) 行数对账, 不等则中止 (live 原样 —— 但归档已写, 重跑会双倍!
    #    所以这里若不等必须连归档也回滚; 实践中不等只会因内存读取出错,
    #    防御性保留该检查并显式抛错)
    if scanned != sum(archived_counts.values()) + len(kept):
        raise RuntimeError(
            f"raw 日志轮转行数对账失败: scanned={scanned} "
            f"archived={sum(archived_counts.values())} kept={len(kept)}")

    # 3) 原子重写 live (tmp + replace)
    tmp = p.with_suffix(p.suffix + ".rotate_tmp")
    with open(tmp, "w", encoding="utf-8") as tf:
        tf.writelines(kept)
        tf.flush()
        os.fsync(tf.fileno())
    os.replace(tmp, p)

    _logger.info("raw 日志按月轮转: 扫描 %d 行, 归档 %s, live 留存 %d 行",
                 scanned, archived_counts, len(kept))
    return {"scanned": scanned, "archived": archived_counts,
            "kept": len(kept), "noop": False}


def _line_month(line: str) -> str | None:
    """从一行 JSONL 取顶层 ts 的月份 (YYYYMM, 本地时区)。

    快路径: 顶层 ts 是 append_raw 写入的最后一个键 (json.dumps 保序),
    rfind '"ts":' 定位的是它 (data 里可能也有 ts, 所以必须 rfind);
    解析失败退回 json.loads; 都失败返回 None (调用方留存不丢)。
    """
    idx = line.rfind('"ts":')
    if idx >= 0:
        try:
            tail = line[idx + 5:].strip().rstrip("}").strip().rstrip(",")
            ts = float(tail)
            return dt.datetime.fromtimestamp(ts).strftime("%Y%m")
        except (ValueError, OverflowError, OSError):
            pass
    try:
        obj = json.loads(line)
        ts = float(obj.get("ts"))
        return dt.datetime.fromtimestamp(ts).strftime("%Y%m")
    except Exception:
        return None


def _cli() -> int:
    """离线 CLI: python -m trade.raw_log <raw_log_path> [--today YYYY-MM-DD]"""
    import argparse
    ap = argparse.ArgumentParser(description="raw 审计日志按月轮转 (停机窗口用)")
    ap.add_argument("path", help="live 文件路径, 如 data/trade/raw_reports.jsonl")
    ap.add_argument("--today", default=None,
                    help="指定'今天'(YYYY-MM-DD), 默认系统日期")
    args = ap.parse_args()
    today = (dt.datetime.strptime(args.today, "%Y-%m-%d").date()
             if args.today else None)
    res = rotate_raw_log_monthly(args.path, today=today)
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())


class _RawLogWriter:
    """JSONL 异步写盘 (2026-08-06 审计 HIGH#3)。

    回调线程只 put_nowait (微秒级, 无锁无磁盘 IO) —— 修掉"回调线程持
    store._lock 同步 write+flush"的铁律 2 违反: tick 暴雨或磁盘抖动
    (Windows 杀软扫盘) 不再堵住网关回调与消费者记账。

    语义边界 (与 M10 定位一致, JSONL 仅审计留痕):
    - 崩溃丢失窗口 ≤ flush_interval 的尾部未落盘批次, 可接受
    - 正常退出经 shutdown() drain: 队列清空 + 终 flush, 一条不丢
    - 队列满 (磁盘卡死) → 丢弃 + dropped 计数 + 节流告警, 绝不阻塞回调
    - writer 线程死亡 → put 时探活, error 告警一次 (不降级回同步写,
      避免把磁盘 IO 引回回调线程)
    """

    _SENTINEL = object()

    def __init__(self, fp, flush_interval: float = 0.5, maxsize: int = 100_000):
        self._fp = fp
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._flush_interval = flush_interval
        self.dropped = 0   # 队列满丢弃计数
        self.errors = 0    # 写盘/flush 异常计数
        self._dead_warned = False
        self._t = threading.Thread(
            target=self._loop, name="raw-log-writer", daemon=True)
        self._t.start()

    def put(self, line: str) -> None:
        if not self._t.is_alive() and not self._dead_warned:
            self._dead_warned = True
            _logger.error("raw writer 线程未存活, 审计日志中断 (不影响交易)")
        try:
            self._q.put_nowait(line)
        except queue.Full:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 10000 == 0:
                _logger.warning(
                    "raw 日志队列满 (磁盘卡住?), 累计丢弃 %d 条", self.dropped)

    def flush(self, timeout: float = 5.0) -> bool:
        """阻塞至队列清空且已落盘 (测试接缝 / close 前 drain)。True=完成。"""
        ev = threading.Event()
        try:
            self._q.put(ev, timeout=timeout)
        except queue.Full:
            return False
        return ev.wait(timeout)

    def shutdown(self, timeout: float = 5.0) -> None:
        """sentinel 停线程 + drain + 终 flush; 队列满则重试至超时。"""
        deadline = time.monotonic() + timeout
        while True:
            try:
                self._q.put(self._SENTINEL, timeout=0.1)
                break
            except queue.Full:
                if time.monotonic() >= deadline:
                    break
        self._t.join(max(0.0, deadline - time.monotonic()) + 1.0)

    def _write_safe(self, line: str) -> None:
        try:
            self._fp.write(line)
        except Exception as e:
            self.errors += 1
            if self.errors == 1 or self.errors % 100 == 0:
                _logger.error("raw 日志写盘异常 (累计 %d): %s", self.errors, e)

    def _flush_safe(self) -> None:
        try:
            self._fp.flush()
        except Exception as e:
            self.errors += 1
            _logger.error("raw 日志 flush 异常: %s", e)

    def _handle(self, item) -> bool:
        """处理一个队列项, 返回 True = 收到关闭信号。"""
        if item is self._SENTINEL:
            return True
        if isinstance(item, threading.Event):
            self._flush_safe()   # flush 请求: 落盘已完成批次后放行
            item.set()
        else:
            self._write_safe(item)
        return False

    def _loop(self) -> None:
        stopping = False
        while True:
            try:
                item = self._q.get(timeout=self._flush_interval)
            except queue.Empty:
                self._flush_safe()   # 空闲兜底: 崩溃丢失窗口 ≤ flush_interval
                continue
            stopping = self._handle(item)
            # 批量 drain: 积攒的一波一次写完, 只做一次 flush
            while True:
                try:
                    item = self._q.get_nowait()
                except queue.Empty:
                    break
                if self._handle(item):
                    stopping = True
            self._flush_safe()
            if stopping:
                return
