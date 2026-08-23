"""影子三态 (MA20 shadow) 测试 (2026-08-23, P0 影子校尺)。

锁住: 三态状态计算(full_cyb/half/full_gold/数据不足)、JSONL 按日去重、
run_shadow fail-soft(任何异常只记日志绝不抛出)、数据不足不落盘。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade.shadow import append_shadow_log, compute_shadow_state, run_shadow


def _series(pattern):
    """构造 260 根收盘序列。"""
    if pattern == "rising":
        return [100.0 + i * 0.1 for i in range(260)]           # 单边上涨
    if pattern == "falling":
        return [100.0 - i * 0.1 for i in range(260)]           # 单边下跌
    if pattern == "half":
        # 高点 100 → 跌 25% → 近 10 日小步回升: MA20 向上但回撤 -23% < -20%
        return ([100.0] * 230 + [75.0] * 20
                + [75.0 + i * 0.2 for i in range(10)])
    raise ValueError(pattern)


def test_shadow_state_full_cyb():
    st = compute_shadow_state(_series("rising"))
    assert st["state"] == "full_cyb"


def test_shadow_state_full_gold():
    st = compute_shadow_state(_series("falling"))
    assert st["state"] == "full_gold"


def test_shadow_state_half():
    st = compute_shadow_state(_series("half"))
    assert st["state"] == "half"


def test_shadow_state_insufficient():
    st = compute_shadow_state([1.0, 2.0, 3.0])
    assert st["state"] is None


def test_append_dedupe_by_date(tmp_path):
    p = tmp_path / "shadow.jsonl"
    append_shadow_log(str(p), "20260821", {"state": "full_cyb"})
    append_shadow_log(str(p), "20260821", {"state": "full_gold"})   # 同日重写应跳过
    append_shadow_log(str(p), "20260822", {"state": "half"})
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["date"] == "20260821"
    assert first["state"] == "full_cyb"      # 保留当日首条, 不被同日覆盖


def test_run_shadow_ok(tmp_path):
    p = tmp_path / "shadow.jsonl"
    st = run_shadow(_series("rising"), "20260821", str(p))
    assert st is not None and st["state"] == "full_cyb"
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["state"] == "full_cyb"


def test_run_shadow_insufficient_not_logged(tmp_path):
    p = tmp_path / "shadow.jsonl"
    st = run_shadow([1.0, 2.0], "20260821", str(p))
    assert st is None
    assert not p.exists()                     # 数据不足不落盘


def test_run_shadow_fail_soft(tmp_path, monkeypatch):
    import trade.rotation as rot
    monkeypatch.setattr(rot, "compute_signal",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    st = run_shadow(_series("rising"), "20260821", str(tmp_path / "s.jsonl"))
    assert st is None                          # 炸了也不抛出, 只返 None
