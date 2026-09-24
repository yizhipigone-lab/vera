"""gs_5m_sweep.do_report 回归锁 (2026-09-11)。

事故: 把达标线收口到 core.farm_rules 时删掉了 `tgt = ...` 的赋值, 但漏改了下游
`if len(tgt):` → 每次 report 都以 `NameError: name 'tgt' is not defined` 收场
(农场粗扫闸门把 report 步的 rc=1 记成"失败", 但 sweep CSV 其实已经写好了)。
本测试直接调 do_report, 保证它不抛且报出「达标/样本不足」两栏。
"""
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture()
def sweep_mod():
    """导入 gs_5m_sweep (它会连带 import quantqq_5m_sweep 的组合函数)。"""
    try:
        return importlib.import_module("tools.gs_5m_sweep")
    except Exception as e:                                       # noqa: BLE001
        pytest.skip("gs_5m_sweep 不可导入 (缺依赖): %r" % e)


def _write_sweep(dirpath: Path, mod, rows):
    dirpath.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows, columns=mod.CSV_COLUMNS)
    df.to_csv(dirpath / "sweep_refine_shard0of1.csv", index=False)


def _row(mod, **kw):
    r = {c: "" for c in mod.CSV_COLUMNS}
    r.update({"error": ""})
    r.update(kw)
    return r


def test_do_report_reports_pass_and_thin(tmp_path, monkeypatch, capsys, sweep_mod):
    monkeypatch.setattr(sweep_mod, "BASE", str(tmp_path))
    monkeypatch.delenv("SWEEP_TAG", raising=False)
    _write_sweep(tmp_path / "GS9001", sweep_mod, [
        _row(sweep_mod, key="k_pass", annret=0.20, maxdd=-0.05, calmar=4.0,
             winrate=0.6, trades=100),
        _row(sweep_mod, key="k_thin", annret=0.90, maxdd=-0.01, calmar=9.0,
             winrate=1.0, trades=3),
    ])

    sweep_mod.do_report(SimpleNamespace(formula="GS9001"))       # 不许抛
    out = capsys.readouterr().out
    assert "达标:1" in out and "样本不足:1" in out
    assert "达标线" in out and "10%" in out     # 达标线 2026-09-22 起 10%
    assert "达标 Top" in out                                     # 有达标行才印该段


def test_do_report_without_pass_skips_pass_section(tmp_path, monkeypatch, capsys,
                                                   sweep_mod):
    monkeypatch.setattr(sweep_mod, "BASE", str(tmp_path))
    monkeypatch.delenv("SWEEP_TAG", raising=False)
    _write_sweep(tmp_path / "GS9002", sweep_mod, [
        _row(sweep_mod, key="k1", annret=0.001, maxdd=-0.004, calmar=0.3,
             winrate=0.5, trades=176),
    ])
    sweep_mod.do_report(SimpleNamespace(formula="GS9002"))
    out = capsys.readouterr().out
    assert "达标:0" in out
    assert "达标 Top" not in out
