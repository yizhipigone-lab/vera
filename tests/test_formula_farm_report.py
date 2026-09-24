"""公式农场粗扫报告文案 (farm_backtest.build_report) 测试。

2026-09-11 审计驱动的修复:
- F1 不再静默漏扫: 报告抬头必须写清「本批 / 本轮 / 余量」;
- F2 抬头必须写全口径 + 达标线 + 声明区间与**实测窗口**;
- F3 笔数不足的组合判「样本不足」, 不参与「最优组合」评选;
- 每行给判定 + 人话原因 (不再只报四指标让人自己猜)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.formula_farm.farm_backtest import build_report  # noqa: E402

CTX = {
    "date": "2026-09-11",
    "declared": ("20240801", "20260911"),
    "actual": ("2024-09-11", "2026-09-10"),
    "total": 20, "done": 10, "remaining": ["GS1295", "GS1296"],
    "capital": 3_000_000.0, "max_buy": 20_000.0, "period": "5m",
    "dividend": "前复权", "universe": "沪深300 (TDX type 23)",
    "priority": "移动止盈优先",
}


def _row(ann, dd, trades, key="c-0.08_a0.08_d0.005_Loff_t12_cd0_cp0.0",
         winrate=0.5, calmar=None):
    return {"key": key, "annret": ann, "maxdd": dd, "trades": trades,
            "winrate": winrate, "calmar": calmar if calmar is not None else 0.3}


def _results():
    return [
        {"gs": "GS1285", "file": "63777_一线看涨.md", "rows": [_row(0.0014, -0.0046, 176)]},
        {"gs": "GS1292", "file": "69383_涨停过前高.md", "rows": [_row(0.00101, -0.000122, 3)]},
        {"gs": "GS1299", "file": "69440_超强主力.md", "rows": []},
    ]


def test_header_carries_full_caliber_and_thresholds():
    md = build_report(_results(), CTX)
    for needle in ("300万", "2万", "5m", "前复权", "沪深300", "移动止盈优先"):
        assert needle in md, needle
    assert "10%" in md and "20" in md          # 达标线 (2026-09-22 起 10%) + 样本门槛
    assert "达标线" in md


def test_header_reports_declared_vs_actual_window():
    md = build_report(_results(), CTX)
    assert "20240801" in md and "20260911" in md
    assert "2024-09-11" in md and "2026-09-10" in md
    assert "实测" in md


def test_header_accounts_for_batch_coverage():
    """F1: 不能再静默漏扫 —— 本批 20 条 / 本轮 10 条 / 余 2 条都要写出来。"""
    md = build_report(_results(), CTX)
    assert "20" in md and "10" in md
    assert "GS1295" in md and "GS1296" in md
    assert "未扫" in md or "余" in md


def test_summary_counts_all_four_states():
    """四态都要有计数; 第四态用户可见名 = 「无有效组合」(零信号或 36 组全失败)。"""
    md = build_report(_results(), CTX)
    assert "达标" in md and "未达标" in md and "样本不足" in md
    assert "无有效组合" in md


def test_thin_sample_is_labelled_not_crowned():
    """F3: 3 笔的组合不许当「最优」结论 —— 要标样本不足。"""
    md = build_report(_results(), CTX)
    line = [l for l in md.splitlines() if "GS1292" in l][0]
    assert "样本不足" in line
    assert "GS1285" in md
    # GS1285 有 176 笔但年化 0.14% → 未达标, 且原因里点出年化
    line2 = [l for l in md.splitlines() if "GS1285" in l][0]
    assert "未达标" in line2 and "年化" in line2


def test_no_valid_combo_row_is_explicit():
    md = build_report(_results(), CTX)
    line = [l for l in md.splitlines() if "GS1299" in l][0]
    assert "无信号" in line or "无有效组合" in line


def test_report_is_pure_markdown_table():
    md = build_report(_results(), CTX)
    assert "<div" not in md and "<span" not in md
    assert md.count("|---") >= 1
    assert md.startswith("# ")


def test_actual_window_unknown_is_not_faked():
    ctx = dict(CTX, actual=None)
    md = build_report(_results(), ctx)
    assert "窗口未知" in md or "未记录" in md
