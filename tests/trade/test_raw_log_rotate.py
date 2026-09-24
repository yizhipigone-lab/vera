# -*- coding: utf-8 -*-
"""tests/trade/test_raw_log_rotate.py — raw 审计日志按月轮转 (2026-09-19 批次 1.3)。"""
from __future__ import annotations

import datetime as dt
import json

from trade.raw_log import _line_month, rotate_raw_log_monthly


def _ts(y, m, d):
    return dt.datetime(y, m, d, 12, 0, 0).timestamp()


def _line(kind, y, m, d):
    # data 里故意也放一个 ts (不同日), 验证 rfind 取的是顶层 ts
    return json.dumps({"kind": kind,
                       "data": {"code": "X", "ts": _ts(y, m, min(d + 1, 28))},
                       "ts": _ts(y, m, d)}, ensure_ascii=False) + "\n"


TODAY = dt.date(2026, 9, 19)


def test_splits_old_months_and_keeps_current(tmp_path):
    p = tmp_path / "raw_reports.jsonl"
    p.write_text(
        _line("tick", 2026, 7, 15) + _line("order", 2026, 8, 3)
        + _line("tick", 2026, 9, 18) + _line("trade", 2026, 9, 19),
        encoding="utf-8")
    r = rotate_raw_log_monthly(p, today=TODAY)
    assert r["noop"] is False
    assert r["scanned"] == 4 and r["kept"] == 2
    assert r["archived"] == {"202607": 1, "202608": 1}
    # 归档文件内容正确
    jul = (tmp_path / "raw_reports_202607.jsonl").read_text(encoding="utf-8")
    assert '"kind": "tick"' in jul and jul.count("\n") == 1
    aug = (tmp_path / "raw_reports_202608.jsonl").read_text(encoding="utf-8")
    assert '"kind": "order"' in aug
    # live 只剩当月两行
    live = p.read_text(encoding="utf-8")
    assert live.count("\n") == 2 and "2026-07" not in live and "2026-08" not in live


def test_existing_archive_appended_not_overwritten(tmp_path):
    p = tmp_path / "raw_reports.jsonl"
    (tmp_path / "raw_reports_202607.jsonl").write_text(
        _line("old", 2026, 7, 1), encoding="utf-8")
    p.write_text(_line("new", 2026, 7, 20), encoding="utf-8")
    rotate_raw_log_monthly(p, today=TODAY)
    arch = (tmp_path / "raw_reports_202607.jsonl").read_text(encoding="utf-8")
    assert arch.count("\n") == 2          # 旧行还在, 新行追加
    assert '"kind": "old"' in arch and '"kind": "new"' in arch


def test_unparseable_line_stays_in_live(tmp_path):
    p = tmp_path / "raw_reports.jsonl"
    p.write_text('{"kind":"tick","data":{ 坏行\n' + _line("tick", 2026, 8, 1),
                 encoding="utf-8")
    r = rotate_raw_log_monthly(p, today=TODAY)
    assert r["archived"] == {"202608": 1}
    live = p.read_text(encoding="utf-8")
    assert "坏行" in live                 # 不猜、不丢


def test_current_month_only_is_noop(tmp_path):
    p = tmp_path / "raw_reports.jsonl"
    content = _line("tick", 2026, 9, 18)
    p.write_text(content, encoding="utf-8")
    r = rotate_raw_log_monthly(p, today=TODAY)
    assert r["noop"] is True
    assert p.read_text(encoding="utf-8") == content   # live 一个字节没动
    assert not list(tmp_path.glob("raw_reports_2*.jsonl"))


def test_missing_file_is_noop(tmp_path):
    r = rotate_raw_log_monthly(tmp_path / "nope.jsonl", today=TODAY)
    assert r == {"scanned": 0, "archived": {}, "kept": 0, "noop": True}


def test_first_line_current_month_fast_path(tmp_path):
    """append-only 时序文件: 首行已是当月 → 全量扫都省了, 直接 no-op。"""
    p = tmp_path / "raw_reports.jsonl"
    content = _line("tick", 2026, 9, 1) + _line("tick", 2026, 8, 31)  # 乱序也信首行
    p.write_text(content, encoding="utf-8")
    r = rotate_raw_log_monthly(p, today=TODAY)
    assert r["noop"] is True and r["scanned"] == 0
    assert p.read_text(encoding="utf-8") == content


def test_line_month_uses_outer_ts_not_data_ts():
    # data.ts 是 8 月 2 日, 顶层 ts 是 8 月 1 日 —— 必须取顶层
    line = _line("tick", 2026, 8, 1)
    assert _line_month(line) == "202608"
    assert _line_month("not json at all") is None
