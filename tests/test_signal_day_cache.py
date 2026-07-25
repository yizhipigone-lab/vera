# -*- coding: utf-8 -*-
"""L2 按日信号缓存测试 (2026-07-26, selection/signal_day_cache.py + StockSelector.run 接缝)。

计划书 docs/plan/2026-07-26_选股缓存二期_L1池缓存_L2按日信号缓存_计划书.md §6:
全 miss→逐日落盘(含空天) / 全命中零调用 / 部分命中只算缺失区段+合并 parity /
区段分组 / 最近2交易日不缓存 / 超龄重算 / 空信号命中 / 批次失败不落盘 /
池变化失效 / 参数变化失效 / 合并去重 / dtype parity / 5m 跳过 / force_refresh / LRU
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.data_fetcher import DataFetcher
from core.formula_runner import FormulaRunner
from selection import signal_day_cache as sdc
from selection.selector import StockSelector

POOL = ["600001.SH", "000001.SZ"]
KW = dict(formula_name="F", formula_arg="", period="1d", dividend_type=1,
          stock_list=POOL)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    """tmp 缓存根 + 假交易日历 (工作日) + 默认配置还原。"""
    monkeypatch.setattr(sdc, "default_cache_root", lambda: tmp_path / "sdc")
    monkeypatch.setattr(
        DataFetcher, "get_trading_days",
        staticmethod(lambda start_time, end_time, market="SH":
                     list(pd.bdate_range(start_time, end_time))))
    saved = (sdc.ENABLED, sdc.FORCE_REFRESH, sdc.MAX_AGE_DAYS, sdc.FRESH_DAYS)
    sdc.configure(enabled=True, force_refresh=False, max_age_days=60, fresh_days=2)
    yield tmp_path / "sdc"
    sdc.configure(enabled=saved[0], force_refresh=saved[1],
                  max_age_days=saved[2], fresh_days=saved[3])


def _fake_runner(calls, signal_fn, errors=0):
    """signal_fn(d, pool) -> [codes]; 记录调用区段; 同步 last_batch_errors。"""
    def run(formula_name, formula_arg, stock_list, start_time, end_time,
            stock_period, dividend_type):
        calls.append((start_time, end_time))
        FormulaRunner.last_batch_errors = errors
        recs = [{"stock_code": c, "select_date": d, "formula_name": formula_name}
                for d in pd.bdate_range(start_time, end_time)
                for c in signal_fn(d, stock_list)]
        if not recs:
            return pd.DataFrame(columns=["stock_code", "select_date", "formula_name"])
        df = pd.DataFrame(recs)
        df["select_date"] = pd.to_datetime(df["select_date"])
        return df
    return run


def _run(start, end, **over):
    kw = {**KW, **over}
    return sdc.get_or_compute(start_time=start, end_time=end, **kw)


class TestComputeAndCache:
    def test_full_miss_saves_every_day_incl_empty(self, _env, monkeypatch):
        calls = []
        # 仅偶数日有信号, 奇数日应落空文件
        monkeypatch.setattr(FormulaRunner, "run_stock_selection_with_dates",
                            _fake_runner(calls, lambda d, p: ["600001.SH"] if d.day % 2 == 0 else []))
        out = _run("20260601", "20260612")
        assert len(out) == 5  # 2,4,8,10,12 偶数日 (6/1~6/12 共 10 个工作日)
        files = sorted(_env.glob("*/*.parquet"))
        assert len(files) == 10         # 10 个工作日全落盘 (含 5 个空天)
        assert len(calls) == 1          # 单个连续区段, 一次算完

    def test_full_hit_zero_calls_and_parity(self, _env, monkeypatch):
        calls = []
        fake = _fake_runner(calls, lambda d, p: ["600001.SH"] if d.day % 2 == 0 else [])
        monkeypatch.setattr(FormulaRunner, "run_stock_selection_with_dates", fake)
        first = _run("20260601", "20260612")
        calls.clear()
        second = _run("20260601", "20260612")
        assert calls == []              # 全命中, 零公式调用
        assert second["stock_code"].tolist() == first["stock_code"].tolist()
        assert second["select_date"].tolist() == first["select_date"].tolist()
        assert second["select_date"].dtype == first["select_date"].dtype

    def test_partial_hit_recomputes_whole_range_once(self, _env, monkeypatch):
        calls = []
        fake = _fake_runner(calls, lambda d, p: ["600001.SH"])
        monkeypatch.setattr(FormulaRunner, "run_stock_selection_with_dates", fake)
        _run("20260601", "20260612")            # 先缓存 6/1~6/12
        calls.clear()
        out = _run("20260601", "20260626")      # 再请求 6/1~6/26 (有缺失)
        assert calls == [("20260601", "20260626")]  # 整段一次重算 (与直跑同价)
        assert len(out) == len(list(pd.bdate_range("20260601", "20260626")))
        assert out["select_date"].tolist() == list(pd.bdate_range("20260601", "20260626"))
        # 重算后全区间按天入库 → 再次请求全命中
        calls.clear()
        _run("20260601", "20260626")
        assert calls == []

    def test_any_miss_single_whole_range_call(self, _env, monkeypatch):
        """实测修正后的策略: 任一缺失 → 整段一次算 (与直跑同价), 不按区段分算。
        手工预置隔天缓存, 缺失也只应产生 1 次整段调用 (防多区段 N 倍地板成本)。"""
        from selection import universe_cache as uc
        combo = sdc._combo_key("F", "", uc.pool_hash(POOL), "1d", 1)
        for d in pd.bdate_range("20260601", "20260612"):
            if d.day % 2 == 0:      # 偶数日已缓存
                sdc._save_day(_env, combo, d.strftime("%Y%m%d"),
                              pd.DataFrame({"stock_code": ["600001.SH"],
                                            "select_date": [d], "formula_name": ["F"]}))
        calls = []
        monkeypatch.setattr(FormulaRunner, "run_stock_selection_with_dates",
                            _fake_runner(calls, lambda d, p: ["600001.SH"]))
        out = _run("20260601", "20260612")
        assert calls == [("20260601", "20260612")]   # 恰好 1 次整段调用
        assert len(out) == len(list(pd.bdate_range("20260601", "20260612")))

    def test_subrange_fully_covered_zero_calls(self, _env, monkeypatch):
        """L2 核心价值: 历史并集覆盖的子区间, 零公式调用。"""
        calls = []
        monkeypatch.setattr(FormulaRunner, "run_stock_selection_with_dates",
                            _fake_runner(calls, lambda d, p: ["600001.SH"]))
        _run("20260601", "20260626")      # 先缓存整月
        calls.clear()
        out = _run("20260608", "20260619")  # 子区间
        assert calls == []
        assert len(out) == len(list(pd.bdate_range("20260608", "20260619")))

    def test_empty_signals_cached_and_hit(self, _env, monkeypatch):
        calls = []
        monkeypatch.setattr(FormulaRunner, "run_stock_selection_with_dates",
                            _fake_runner(calls, lambda d, p: []))
        out = _run("20260601", "20260605")
        assert out.empty
        n_files = len(list(_env.glob("*/*.parquet")))
        assert n_files == 5           # 空天也落盘
        calls.clear()
        _run("20260601", "20260605")
        assert calls == []            # 空信号也命中, 不再调公式

    def test_batch_failure_not_cached(self, _env, monkeypatch):
        calls = []
        monkeypatch.setattr(FormulaRunner, "run_stock_selection_with_dates",
                            _fake_runner(calls, lambda d, p: [], errors=1))
        _run("20260601", "20260605")
        assert len(list(_env.glob("*/*.parquet"))) == 0   # 失败区段不落盘
        assert len(calls) == 1
        _run("20260601", "20260605")
        assert len(calls) == 2        # 下次重试

    def test_pool_change_invalidates(self, _env, monkeypatch):
        calls = []
        monkeypatch.setattr(FormulaRunner, "run_stock_selection_with_dates",
                            _fake_runner(calls, lambda d, p: ["600001.SH"]))
        _run("20260601", "20260605")
        _run("20260601", "20260605", stock_list=POOL + ["000002.SZ"])
        assert len(calls) == 2        # 池变 → combo 变 → 全 miss

    def test_formula_arg_change_invalidates(self, _env, monkeypatch):
        calls = []
        monkeypatch.setattr(FormulaRunner, "run_stock_selection_with_dates",
                            _fake_runner(calls, lambda d, p: ["600001.SH"]))
        _run("20260601", "20260605")
        _run("20260601", "20260605", formula_arg="3")
        assert len(calls) == 2

    def test_merge_dedupes(self, _env, monkeypatch):
        # 全命中路径: 日文件内含重复行, 合并应去重
        from selection import universe_cache as uc
        combo = sdc._combo_key("F", "", uc.pool_hash(POOL), "1d", 1)
        days = list(pd.bdate_range("20260601", "20260605"))
        for d in days:
            dup = pd.DataFrame({"stock_code": ["600001.SH", "600001.SH"],
                                "select_date": [d, d], "formula_name": ["F", "F"]})
            sdc._save_day(_env, combo, d.strftime("%Y%m%d"), dup)
        monkeypatch.setattr(FormulaRunner, "run_stock_selection_with_dates",
                            _fake_runner([], lambda d, p: []))
        out = _run("20260601", "20260605")
        assert len(out) == len(days)        # 每天 1 条 (文件内 2 条已去重)

    def test_force_recomputes_and_overwrites(self, _env, monkeypatch):
        calls = []
        monkeypatch.setattr(FormulaRunner, "run_stock_selection_with_dates",
                            _fake_runner(calls, lambda d, p: ["600001.SH"]))
        _run("20260601", "20260605")
        _run("20260601", "20260605", force=True)
        assert len(calls) == 2

    def test_lru_prune(self, _env, monkeypatch):
        monkeypatch.setattr(sdc, "KEEP_FILES", 5)
        monkeypatch.setattr(FormulaRunner, "run_stock_selection_with_dates",
                            _fake_runner([], lambda d, p: []))
        _run("20260601", "20260612")   # 9 天 > 5
        assert len(list(_env.glob("*/*.parquet"))) == 5


class TestFreshAndStale:
    @staticmethod
    def _fake_dt(y, m, d):
        from datetime import datetime as _RealDT

        class FakeDT:
            @staticmethod
            def now():
                return _RealDT(y, m, d, 20, 0)

            @staticmethod
            def fromtimestamp(ts):
                return _RealDT.fromtimestamp(ts)
        return FakeDT

    def test_today_never_cached(self, _env, monkeypatch):
        # 当日 (2026-07-24 周五, 交易日) 永不缓存
        import os
        FakeDT = self._fake_dt(2026, 7, 24)
        monkeypatch.setattr(sdc, "datetime", FakeDT)
        calls = []
        monkeypatch.setattr(FormulaRunner, "run_stock_selection_with_dates",
                            _fake_runner(calls, lambda d, p: ["600001.SH"]))
        _run("20260720", "20260724")
        saved = {f.stem for f in _env.glob("*/*.parquet")}
        assert "20260724" not in saved          # 当日不落盘
        calls.clear()
        _run("20260720", "20260724")
        assert calls                            # 当日天每次现算

    def test_recent_days_hit_only_same_day(self, _env, monkeypatch):
        # 最近交易日: 当日计算 → 当日命中; mtime 跨日 → 重算
        import os
        from datetime import datetime as _RealDT
        FakeDT = self._fake_dt(2026, 7, 24)
        monkeypatch.setattr(sdc, "datetime", FakeDT)
        calls = []
        monkeypatch.setattr(FormulaRunner, "run_stock_selection_with_dates",
                            _fake_runner(calls, lambda d, p: ["600001.SH"]))
        _run("20260720", "20260723")
        # 对齐 mtime 到"当日" (真实文件系统时间 ≠ 模拟时间)
        ts = _RealDT(2026, 7, 24, 20, 0).timestamp()
        for f in _env.glob("*/*.parquet"):
            os.utime(f, (ts, ts))
        calls.clear()
        _run("20260720", "20260723")
        assert calls == []                       # 当日重跑全命中
        # mtime 拨回昨天 → 跨日失效
        ts_old = _RealDT(2026, 7, 23, 20, 0).timestamp()
        for f in _env.glob("*/*.parquet"):
            os.utime(f, (ts_old, ts_old))
        _run("20260720", "20260723")
        assert calls                             # 跨日 → 整段重算

    def test_stale_days_recomputed(self, _env, monkeypatch):
        sdc.configure(max_age_days=30)
        calls = []
        monkeypatch.setattr(FormulaRunner, "run_stock_selection_with_dates",
                            _fake_runner(calls, lambda d, p: ["600001.SH"]))
        _run("20260601", "20260605")                 # 距今 >30 天 → stale
        saved = list(_env.glob("*/*.parquet"))
        assert saved                                  # stale 重算后仍落盘 (覆盖)
        calls.clear()
        _run("20260601", "20260605")
        assert calls                                  # 但下次仍重算 (不命中)


class TestSelectorSeam:
    def test_run_uses_l2_for_1d(self, _env, monkeypatch):
        marker = pd.DataFrame({"stock_code": ["600001.SH"],
                               "select_date": pd.to_datetime(["2026-06-02"]),
                               "formula_name": ["F"]})
        used = {}
        monkeypatch.setattr(sdc, "get_or_compute",
                            lambda **kw: used.__setitem__("l2", True) or marker)
        sel = StockSelector({"formula_name": "F", "period": "1d",
                             "universe": {"type": "50"}})
        out = sel.run(start_time="20260601", end_time="20260605", stock_list=POOL)
        assert used.get("l2") and len(out) == 1

    def test_run_skips_l2_for_5m(self, _env, monkeypatch):
        calls = []
        monkeypatch.setattr(FormulaRunner, "run_stock_selection_with_dates",
                            _fake_runner(calls, lambda d, p: []))
        monkeypatch.setattr(sdc, "get_or_compute",
                            lambda **kw: (_ for _ in ()).throw(AssertionError("不应走 L2")))
        sel = StockSelector({"formula_name": "F", "period": "5m",
                             "universe": {"type": "50"}})
        sel.run(start_time="20260601", end_time="20260605", stock_list=POOL)
        assert calls                                  # 走了直跑

    def test_l2_exception_falls_back(self, _env, monkeypatch):
        calls = []
        monkeypatch.setattr(FormulaRunner, "run_stock_selection_with_dates",
                            _fake_runner(calls, lambda d, p: ["600001.SH"]))
        monkeypatch.setattr(sdc, "get_or_compute",
                            lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        sel = StockSelector({"formula_name": "F", "period": "1d",
                             "universe": {"type": "50"}})
        out = sel.run(start_time="20260601", end_time="20260605", stock_list=POOL)
        assert calls and not out.empty                # 异常回退直跑, 不中断
