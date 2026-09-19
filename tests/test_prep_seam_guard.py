# -*- coding: utf-8 -*-
"""tests/test_prep_seam_guard.py — 矩阵缓存"准备段口径"守卫 (2026-09-20 审计 P1-4)。

背景: 4 个 sweep + research 脚本原先各自判断(或干脆不判断)缓存口径 —— 只有
gs_5m_sweep 一家校验, 而**口径真变**的 quantqq 5m/1m 反而不校验 → 旧缓存
(手工复刻配方, 窗口不截断到 end_time) 被静默复用。现在校验只有一份实现
(backtest.engine.check_prep_caliber), 默认 fail-closed。
"""
from __future__ import annotations

import pytest

from backtest.engine import PREP_SEAM, check_prep_caliber


def test_current_caliber_passes():
    check_prep_caliber({"prep_seam": PREP_SEAM}, where="test")


def test_missing_or_stale_marker_refuses(monkeypatch):
    monkeypatch.delenv("VERA_SWEEP_ALLOW_OLD_PREP", raising=False)
    for meta in ({}, {"prep_seam": ""}, {"prep_seam": "old_manual_recipe"},
                 {"engine_version": "v3.5"}):
        with pytest.raises(RuntimeError, match="口径不符"):
            check_prep_caliber(meta, where="test")


def test_escape_hatch_only_warns(monkeypatch, caplog):
    monkeypatch.setenv("VERA_SWEEP_ALLOW_OLD_PREP", "1")
    with caplog.at_level("WARNING"):
        check_prep_caliber({}, where="test")     # 不抛
    assert any("旧缓存" in r.message or "口径不符" in r.message
               for r in caplog.records), "逃生开关下必须留告警痕迹"


def test_all_sweeps_use_the_shared_guard():
    """四个 sweep 必须都调用共享校验 (防"只写标记不校验"复发)。"""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for rel in ("tools/gs_5m_sweep.py", "tools/quantqq_5m_sweep.py",
                "tools/quantqq_1m_sweep.py", "tools/quantqq_5m_sweep_2010.py",
                "research/gupiao_stability_sweep.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert "check_prep_caliber(" in src, f"{rel} 没有调用共享口径校验"
        assert "PREP_SEAM" in src, f"{rel} 落盘标记没有用常量 (会与校验漂移)"


# _load_cache 的调用签名: (模块相对路径, 调 _load_cache 时要传的位置参数)
_SWEEPS = (
    ("tools/gs_5m_sweep.py", ("F", 60)),
    ("tools/quantqq_5m_sweep.py", (60,)),
    ("tools/quantqq_1m_sweep.py", (60,)),
    ("tools/quantqq_5m_sweep_2010.py", (60,)),
    ("research/gupiao_stability_sweep.py", ("F", "2020")),
)


def _load_sweep_module(rel: str):
    """按路径加载 sweep 脚本 (它们不是包的一部分, 只能 spec_from_file_location)。"""
    import importlib.util
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    name = "sweep_probe_" + Path(rel).stem
    spec = importlib.util.spec_from_file_location(name, root / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _write_cache(dirpath, prep_seam=None):
    """在 dirpath 造一份形状自洽的矩阵缓存 (3 交易日 × 2 股)。"""
    import json
    import numpy as np
    shape = (3, 2)
    np.save(dirpath / "close.npy", np.ones(shape, dtype=np.float64))
    np.save(dirpath / "high.npy", np.ones(shape, dtype=np.float64))
    np.save(dirpath / "low.npy", np.ones(shape, dtype=np.float64))
    np.save(dirpath / "open.npy", np.ones(shape, dtype=np.float64))
    np.save(dirpath / "entries.npy", np.zeros(shape, dtype=bool))
    np.save(dirpath / "tradable.npy", np.ones(shape, dtype=bool))
    np.save(dirpath / "last_tradable_idx.npy", np.full(2, 2, dtype=np.int64))
    meta = {"index": ["2026-01-05", "2026-01-06", "2026-01-07"],
            "columns": ["000001.SZ", "600000.SH"], "shape": [3, 2]}
    if prep_seam is not None:
        meta["prep_seam"] = prep_seam
    (dirpath / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


@pytest.mark.parametrize("rel,args", _SWEEPS)
def test_load_cache_actually_enforces_caliber(rel, args, tmp_path, monkeypatch):
    """**真跑**每个 sweep 的 _load_cache (2026-09-20 审计自捕获)。

    原先的正则/子串断言查不出这一类: 守卫调用被写进 _load_cache, 而
    check_prep_caliber 只在 do_prep 里局部 import → 调用点 NameError。
    旧缓存 (无 prep_seam) 必须 RuntimeError 拒绝; 新缓存必须能读通 ——
    后半句同时证明"校验名字在该作用域真的可解析" (NameError 会让它红)。
    """
    monkeypatch.delenv("VERA_SWEEP_ALLOW_OLD_PREP", raising=False)
    mod = _load_sweep_module(rel)

    stale_dir = tmp_path / "stale"
    stale_dir.mkdir()
    _write_cache(stale_dir, prep_seam=None)
    monkeypatch.setattr(mod, "_cache_dir", lambda *a, **k: str(stale_dir))
    with pytest.raises(RuntimeError, match="口径不符"):
        mod._load_cache(*args)

    fresh_dir = tmp_path / "fresh"
    fresh_dir.mkdir()
    _write_cache(fresh_dir, prep_seam=PREP_SEAM)
    monkeypatch.setattr(mod, "_cache_dir", lambda *a, **k: str(fresh_dir))
    meta, mats = mod._load_cache(*args)
    assert meta["prep_seam"] == PREP_SEAM
    assert mats["close_df"].shape == (3, 2)

