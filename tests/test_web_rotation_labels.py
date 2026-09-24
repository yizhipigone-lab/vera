"""交易页设置面板 ETF 轮动区的界面文字与分组布局契约 (2026-08-20 用户拍板)。

术语约定:
- 两只进攻腿统称「主ETF ① / 主ETF ②」(创业板50 是 ①), 禁用「第二风险腿」;
- 避险篮子 = 「避险ETF①（黄金）」+「避险ETF2」, 两者同一行;
- 「动量窗口」是规则参数, 必须独占一行, 不与 ETF 代码混排。
"""

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _rotation_section() -> str:
    html = (PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    start = html.index("ETF 轮动系统 (双池资金分配)")
    end = html.index("弱市择时闸门")
    return html[start:end]


def _rows(section: str) -> list[str]:
    """拆出设置面板里的每一行 tds-row (行内不再嵌套 div, 可直接正则)。"""
    return re.findall(r'<div class="tds-row">(.*?)</div>', section, flags=re.S)


def test_no_risk_leg_wording_in_settings():
    section = _rotation_section()
    assert "第二风险腿" not in section
    assert "两只风险腿" not in section
    assert "避险腿" not in section


def test_main_etf_labels():
    section = _rotation_section()
    assert "主ETF ①" in section
    assert "主ETF ②" in section
    # 创业板50 是 ①: 主ETF ① 标签必须配 tdsRotCyb 输入框
    row_main = next(r for r in _rows(section) if "主ETF ①" in r)
    assert 'id="tdsRotCyb"' in row_main
    assert 'id="tdsRotRisk2"' in row_main
    # 主ETF 行不得混入避险 ETF
    assert "tdsRotGold" not in row_main
    assert "tdsRotHedge2" not in row_main


def test_hedge_etfs_share_one_row():
    section = _rotation_section()
    assert "避险ETF①（黄金）" in section
    row_hedge = next(r for r in _rows(section) if "避险ETF①（黄金）" in r)
    assert 'id="tdsRotGold"' in row_hedge
    assert 'id="tdsRotHedge2"' in row_hedge


def test_momentum_window_on_own_row():
    section = _rotation_section()
    row_mom = next(r for r in _rows(section) if "动量窗口" in r)
    assert 'id="tdsRotMomWin"' in row_mom
    # 动量窗口行不得混入任何 ETF 代码输入框
    for etf_input in ("tdsRotCyb", "tdsRotRisk2", "tdsRotGold", "tdsRotHedge2"):
        assert etf_input not in row_mom


def test_hedge_ratio_slider_right_below_hedge_etf_row():
    """黄金占比滑条必须紧跟在避险ETF 那一行的下一行 (2026-08-20 用户拍板)。"""
    section = _rotation_section()
    rows = _rows(section)
    i_hedge = next(i for i, r in enumerate(rows) if "避险ETF①（黄金）" in r)
    i_slider = next(i for i, r in enumerate(rows) if "避险篮子黄金占比" in r)
    assert 'id="tdsRotHedge"' in rows[i_slider]
    assert i_slider == i_hedge + 1


def test_regime_gate_annotated_stock_pool_only():
    """弱市择时闸门必须标注「仅对股票池生效; ETF 轮动自行管理」。"""
    html = (PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    start = html.index("弱市择时闸门")
    section = html[start:start + 800]
    assert "仅对股票池生效" in section
    assert "ETF 轮动自行管理" in section


def test_rotation_timing_text_matches_t_day_execution():
    """轮动时机说明必须与代码口径一致 (2026-08-20 动量改造后):
    信号日 = 可配 + 节假日前移; 执行 = 信号日当日尾盘 (T日), 非次日。
    旧文字「次日尾盘执行/次日执行」是改造前的老黄历, 禁止复活。"""
    section = _rotation_section()
    assert "次日尾盘执行" not in section
    assert "次日执行" not in section
    assert "每周最后一个交易日算信号" not in section
    # 新口径关键字: 当日执行 + 信号日可配 + 节假日前移
    assert "当日" in section
    assert "前移" in section
