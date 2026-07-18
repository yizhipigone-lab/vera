"""_merge_window_masks parity 测试 (2026-07-18 假死事件修复)。

背景: 原 concat(axis=0) + groupby.max 在 bool+NaN→object 时退化为纯 Python
逐列聚合 (~0.84s/列 × 3873 列 ≈ 54 分钟, py-spy 实锤回测假死)。
修复: 利用批间列互斥性质, 逐批 reindex 到并集时间轴 + concat(axis=1)。

本文件钉死三件事:
1. 快速路径与旧 groupby 参考实现结果**逐单元格一致** (含 index/columns/dtype)
2. 批间列重叠 / 批内重复时间戳时**退回慢速路径**且结果正确 (OR 语义)
3. 快速路径结果 dtype 为 bool (不再产生 object 帧)
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.data_fetcher import _merge_window_masks


def _old_merge_reference(mask_frames):
    """旧实现原样照抄 (git HEAD 版本), 作 parity 甲骨文。"""
    window_mask = pd.concat(mask_frames, axis=0)
    return window_mask.groupby(level=0).max().sort_index().fillna(False)


def _bars(day: str, n: int = 48) -> pd.DatetimeIndex:
    """n 根 5m bar 标准时刻 (9:35 起)。"""
    return pd.date_range(f"{day} 09:35", periods=n, freq="5min")


def _frame(days, codes, seed, true_ratio=0.3) -> pd.DataFrame:
    idx = _bars(days[0])
    for d in days[1:]:
        idx = idx.append(_bars(d))
    rng = np.random.default_rng(seed)
    vals = rng.random((len(idx), len(codes))) < true_ratio
    return pd.DataFrame(vals, index=idx, columns=codes)


def _assert_parity(frames):
    """新旧实现逐单元格一致 (旧结果 astype(bool) 后比较)。"""
    old = _old_merge_reference(frames).astype(bool)
    new = _merge_window_masks(frames)
    assert list(new.columns) == list(old.columns), "列顺序不一致"
    # check_freq=False: freq 元数据属 pandas 实现细节 (groupby/reindex 保留策略不同),
    # 语义只看时间戳取值; 下游会再 reindex 到 Close.index, freq 不影响行为
    pd.testing.assert_frame_equal(new, old, check_dtype=True, check_freq=False)
    # dtype 钉死: 快速路径必须产出 bool 帧, 不再是 object
    assert all(t == np.bool_ for t in new.dtypes), f"dtype 非 bool: {new.dtypes.unique()}"


class TestParity:
    def test_overlapping_timestamps_disjoint_columns(self):
        """主场景: 18 批式结构缩小版 — 时间轴重叠 + 列互斥。"""
        frames = [
            _frame(["2025-01-02", "2025-01-03", "2025-01-06"], ["600001", "600002"], seed=1),
            _frame(["2025-01-03", "2025-01-06", "2025-01-07"], ["000001", "000002"], seed=2),
            _frame(["2025-01-06", "2025-01-07", "2025-01-08"], ["300001"], seed=3),
        ]
        _assert_parity(frames)

    def test_disjoint_timestamps(self):
        """批间时间轴完全不重叠。"""
        frames = [
            _frame(["2025-01-02"], ["600001"], seed=4),
            _frame(["2025-02-05"], ["000001"], seed=5),
        ]
        _assert_parity(frames)

    def test_subset_index(self):
        """一批的时间轴是另一批的子集 (极端重叠)。"""
        frames = [
            _frame(["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"], ["600001"], seed=6),
            _frame(["2025-01-03"], ["000001"], seed=7, true_ratio=0.9),
        ]
        _assert_parity(frames)

    def test_single_batch(self):
        frames = [_frame(["2025-01-02", "2025-01-03"], ["600001", "600002"], seed=8)]
        _assert_parity(frames)

    def test_all_false_and_all_true(self):
        f1 = pd.DataFrame(False, index=_bars("2025-01-02"), columns=["600001"])
        f2 = pd.DataFrame(True, index=_bars("2025-01-02"), columns=["000001"])
        _assert_parity([f1, f2])

    def test_many_batches_scale(self):
        """18 批 × 20 列 规模冒烟 (真实现场为 18 批 × ~215 列)。"""
        frames = [
            _frame(["2025-01-02", "2025-01-03"], [f"{b*100+i:06d}" for i in range(20)], seed=b)
            for b in range(18)
        ]
        _assert_parity(frames)


class TestFallback:
    def test_shared_column_falls_back_and_ors(self, caplog):
        """批间列重叠 → 退回 groupby 路径, 且同 (行,列) 取 OR。"""
        idx = _bars("2025-01-02", n=4)
        f1 = pd.DataFrame({"600001": [True, False, False, False]}, index=idx)
        f2 = pd.DataFrame({"600001": [False, False, True, False]}, index=idx)
        with caplog.at_level("WARNING", logger="core.data_fetcher"):
            out = _merge_window_masks([f1, f2])
        assert "退回 groupby 慢速合并" in caplog.text
        expected = pd.DataFrame(
            {"600001": [True, False, True, False]}, index=idx)
        pd.testing.assert_frame_equal(
            out.astype(bool).sort_index(), expected, check_dtype=False, check_freq=False)

    def test_duplicate_index_within_batch_falls_back(self, caplog):
        """批内重复时间戳 → 退回 groupby 路径 (reindex 会炸, 必须兜底)。"""
        idx = _bars("2025-01-02", n=2).append(_bars("2025-01-02", n=2))  # 4 行含 2 重复
        f1 = pd.DataFrame({"600001": [True, False, False, True]}, index=idx)
        with caplog.at_level("WARNING", logger="core.data_fetcher"):
            out = _merge_window_masks([f1])
        assert "退回 groupby 慢速合并" in caplog.text
        # OR 语义: 两个重复时间戳都是 (True|False)=True, (False|True)=True
        assert out.astype(bool).values.all()
