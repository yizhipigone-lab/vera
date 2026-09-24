"""trade/llm_review.py — 盘后 LLM 复盘 (2026-08-15)。

设计意图:
    把「取数」和「成文」拆开 (照 tools/daily_brief.py 的自包含模式):
    Python 侧把盘后日报 payload 格式化成文本数据包, LLM 只做一次调用
    按模板成文 —— 不让 LLM 现场搜数据/调工具 (避开 300s 超时事故)。

    松耦合铁律: LLM 挂/超时/无 key → build_daily_summary 返 None,
    调用方 (notifier) 兜底跳过, 日报照常出数字, 交易零感知。

公开接口 (≤8):
    - format_daily_data(payload) -> str          纯函数: payload → 文本数据包
    - build_daily_summary(payload) -> str | None 调 LLM 成文 (失败返 None)
"""

from __future__ import annotations

from llm.providers import get_client

from trade.book import DIRECTION_BUY as _DIRECTION_BUY  # 治理III W1-c: 唯一真相源 (book 只依赖标准库+logger, 无环可防)

# 成文模板 (system prompt): 人话铁律 (结论先行/大白话/报数字给参照/禁英文缩写)
# 2026-09-17 按用户明确要求加第 7 条 (「我是小白, 所有给我的数据、表述都要用大白话,
# 不要用黑话, 可以打比方, 要清晰、专业、但是浅显易懂」) —— 落地靠模板, 不靠自觉。
# 完整条款见 AGENTS.md 沟通风格第 7 条。
_PROMPT = """你是 VERA 量化实盘系统的盘后复盘助手, 读者是量化入门者(自述"我是小白")。
根据用户提供的「当日交易数据」, 用大白话写 3~5 句复盘。要求:

1. 结论先行: 第一句说清今天赚/亏多少(金额 + 比例);
2. 归因: 主要盈亏来自什么(涨的持仓 / 止盈落袋 / 止损割肉), 点名具体标的;
3. 报数字给参照: 不只说绝对值, 尽量说"比昨天多/少"或"占总资产多少";
4. 指出 1~2 个值得注意的点(轮动换仓信号、异常卖出、连续触发止盈止损);
5. 卖飞识别: 某笔卖出若「盘中最高涨幅」明显高于「卖在涨幅」(如盘中最高 +9% 你卖在 +2%), 要点名说"这票盘中冲高 X%、你卖在 Y%、卖飞了、少赚了"; 若卖在最高点附近就说止盈及时;
6. 大白话 + 生活化比喻, 禁止英文缩写和 emoji, 纯文本, 不用 Markdown 标题/列表符号;
7. **每个数字后面必须跟一句"这意味着什么"** —— 不许只丢一个数就完事。
   例: 不写"今日收益 -0.8%", 要写"今日收益 -0.8%, 相当于 100 万亏了 8000 块,
   幅度跟大盘差不多, 属于正常波动"; 不写"换手率 12%", 要写"换手率 12%,
   也就是这一天的调仓把仓位的八分之一动过了"。
   **该给的数字、口径、样本量一个都不能少, 只是顺序改成「先用白话说什么意思 → 再给数字」。**

直接输出复盘正文, 不要复述原始数据, 不要"根据数据""今天数据显示"之类的废话开头。"""


def format_daily_data(payload: dict) -> str:
    """盘后日报 payload → 给 LLM 看的文本数据包。缺字段 fail-soft (跳过该行)。"""
    lines: list[str] = []

    def _f(v) -> str:
        return f"{float(v):,.2f}"

    total = payload.get("total_asset")
    if total is not None:
        lines.append(f"总资产 {_f(total)}")
    day_pnl = payload.get("day_pnl")
    day_pnl_pct = payload.get("day_pnl_pct")
    if day_pnl is not None:
        sign = "+" if float(day_pnl) >= 0 else ""
        s = f"当日盈亏 {sign}{_f(day_pnl)}"
        if day_pnl_pct is not None:
            s += f" ({sign}{float(day_pnl_pct):.2f}%)"
        lines.append(s)
    pos_count = payload.get("position_count")
    if pos_count is not None:
        lines.append(f"持仓 {int(pos_count)} 只")
    floating = payload.get("floating_pnl")
    if floating is not None:
        lines.append(f"浮盈 {float(floating):+,.2f}")

    buy_count = payload.get("buy_count")
    sell_count = payload.get("sell_count")
    if buy_count is not None or sell_count is not None:
        lines.append(f"当日买 {int(buy_count or 0)} 笔 · 卖 {int(sell_count or 0)} 笔")
    realized = payload.get("realized_pnl")
    if realized is not None:
        win_rate = payload.get("win_rate")
        wr = f" 胜率 {float(win_rate) * 100:.0f}%" if win_rate is not None else ""
        lines.append(f"已实现盈亏 {float(realized):+,.2f}{wr}")

    # 仓位变动
    changes = payload.get("position_changes") or {}
    clines = []
    for key, label in (("new", "新进"), ("closed", "清仓"),
                       ("added", "加仓"), ("reduced", "减仓")):
        for it in (changes.get(key) or []):
            delta = int(it.get("delta", 0) or 0)
            clines.append(f"{label} {it.get('code')}({'+' if delta >= 0 else ''}{delta})")
    if clines:
        lines.append("仓位变动 " + " / ".join(clines))

    # 交易明细 (最多 10 笔, 买卖混排; 卖出带盈亏+原因)
    for t in (payload.get("trade_details") or [])[:10]:
        act = "买" if t.get("direction") == _DIRECTION_BUY else "卖"
        pnl = t.get("pnl_amount")
        pnl_s = f" 盈亏 {float(pnl):+,.2f}" if pnl is not None else ""
        hi = t.get("intraday_high_pct")
        hi_s = f" 盘中最高{float(hi):+.2f}%" if hi is not None else ""
        sp = t.get("sell_pct_vs_prev")
        sp_s = f" 卖在{float(sp):+.2f}%" if sp is not None else ""
        reason = (t.get("reason") or "").strip()
        reason_s = f" 原因({reason})" if reason else ""
        lines.append(f"{act} {t.get('code', '')}{pnl_s}{hi_s}{sp_s}{reason_s}")

    # 轮动信号 (2026-08-20 动量改造: 目标代码 + 各腿动量 + 移动止损基准;
    # 2026-09-16 三份错峰: rotation 可能是逐份列表, 逐份一句话)
    rot = payload.get("rotation")
    rots = rot if isinstance(rot, list) else ([rot] if rot else [])
    for idx, r in enumerate(rots):
        if not r or ("target" not in r and "momentum" not in r):
            continue
        target = r.get("target")
        prefix = ""
        if len(rots) > 1:
            anchor = r.get("anchor") or ""
            prefix = f"份{idx + 1}({anchor}) "
        s = f"{prefix}轮动信号: 目标 {target or '避险篮子(黄金)'}"
        mom = r.get("momentum")
        if mom:
            parts = []
            for c, m in mom.items():
                parts.append(f"{c} {float(m)*100:+.1f}%"
                             if m is not None else f"{c} 数据不足")
            s += " (" + " / ".join(parts) + ")"
        eh = r.get("entry_high")
        if eh:
            parts = [f"{c}@{float(v):.3f}" for c, v in eh.items()]
            s += f", 移动止损基准 {', '.join(parts)}"
        lines.append(s)

    return "\n".join(lines)


def build_daily_summary(payload: dict, timeout: int = 45) -> str | None:
    """调 LLM 生成人话复盘。失败/无 key/数据空 → 返 None (调用方兜底跳过)。

    2026-08-21 修复: temperature 0.0→0.7 + max_tokens 512→1024 ——
    deepseek-v4-flash 在 temperature=0.0 + 本复盘 prompt 下对日报数据
    "只思考不输出" (reasoning_content 一大段, content 恒空); temp0.7 下
    max_tokens=512 仍被思考过程吃光 content=0, 1024 起正常输出 (实测表)。
    """
    data = format_daily_data(payload)
    if not data.strip():
        return None
    try:
        return get_client().chat(
            [{"role": "system", "content": _PROMPT},
             {"role": "user", "content": data}],
            temperature=0.7, max_tokens=1024, timeout=timeout,
        )
    except Exception:
        return None
