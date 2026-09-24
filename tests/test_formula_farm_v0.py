# -*- coding: utf-8 -*-
"""公式农场 v0 离线回归测试 (red/green)。

v0 纪律: 只增不改 — 只测 tools.formula_farm 新包, 不碰 TDX/现有模块。
依赖 gongshi 语料的用例在语料缺失时自动跳过, 不拖累仓库全量测试。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools.formula_farm import common, intake, dedupe, static_vetting, pack_tni  # noqa: E402

GONGSHI = r"E:\1target\gongshi"
GS_TXT = r"E:\NEW_TDX\T0001\export\gs_txt"


def _md(tmp_path, name, url, code, title=None):
    p = tmp_path / name
    p.write_text(
        "# %s\n\n> 来源: %s\n> 归档时间: 2026-09-06 00:00:00\n\n```\n%s\n```\n"
        % (title or name, url, code),
        encoding="utf-8",
    )
    return p


def test_normalize_code_stable():
    a = common.normalize_code("VAR1:=MA(C,5);\nXG:crOSS(C,VAR1);")
    b = common.normalize_code("  VAR1 := MA( C , 5 ) ; XG:cross(C,VAR1);  ")
    assert a == b


def test_intake_parses_md(tmp_path):
    p = _md(tmp_path, "测试之选股指标公式.md",
            "https://www.gupang.com/202609/99999.html",
            "VAR1:=HIGH;\nXG:C>VAR1;")
    rec = intake.parse_md(p)
    assert rec["url"] == "https://www.gupang.com/202609/99999.html"
    assert "XG:C>VAR1" in rec["code"]
    assert rec["title"] == "测试之选股指标公式"


def test_vetting_main_chart_excluded(tmp_path):
    # 含主图绘图函数 → 不是纯选股, 排除
    p = _md(tmp_path, "某某主图之选股指标公式.md",
            "https://www.gupang.com/x.html",
            "VAR1:=HIGH;\nSTICKLINE(VAR1,H,L,0,0),COLORRED;\nXG:VAR1;")
    rec = intake.parse_md(p)
    assert static_vetting.is_main(rec) is True


def test_vetting_chip_function_excluded(tmp_path):
    # 筹码函数 WINNER/COST → 专有数据函数, 排除
    p = _md(tmp_path, "筹码类之选股指标公式.md",
            "https://www.gupang.com/x.html",
            "A:=WINNER(C)>0.5;\nXG:A AND COST(50)<C;")
    rec = intake.parse_md(p)
    assert static_vetting.exclude_tokens(rec)  # 非空 = 命中排除


def test_pack_roundtrip(tmp_path):
    rows = [
        {"gs": "GS0001", "url": "https://www.gupang.com/a.html",
         "code": "VAR1:=HIGH;\nXG:crOSS(VAR1,MA(C,5));"},
        {"gs": "GS0002", "url": "https://www.gupang.com/b.html",
         "code": "主力成本:=MA(C,20);\nXG:C>主力成本;"},
    ]
    blob = pack_tni.build_pack(rows)
    secs = pack_tni.parse_pack(blob)
    assert len(secs) == 2
    assert secs[0]["name"] == "GS0001"
    assert secs[0]["type"] == "XG"
    assert secs[0]["desc"] == "https://www.gupang.com/a.html"
    assert secs[0]["code"].rstrip() == rows[0]["code"].rstrip()
    # 中文变量名在 GBK 往返后不丢
    assert "主力成本" in secs[1]["code"]


def test_dedupe_hash_identical_code_different_name(tmp_path):
    rec_a = intake.parse_md(_md(tmp_path, "甲之选股指标公式.md",
                                "https://www.gupang.com/1.html",
                                "A:=1;\nXG:A;"))
    rec_b = intake.parse_md(_md(tmp_path, "乙之选股指标公式.md",
                                "https://www.gupang.com/2.html",
                                "A := 1 ; XG : A ;"))
    assert rec_a["hash"] == rec_b["hash"]


@pytest.mark.skipif(not os.path.exists(GONGSHI), reason="需要 gongshi 语料")
def test_parity_clean_list_vs_baseline():
    """回归: 我们的 clean 编号清单 == gongshi _clean_list.txt(397 行)。"""
    res = static_vetting.build_clean(GONGSHI)
    bl = open(os.path.join(GONGSHI, "_clean_list.txt"), encoding="utf-8").read().splitlines()
    assert len(res["clean"]) == len(bl) == 397
    assert [c["gs"] for c in res["clean"]] == bl


@pytest.mark.skipif(not os.path.exists(GONGSHI), reason="需要 gongshi 语料")
def test_parity_excluded_vs_baseline():
    """回归: 我们的排除清单(gs+file+tokens) == _clean_formulas.json 的 excluded。"""
    import json
    res = static_vetting.build_clean(GONGSHI)
    mine = {(e["gs"], e["file"]): e["tokens"] for e in res["excluded"]}
    j = json.load(open(os.path.join(GONGSHI, "_clean_formulas.json"), encoding="utf-8"))
    theirs = {(e["gs"], e["file"]): e["tokens"] for e in j["excluded"]}
    assert set(mine) == set(theirs)
    for k in theirs:
        assert set(mine[k]) == set(theirs[k]), k


# ---- v1 运行时体检(farm 更严: 未来函数硬闸) ----

def test_vet_runtime_future_function_blocked():
    ok, reasons = static_vetting.vet_runtime(
        {"file": "x.md", "code": "A:=ZIG(3,5);\nXG:A;"}, {})
    assert ok is False
    assert any("未来函数" in r for r in reasons)


def test_vet_runtime_clean_passes():
    ok, reasons = static_vetting.vet_runtime(
        {"file": "x.md", "code": "VAR1:=MA(C,10);\nXG:CROSS(C,VAR1);"}, {})
    assert ok is True and not reasons


def test_vet_runtime_chip_and_nooutput_blocked():
    ok1, _ = static_vetting.vet_runtime(
        {"file": "x.md", "code": "A:=COST(50);\nXG:A>1;"}, {})
    assert ok1 is False
    ok2, r2 = static_vetting.vet_runtime(
        {"file": "x.md", "code": "VAR1:=MA(C,10);"}, {})
    assert ok2 is False and any("输出" in r for r in r2)


@pytest.mark.parametrize("spelling", ["POLYLINE", "PLOYLINE"])
def test_vet_runtime_drift_polyline_both_spellings_blocked(spelling):
    """2026-09-17 GS1318 漏网事件: PLOYLINE 是 POLYLINE 的俗写, 两种拼法都必须拦。

    真实事故: GS0607/GS1318/GS0737/GS1333 四条达标公式用 PLOYLINE, 而黑名单
    只登记了 POLYLINE → 全部混进达标榜, 且 GS0607 被标「可用池」。
    """
    ok, reasons = static_vetting.vet_runtime(
        {"file": "x.md", "code": f"A:={spelling}(CROSS(C,MA(C,5)),C);\nXG:A>1;"}, {})
    assert ok is False
    assert any("未来函数" in r for r in reasons)

