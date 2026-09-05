"""brain 输出可视化改造护栏 (2026-09-02) — L1 排版 + L2 chart 图表块。

锁三件事:
1. **prompt 面**: SYSTEM_PROMPT / SLIM_SYSTEM_PROMPT 必须带 ```chart 图表块规则
   (白名单 + 不编数 + source 必填 + 张数上限)。少一条, LLM 要么不知道能画图,
   要么乱画/编数画。
2. **模板面**: 四个成文模板都提 chart 图表块 (模板随数据包进 prompt,
   快路径 try_stock_diagnosis/try_market_brief 靠它把画图规则带给 LLM)。
3. **前端面**: brain_viz.js / brain-md.css 存在, index.html 挂了引用,
   brain_chat.js 走 BrainViz 渲染路径 (松耦合: BrainViz 缺失时回退旧 marked 路径)。

前端纯函数 (chart 块提取/白名单校验/徽章) 的行为级测试在
tests/web/test_brain_viz.mjs (node 直跑), 此处只锁资源与接线。
"""
from __future__ import annotations

from pathlib import Path

from brain.prompts import SLIM_SYSTEM_PROMPT, SYSTEM_PROMPT

_ROOT = Path(__file__).resolve().parent.parent


def test_system_prompt_has_chart_block_rules():
    """完整 system prompt 必须教 LLM: 图表块怎么写 + 纪律四条。"""
    assert "```chart" in SYSTEM_PROMPT, "缺少 chart 围栏块示例"
    assert "禁止为了画图编数" in SYSTEM_PROMPT, "缺少不编数纪律"
    assert '"source"' in SYSTEM_PROMPT, "缺少 source 示例 (数据来源+日期必填)"
    assert "最多 3 张" in SYSTEM_PROMPT, "缺少张数上限"
    for kw in ("bar", "line", "pie", "series"):
        assert kw in SYSTEM_PROMPT, f"白名单类型/字段缺 {kw}"


def test_slim_prompt_has_chart_rules():
    """快路径瘦身 prompt 也要带图表块规则 (个股诊断/大盘简报走 SLIM 成文)。"""
    assert "```chart" in SLIM_SYSTEM_PROMPT or "chart 块" in SLIM_SYSTEM_PROMPT
    assert "不编数" in SLIM_SYSTEM_PROMPT, "快路径缺少不编数纪律"
    assert "source" in SLIM_SYSTEM_PROMPT, "快路径缺少 source 必填"


def test_templates_mention_chart():
    """四个成文模板都要把画图规则带给 LLM (模板文本会拼进 prompt)。"""
    tpl_dir = _ROOT / "brain" / "templates"
    for name in ("stock_diagnosis.md", "market_brief.md",
                 "daily_brief.md", "sector_diagnosis.md"):
        text = (tpl_dir / name).read_text(encoding="utf-8")
        assert "chart" in text, f"{name} 未提 chart 图表块"


def test_frontend_assets_exist_and_wired():
    """brain_viz.js / brain-md.css 存在, 且 index.html / brain_chat.js 已接线。"""
    viz = _ROOT / "web" / "js" / "brain_viz.js"
    css = _ROOT / "web" / "css" / "brain-md.css"
    assert viz.is_file(), "web/js/brain_viz.js 不存在"
    assert css.is_file(), "web/css/brain-md.css 不存在"

    idx = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert "brain_viz.js" in idx, "index.html 未挂 brain_viz.js"
    assert "brain-md.css" in idx, "index.html 未挂 brain-md.css"
    # 加载顺序: brain_viz.js 必须在 brain_chat.js 之前 (后者依赖 window.BrainViz)。
    # 匹配带 /web/js/ 前缀的 script 标签, 避免命中页面顶部卸载说明注释。
    assert idx.index("/web/js/brain_viz.js") < idx.index("/web/js/brain_chat.js"), \
        "brain_viz.js 须先于 brain_chat.js 加载"

    chat = (_ROOT / "web" / "js" / "brain_chat.js").read_text(encoding="utf-8")
    assert "BrainViz" in chat, "brain_chat.js 未走 BrainViz 渲染路径"
    assert "marked.parse" in chat, "松耦合回退路径 (纯 marked) 被删了"
