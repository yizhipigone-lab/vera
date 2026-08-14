"""brain/fastpath.py — 高频意图快路径：跳过 agent 多轮循环，单次 LLM 成文。

动机（2026-08-13 提速二期）：研究 TAB 个股诊断类问题原来走 claude agent loop，
LLM 分 4~6 轮调工具（每轮 = 1 次 LLM 往返 + 1 次子进程冷启动），端到端几十秒。
学 tools/daily_brief.py 的日报模式：Python 侧一次性把数取全（stock_diagnosis
一站式 + search_web 软舆情，并发），LLM 只调一次按数据包末尾的模板成文。

设计（深模块）：调用方只问一句 try_stock_diagnosis()——
返 None = 意图不匹配 / 取数异常，调用方回落原 agent loop（松耦合）；
返 dict = 与 ask_brain 相同结构的结果（流式 on_line / 归档 / 反证+引用校验
全部复用 claude_cli 原路径，不重造轮子）。
意图识别刻意保守：宁可漏判走老路（只是慢），不可误判走错路（答错题）。

2026-08-13 提速三轮（本会话）：
- ① 名字反查：resolve_stock 除 6 位代码外，还按公司名反查代码
  （"宁德时代怎么样"不再漏判走老路），靠 data_tools._kg_all_companies。
- ② 瘦身 prompt：成文时用 prompts.SLIM_SYSTEM_PROMPT（~40 行），不再背
  完整 100 行 SYSTEM_PROMPT（快路径已明令不再调工具，用不上模式A/B/固定打法）。
- ④ 大盘/盘面快路径：try_market_brief 把"今天大盘怎么样"这类问法也收进
  快车道（market_snapshot 单次取数 + 单次成文），research_api 个股→大盘依次试。
"""
from __future__ import annotations

import asyncio
import re

from utils.logger import get_logger

logger = get_logger(__name__)

_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
# YYYYMM 排除：202608 这类是月份不是股票代码（实测误伤场景：「202608 行情怎么样」）
_YM_RE = re.compile(r"^(19|20)\d{2}(0[1-9]|1[0-2])$")
_DIAG_KEYWORDS = ("怎么样", "如何", "诊断", "分析", "看看", "值多少",
                  "技术面", "能买", "可以买", "值得", "估值", "走势",
                  "还能涨", "要不要卖", "基本面", "能不能买")

# 大盘/盘面意图词（保守，宁可漏判走老路）。刻意不含"指数"（中证500这类具体
# 指数 market_snapshot 没有，答了反错），也不含"今天行情/今日行情"（那是
# "XXX今天行情怎么样"的个股问法，容易误判成大盘）。
_MARKET_KEYWORDS = ("大盘", "盘面", "市场情绪", "涨停家数", "跌停家数",
                    "南向资金", "市场涨跌")


def match_stock_diagnosis(question: str) -> str | None:
    """问题是否「个股诊断」意图。是 → 6 位代码；否 → None（走原 agent loop）。"""
    if not any(k in (question or "") for k in _DIAG_KEYWORDS):
        return None
    m = _CODE_RE.search(question)
    if not m or _YM_RE.match(m.group(1)):
        return None
    return m.group(1)


def resolve_stock(question: str) -> str | None:
    """问题 → 个股代码：先认 6 位代码，再按公司名反查（名字问法如"宁德时代怎么样"）。

    名字反查刻意保守（宁可漏判走老路、不可误判答错题）：
    - 只认诊断词（_DIAG_KEYWORDS）+ 最长公司名子串，名字 ≥3 字才参与匹配
      （过滤 2 字简称误伤，如"柳工"这类漏判也就走老路，只是慢、不错）。
    - 任何一步失败返 None（调用方回落原 agent loop）。
    """
    code = match_stock_diagnosis(question)  # 代码路径（已含诊断词门）
    if code:
        return code
    if not any(k in (question or "") for k in _DIAG_KEYWORDS):
        return None
    from brain import data_tools
    best: tuple[str, str] | None = None
    for c, name in data_tools._kg_all_companies():
        if name and len(name) >= 3 and name in question:
            if best is None or len(name) > len(best[1]):
                best = (c, name)
    return best[0] if best else None


def match_market_brief(question: str) -> bool:
    """问题是否「大盘/盘面」意图。保守：有盘面词 + 无 6 位代码。

    无代码这一条保证个股问法（含名字问法）永远先被个股快路径接走，
    不会把"宁德时代今天行情怎么样"误判成大盘。
    """
    if _CODE_RE.search(question or ""):
        return False
    return any(k in (question or "") for k in _MARKET_KEYWORDS)


def _fmt_hits(hits: list[dict]) -> str:
    """联网搜索结果 → 软舆情摘要文本（无结果时按纪律明说待核实）。"""
    if not hits:
        return "（联网搜索无结果/不可用——软舆情缺失，成文时如实标注「待核实」）"
    return "\n".join(
        f"- {h.get('title', '')}：{h.get('snippet', '')[:150]}"
        f"（{h.get('url', '')}）"
        for h in hits[:5])


async def try_stock_diagnosis(question: str, channel: str = "default",
                              on_line=None, timeout: int = 300) -> dict | None:
    """个股诊断快路径。返 None = 不适用（调用方回落 agent loop）。

    数据获取与成文分离：stock_diagnosis（结构化三包+模板）与 search_web
    （软舆情，固定打法里"不可省"的那条腿）并发跑，都进 prompt；
    LLM 单次调用成文（max_turns=4 只是防呆上限，prompt 已明令不再调工具）。
    """
    code = resolve_stock(question)
    if not code:
        return None
    try:
        from brain import data_tools
        from brain.search_web import search_web
        name = data_tools._kg_company_name(code)  # 同包私有复用：kg 官方名防记错
        pack, hits = await asyncio.gather(
            asyncio.to_thread(data_tools.stock_diagnosis, code),
            asyncio.to_thread(search_web, f"{name or code} 最新消息",
                              5, "auto", 14))
    except Exception as e:  # 松耦合：取数挂了不挡路，回落 agent loop
        logger.warning(f"快路径取数异常，回落 agent loop: {e}", exc_info=True)
        return None
    prompt = (
        f"以下是 {code}（{name or '名称未知'}）的个股诊断数据包，"
        "Python 已取好（含成文模板），外加联网搜索的软舆情摘要。\n"
        "请直接按数据包末尾的模板成文：不要再调用任何工具搜数据；"
        "术语首次出现配大白话；标【缺】的部分如实写数据缺失；"
        "末尾必须输出 <counter_evidence> 段。\n\n"
        "# 软舆情（联网搜索摘要，可信度和日期需自行判断）\n\n"
        + _fmt_hits(hits)
        + "\n\n---\n\n" + pack)
    from brain import prompts
    from brain.claude_cli import ask_brain
    result = await ask_brain(prompt, timeout=timeout, max_turns=4,
                             channel=channel, on_line=on_line,
                             system=prompts.SLIM_SYSTEM_PROMPT)
    result.setdefault("warnings", []).append(
        "已走个股诊断快路径（跳过 agent 多轮循环）")
    return result


async def try_market_brief(question: str, channel: str = "default",
                           on_line=None, timeout: int = 300) -> dict | None:
    """大盘/盘面快路径。返 None = 不适用（调用方回落 agent loop）。

    与个股诊断快路径同构：market_snapshot（2h 缓存，命中 ~0.06s）单次取数，
    末尾附成文模板（templates/market_brief.md），LLM 单次成文，
    跳过 agent「决定调工具→读输出→成文」的多轮往返。
    """
    if not match_market_brief(question):
        return None
    try:
        from brain import data_tools
        snap = await asyncio.to_thread(data_tools.market_snapshot)
    except Exception as e:  # 松耦合：取数挂了不挡路，回落 agent loop
        logger.warning(f"大盘快路径取数异常，回落 agent loop: {e}", exc_info=True)
        return None
    tpl = ""
    try:  # 附成文模板（读不到不影响快照）
        from utils.sysutil import project_root
        tpl = (project_root() / "brain" / "templates" / "market_brief.md"
               ).read_text(encoding="utf-8").strip()
    except Exception:
        pass
    prompt = (
        "以下是今天的盘面快照（A股/港股/美股指数 + 南向资金 + 涨停池，"
        "数据已由 Python 取好）。\n"
        "请直接按末尾的成文模板成文，不要再调用任何工具；术语首次出现配大白话；"
        "数字以快照为准，别编；末尾必须输出 <counter_evidence> 段。\n\n" + snap)
    if tpl:
        prompt += (f"\n\n---\n\n## 成文模板（按此结构成文，无需再 Read 模板文件）"
                   f"\n\n{tpl}")
    from brain import prompts
    from brain.claude_cli import ask_brain
    result = await ask_brain(prompt, timeout=timeout, max_turns=4,
                             channel=channel, on_line=on_line,
                             system=prompts.SLIM_SYSTEM_PROMPT)
    result.setdefault("warnings", []).append(
        "已走大盘/盘面快路径（跳过 agent 多轮循环）")
    return result
