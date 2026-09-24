# -*- coding: utf-8 -*-
"""farm_backtest 看板数据源测试 (2026-09-16 计划书阶段 1c):
backtest_summary.json + archive.json 增量更新 (造假 sweep CSV, 不碰 TDX)。"""
import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.formula_farm import farm_backtest as fb  # noqa: E402

CSV_HEADER = ["key", "cost", "act", "dd", "ladder", "time_days", "cond_days",
              "cond_profit", "annret", "maxdd", "calmar", "winrate", "trades",
              "error"]


def _row(key="A", cost="-0.20", act="0.08", dd="0.005", ladder="off",
         time_days="20", cond_days="0", cond_profit="0.0", annret="0.20",
         maxdd="-0.10", calmar="2.0", winrate="0.60", trades="30", error=""):
    return {"key": key, "cost": cost, "act": act, "dd": dd, "ladder": ladder,
            "time_days": time_days, "cond_days": cond_days,
            "cond_profit": cond_profit, "annret": annret, "maxdd": maxdd,
            "calmar": calmar, "winrate": winrate, "trades": trades, "error": error}


def _write_sweep(sweep_out, gs, rows):
    d = Path(sweep_out) / gs
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "sweep_a.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADER)
        w.writeheader()
        for r in rows:
            w.writerow(r)


@pytest.fixture()
def sweep(tmp_path, monkeypatch):
    monkeypatch.setattr(fb, "SWEEP_OUT", str(tmp_path / "sweep"))
    fb._ROWS_CACHE.clear()
    yield tmp_path
    fb._ROWS_CACHE.clear()


def test_archive_entry_verdicts(sweep):
    _write_sweep(fb.SWEEP_OUT, "GS0001", [_row()])
    _write_sweep(fb.SWEEP_OUT, "GS0002", [_row(annret="0.05")])
    _write_sweep(fb.SWEEP_OUT, "GS0003", [_row(annret="0.50", trades="3")])
    # GS0004 无 CSV → 无有效组合 (停牌/全失败)
    idx = {g: {"file": g + ".md", "url": "http://x", "date": "2026-09-16"}
           for g in ("GS0001", "GS0002", "GS0003", "GS0004")}
    path = str(sweep / "archive.json")
    arch = fb.update_archive(path, sorted(idx), idx)
    assert arch["GS0001"]["verdict"]["code"] == "pass"
    assert arch["GS0001"]["best"]["trades"] == 30
    assert arch["GS0001"]["onboard_date"] == "2026-09-16"
    assert arch["GS0002"]["verdict"]["code"] == "fail"
    assert arch["GS0003"]["verdict"]["code"] == "insufficient"  # 3 笔, 数字再好看也不算
    assert arch["GS0004"]["verdict"]["code"] == "invalid"
    assert arch["GS0004"]["best"] is None
    # 落盘可回读
    back = json.loads(Path(path).read_text(encoding="utf-8"))
    assert set(back) == set(idx)


def test_slim_best_carries_params(sweep):
    """2026-09-16 回填回测页: 最优组合必须带完整 params (回填数据源)。"""
    _write_sweep(fb.SWEEP_OUT, "GS0001", [_row()])
    idx = {"GS0001": {"file": "a.md", "url": "", "date": "2026-09-16"}}
    arch = fb.update_archive(str(sweep / "archive.json"), ["GS0001"], idx)
    p = arch["GS0001"]["best"]["params"]
    assert p == {"cost": -0.20, "act": 0.08, "dd": 0.005, "ladder": "off",
                 "time_days": 20, "cond_days": 0, "cond_profit": 0.0}
    assert isinstance(p["time_days"], int) and isinstance(p["cost"], float)


def test_caliber_is_farm_rules_sweep_caliber():
    """防漂移锁: 报告抬头口径与回填口径同一对象 (改一处不许漂另一处)。"""
    from core import farm_rules
    assert fb.CALIBER is farm_rules.SWEEP_CALIBER
    assert fb.CALIBER["universe_type"] == "23"
    assert fb.CALIBER["priority"] == "移动止盈优先"          # 报告抬头显示值
    assert fb.CALIBER["priority_value"] == "trailing_first"  # 回填机器值
    assert fb.CALIBER["trailing_confirm"] == "intraday"      # 审计 HIGH-1


def test_slim_best_ladder_none_not_stringified(sweep):
    """审计 LOW-7: 空/缺 ladder 记 None, 不许 str(None) 变 "None" 触发假告警。"""
    _write_sweep(fb.SWEEP_OUT, "GS0001", [_row(ladder="")])
    idx = {"GS0001": {"file": "a.md", "url": "", "date": "2026-09-16"}}
    arch = fb.update_archive(str(sweep / "archive.json"), ["GS0001"], idx)
    assert arch["GS0001"]["best"]["params"]["ladder"] is None


def test_update_archive_incremental_preserves_untouched(sweep):
    _write_sweep(fb.SWEEP_OUT, "GS0001", [_row()])
    path = str(sweep / "archive.json")
    idx = {"GS0001": {"file": "a.md", "url": "", "date": "2026-09-16"},
           "GS0002": {"file": "b.md", "url": "", "date": "2026-09-16"}}
    fb.update_archive(path, ["GS0001"], idx)
    # 第二轮只扫 GS0002 —— GS0001 的条目必须原样保留
    arch = fb.update_archive(path, ["GS0002"], idx)
    assert arch["GS0001"]["verdict"]["code"] == "pass"
    assert arch["GS0002"]["verdict"]["code"] == "invalid"


def test_update_archive_rebuild_drops_stale(sweep):
    path = str(sweep / "archive.json")
    Path(path).write_text(json.dumps({"GS9999": {"verdict": {"code": "pass"}}}),
                          encoding="utf-8")
    arch = fb.update_archive(path, [], {}, rebuild=True)
    assert arch == {}   # 全量重建不信旧档


def test_write_backtest_summary(tmp_path):
    ctx = {"date": "2026-09-16", "batch_date": "2026-09-16",
           "remaining": ["GS0007", "GS0008"], "total": 20, "done": 18}
    stats = {"pass": 1, "fail": 14, "insufficient": 3, "invalid": 0}
    out = fb.write_backtest_summary(str(tmp_path / "d" / "backtest_summary.json"),
                                    ctx, stats)
    assert out["stats"]["pass"] == 1
    assert out["remaining"] == 2
    assert out["batch_date"] == "2026-09-16"
    back = json.loads((tmp_path / "d" / "backtest_summary.json").read_text(encoding="utf-8"))
    assert back["done"] == 18
