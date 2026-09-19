# -*- coding: utf-8 -*-
"""core/market_thermometer.py — 大盘体温表 Markdown + 飞书推送。

2026-09-19 架构修订批次 5.1 第五刀 (最后一刀): 自 core/market_position_runner.py 端出。
本模块是依赖链**最上层**: 它把位置表/宽度/涨跌停/成交额/估值/牛熊区间/照镜子/
影子回放揉成一篇给人看的 Markdown (每个数字后跟一句人话解释 —— 用户规则:
先说人话再给数字, 不许出现"生存者偏差"这类黑话, 有测试锁)。
`push_thermometer` 走 tools/send_report_feishu 推飞书卡片。
"""
from __future__ import annotations

import pandas as pd

from core import market_position_io as mpio
from core.market_position_io import (  # 共享底座原语
    INDEX_SPECS,
    _num,
    _pct,
    _rat,
    _yi,
    latest,
)
from core.market_mirror import mirror
from core.market_regime import _regime_all
from core.market_shadow_replay import shadow_replay
from core.market_validity import _dimension_validity
from utils.logger import get_logger

_logger = get_logger(__name__)


#: 体温表尾部铁律提示 + 已知偏差 (**2026-09-17 用大白话重写**: 用户是小白,
#: 原来的「生存者偏差」「ST 股 (±5%) 会漏计」他看不懂 —— 见计划书 §16.9 第 10 条
#: 「大白话·硬条款」。**黑话一律翻译掉**: 技术术语只留在代码注释与 CLAUDE.md 里。)
CALIBER_FOOTER = (
    "只读参考，不联入任何仓位调度（业务铁律 1：宏观与研判只出报告，"
    "最后一步永远由人来做）。指标怎么算，唯一真相源 = `core/market_position.py`；"
    "牛熊怎么判 = `core/index_regime.py`；涨跌停幅度 = `core/limit_ratio.py`。\n"
    "**这份表有三处「看不清」，读的时候要一起记住**：\n"
    "① **涨停家数是本地按 10%/20% 的涨跌幅自己算的**，而 ST 股（被交易所特别处理、"
    "每天最多只能涨跌 ±5% 的那些股票）用的不是这个幅度 → **它们会被漏掉，"
    "所以涨停家数偏低**（真实可能更多）。\n"
    "② **历史数据里只有「今天还活着的公司」** —— 已经退市的公司，我们手里没有它们当年的数据。"
    "而退市的往往是当年的差股票，**所以过去的「多少股票在涨」会被算得比当年实际好看**"
    "（方向是**高估**；说白了就是：只有活到今天的公司被统计进去了）。\n"
    "③ **「十年百分位」在早年其实凑不满十年** —— 本机指数数据最早只到 2013 年，"
    "而这项指标要求至少满 3 年才给数，所以 **2016~2019 年那几年的百分位是拿更短的窗口算的**，"
    "不能和今天的数字完全并排比较。")

def thermometer_md(rec: dict | None = None, mirror_data: dict | None = None,
                   shadow_data: dict | None = None,
                   validity_data: dict | None = None) -> str:
    """组「大盘体温表」Markdown (飞书推送与页面共用同一份文案)。

    三个 `*_data` 参数是**注入接缝**: 传进来就直接用 (页面/测试避免重复计算),
    传 None 就现算。生产路径全部传 None。
    """
    rec = rec or latest()
    if not rec:
        return "# 大盘体温表\n\n【缺】还没有连续录像, 先跑 " \
               "`python tools/market_position_collect.py --backfill`"
    st = ("（**数据滞后**: 最新有效交易日 " + rec["date"] + ", 应有 "
          + rec.get("expected_date", "?") + "）") if rec.get("stale") else ""
    out = [f"# 大盘体温表 {rec['date']} {st}".rstrip()]
    # 审计 F-09：数据滞后时正文里十几处「今天」会让人把日期认错。**由数据生成日词**，
    # 并在抬头紧跟一句说明 —— 只改这一处比改十几处文案可靠（改文案总会漏）。
    try:
        _d = pd.Timestamp(rec["date"])
        day_word = "今天" if not rec.get("stale") else f"{_d.month}月{_d.day}日"
    except Exception:
        day_word = "今天"
    if rec.get("stale"):
        out.append(f"⚠ **下面正文里凡说「{day_word}」的地方，指的都是 "
                   f"{rec['date']} 收盘 —— 不是今天。**"
                   "（本地日线缓存还没拿到更新的收盘数据，报告不拿旧数据冒充新数据。）")
    b = rec.get("breadth") or {}
    t = rec.get("turnover") or {}
    lim = rec.get("limit") or {}
    traded = b.get("traded") or 0
    w = b.get("above_ma20_pct")
    hi, lo, spread = b.get("new_high_60"), b.get("new_low_60"), b.get("hl_spread")
    apct = t.get("amount_pct_1y")
    out.append(
        f"**一句话（先说人话，再给数字）**\n"
        f"{day_word}全市场有 **{traded}** 只股票在交易。\n"
        f"- **站上 20 日均线的只有 {_rat(w)}**（20 日均线 = 最近一个月的平均买入成本，"
        f"跌破它意味着最近一个月买的人大多在亏）—— {_width_plain(w)}\n"
        f"- 一边创新高的有 **{hi}** 家、一边创新低的却有 **{lo}** 家"
        f"（差 {spread}）—— {_hl_plain(hi, lo)}\n"
        f"- 全市场今天成交 **{_yi(t.get('amount_yi'))}**（这是把当天所有股票的成交金额加起来），"
        f"处在**过去一年 {_rat(apct)} 分位** —— {_amount_plain(apct)}\n"
        f"- 涨停 **{lim.get('up')}** 家、跌停 **{lim.get('down')}** 家 —— {_zdt_plain(lim)}")

    rows = ["| 指数 | 收盘 | 十年百分位 | 距十年最高 | 年化波动20日 | 近一年 | 偏离年线 | 牛熊 |",
            "|---|---|---|---|---|---|---|---|"]
    for _key, _name, _code in INDEX_SPECS:
        item = (rec.get("indices") or {}).get(_key) or {}
        _p = item.get("pct_10y")
        rows.append("| {n}({c}) | {cl} | {p} {lbl} | {h} | {v} | {r} | {m} | {g} |".format(
            n=item.get("name", _key), c=item.get("code", _code),
            cl=_num(item.get("close"), 2), p=_rat(_p),
            lbl=_position_plain(_p) if _p is not None else "",
            h=_pct(item.get("from_high_pct")), v=_rat(item.get("vol_ann_20")),
            r=_pct(item.get("ret_1y_pct")), m=_pct(item.get("ma250_dev_pct")),
            g={"bull": "牛", "bear": "熊", "range": "震荡"}.get(
                item.get("regime"), "【缺】")))
    out.append(
        "## 位置（三大指数，各自独立，不合成总分）\n" + "\n".join(rows) + "\n\n"
        "**怎么读这张表**：\n"
        "- **十年百分位** = 「现在的点位比过去十年里百分之多少的交易日都高」。"
        "90.9% 就是「比过去十年 90.9% 的日子都高」→ **偏贵区**。"
        "但它只回答「贵不贵」，**不回答「接下来涨还是跌」**。\n"
        "- **距十年最高** = 离过去十年的最高点还差多少（负数 = 还没回到最高点）。\n"
        "- **年化波动20日** = 最近一个月价格晃得有多凶（越大越坐过山车）。\n"
        "- **偏离年线** = 现在比「年线」（过去 250 个交易日的平均价）高还是低。\n"
        "- **牛熊** = 按「年线斜率」这套口径判的（下面「牛熊区间」一节有第二套口径）。\n"
        "- **为什么不给总分**：三个指数各自独立看。我们**不合成一个总分**，"
        "因为同一天、同一批数据，**换一个判定口径就能得出相反结论**"
        "（下面「牛熊区间」一节有真实例子）。")

    # 牛熊区间与时长 (§14.5): 单点状态答不了"这轮走了多久", 而那是"位置"的一部分。
    # **两条口径并排** (§14.6): 同一天两封外部邮件结论相反, 根因就是口径不同。
    rg = _regime_all()
    rg_lines = [
        "**为什么给两条口径**：2026年9月13日，同一条外部研究流水线在**同一天**发的两封邮件"
        "结论相反（相隔 47 分钟、同一批数据）—— 一封按「20% 法则」说**现在是牛市**，"
        "另一封按「历史特征类比」说**牛市已结束、顶部已过**。根因就是判定口径不同。"
        "**挑一个口径就会得出「唯一答案」的假象，所以两条都给你。**\n"
        "两条口径分别是什么：**年线斜率** = 看价格在「年线」（过去 250 个交易日的平均价）"
        "上方还是下方、且年线自己在往上还是往下；**20% 法则** = 从最近的低点涨够 20% 就算牛、"
        "从最近的高点跌够 20% 就算熊，都没够就是震荡。",
        "",
        "| 指数 | 口径 | 现在 | 本轮起点 | 已走多久 | 本轮涨跌 | 历史上同状态的中位 | 本轮月数的百分位 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    rg_state = {"bull": "牛", "bear": "熊", "range": "震荡"}
    detail: list[str] = []
    flicked: list[str] = []
    for key, _name, _code in INDEX_SPECS:
        item = rg.get(key) or {}
        for ck, short in (("ma250", "年线斜率"), ("pct20", "20% 法则")):
            s = item.get(ck)
            if not s:
                rg_lines.append(f"| {(item or {}).get('name', key)}"
                                f"({(item or {}).get('code', _code)}) | {short} |"
                                " 【缺】 | | | | | |")
                continue
            rg_lines.append(
                f"| {item['name']}({item['code']}) | {short} | "
                f"**{rg_state.get(s['state'], s['state'])}** | {s['since']} | "
                f"{_num(s['months'], 1)} 个月 | {_pct(s['ret_pct'])} | "
                f"{_num(s['median_months'], 1)} 个月 / {_pct(s['median_ret_pct'])} | "
                f"{_rat(s['months_percentile'])} |")
        # 沪深300 的历史明细单独列 (它是影子回放与基准的参照指数)
        if key == "hs300":
            for ck, short in (("ma250", "年线斜率"), ("pct20", "20% 法则")):
                s = item.get(ck)
                if not s or not s.get("longest_rows"):
                    continue
                detail += ["", f"**沪深300 历史上「{rg_state.get(s['state'], s['state'])}」"
                            f"最长的几段（{short}口径，从长到短）**:", "",
                           "| 起 | 止 | 持续月数 | 区间涨跌 |", "|---|---|---|---|"]
                for r in s["longest_rows"]:
                    detail.append(f"| {r['start']} | {r['end']} | {_num(r['months'], 1)} | "
                                  f"{_pct(r['ret_pct'])} |")
        flick = [f"{item['name']}·{(item.get(ck) or {}).get('caliber', ck)}"
                 for ck in ("ma250", "pct20")
                 if (item.get(ck) or {}).get("flicker_note")]
        if flick:
            flicked.extend(flick)
    if flicked:
        detail.append("\n⚠ **有一个口径要打折看**：" + "、".join(flicked) +
                      " —— 该口径在日频上翻状态太勤（中位区间不到 1 个月），"
                      "所以它的「本轮已走多久 / 历史中位」参考价值有限；"
                      "这正是要把两条口径并排摆出来的理由（挑一个就会以为答案唯一）。")
    out.append("## 牛熊区间（这轮走了多久，两条口径并排看）\n"
               + "\n".join(rg_lines + detail))

    # 估值维度 (§14.4): 价格分位 ≠ 估值分位 —— 指数可以在价格高位而估值不高。
    v = rec.get("valuation")
    if v:
        out.append(
            "## 估值（贵不贵，跟位置是两回事）\n"
            f"**股债性价比 {_num(v.get('erp_pct'), 2)}%**"
            f"（= 沪深300 的盈利收益率 1/PE 减掉 10 年期国债收益率），"
            f"处在**过去十年 {_rat(v.get('erp_pct_10y'))} 分位**。\n\n"
            f"怎么读：这个数**越高越划算**（拿着股票的预期回报比拿着国债强多少）。"
            f"现在的 {_num(v.get('erp_pct'), 2)}% 意味着过去十年里只有 "
            f"{_rat(100 - (v.get('erp_pct_10y') or 0))} 的时间比现在更划算；"
            f"十年中位数是 {_num(v.get('erp_median_10y_pct'), 2)}%，"
            f"十年区间 {_num(v.get('erp_min_10y_pct'), 2)}% ~ "
            f"{_num(v.get('erp_max_10y_pct'), 2)}%。\n\n"
            f"口径：{v.get('caliber')}（数据日 {v.get('asof')}，"
            f"十年窗口 {v.get('n_obs')} 个交易日；"
            f"来源：{v.get('source_plain') or v.get('source')}）。"
            "**注意这是沪深300 口径，不是「全市场」口径**。\n\n"
            "**为什么单列这一节**：前面那张位置表全是**价格**算出来的——"
            "价格在高位不等于**贵**（如果公司盈利涨得比股价还快，价格高但估值不高）。"
            "股债性价比是目前唯一有外部独立证据支持的长周期维度"
            "（外部研究实测它与未来 12 个月收益的相关性 rho=+0.48、五等分价差 +26.7pp）。")
    else:
        out.append("## 估值（贵不贵，跟位置是两回事）\n\n"
                   "【缺】没有本地 ERP（股债性价比）缓存。补的办法："
                   "`python tools/market_position_collect.py`（不带参数就是日常采集，"
                   "会联网拉一次）,"
                   "或手工把 `data/market_position/erp.jsonl` 造出来。")

    md = mirror_data if isinstance(mirror_data, dict) else mirror()
    if not md.get("ok"):
        out.append("## 照镜子（历史上跟今天最像的那些日子，之后实际怎么走）\n\n"
                   f"【缺】{md.get('reason')}")
    else:
        s = md.get("summary") or {}
        mir = [
            "**先说清楚：这不是预测。** 下面是把今天的六个指标（三大指数各自的十年位置、"
            "多少股票站在 20 日均线上方、创新高与新低的差、波动大小、成交额高低）"
            "去跟过去每一天比「像不像」，挑出最像的一批，再看**它们之后实际怎么走**。"
            "历史像，不等于这次也会照着走。",
            f"- 能当参照的历史交易日有 **{md.get('eligible')} 天**"
            f"（已经把最近 {md.get('exclude_recent')} 个交易日排除掉 —— "
            "不许拿上个月的日子冒充「历史」，那等于用答案去对答案）。",
            f"- **最像的一档**（把历史上所有的日子按「像今天的程度」排序，"
            f"取最像的前 {_rat((md.get('band_quantile') or 0) * 100)}，共 {s.get('n')} 天）："
            f"这些日子之后 **20 个交易日**（约一个月），沪深300 涨跌的"
            f"**中位数是 {_pct(s.get('fwd_20_median'))}**（一半的日子比它好、一半比它差），"
            f"平均 {_pct(s.get('fwd_20_mean'))}，中间一半落在 "
            f"{_pct(s.get('fwd_20_q25'))} 到 {_pct(s.get('fwd_20_q75'))} 之间，"
            f"上涨的占 {_rat(s.get('fwd_20_up_ratio'))}。",
            f"- **但这个数字要狠狠打折**：这 {s.get('n')} 天挨得很近、涨跌高度重叠，"
            f"**真正独立的信息只有大约 {_num(s.get('n_eff_20'), 1)} 份**"
            "（说白了：看着有一百多个样本，其实只相当于几次互不相干的经历）。"
            f"再往后看 **60 个交易日**（约三个月），中位数 "
            f"{_pct(s.get('fwd_60_median'))}、上涨占比 {_rat(s.get('fwd_60_up_ratio'))}"
            f"（独立信息约 {_num(s.get('n_eff_60'), 1)} 份）。",
        ]
        yrs = md.get("years") or []
        if yrs:
            top2 = sorted(yrs, key=lambda y: -y["n"])[:2]
            tot = sum(y["n"] for y in yrs) or 1
            mir += ["", "**按年份拆开看**（看有没有哪一年在唱独角戏）:", ""]
            if len(top2) == 2:
                mir.append(
                    f"命中日最集中的两年是 **{top2[0]['year']} 年**（{top2[0]['n']} 天）和 "
                    f"**{top2[1]['year']} 年**（{top2[1]['n']} 天），"
                    f"两者合计占了 {_rat((top2[0]['n'] + top2[1]['n']) / tot * 100)} —— "
                    "也就是说，上面的「历史平均」里有多少是这两年的经验，要心里有数。")
            mir += ["",
                    "| 命中日所属年份 | 命中几天 | 之后20日(沪深300)中位 | 上涨占比 |",
                    "|---|---|---|---|"]
            for y in yrs:
                mir.append(f"| {y['year']} 年 | {y['n']} 天 | "
                           f"{_pct(y.get('median_pct'))} | {_rat(y.get('up_ratio_pct'))} |")
        mir += ["", "**最像的几天（明细）**:", "",
                "| 相似日 | 像的程度(距离，越小越像) | 之后20日(沪深300) | "
                "之后60日(沪深300) | 之后20日(上证) |",
                "|---|---|---|---|---|"]
        for m in md.get("matches") or []:
            mir.append(f"| {m['date']} | {m['distance']} | "
                       f"{_pct(m.get('fwd_20_hs300_pct'))} | "
                       f"{_pct(m.get('fwd_60_hs300_pct'))} | "
                       f"{_pct(m.get('fwd_20_sh_pct'))} |")
        mir += ["", md.get("warning", "")]
        out.append("## 照镜子（历史上跟今天最像的那些日子，之后实际怎么走）\n"
                   + "\n".join(mir))

    sd = shadow_data if isinstance(shadow_data, dict) else shadow_replay()
    sh_now = rec.get("shadow") or {}
    _rule_cn = {"ma20": "沪深300 收盘站上自己的 20 日均线就满仓",
                "breadth50": "全市场有一半以上股票站上 20 日均线就满仓",
                "regime": "项目牛熊口径判为「牛」就满仓",
                "buy_hold": "什么都不做，一直拿着（对照用）"}
    sd_lines = [f"**{day_word}这三条规则各自怎么说**: " + "；".join(
        f"{_rule_cn.get(k, k)} → "
        f"{'**在场内**' if v == 'on' else '**空仓**'}" for k, v in sh_now.items()),
        "注意：这只是**影子记录**，系统绝不会按它下单（业务铁律 1：宏观与研判只出报告，"
        "不接仓位）。"]
    if not sd.get("ok"):
        sd_lines.append(f"【缺】{sd.get('reason')}")
    else:
        sd_lines.append(
            f"**下面这段在算什么**: 假设从 {sd.get('start')} 到 {sd.get('end')}"
            f"（{_num(sd.get('years'), 1)} 年）一直照某一条规则做，事后算账会是多少。"
            "**毛** = 不算交易费用；**净** = 按项目默认费用扣掉。费用只算了佣金和印花税，"
            "**没算滑点**，所以真实的净结果只会更差。")
        sd_lines += ["",
                     "| 规则 | 毛年化 | 净年化 | 净口径95%区间 | 净最大回撤 | "
                     "净夏普 | 建仓次数 | 在场时间占比 |",
                     "|---|---|---|---|---|---|---|---|"]
        for r in (sd.get("rows") or []) + [sd.get("buy_hold") or {}]:
            if not r:
                continue
            net = r.get("net") or {}
            sd_lines.append(
                f"| {_rule_cn.get(r.get('rule'), r.get('rule'))} | "
                f"{_pct(r.get('annualized_pct'))} | {_pct(net.get('annualized_pct'))} | "
                f"{_pct(net.get('ci_low_pct'))} ~ {_pct(net.get('ci_high_pct'))} | "
                f"{_pct(net.get('max_drawdown_pct'))} | {_num(net.get('sharpe'), 2)} | "
                f"{r.get('round_trips')} 次 | {_rat(r.get('exposure_pct'))} |")
        sd_lines += ["", f"回放口径: {sd.get('caliber')}", "",
                     "**怎么读这张表**:\n"
                     "- **t 值** = 这个结果离「纯属碰运气」有多远，**越大越不像碰运气**"
                     "（一般要 2 以上才算像样；这里全是 1 上下，也就是**看不出真本事**）。\n"
                     "- **95% 区间** = 「真实水平大概落在这个范围里」。只要这个范围"
                     "**跨过 0**（左边负、右边正），就只能说「**看不出显著的优势或劣势**」"
                     "—— **既不写「这条规则无效」，也不写「跑输一直拿着」**。"
                     "只有整个范围都在 0 以下，才能说「明显比一直拿着差」。\n"
                     "- **有效独立样本** = 天数看着很多，但相邻日子的涨跌是重叠的，"
                     "**真正独立的信息没那么多**，这个数就是打了折之后的信息量。"
                     "⚠ **它是「按天算」的口径**（把每个持仓日当一个观测），所以数出来"
                     "接近总天数；**真正不重复的「下注次数」是「建仓次数」那一列**"
                     "（一个往返 = 建仓到清仓算一次下注）。两个数要一起看："
                     "按天的样本大、按次的下注少，后者才是保守的下界。"]
        # 逐条写人话结论 (§15.2 E4 措辞纪律: 不说"跑输", 说"无显著净边际")
        for r in (sd.get("rows") or []):
            net = r.get("net") or {}
            lo, hi = net.get("ci_low_pct"), net.get("ci_high_pct")
            seg = r.get("segments") or {}
            dtxt = (f"{_num(net.get('t'), 2)}（按天算的有效独立样本约 "
                    f"{_num(net.get('n_eff'), 0)} 天；但**真正独立的下注只有 "
                    f"{_num(seg.get('n'), 0)} 次**（= 建仓次数），"
                    f"**以少的那个为准**）" if net.get("t") is not None else "【缺】")
            if lo is None or hi is None:
                verdict = "数据不足，判不了"
            elif lo <= 0 <= hi:
                verdict = "看不出显著的优势或劣势（区间跨过 0）"
            elif hi < 0:
                verdict = "明显比一直拿着差（整个区间都在 0 以下）"
            else:
                verdict = "明显比一直拿着好（整个区间都在 0 以上）"
            sd_lines.append(
                f"- **{_rule_cn.get(r.get('rule'), r.get('rule'))}**："
                f"这 {_num(sd.get('years'), 1)} 年里一共建仓 {r.get('round_trips')} 次，"
                f"每次持仓中位 {_num(seg.get('median_days'), 0)} 个交易日"
                f"（最短 {seg.get('min_days')} 天、最长 {seg.get('max_days')} 天），"
                f"在场时间占 {_rat(r.get('exposure_pct'))}。"
                f"按**每一笔**算（扣费后）：平均 {_pct(seg.get('mean_return_pct'))}、"
                f"中位 {_pct(seg.get('median_return_pct'))}、"
                f"赚钱的只占 {_rat(seg.get('win_ratio_pct'))}"
                f"（多数小亏、少数大赚，是趋势类规则的典型长相）；"
                f"每笔口径 t 值 {_num(seg.get('t'), 2)}。"
                f"整段的净口径 t 值 {dtxt}；结论：**{verdict}**。"
                + (f"（{seg.get('note')}）" if seg.get("note") else ""))
        # 双窗口一致性 (复用《公式因子体检方法论》纪律 2)
        sd_lines += ["",
                     "**双窗口一致性**（纪律 2「双窗口一致才算数」：把这段历史对半切开，"
                     "两半各算一遍，**只有两半同向才算数**，不一致的结论一律标「待复核」）:",
                     "",
                     "| 规则 | 前半段净年化 | 后半段净年化 | 前半段持有段数 | "
                     "后半段持有段数 | 一致? |", "|---|---|---|---|---|---|"]
        for r in (sd.get("rows") or []) + [sd.get("buy_hold") or {}]:
            if not r:
                continue
            w = r.get("windows") or {}
            wi, wo = w.get("in") or {}, w.get("out") or {}
            if w.get("consistent"):
                verdict = "✅ 同向，算数"
            elif w.get("note"):
                verdict = "⚠ 样本不足，不算数"
            else:
                verdict = "⚠ 不一致，待复核"
            sd_lines.append(
                f"| {_rule_cn.get(r.get('rule'), r.get('rule'))} | "
                f"{_pct(wi.get('annualized_pct'))}（{wi.get('start')} 起） | "
                f"{_pct(wo.get('annualized_pct'))}（{wo.get('start')} 起） | "
                f"{_num(wi.get('round_trips'), 0)} 段 | "
                f"{_num(wo.get('round_trips'), 0)} 段 | {verdict} |")
        _wnote = next((w.get("note") for w in
                       [(r.get("windows") or {}) for r in
                        (sd.get("rows") or []) + [sd.get("buy_hold") or {}]] if w.get("note")),
                      "")
        if _wnote:
            sd_lines += ["",
                         f"（为什么有「样本不足」：{_wnote}。"
                         f"一个往返 = 建仓到清仓算一次「下注」，"
                         f"段数太少时「两半同号」可能只是碰巧 —— "
                         f"所以按纪律 2 的精神标「不算数」，而不是当它通过了。）"]
        c = sd.get("cost") or {}
        if c:
            sd_lines += [
                "",
                f"成本口径: 单次往返 {_num(c.get('round_trip_pct'), 2)}%"
                f"（佣金+印花税）；若再按项目默认滑点 0.1%/边 加 0.2%，"
                f"单次往返就是 {_num(c.get('round_trip_with_slippage_pct'), 2)}% —— "
                "**上表的净口径是乐观下限**。"]
    out.append("## 候选择时规则影子回放（只记录不交易）\n" + "\n".join(sd_lines))

    # 维度体检 (§14.7): 上面那些指标到底有没有用 —— 用数据说话, 不靠"看起来有道理"。
    vd = validity_data if isinstance(validity_data, dict) else _dimension_validity()
    vd_lines = [
        "**为什么要有这一节**：上面的指标**全是价格算出来的**（位置/宽度/量能/波动）。"
        "一条外部研究给了句很扎心的批评：「**该有效的没被重用，不该用的占了 85% 权重**」——"
        "这话对我们是**接近 100%**。所以必须用数据确认「到底哪一维真能预测收益」，"
        "不能靠「看起来有道理」。",
        "",
        "做法：**每个月取一个观测**（不是每天 —— 每天的数据前后重叠得太厉害，"
        "「有效独立样本」只剩个位数，按天算出来的显著性是**假的精度**），"
        "算指标与之后 1/3/6/12 个月沪深300 涨跌的**排名相关性（rho）**"
        "（把指标和收益各自排个名，看两个名次合不合拍；越远离 0 关系越强）"
        "**+ 五分位差**（把历史上所有日子按指标从低到高分成五组，"
        "算「最高那组」比「最低那组」之后多赚/少赚多少）**+ 有效独立样本数**。",
    ]
    if not vd.get("ok"):
        vd_lines.append(f"\n【缺】{vd.get('reason')}")
    else:
        vd_lines += [
            "",
            "| 指标 | 族 | 1 个月 | 3 个月 | 6 个月 | 12 个月 | 12 个月五分位差 | "
            "12 个月的有效独立样本 | 判定 |",
            "|---|---|---|---|---|---|---|---|---|"]
        by_field: dict = {}
        for r in vd["rows"]:
            by_field.setdefault(r["field"], {})[r["horizon_days"]] = r
        warn_cells: list[str] = []      # 收集所有 ⚠（审计 F-04：正文不许与表格打架）

        def _cell(r):
            if not r or r.get("rho") is None:
                return "—"
            ok = r.get("consistent")
            mark = "⚠" if not ok else ""
            if not ok:
                warn_cells.append(f"{r['name']}·{r['horizon']}")
            return f"{r['rho']:+.2f}{_stars(r.get('p'))}{mark}"

        for s in vd["summary"]:
            h = by_field.get(s["field"], {})
            q = (h.get(252) or {}).get("quintile_spread_pct")
            vd_lines.append(
                f"| {s['name']} | {s['family']} | {_cell(h.get(21))} | {_cell(h.get(63))} | "
                f"{_cell(h.get(126))} | {_cell(h.get(252))} | {_pct(q)} | "
                f"{_num((h.get(252) or {}).get('n_eff'), 1)} 份 | {s['label']} |")
        if warn_cells:
            vd_lines.append(
                f"\n**本次共 {len(warn_cells)} 处标了 ⚠**（两半方向打架 → 一律待复核）："
                + "、".join(warn_cells)
                + "。**这些格子里的数一个都不能当结论用。**（这段是从数据生成的，"
                  "不是手写的 —— 免得正文与表格打架。）")
        vd_lines += [
            "",
            "**怎么读**：rho 是「名次合不合拍」—— **+0.5 表示指标越高、之后涨得越多**，"
            "**−0.5 就是反过来**（指标越高、之后跌得越多）。"
            "星号是「这不太可能是碰巧」的可信程度：`***` = 很可信、`**` = 可信、"
            "`*` = 勉强、**没有星号 = 看不出来**。"
            "**⚠ = 两半打架**（把样本按时间对半切开，两半的方向不一致）→ 一律标「待复核」，"
            "**不算数**（项目《公式因子体检方法论》纪律 2「双窗口一致才算数」）。",
            # 审计 F-04：原来这里硬编码「本次唯一一处 ⚠ 是 ERP 的 1 个月」，而同一份
            # 报告的表里其实有 10 处 ⚠ —— **正文与自己的表格打架，会系统性高估可信度**。
            # 改成**由数据生成**（下面的 _warn_cells 在渲染表格时收集）。
            "",
            "**表头怎么读**：「有效独立样本」= 月数 ÷ 持有期月数"
            "（重叠窗口会让信息量远小于观测条数）；「五分位差」= 把历史日子按指标"
            "从低到高分成五组后，最高一组减最低一组之后多赚或少赚的百分点。",
            "",
            "**这次实测出来的几件事**（数字全部由本次数据现算，不写死 —— "
            "写死的统计结论会随数据漂移变成假话，审计 F-06/F-08/【F-04】同一根因）："]
        # 下面三条的**挑法**写死、**数字**全部现算。挑法：12 个月上显著且两半一致的
        # 指标里，rho 最大的是"最强正向"、最小的是"最强负向"；一个持有期都没通过的族
        # 就是"看不出相关性"的族。
        _sig = [s for s in vd["summary"]
                if s["p_12m"] is not None and s["p_12m"] < 0.05
                and s["any_significant_consistent"]]
        _pos = max((s for s in _sig if (s["rho_12m"] or 0) > 0),
                   key=lambda s: s["rho_12m"], default=None)
        _neg = min((s for s in _sig if (s["rho_12m"] or 0) < 0),
                   key=lambda s: s["rho_12m"], default=None)
        _weak = sorted(f for f, v in (vd.get("families") or {}).items() if not v["usable"])
        _weak_fields = [s["name"] for s in vd["summary"] if s["family"] in _weak]
        _facts: list[str] = []

        def _spread(s) -> str:
            q = s.get("spread")
            if q is None:
                return "五分位差算不出来（月频样本不足）"
            return (f"五分位差 **{_pct(q)}**（把历史日子按这个指标从低到高分成五组，"
                    f"最高那组之后比最低那组**{'多' if q > 0 else '少'}赚 "
                    f"{_num(abs(q), 1)} 个百分点**）")

        if _pos:
            _erp_note = ("—— **独立复现了外部研究的结果**（他们 +0.48 / +26.7 个百分点）。"
                         "前面「估值」那一节之所以必须单列，就是这条证据撑起来的。"
                         if _pos["field"] == "erp" else "")
            _facts.append(
                f"**{_pos['name']}是 {_pos['best_horizon']}上最强的正向维度**："
                f"rho **{_pos['rho_12m']:+.2f}**、{_spread(_pos)}"
                + _erp_note)
        if _neg:
            _facts.append(
                f"**{_neg['name']}是 {_neg['best_horizon']}上最强的负相关**"
                f"（方向和直觉相反）："
                f"rho **{_neg['rho_12m']:+.2f}**、{_spread(_neg)}。"
                f"所以位置指标**不是没用，而是方向跟直觉相反** —— 位置越高，之后一年越差"
                f"（样本内均值回归）。这条要与「现在指数在什么位置」放在一起读。")
        if _weak_fields:
            _facts.append(
                f"**{'、'.join(_weak_fields)}（{'、'.join(_weak)}）在四个持有期上全部看不出"
                f"相关性**，与外部研究「资金层/广度层被证伪」的结论一致 —— 所以它们"
                f"**只描述现状，不作预测依据**（注意：我们**不给它们权重、也不合成总分**）。")
        if not _facts:
            _facts.append("**本次没有任何指标在任何持有期上通过双窗口检验** —— "
                          "所有指标都只能描述现状、不作预测依据。")
        vd_lines += [f"{i}. {t}" for i, t in enumerate(_facts, 1)]
        vd_lines += [
            "",
            f"规模：月频样本 **{vd['n_months']} 个月**（{vd['start']} ~ {vd['end']}）、"
            f"共检验 **{vd['n_tests']} 个组合**、归到 **{vd['n_families']} 个族**"
            f"（其中 **{vd['n_families_usable']} 个族**至少在一个持有期上有可用证据："
            f"{'、'.join(vd.get('usable_families') or []) or '无'}）。",
            "",
            "**诚实限制（必须一起读）**："]
        vd_lines += [f"- {x}" for x in vd["limitations"]]
    out.append("## 指标体检（这些指标到底有没有用？）\n" + "\n".join(vd_lines))
    out.append("---\n" + CALIBER_FOOTER)
    return "\n\n".join(out)

def _width_plain(w) -> str:
    """站上 20 日均线占比 → 一句人话(含"意味着什么")。"""
    if w is None:
        return "（今天算不出宽度：日线数据不够）"
    below = 100 - float(w)
    if w >= 70:
        feel = "绝大多数股票都在往上走，市场是**普涨**的底子"
    elif w >= 50:
        feel = "过半股票还在往上走，市场**不算弱**"
    elif w >= 30:
        feel = "只有三到五成的股票还在往上走，市场**偏弱**"
    elif w >= 15:
        feel = "只剩不到三分之一的股票还在往上走，**个股层面已经明显失血**"
    else:
        feel = "还在往上走的股票不到六分之一，**绝大多数个股已经跌破**"
    return f"换句话说 **{_rat(below)} 的股票已经跌到 20 日均线下方**，{feel}。"

def _hl_plain(hi, lo) -> str:
    """创新高/新低家数 → 一句人话(强弱对比)。"""
    hi, lo = int(hi or 0), int(lo or 0)
    if hi == 0 and lo == 0:
        return "两边都没有，**看不出方向**。"
    if hi == 0:
        return f"创新高一家都没有、创新低有 {lo} 家，**一边倒地向下**。"
    ratio = lo / hi
    if ratio >= 3:
        feel = "**跌到新低的家数远多于创新高的**，破位下跌的股票明显更多"
    elif ratio >= 1.5:
        feel = "跌到新低的比创新高的多一些，**偏弱**"
    elif ratio >= 0.7:
        feel = "两边差不多，**没有明显方向**"
    else:
        feel = "创新高的明显多于创新低的，**走强的股票更多**"
    return f"跌到新低的家数是创新高的 **{ratio:.1f} 倍**，{feel}。"

def _amount_plain(apct) -> str:
    """成交额的一年百分位 → 一句人话(量能冷热)。"""
    if apct is None:
        return "（量能冷热算不出来：历史不足一年）"
    a = float(apct)
    if a >= 80:
        feel = "**成交非常活跃**，处在过去一年最热闹的那一档"
    elif a >= 60:
        feel = "成交偏活跃"
    elif a >= 40:
        feel = "成交中等，不温不火"
    elif a >= 20:
        feel = "成交偏冷"
    else:
        feel = (f"**成交冷到了地板上**，过去一年里 {_rat(100 - a)} 的交易日"
                "都比今天热闹")
    return (f"{feel}（量能是行情的燃料：燃料少的时候，"
            "涨也涨不远、跌也跌不深）。")

def _zdt_plain(lim) -> str:
    """涨停/跌停家数 → 一句人话(多空强弱)。"""
    up, dn = int((lim or {}).get("up") or 0), int((lim or {}).get("down") or 0)
    if up == 0 and dn == 0:
        return "两边都没有，**没有极端情绪**。"
    if dn == 0:
        return f"涨停 {up} 家、跌停一家都没有，**情绪偏多**。"
    ratio = up / dn
    if ratio >= 2:
        feel = "**涨停明显多于跌停，情绪偏多**"
    elif ratio <= 0.5:
        feel = "**跌停明显多于涨停，情绪偏空**"
    else:
        feel = "**涨的和跌的差不多，今天没有明显方向**"
    return f"涨停是跌停的 **{ratio:.1f} 倍**，{feel}。"

def _position_plain(pct) -> str:
    """十年百分位 → 一个短标签 (表格里跟在数字后面, 不用读者自己换算)。"""
    if pct is None:
        return ""
    p = float(pct)
    if p >= 80:
        return "（偏贵区）"
    if p >= 60:
        return "（偏高）"
    if p >= 40:
        return "（中间）"
    if p >= 20:
        return "（偏低）"
    return "（便宜区）"

def _stars(p) -> str:
    """p 值 → 星号 (p 为 None 时不写星号, 表示"没算出/不显著", 绝不冒充显著)。"""
    if p is None:
        return ""
    if p < 0.01:
        return "***"
    if p < 0.05:
        return "**"
    if p < 0.10:
        return "*"
    return ""

def push_thermometer(rec: dict | None = None, title: str | None = None,
                     mirror_data: dict | None = None,
                     shadow_data: dict | None = None) -> dict:
    """把体温表推飞书 (复用 tools/send_report_feishu.py, 不写第二份推送)。

    永远 fail-soft: webhook 未配置 / 推送失败都只返 {"ok": False, "reason": ...},
    绝不抛 —— 调度器 job 与页面按钮都不该因为推送失败而中断。
    """
    rec = rec or latest()
    if not rec:
        return {"ok": False, "reason": "还没有连续录像"}
    md = thermometer_md(rec, mirror_data=mirror_data, shadow_data=shadow_data)
    try:
        from tools.send_report_feishu import (chunk_sections, load_webhook,
                                              md_to_lark, send_card)
    except Exception as e:
        return {"ok": False, "reason": f"飞书推送器不可用: {e}"}
    try:
        webhook = load_webhook()
    except SystemExit:
        return {"ok": False, "reason": "未配置 FEISHU_WEBHOOK_URL (.env)"}
    except Exception as e:
        return {"ok": False, "reason": f"读取 webhook 失败: {e}"}
    title = title or f"大盘体温表 {rec.get('date', '')}"
    try:
        chunks = chunk_sections(md_to_lark(md))
        codes = []
        for i, c in enumerate(chunks, 1):
            r = send_card(webhook, title, c, i, len(chunks))
            codes.append(r.get("code", r.get("StatusCode")))
        return {"ok": True, "cards": len(chunks), "codes": codes}
    except Exception as e:
        return {"ok": False, "reason": f"推送失败: {e}"}

