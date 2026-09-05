"""交易驾驶舱页静态提示文案契约 (2026-09-04 用户拍板: 大白话长句 + 时点与代码一致)。

背景: 轮动卡提示曾写死「每日 09:30 自动算信号并调仓」, 而 2026-08-20 动量改造
(commit 8f731e9) 已把执行时点改为尾盘 execute_time (默认 14:54), 文案与代码脱节。
本次用户拍板: 交易页静态提示全部改成通俗直白长句, 数值写死但必须与代码默认值一致。

契约:
- index.html 不得再出现旧时点 09:30 (轮动实际时点 = execute_time 默认 14:54);
- 轮动卡提示 (tdRotHint) 必须含 trade/config.py 的 rotation.execute_time 默认值,
  且写明: 盘中点=真实下单 / 收盘后点=仅演算不改当前目标 / 参数可在设置面板调;
- 尾盘自动选股卡提示 (tdAbHint) 必须含 auto_buy.time 默认值 (默认 14:52),
  且写明 T+1 与次日 9:15 纳入预埋与监控;
- 设置面板底部提示 (tdsHint) 必须写明热生效边界: 面板字段保存即生效,
  账号/路径类改 config/trade.yaml 并重启 trade_main;
- 设置卡折叠标题 (tdSettings summary) 必须枚举全部设置区块, 加区块必须同步改标题。

VERA_INDEX_HTML 环境变量可覆盖读取路径 (红绿演示: 对 git 历史版本跑红)。
"""

import os
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = Path(os.environ.get(
    "VERA_INDEX_HTML", PROJECT_ROOT / "web" / "index.html"))
CONFIG_PY = PROJECT_ROOT / "trade" / "config.py"


def _html() -> str:
    return HTML_PATH.read_text(encoding="utf-8")


def _hint(hint_id: str) -> str:
    """取指定 id 的 trade-hint/summary 元素内的纯文本 (元素内无嵌套标签)。"""
    m = re.search(rf'id="{hint_id}"[^>]*>([^<]+)<', _html())
    assert m, f"index.html 中未找到 id={hint_id} 的提示元素"
    return m.group(1)


# ── 旧时点清零 ──────────────────────────────────────────────

def test_no_stale_0930_in_index_html():
    """09:30 是 2026-08-20 改造前的旧时点, 界面任何位置不得再出现。"""
    assert "09:30" not in _html(), (
        "index.html 仍有旧时点 09:30; 轮动实际时点是 execute_time (默认 14:54)")


# ── 轮动卡提示 ──────────────────────────────────────────────

def test_rotation_hint_time_matches_config_default():
    """tdRotHint 写死的时点必须与 trade/config.py 的 execute_time 默认值一致。"""
    cfg = CONFIG_PY.read_text(encoding="utf-8")
    m = re.search(r'execute_time: str = "(\d{2}:\d{2})"', cfg)
    assert m, "trade/config.py 未找到 execute_time 默认值"
    assert m.group(1) in _hint("tdRotHint"), (
        f"tdRotHint 缺少与代码一致的执行时点 {m.group(1)}")


def test_rotation_hint_plain_language_contract():
    """轮动卡提示必须是大白话长句: 覆盖周频/日频/止损/盘中收盘两种点法。"""
    hint = _hint("tdRotHint")
    for key in ("每周信号日", "默认周五", "移动止损",
                "收盘后再点", "不会真实买卖", "可在设置面板调"):
        assert key in hint, f"tdRotHint 缺少关键表述: {key}"
    # 旧电报式短语禁止复活
    assert "盘中点=立即调仓" not in hint


# ── 尾盘自动选股卡提示 ──────────────────────────────────────

def test_auto_buy_hint_time_matches_config_default():
    """tdAbHint 写死的时点必须与 trade/config.py 的 auto_buy.time 默认值一致。"""
    cfg = CONFIG_PY.read_text(encoding="utf-8")
    times = re.findall(r'^\s*time: str = "(\d{2}:\d{2})"', cfg, flags=re.M)
    assert len(times) == 1, f"auto_buy.time 默认值应唯一, 实际: {times}"
    assert times[0] in _hint("tdAbHint"), (
        f"tdAbHint 缺少与代码一致的触发时点 {times[0]}")


def test_auto_buy_hint_contract():
    hint = _hint("tdAbHint")
    for key in ("T+1", "9:15", "后台"):
        assert key in hint, f"tdAbHint 缺少关键表述: {key}"
    assert "选股在工作线程跑" not in hint, "旧电报式提示禁止复活"


# ── 设置面板提示与标题 ──────────────────────────────────────

def test_settings_hint_hot_reload_boundary():
    """tdsHint 必须写清热生效边界: 面板即存即生效, 账号/路径改 yaml 重启。"""
    hint = _hint("tdsHint")
    assert "马上生效" in hint
    assert "config/trade.yaml" in hint
    assert "重启" in hint


def test_settings_summary_enumerates_all_sections():
    """折叠标题必须枚举全部设置区块; 新增区块时须同步改标题与这里的数字。"""
    html = _html()
    titles = re.findall(r'tds-sec-title">([^<]+)<', html)
    assert len(titles) == 7, (
        f"设置区块数量变为 {len(titles)} ({titles}); "
        "请同步修改 tdSettings summary 的枚举与本断言")
    summary = re.search(r'<summary class="tds-summary">([^<]+)</summary>', html)
    assert summary, "未找到 tdSettings 折叠标题"
    text = summary.group(1)
    for key in ("止盈止损", "风控", "监控", "尾盘", "轮动", "弱市", "飞书"):
        assert key in text, f"设置折叠标题漏了区块关键词: {key}"
