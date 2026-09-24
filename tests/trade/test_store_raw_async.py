"""_RawLogWriter 专项测试 (2026-08-06 审计 HIGH#3 异步化).

修复前: 回调线程持 store._lock 同步 write+flush (铁律 2 违反) ——
tick 暴雨 / 磁盘抖动时网关回调与消费者记账互堵。
修复后语义锁定:
1. flush_raw 后内容完整、顺序保持
2. close() 自动 drain —— 不显式 flush 也一条不丢
3. 队列满 → put 不阻塞 + dropped 计数 (绝不堵回调线程)
4. 写盘异常 → writer 不死, 后续批次照常, errors 计数
5. tick 暴雨: 10k 条多线程 put 快速返回, flush 后一条不少
"""
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.store import TradeStore, _RawLogWriter


class _FakeFp:
    """可控假文件: write 可阻塞/可炸, 记录全部写入。"""

    def __init__(self):
        self.lines = []
        self.flushes = 0
        self.gate = threading.Event()
        self.gate.set()               # 默认不阻塞
        self.fail_next = 0            # 接下来 N 次 write 抛异常

    def write(self, s):
        self.gate.wait(5.0)
        if self.fail_next > 0:
            self.fail_next -= 1
            raise OSError("disk glitch")
        self.lines.append(s)
        return len(s)

    def flush(self):
        self.flushes += 1

    def close(self):
        pass


def test_flush_raw_makes_content_visible_in_order(tmp_path):
    """append 只入队; flush_raw 后文件可见且顺序与入队一致。"""
    store = TradeStore(tmp_path / "t.db", tmp_path / "raw.jsonl")
    for i in range(20):
        store.append_raw({"seq": i})
    assert store.flush_raw()
    lines = (tmp_path / "raw.jsonl").read_text(encoding="utf-8").splitlines()
    store.close()
    assert len(lines) == 20
    import json
    assert [json.loads(l)["seq"] for l in lines] == list(range(20))


def test_close_drains_without_explicit_flush(tmp_path):
    """close() 先 drain 再关 fp: 不调 flush_raw 也一条不丢。"""
    store = TradeStore(tmp_path / "t.db", tmp_path / "raw.jsonl")
    for i in range(50):
        store.append_raw({"seq": i})
    store.close()                     # 无 flush_raw, 直接关
    lines = (tmp_path / "raw.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 50


def test_shutdown_drains_backlog_and_stops_thread():
    """确定性 drain 验证 (2026-08-07 复核: 上个用例在写盘跟得上时,
    没有 drain 也能蒙混过关 —— 变异测试抓出)。write 阻塞制造积压,
    恢复后 shutdown: sentinel 在队列尾, FIFO 保证积压全落盘后线程才退。"""
    fp = _FakeFp()
    fp.gate.clear()                   # write 阻塞 → 5 条全积压
    w = _RawLogWriter(fp, flush_interval=0.05)
    for i in range(5):
        w.put(f"L{i}\n")
    fp.gate.set()
    w.shutdown(timeout=10.0)
    assert not w._t.is_alive()        # 收到 sentinel 才退 —— 无 drain 的变异在此现形
    assert fp.lines == [f"L{i}\n" for i in range(5)]


def test_full_queue_drops_without_blocking():
    """磁盘卡死 (write 阻塞) + 队列满 → put 立即返回, dropped 计数,
    绝不阻塞回调线程; 恢复后已入队的照常落盘。"""
    fp = _FakeFp()
    fp.gate.clear()                   # write 全部阻塞, 模拟磁盘卡死
    w = _RawLogWriter(fp, flush_interval=0.05, maxsize=3)
    t0 = time.monotonic()
    for i in range(10):
        w.put(f"line{i}\n")           # 全部立即返回 (capacity 3, 1 在处理)
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0              # 10 次 put 微秒级, 给足余量防 flake
    assert w.dropped >= 6             # 10 - 1(处理中) - 3(队列) = 6 起
    fp.gate.set()                     # 磁盘恢复
    w.shutdown()
    # writer 启动早晚影响"在处理"计数, 逐条断言会竞态; 锁不变量:
    # 顺序保持 + 落盘数与丢弃数互补 + 首批必达 + 丢弃确实发生
    idx = [int(l.strip()[4:]) for l in fp.lines]
    assert idx == sorted(idx) and idx[0] == 0
    assert len(fp.lines) >= 3 and w.dropped == 10 - len(fp.lines)


def test_writer_survives_write_errors():
    """write 抛异常 → errors 计数, writer 不死, 后续批次照常写。"""
    fp = _FakeFp()
    fp.fail_next = 2                  # 前 2 次 write 炸
    w = _RawLogWriter(fp, flush_interval=0.05)
    w.put("bad1\n")
    w.put("bad2\n")
    w.put("good\n")
    assert w.flush(timeout=5.0)
    w.shutdown()
    assert w.errors == 2
    assert fp.lines == ["good\n"]     # 炸的丢了 (有计数), 活的照常


def test_tick_storm_put_is_fast_and_lossless(tmp_path):
    """暴雨压测: 4 线程 × 2500 条 = 10k put, 总耗时 << 同步写盘量级,
    flush 后一条不少 (队列深度 10 万, 远够)。"""
    store = TradeStore(tmp_path / "t.db", tmp_path / "raw.jsonl")

    def pump(base):
        for i in range(2500):
            store.append_raw({"t": base + i})

    t0 = time.monotonic()
    threads = [threading.Thread(target=pump, args=(n * 2500,))
               for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    put_secs = time.monotonic() - t0
    assert put_secs < 2.0             # 1 万次入队; 同步写盘时代是秒级×10
    assert store.flush_raw(timeout=30.0)
    store.close()
    n = len((tmp_path / "raw.jsonl").read_text(
        encoding="utf-8").splitlines())
    assert n == 10_000
