"""零号报告 (tools/policy_report_zero.py) 测试 — 合成 trade.db, 不碰真实数据。

覆盖: FIFO 配对 (部分卖出/跨笔)、分档战绩统计、偏离清单、样本不足口径、
无配对卖出提示、空库兜底、标签失败降级 (tiers=None → UNTAGGED)。
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
# tools/ 不是包, 按路径加载; 若 notes_gen 等已加载过则复用同一实例 —
# 否则两套 sys.modules 注册互相覆盖, monkeypatch 打错对象 (跨文件污染实测)。
if "policy_report_zero" in sys.modules:
    prz = sys.modules["policy_report_zero"]
else:
    _SPEC = importlib.util.spec_from_file_location(
        "policy_report_zero", _ROOT / "tools" / "policy_report_zero.py")
    prz = importlib.util.module_from_spec(_SPEC)
    sys.modules["policy_report_zero"] = prz
    _SPEC.loader.exec_module(prz)

_D = 86400.0
_T0 = 1750000000.0  # 固定纪元起点, 日期断言只看相对天数


def _mk_db(path: Path, trades: list[tuple]) -> Path:
    """造一个最小 trades 表的 db。trades: (code, direction, price, qty, ts)。"""
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE trades (
        traded_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, code TEXT NOT NULL,
        direction INTEGER NOT NULL, price REAL NOT NULL, qty INTEGER NOT NULL,
        amount REAL NOT NULL, ts REAL NOT NULL)""")
    conn.executemany(
        "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?)",
        [(f"t{i}", "o", c, d, p, q, p * q, ts)
         for i, (c, d, p, q, ts) in enumerate(trades)])
    conn.commit()
    conn.close()
    return path


class TestPairFifo:
    def test_partial_sell_splits_lots(self):
        trades = [
            {"code": "A", "direction": 23, "price": 10.0, "qty": 100, "ts": _T0},
            {"code": "A", "direction": 23, "price": 11.0, "qty": 100, "ts": _T0 + _D},
            {"code": "A", "direction": 24, "price": 12.0, "qty": 150, "ts": _T0 + 3 * _D},
        ]
        res = prz.pair_fifo(trades)
        assert len(res.closed) == 2
        # FIFO: 先吃 10 元的 100 股, 再吃 11 元的 50 股
        assert (res.closed[0].qty, res.closed[0].buy_price) == (100, 10.0)
        assert (res.closed[1].qty, res.closed[1].buy_price) == (50, 11.0)
        assert len(res.open_lots) == 1
        assert (res.open_lots[0].qty, res.open_lots[0].price) == (50, 11.0)
        assert res.unmatched_sell_qty == 0

    def test_oversell_counts_unmatched(self):
        trades = [{"code": "A", "direction": 24, "price": 12.0, "qty": 100, "ts": _T0}]
        res = prz.pair_fifo(trades)
        assert res.closed == [] and res.unmatched_sell_qty == 100

    def test_hold_days(self):
        trades = [
            {"code": "A", "direction": 23, "price": 10.0, "qty": 100, "ts": _T0},
            {"code": "A", "direction": 24, "price": 11.0, "qty": 100, "ts": _T0 + 5 * _D},
        ]
        lot = prz.pair_fifo(trades).closed[0]
        assert lot.hold_days == 5 and abs(lot.ret - 0.1) < 1e-9 and lot.pnl == 100.0


_TIERS = {
    "WIN1": {"tier": "P1", "sector_code": "881001.SH", "sector_name": "半导体"},
    "LOS1": {"tier": "AVOID", "sector_code": "881099.SH", "sector_name": "房地产"},
    "OPEN": {"tier": "AVOID", "sector_code": "881099.SH", "sector_name": "房地产"},
}


class TestBuildReport:
    def _res(self):
        trades = [
            {"code": "WIN1", "direction": 23, "price": 10.0, "qty": 100, "ts": _T0},
            {"code": "WIN1", "direction": 24, "price": 12.0, "qty": 100, "ts": _T0 + 4 * _D},
            {"code": "LOS1", "direction": 23, "price": 10.0, "qty": 100, "ts": _T0},
            {"code": "LOS1", "direction": 24, "price": 9.0, "qty": 100, "ts": _T0 + 6 * _D},
            {"code": "OPEN", "direction": 23, "price": 5.0, "qty": 200, "ts": _T0 + _D},
        ]
        return prz.pair_fifo(trades)

    def test_sections_and_stats(self):
        md = prz.build_report(self._res(), _TIERS, min_n=1, db_path="x.db", since=None)
        # 仓位体检: OPEN 在持, AVOID 档 1000 元成本
        assert "| OPEN | 房地产 | AVOID | 200 | 5.000 | 1,000 |" in md
        # 分档战绩: P1 胜率 100%, AVOID 胜率 0%
        assert "| P1 | 1 | 100.0% | 20.0% | 4 | 200 |" in md
        assert "| AVOID | 1 | 0.0% | -10.0% | 6 | -100 |" in md
        # 偏离清单: LOS1 已平仓 + OPEN 在持都在
        assert "| LOS1 | 房地产 | AVOID |" in md and "### 在持" in md
        # 规律候选: min_n=1 两个档位都够 → 有对比句
        assert "P1 胜率 100.0%" in md and "AVOID 0.0%" in md

    def test_min_n_gate(self):
        md = prz.build_report(self._res(), _TIERS, min_n=5, db_path="x.db", since=None)
        assert "样本不足 (n=1 < 5), 不下结论" in md
        assert "不做档位对比结论" in md

    def test_tiers_none_degrades_to_untagged(self):
        md = prz.build_report(self._res(), None, min_n=1, db_path="x.db", since=None)
        assert "行业标签不可用" in md and "| UNTAGGED | 2 |" in md

    def test_empty_pair_result(self):
        md = prz.build_report(prz.PairResult(), _TIERS, min_n=5, db_path="x.db", since=None)
        assert "当前无在持仓位" in md and "无已平仓成交" in md


class TestLoadTrades:
    def test_readonly_and_since(self, tmp_path):
        db = _mk_db(tmp_path / "t.db", [
            ("A", 23, 10.0, 100, _T0),
            ("A", 24, 11.0, 100, _T0 + 10 * _D),
        ])
        rows = prz.load_trades(db)
        assert len(rows) == 2 and rows[0]["direction"] == 23
        import datetime as dt
        since = dt.datetime.fromtimestamp(_T0 + 5 * _D).strftime("%Y%m%d")
        rows = prz.load_trades(db, since=since)
        assert len(rows) == 1 and rows[0]["direction"] == 24

    def test_main_end_to_end(self, tmp_path, monkeypatch, capsys):
        db = _mk_db(tmp_path / "t.db", [
            ("WIN1", 23, 10.0, 100, _T0),
            ("WIN1", 24, 12.0, 100, _T0 + 4 * _D),
        ])
        monkeypatch.setattr(prz, "load_tiers", lambda codes: _TIERS)
        out = tmp_path / "report"
        rc = prz.main(["--db", str(db), "--out", str(out), "--min-n", "1"])
        assert rc == 0
        files = list(out.glob("zero_report_*.md"))
        assert len(files) == 1
        md = files[0].read_text(encoding="utf-8")
        assert "| P1 | 1 | 100.0%" in md
        assert "已平仓 1 笔" in capsys.readouterr().out
