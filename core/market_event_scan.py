# -*- coding: utf-8 -*-
"""core/market_event_scan.py — 大盘事件·自动扫描器 (2026-09-19)。

定位：把"事件靠人工手敲"换成"Agent 自动联网抓取 + LLM 判断归类 + 落库"。
      本文件只提供**确定性辅助**（判定标准 RUBRIC、检索词、去重、落库封装），
      **真正的判断由运行期的 Agent（LLM）完成** —— 不在代码里嵌脆弱的 LLM 调用，
      而是把判定标准写死(RUBRIC)，让每次扫描的判断保持一致、可复查。
      **联网取数已全部迁到 `core/market_event_sources.py`（取数层）**——
      本层只做"判定辅助 + 落库"，不知道任何源的 HTTP 细节。

为什么不直接在打分引擎里嵌 LLM 调用：
  - LLM 对"事件等级"的判定若每次不同，会让总分不可复现；
  - 改成"Agent 定时跑扫描 → 按 RUBRIC 判断 → 落库 events.jsonl"后，
    事件一旦入库就确定性地参与打分，分数从那刻起可复现，且每条带 reasoning 可追溯。

用法（Agent 自动扫描流程）：
  1. 调用 fetch_candidates(days=7) 从权威实时源拉取近 7 天候选
     （财联社电报 / 同花顺 / 新浪财经 / 美联储RSS / 华尔街见闻快讯，
     每条带 fact_level: primary=一手可指原文 / secondary=转述须复核）；
  2. 读取本文件 RUBRIC，按标准判定"算不算事件 / 几档 / 利好利空 / 分数"；
     判定必须以候选的"原文+链接"为依据，严禁脱离原文凭印象下结论；
  3. 对每条核实过的事件，调用 record_event（source="agent_scan"）落库；
     logic 字段必须写入"原文摘录 + 链接"，保证可溯源、可人工复查；
  4. 去重/在窗校验由 record_event 内部完成。
  5. 【收尾必做(2026-09-19 用户拍板)】全部事件落库后，**顺手触发一次仪表盘
     刷新**，把新事件立刻"烤进"快照 —— 页面读的是快照不是台账，不刷新
     事件跟踪页会一直显示旧清单。两种等价做法：
       a. POST /api/market_position/dashboard/refresh（server.py 在跑时，
          等价于点页面「手动刷新」按钮）；
       b. server 没在跑时本地执行:
          python -c "from core import market_dashboard_runner as mdr; \
print(mdr.refresh_close(write=True).get('ok'))"
     周末/节假日刷新会自动回退到最近交易日落账，不会落"周末快照"，
     可放心调用。漏了这一步的兜底：调度器每交易日 16:30/08:30 及
     每周六 18:00 的定时刷新也会把事件带进快照。

数据源说明（2026-09-19 纠偏 + 扩充）：
  - 之前用 WebSearch 自由文本碎片当事实源，曾灌入一条假事件（"9月15日降准"，实际
    未发生）。WebSearch 摘要混有大量 AI 生成/低质站点内容，不可信。
  - 事实源 = akshare 结构化实时源（财联社电报等，带发布时间+原文）+
    **美联储公告 RSS（一手，category=Monetary Policy 是高优先确定性触发）**；
  - **华尔街见闻快讯 = 转述源（有发布时间、半事实源）**：海外宏观进中文世界最快的
    一站，可当线索直接看；但录"普通重大"及以上档前，必须复核到一手原文
    （溯源终点见 RUBRIC 末节）；
  - fed_rate 常驻跟踪器数据源升级为**美财政部收益率 CSV（一手）**主源 +
    akshare 兜底 + 双源交叉校验（>5bp 告警）；
  - 被墙/失效源（FRED/GDELT/BLS/金十 flash-api/RSSHub 公共实例）一律不接
    （2026-09-19 本机实测，见计划书 docs/plan/2026-09-19_事件跟踪数据源扩充_计划书.md）。
"""
from __future__ import annotations

import datetime as dt

from core import market_event_sources as mes
from core import market_events as me

#: 判定与分级标准 —— Agent 每次扫描必须一致执行，避免"这次算那次不算"。
RUBRIC = """
事件判定与分级标准 (Agent 自动扫描用, 须每次一致执行)

【什么算事件】(全部满足才纳入):
1. 影响 A 股整体市场风险偏好/流动性的宏观、政策、海外事件, 而非个股消息。
2. 是"新增信息/新发生", 不是已常态化的日常; 已充分预期且兑现的, 影响力打折。
3. 可在公开渠道核实 (央行/统计局公告、主流财经媒体), 无法核实的传闻不录。
4. 在有效时间窗内 (从事件发生日起算, 自然日线性衰减):
   史诗级 30 天 / 普通重大 10 天 / 短期情绪 3 天。

【什么不算事件】:
- 个股或单一行业小消息; 券商日常研报; 已连续多月不变的状态(如 LPR 连月不变未出新);
- 无法核实的社媒传言; 单纯盘面涨跌(那是结果不是事件)。

【分级与打分】(initial_score 正负号=方向: 正=利好, 负=利空; 绝对值≤该档上限):
- 史诗级 ±1.0 (30天): 改变规则的政策 —— 印花税调整、IPO 暂停/重启、平准基金明确大规模入场、
  万亿级财政刺激、924 级别救市。
- 普通重大 ±0.5 (10天): 货币政策(降准/降息/LPR 变动)、美联储加息降息≥25bp、
  关键数据大幅超预期或不及预期(社融/CPI/PPI/PMI/非农)、重大贸易/地缘事件。
- 短期情绪 ±0.2 (3天): 官员表态、符合预期的常规数据发布、小幅政策、单日外围波动、行业事件。

【方向判定】(按对 A 股整体风险偏好的影响):
- 降准/降息=利好; 加息=利空;
- 经济数据强(超预期)=偏利好, 但信贷收缩/内需弱=偏利空;
- 海外风险事件(地缘/制裁/科技战)=偏空; 海外宽松(美联储降息)=偏利好。

【幅度微调】(在档内调整, 不超上限):
- 已充分预期且"利好兑现反跌" → 下调 0.1~0.2;
- 超预期强/弱 → 取档内上限附近;
- 影响有对冲(利好但带副作用) → 取档内中值。

【去重】同一事件只录一次(按标题+发生日)。若 active 列表已有同标题, 不重复加。

【事实溯源铁律】(2026-09-19 加入, 防假事件): 每一条入库事件, 其"发生了什么"必须
  能直接对应到某个权威源的原文摘录 + 链接; logic 字段必须包含该摘录与链接。
  若某条信息只在 WebSearch 摘要里出现、在财联社电报/同花顺/新浪等权威源里找不到对应,
  一律不录 —— 宁可漏、不可错。严禁凭"我记得/好像有"录入。

【溯源终点清单】(2026-09-19 扩充, 候选 fact_level 口径):
- 候选带 fact_level 标记: primary=一手可指原文(财联社电报/同花顺/新浪/美联储RSS);
  secondary=转述(华尔街见闻快讯)。secondary 线索录"普通重大"及以上档前,
  必须复核到下列一手终点之一, 并在 logic 里放终点链接:
- 货币政策(降准/降息/LPR/MLF): 中国人民银行 pbc.gov.cn 公告原文;
- 经济数据(社融/CPI/PPI/PMI): 国家统计局 stats.gov.cn 或央行发布页;
- 资本市场政策: 证监会 csrc.gov.cn / 中国政府网 gov.cn 政策文件;
- 美联储(利率决议/FOMC 声明/纪要/经济预测): federalreserve.gov 原文页
  (美联储RSS 候选自带原文链接, 可直接用);
- 美债收益率读数: 美财政部 home.treasury.gov 日度收益率 CSV(一手)。
"""

#: Agent 每次扫描要跑的检索词 (覆盖五类事件源)。时间窗在检索时限定"近 7 天"。
NEWS_QUERIES = [
    "央行 降准 降息 LPR 2026 货币政策 A股 流动性",
    "A股 重大政策 财政刺激 平准基金 稳市机制 救市",
    "美联储 利率决议 降息 加息 2026 9月",
    "中国 社融 CPI PPI PMI 经济数据 公布 超预期 不及预期 2026年8月",
    "地缘 贸易 科技制裁 风险事件 A股 市场情绪 2026年9月",
]

#: 各档有效天数(与 market_events.EVENT_LEVELS 对齐, 供 Agent 判断"是否在窗内")。
WINDOW_DAYS = {k: v["expire_days"] for k, v in me.EVENT_LEVELS.items()}

#: 宏观/政策相关性关键词(第一道粗筛: 只保留可能影响 A 股整体的宏观/政策/海外事件,
#: 过滤掉纯个股、纯行业碎片, 降低 Agent 判断噪声)。命中任一即视为"可能相关"。
MACRO_KEYWORDS = [
    "降准", "降息", "加息", "LPR", "MLF", "逆回购", "央行", "货币", "流动性",
    "财政", "刺激", "平准", "稳市", "救市", "印花税", "IPO", "社融", "信贷",
    "CPI", "PPI", "PMI", "GDP", "非农", "美联储", "FOMC", "鲍威尔", "美元",
    "汇率", "地缘", "贸易", "制裁", "关税", "会谈", "磋商", "政治局", "国常会",
    "证监会", "汇金", "国家队", "互换便利", "再贷款",
    # 美联储 RSS 是英文一手源: 英文关键词只做"放行"(候选还要 Agent 判定), 不漏 FOMC
    "Federal Reserve", "FOMC", "federal funds rate", "Treasury",
]


def fetch_candidates(days: int = 7, today: dt.date | None = None) -> list[dict]:
    """从全部候选源拉取近 `days` 天候选, 宏观粗筛 + 按时间倒序。

    取数全部委托取数层 `market_event_sources.fetch_all_candidates`
    (财联社电报/同花顺/新浪/美联储RSS/华尔街见闻快讯, 逐源独立 fail-soft);
    本函数只做判定辅助侧的三件事: 宏观关键词粗筛(降 Agent 噪声)、排序、返回。
    候选字段含 fact_level(primary 一手 / secondary 转述)与 hint, 供 RUBRIC 溯源用。
    """
    cands = mes.fetch_all_candidates(days=days, today=today)
    cands = [c for c in cands
             if any(kw in (c["title"] + c["content"]) for kw in MACRO_KEYWORDS)]
    cands.sort(key=lambda c: c["date"], reverse=True)
    return cands


def already_active(title: str) -> bool:
    """标题是否已在生效事件列表中(去重用)。"""
    t = title.strip()
    return any(e["title"].strip() == t for e in me.list_active())


def in_window(event_date: str, level: str, today: dt.date | None = None) -> bool:
    """事件是否仍在有效时间窗内(相对今天)。过期的不录(录了也立即失效)。"""
    today = today or dt.date.today()
    d = me._parse_date(event_date)
    win = WINDOW_DAYS.get(level, 3)
    return (today - d).days < win


def record_event(level: str, title: str, score: float, event_date: str,
                 logic: str, today: dt.date | None = None) -> dict | None:
    """落库一条 Agent 判定事件。去重/在窗内校验; 返回事件 dict 或 None(被跳过)。"""
    if already_active(title):
        return None
    if not in_window(event_date, level, today):
        return None
    return me.add_event(level=level, title=title, initial_score=score,
                        start_date=event_date, logic=logic, source="agent_scan")


def total_adj_now(today: dt.date | None = None) -> tuple[float, int]:
    """非破坏性计算当前事件修正分合计与生效数(不写文件)。"""
    today = today or dt.date.today()
    active = me.list_active(today)
    total = sum(a["score_now"] for a in active)
    adj = max(-me.TOTAL_ADJ_LIMIT, min(me.TOTAL_ADJ_LIMIT, round(total, 4)))
    return adj, len(active)


#: 美联储利率预期跟踪器的 tracker key(与 market_events.upsert_tracker 对应)。
FED_RATE_TRACKER = "fed_rate"

#: 双源交叉校验容忍度: 主源(美财政部)与 akshare 的 2 年期读数差超过此值(%)即告警。
#: 0.05% = 5bp; 2026-09-19 实测两源同为 4.76(差 0), 正常应远小于此。
_CROSS_CHECK_TOLERANCE = 0.05


def fetch_fed_rate_proxy(today: dt.date | None = None) -> dict | None:
    """用「美债 2 年期收益率」作为美联储利率预期(加息概率)的可靠每日代理。

    返回 {cur, base, cur_date, base_date, delta_bp, source} 或 None(两源全失败);
    双源同取且偏差 >5bp 时附带 cross_check 告警串。

    数据源(2026-09-19 升级):
      - **主源 = 美财政部日度收益率 CSV(一手)** —— 发行方官方数据, 实测与 akshare
        分毫不差(2026-09-18 两源同为 4.76), 且少一层转载;
      - 兜底 = akshare bond_zh_us_rate(东财/中债转载);
      - 双源都通时交叉校验, 差 >5bp 在返回值里写 cross_check(不阻断, 只告警)。

    为什么用它(而非 CME 隐含概率原文):
      - akshare 无直接的 CME FedWatch 概率接口; 同花顺/财联社虽会转载「美联储观察」
        概率电报, 但该电报在新闻池里**轮转、时有时无**(实测同一函数先后两次抓取,
        一次含 FedWatch 行、一次不含) —— 做"每日跟踪"不可靠;
      - 美债 2 年收益率是**每日更新、有历史、稳定可抓**的序列,
        它正是市场对美联储路径(加/降息概率)定价的直接载体(CME 概率背后的信号)。
    基准 = 约 20 个交易日(≈1个月)前的 2 年收益率, 看"近 1 个月预期移动方向"。
    """
    rows = mes.fetch_treasury_2y_rows(today)
    ak_rows = mes.fetch_akshare_2y_rows()  # 兜底 + 交叉校验(同取)
    cross_check = None
    # 只在"同一天"比数: 两源发布节奏不同, 日期不齐时比数值会把"一方滞后一天"
    # 误报成数据冲突(实测 9/17→9/18 就差 9bp, 超 5bp 阈值)。日期不齐不比、不告警。
    if rows and ak_rows and rows[-1][0] == ak_rows[-1][0]:
        diff = abs(rows[-1][1] - ak_rows[-1][1])
        if diff > _CROSS_CHECK_TOLERANCE:
            cross_check = (f"双源同日({rows[-1][0]})偏差 {diff:.2f}%"
                           f"(>{_CROSS_CHECK_TOLERANCE}%): "
                           f"财政部 {rows[-1][1]}% vs akshare {ak_rows[-1][1]}%, 请人工复核")
    if rows and len(rows) >= 21:
        cur_d, cur = rows[-1]
        base_d, base = rows[-21]
        source = "美财政部 daily treasury rates(一手)"
    elif ak_rows and len(ak_rows) >= 21:
        cur_d, cur = ak_rows[-1]
        base_d, base = ak_rows[-21]
        source = "akshare bond_zh_us_rate(美债2年收益率)"
    else:
        return None
    delta_bp = round((cur - base) * 100, 1)
    out = {"cur": round(cur, 2), "base": round(base, 2),
           "cur_date": cur_d.isoformat(), "base_date": base_d.isoformat(),
           "delta_bp": delta_bp, "source": source}
    if cross_check:
        out["cross_check"] = cross_check
    return out


def update_fed_rate_event(today: dt.date | None = None) -> dict:
    """每日刷新美联储利率预期(常驻)跟踪事件。

    逻辑: 拉美债2年收益率(近1个月移动)→ 映射分数(收益率↑=加息/收紧预期升温=偏空A股;
    ↓=宽松预期=偏多)→ 调 upsert_tracker 维护常驻事件。仅当分数实质性变化(≥0.02)才改分,
    否则只更新最新读数展示。返回 {updated, changed, score, proxy, event}。
    """
    today = today or dt.date.today()
    proxy = fetch_fed_rate_proxy(today)
    if proxy is None:
        return {"updated": False, "reason": "no_data", "event": None,
                "score": None, "proxy": None, "changed": False}
    # 分数映射: 近1个月收益率变动(bp) × (-0.004), 封顶 ±0.2(短期情绪档)。
    # 加息预期升温(delta>0)→ 分数负(偏空); 降息预期升温(delta<0)→ 分数正(偏多)。
    score = round(max(-0.2, min(0.2, -proxy["delta_bp"] * 0.004)), 2)
    if proxy["delta_bp"] > 0.5:
        direction = "加息/收紧预期升温"
    elif proxy["delta_bp"] < -0.5:
        direction = "降息/宽松预期升温"
    else:
        direction = "预期基本平稳"
    title = (f"美联储利率预期(美债2年{proxy['cur']:.2f}%): "
             f"近1月{proxy['delta_bp']:+.0f}bp→{direction}")
    logic = (f"美债2年期收益率 {proxy['cur']}% (截至{proxy['cur_date']}, 源:{proxy['source']}), "
             f"较 {proxy['base_date']} 的 {proxy['base']}% {proxy['delta_bp']:+.0f}bp —— {direction}。"
             f"美债2年是市场给美联储路径定价的直接载体(即 CME 加息概率背后的信号)。"
             f"加息预期升温→海外流动性收紧→A股风险偏好偏空; "
             f"分数 = -(变动bp)×0.004, 封顶±0.2 = {score:+.2f}。")
    if proxy.get("cross_check"):
        logic += f" ⚠交叉校验: {proxy['cross_check']}"
    res = me.upsert_tracker(FED_RATE_TRACKER, "minor", title, score, logic,
                            extra=proxy, today=today)
    return {"updated": True, "changed": res["changed"], "score": score,
            "proxy": proxy, "event": res["event"]}
