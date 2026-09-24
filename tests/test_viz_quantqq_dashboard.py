# -*- coding: utf-8 -*-
"""QUANTQQ 5m 真·2% 可视化仪表盘 验收测试。

对应接力包 research/2026-08-27_QUANTQQ_5m真2pct可视化仪表盘_接力包.md 的验收标准：
- 抽查点：总笔数 37,525；胜率 81.4%；2015/2020/2024 三根逐年柱。
- 指标卡与 metrics.json 一致；红线声明必须在报告中。
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "research"))

OUT_HTML = os.path.join(ROOT, "output", "quantqq_2014_5m_pct2", "analysis_dashboard.html")
PCT2_DIR = os.path.join(ROOT, "output", "quantqq_2014_5m_pct2")

# 2026-09-20 CI 修红: 本文件验收的是本地产物 (output/quantqq_2014_5m_pct2/,
# 由 research/viz_quantqq_5m_pct_dashboard.py 生成, output/ 被 gitignore),
# CI checkout 永远没有 → 产物不存在即整文件跳过 (有产物的机器照常验收)。
pytestmark = pytest.mark.skipif(
    not os.path.exists(OUT_HTML),
    reason="仪表盘产物不存在 (本地产物, 不入库) —— 先运行 research/viz_quantqq_5m_pct_dashboard.py")


# ---------------------------------------------------------------- 产物存在性
def test_dashboard_html_exists():
    """验收①：analysis_dashboard.html 必须已生成。"""
    assert os.path.exists(OUT_HTML), "analysis_dashboard.html 不存在，先运行 research/viz_quantqq_5m_pct_dashboard.py"


def test_dashboard_contains_10_chart_containers():
    """验收①：10 个图表容器齐全（c1..c10）。"""
    html = open(OUT_HTML, encoding="utf-8").read()
    for i in range(1, 11):
        assert f'id="c{i}"' in html, f"缺少图表容器 c{i}"


def test_dashboard_inlines_echarts_and_redline():
    """验收⑥+红线：内联 ECharts 且必须显式标注「未计入流动性约束」。"""
    html = open(OUT_HTML, encoding="utf-8").read()
    assert "echarts" in html.lower()
    assert "未计入流动性约束" in html, "红线声明（未计入流动性约束）缺失，不得交付"


# ---------------------------------------------------------------- 数据统计正确性
def test_trades_count_and_winrate():
    """验收④：总笔数 37,525；胜率 81.4%。"""
    from viz_quantqq_5m_pct_dashboard import load_trades, trades_stats

    trades = load_trades(os.path.join(PCT2_DIR, "trades.csv"))
    reason, _reason_pct, pcts, _holds, _stock_pnl, _ya, _yn = trades_stats(trades)
    assert len(trades) == 37525
    assert sum(reason.values()) == 37525
    win_rate = sum(1 for p in pcts if p > 0) / len(pcts)
    assert win_rate == pytest.approx(0.814, abs=0.002)


def test_yearly_returns_match_relay_pack():
    """验收③：逐年收益与接力包关键数字一致（抽查 2015 / 2020 / 2024）。"""
    from viz_quantqq_5m_pct_dashboard import DIRS, load_daily_eq, yearly_returns

    daily = load_daily_eq(os.path.join(DIRS["pct2"], "equity_curve.csv"))
    yr = yearly_returns(daily)
    assert yr["2015"] == pytest.approx(3.622, abs=0.01)
    assert yr["2020"] == pytest.approx(2.120, abs=0.01)
    assert yr["2024"] == pytest.approx(2.408, abs=0.01)


def test_metric_cards_match_metrics_json():
    """验收⑤：指标卡数字与 metrics.json 一致（年化 / 回撤 / Sharpe / 胜率 / 笔数）。"""
    m = json.load(open(os.path.join(PCT2_DIR, "metrics.json"), encoding="utf-8"))
    assert m["total_trades"] == 37525
    assert m["annualized_return"] == pytest.approx(2.1244, abs=0.001)
    assert m["max_drawdown"] == pytest.approx(-0.0594, abs=0.001)
    assert m["sharpe_ratio"] == pytest.approx(1.38, abs=0.005)
    assert m["win_rate"] == pytest.approx(0.814, abs=0.001)


def test_dashboard_contains_key_numbers():
    """验收④⑤：HTML 里应出现关键数字（37,525 笔 / 81.4% / 212.4%）。"""
    html = open(OUT_HTML, encoding="utf-8").read()
    assert "37,525" in html or "37525" in html
    assert "81.4%" in html
    assert "+212.4%" in html
