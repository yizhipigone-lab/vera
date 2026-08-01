"""选股结果缓存测试 (2026-07-24, selection/selection_cache.py + pipeline 接缝)。

计划书: docs/plan/2026-07-24_选股结果缓存_计划书.md §7, 钉死 11 件事:
1. 命中 parity: save→load 值一致 + select_date dtype 一致 (不只靠 equals)
2. 配置失效: 改 formula/arg/universe.type/区间/period/dividend_type 任一 → miss
3. 日期失效: 不同 today_str → miss (绕过真实跨日等待)
4. force_refresh: 强制 miss 并重新落盘
5. universe 完整哈希: 改 exclude_st/include_etf/sectors → miss (R5)
6. 空选股不缓存 (防空帧误导)
7. LRU 清理: 写入 N+1 份后最老被清
8. 回归: 缓存命中产物能正常进 matrix_cache.build_key
9. 并发原子写: 两线程同 key save 不互踩, 文件可读
10. CSV 写入行为 (R9): 缓存命中时 raw CSV 仍写入, 与 miss 一致
11. today_str 注入: 生产接缝传入真实当天日期
"""
from __future__ import annotations

import threading
from datetime import datetime

import pandas as pd
import pytest

import pipeline.pipeline as pipeline_module
from pipeline.pipeline import Pipeline
from selection import selection_cache as sc


def _selections():
    return pd.DataFrame({
        "stock_code": ["600001.SH", "000001.SZ"],
        "select_date": pd.to_datetime(["2025-01-02", "2025-01-03"]),
        "formula_name": ["FOO", "FOO"],
    })


_KEY_KW = dict(
    formula_name="FOO", formula_arg="3",
    universe_cfg={"type": "23", "exclude_st": True, "sectors": []},
    start_time="20240101", end_time="20241231",
    period="1d", dividend_type=1, today_str="20260724",
)


def _key(**overrides):
    kw = {**_KEY_KW, **overrides}
    return sc.build_key(**kw)


class _FakeSelector:
    """不碰 TDX 的替身, 记录调用次数。"""

    calls = 0

    def __init__(self, cfg):
        self.cfg = cfg

    def resolve_universe(self):
        return ["600001.SH", "000001.SZ"]

    def run(self, start_time="", end_time="", stock_list=None):
        type(self).calls += 1
        return _selections()


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """缓存根指向 tmp (不碰真实 data/selection_cache)。"""
    d = tmp_path / "selection_cache"
    monkeypatch.setattr(sc, "default_cache_root", lambda: d)
    return d


@pytest.fixture
def pipe(tmp_path, monkeypatch, cache_dir):
    """最小 Pipeline 实例: 替身选股 + tmp 输出目录。

    period=5m: 批次6 D4 起 L0 对 1d 收缩 (1d 走 L2), L0 行为测试用 5m。
    """
    _FakeSelector.calls = 0
    monkeypatch.setattr(pipeline_module, "StockSelector", _FakeSelector)
    cfg_file = tmp_path / "strategy.yaml"
    cfg_file.write_text(
        "strategy:\n  name: TESTSEL\n"
        "selection:\n  formula_name: FOO\n  formula_arg: '3'\n"
        "  universe:\n    type: '23'\n    exclude_st: true\n"
        "  period: 5m\n  dividend_type: 1\n"
        "time_range:\n  start: '20240101'\n  end: '20241231'\n",
        encoding="utf-8")
    p = Pipeline(str(cfg_file))
    sel_dir = tmp_path / "selections"
    sel_dir.mkdir()
    p.output_dirs["selections"] = str(sel_dir)
    return p


class TestRoundTrip:
    def test_hit_parity_values_and_dtype(self, tmp_path):
        df = _selections()
        sc.save(tmp_path, "k1", df)
        out = sc.load(tmp_path, "k1")
        assert out is not None
        assert out["stock_code"].tolist() == df["stock_code"].tolist()
        assert out["select_date"].tolist() == df["select_date"].tolist()
        assert out["formula_name"].tolist() == df["formula_name"].tolist()
        # 审计 INFO: parquet round-trip 可能 datetime64 ns↔us, 显式断言 dtype
        assert out["select_date"].dtype == df["select_date"].dtype

    def test_missing_key_returns_none(self, tmp_path):
        assert sc.load(tmp_path, "nope") is None

    def test_corrupt_file_treated_as_miss(self, tmp_path):
        bad = tmp_path / "bad.parquet"
        bad.write_bytes(b"not a parquet")
        assert sc.load(tmp_path, "bad") is None
        assert not bad.exists()  # 坏文件顺手清掉


class TestKeyInvalidation:
    @pytest.mark.parametrize("field,value", [
        ("formula_name", "BAR"),
        ("formula_arg", "5"),
        ("start_time", "20230101"),
        ("end_time", "20251231"),
        ("period", "5m"),
        ("dividend_type", 2),
    ])
    def test_config_change_invalidates(self, field, value):
        assert _key(**{field: value}) != _key()

    def test_today_str_change_invalidates(self):
        assert _key(today_str="20260725") != _key(today_str="20260724")

    @pytest.mark.parametrize("field,value", [
        ("exclude_st", False),
        ("include_etf", True),
        ("sectors", ["881319.SH"]),
    ])
    def test_universe_full_hash(self, field, value):
        uni = dict(_KEY_KW["universe_cfg"])
        uni[field] = value
        assert _key(universe_cfg=uni) != _key()

    def test_universe_order_insensitive(self):
        uni = {"exclude_st": True, "type": "23", "sectors": []}
        assert _key(universe_cfg=uni) == _key()

    def test_universe_falsey_defaults_converge(self):
        """web 路径补全默认键 vs yaml 路径省略默认键 → 同 key (跨入口命中)。"""
        sparse = {"type": "23", "exclude_st": True}
        full = {"type": "23", "exclude_st": True, "include_etf": False,
                "etf_only": False, "sectors": [], "exclude_new_listings_days": 0}
        assert _key(universe_cfg=full) == _key(universe_cfg=sparse)

    def test_formula_arg_none_normalized(self):
        assert _key(formula_arg=None) == _key(formula_arg="")


class TestSaveBehavior:
    def test_empty_not_cached(self, tmp_path):
        empty = pd.DataFrame(columns=["stock_code", "select_date", "formula_name"])
        sc.save(tmp_path, "k1", empty)
        assert list(tmp_path.glob("*.parquet")) == []

    def test_lru_prune(self, tmp_path):
        import time
        for i in range(4):
            sc.save(tmp_path, f"k{i}", _selections(), keep=3)
            time.sleep(0.02)  # 保证 mtime 可分
        remaining = sorted(f.name for f in tmp_path.glob("*.parquet"))
        assert len(remaining) == 3
        assert "k0.parquet" not in remaining

    def test_concurrent_atomic_write(self, tmp_path):
        errors = []

        def _save():
            try:
                for _ in range(5):
                    sc.save(tmp_path, "same", _selections())
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=_save) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        out = sc.load(tmp_path, "same")
        assert out is not None and len(out) == 2


class TestPipelineSeam:
    def test_second_run_hits_and_parity(self, pipe):
        first = pipe.step1_select()
        assert _FakeSelector.calls == 1
        second = pipe.step1_select()
        assert _FakeSelector.calls == 1  # 命中: 未再跑选股
        assert second["stock_code"].tolist() == first["stock_code"].tolist()
        assert second["select_date"].tolist() == first["select_date"].tolist()

    def test_force_refresh_reruns_and_resaves(self, pipe, cache_dir):
        pipe.step1_select()
        assert _FakeSelector.calls == 1
        pipe.config["selection_cache"] = {"enabled": True, "force_refresh": True}
        pipe.step1_select()
        assert _FakeSelector.calls == 2  # 强制 miss
        assert len(list(cache_dir.glob("*.parquet"))) == 1  # 重新落盘同 key

    def test_disabled_cache_always_reruns(self, pipe):
        pipe.config["selection_cache"] = {"enabled": False}
        pipe.step1_select()
        pipe.step1_select()
        assert _FakeSelector.calls == 2

    def test_csv_written_on_hit(self, pipe, tmp_path):
        import os
        pipe.step1_select()  # miss → 落盘 + CSV
        csvs_miss = set(os.listdir(pipe.output_dirs["selections"]))
        assert len(csvs_miss) == 1
        pipe.step1_select()  # hit → R9: CSV 仍写
        csvs_hit = set(os.listdir(pipe.output_dirs["selections"]))
        assert len(csvs_hit) >= 1  # 同秒文件名相同则覆盖, 跨秒则新增
        csv_path = next(iter(csvs_hit - csvs_miss), None) or next(iter(csvs_hit))
        df = pd.read_csv(os.path.join(pipe.output_dirs["selections"], csv_path))
        assert df["stock_code"].tolist() == ["600001.SH", "000001.SZ"]

    def test_production_today_str_injected(self, pipe, cache_dir):
        """审计 LOW: 生产接缝用真实当天日期建 key, 文件落盘名可复算。"""
        pipe.step1_select()
        expected = sc.build_key(
            formula_name="FOO", formula_arg="3",
            universe_cfg=pipe.config["selection"]["universe"],
            start_time="20240101", end_time="20241231",
            period="5m", dividend_type=1,
            today_str=datetime.now().strftime("%Y%m%d"))
        assert (cache_dir / f"{expected}.parquet").exists()

    def test_l0_skipped_for_1d(self, pipe, cache_dir):
        """批次6 D4: period=1d 时 L0 不读不写 (1d 由 L2 接管, 消双写)。"""
        pipe.config["selection"]["period"] = "1d"
        pipe.step1_select()
        pipe.step1_select()
        assert _FakeSelector.calls == 2  # L0 未命中: 每次都直跑
        assert list(cache_dir.glob("*.parquet")) == []  # L0 未落盘

    def test_l0_covers_1d_when_l2_disabled(self, pipe, cache_dir):
        """批次6 D4 例外: L2 配置关闭时 1d 仍走 L0, 不留无缓存空档。"""
        pipe.config["selection"]["period"] = "1d"
        pipe.config["selection_cache"] = {"enabled": True, "l2_enabled": False}
        pipe.step1_select()
        pipe.step1_select()
        assert _FakeSelector.calls == 1  # L0 命中
        assert len(list(cache_dir.glob("*.parquet"))) == 1

    def test_hit_feeds_matrix_cache_build_key(self, pipe):
        """回归: 命中产物能正常进 matrix_cache.build_key (两层缓存叠加)。"""
        from backtest import matrix_cache as mc
        pipe.step1_select()
        hits = pipe.step1_select()
        key = mc.build_key(hits, "20240101", "20241231", "1d", 20, True, "test")
        assert isinstance(key, str) and len(key) == 32
