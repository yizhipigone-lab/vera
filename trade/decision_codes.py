"""trade/decision_codes.py — 决策台账的「原因码」唯一真相源 (2026-09-18)。

为什么要单独一个模块
--------------------
「今天为什么动 / 为什么没动」这件事, 系统里原本是散着的: ETF 轮动把原因写进
审计表的 ``rotation_summary``, 尾盘选股写进 ``auto_buy_summary``, 止盈止损那条腿
**没触发时一条都不写**。要把它们收成一天一行的台账, 先得有一份**统一的原因清单**
—— 否则每个写入点自己发明一套词, 前端要认的词就有四五份, 迟早对不上。

本模块只做三件事, **全是纯函数** (不碰数据库、不碰网络、不读时钟、不写文件):

1. ``CODES`` —— 24 个原因码 → 卡片上的白话短标签 + 色调 + 动作;
2. ``classify_rotation()`` —— 从 ETF 轮动审计的结构化明细复原出原因码;
3. ``classify_auto_buy()`` —— 从尾盘选股审计复原出原因码。

第 2、3 条是**实时写入与历史回填共用**的同一份判断, 这是刻意的: 如果实时走一套、
回填走另一套, 两边口径必然越走越远 (项目里「同一规则手写两份」的坑踩过不止一次,
见 ``trade/rotation.py`` 里 ``_rotation_codes`` 的注释)。

动作只有 5 种 (2026-09-18 设计收敛, 原为 7 种)
---------------------------------------------
``BUY`` 买 / ``SELL`` 卖 / ``HOLD`` 没动 / ``FAIL`` 想动没动成 / ``INFO`` 只是告知。
细分交给 ``reason_code`` 与 ``tone``, 前端就少写两个分支。

色调 ``tone`` 是前端上色的唯一依据, 与中国股市习惯一致: **买=红、卖=绿**
(页面既有实现同口径, 见 ``web/js/trade.js`` 里买用 ``var(--up)``、卖用 ``var(--down)``)。
"""

from __future__ import annotations

# ── 动作码 (5 种) ──────────────────────────────────────────────

ACTION_BUY = "BUY"
ACTION_SELL = "SELL"
ACTION_HOLD = "HOLD"
ACTION_FAIL = "FAIL"
ACTION_INFO = "INFO"

# 动作强弱 (「一天一行」的合并规则用): 有成交的那条永远压得住没成交的那条。
# 大白话: 同一天同一份资金先「想卖没卖成」(FAIL)、后来真卖成了 (SELL),
# 主结论要显示「卖成了」; 但「曾经失败过」这件事会留在明细里可查。
ACTION_RANK = {
    ACTION_SELL: 4,
    ACTION_BUY: 3,
    ACTION_FAIL: 2,
    ACTION_INFO: 1,
    ACTION_HOLD: 0,
}

# ── 色调 (前端 CSS 变量映射) ───────────────────────────────────
# up=红(涨/买) / down=绿(跌/卖) / muted=灰(中性没动作) / warn=黄(该做没做成)
# / info=蓝(只是告知)

TONE_UP = "up"
TONE_DOWN = "down"
TONE_MUTED = "muted"
TONE_WARN = "warn"
TONE_INFO = "info"


def _c(label: str, tone: str, action: str) -> dict:
    """造一条原因码记录 (内部小工具, 少写点重复的键名)。"""
    return {"label": label, "tone": tone, "action": action}


# ── 24 个原因码 ────────────────────────────────────────────────
# 命名规则: 前缀 = 哪条策略 (ROT 轮动 / PICK 选股 / EXIT 止盈止损 / LADDER 预埋单
# / NO_RUN 系统没跑), 后缀 = 具体原因。
#
# 为什么没有「某某策略没运行」这类码 (2026-09-18 第二轮自审删掉了 4 个):
# 没有任何写入方会写它们 —— 进程都没开机, 谁来写? 那种情况由读取侧合成一行
# NO_RUN 回答 (见 trade/decision_api.py); 而「某条策略今天没留痕」只是分组为空,
# 由前端在该组内显示一行灰色占位文案, 不需要原因码。

CODES: dict[str, dict] = {
    # ── ETF 轮动 (7 个) ──
    "ROT_SWITCH": _c("换腿买入", TONE_UP, ACTION_BUY),
    "ROT_HOLD_KEEP": _c("维持持仓（不用换腿）", TONE_MUTED, ACTION_HOLD),
    "ROT_HEDGE_BOTH_NEG": _c("两条腿都跌 → 买黄金避险", TONE_UP, ACTION_BUY),
    "ROT_TRAILING_STOP": _c("回撤超线 → 卖风险腿切避险", TONE_DOWN, ACTION_SELL),
    "ROT_NOT_SIGNAL_DAY": _c("今天不轮到这份重算", TONE_MUTED, ACTION_HOLD),
    "ROT_DATA_MISSING": _c("行情不够，维持现状不乱动", TONE_WARN, ACTION_HOLD),
    "ROT_ERROR": _c("出错", TONE_WARN, ACTION_FAIL),
    # 注: 这三条只在「当场」的那条路径上出现, 回填翻不出来 (当时压根没记),
    # 留着是因为实时路径确实会写它们。
    "ROT_HOLD_FIRST_SIG": _c("首个信号未算，先守着", TONE_MUTED, ACTION_HOLD),
    "ROT_INIT": _c("第一次接入：把现有持仓分到各份", TONE_INFO, ACTION_INFO),
    "ROT_SKIP_SESSION": _c("非连续竞价时段，跳过", TONE_MUTED, ACTION_HOLD),
    "ROT_BUSY": _c("上一轮还在跑，本次忽略", TONE_WARN, ACTION_HOLD),

    # ── 尾盘选股买入 (5 个) ──
    "PICK_BUY": _c("买入了新股票", TONE_UP, ACTION_BUY),
    "PICK_NO_SIGNAL": _c("公式今天没选出票", TONE_MUTED, ACTION_HOLD),
    "PICK_ALL_FILTERED": _c("选出的票全被过滤掉了", TONE_MUTED, ACTION_HOLD),
    "PICK_REGIME_BLOCK": _c("大盘没站上年线，今天不买新股", TONE_WARN, ACTION_FAIL),
    "PICK_ERROR": _c("选股流程出错", TONE_WARN, ACTION_FAIL),

    # ── 止盈止损 (4 个) ──
    "EXIT_TRIGGERED": _c("触发了卖出（原因见正文）", TONE_DOWN, ACTION_SELL),
    "EXIT_ARM_FAIL": _c("想卖但没卖成", TONE_WARN, ACTION_FAIL),
    "EXIT_NO_TRIGGER": _c("没到任何一条卖出线", TONE_MUTED, ACTION_HOLD),
    "EXIT_NO_QUOTE": _c("拿不到这个票的行情，判不了", TONE_WARN, ACTION_FAIL),

    # ── 预埋单 (3 个) ──
    "LADDER_PLACED": _c("预埋单已挂出", TONE_INFO, ACTION_INFO),
    "LADDER_DISABLED": _c("阶梯止盈开关关着，今天没挂", TONE_MUTED, ACTION_HOLD),
    "LADDER_SKIP": _c("有票没挂成（原因见正文）", TONE_MUTED, ACTION_HOLD),

    # ── 系统 (1 个) ──
    # 只由读取侧合成 (整天一行都没有时), 永不落库为 live —— 见 §3.5E
    "NO_RUN": _c("当天没有运行记录", TONE_MUTED, ACTION_HOLD),
}


def get(code: str) -> dict:
    """取某原因码的 (短标签/色调/动作)。**未知码不报错** —— 返回一条灰字兜底,
    宁可页面上少一点描述, 也不能因为不认识一个码就让整张卡打不开。"""
    return CODES.get(code) or _c(code or "未知原因", TONE_MUTED, ACTION_HOLD)


def label_of(code: str) -> str:
    """取某原因码的白话短标签 (卡片上直接显示的那几个字)。"""
    return get(code)["label"]


def tone_of(code: str) -> str:
    """取某原因码的色调 (前端上色用; up=红 / down=绿 / muted=灰 / warn=黄 / info=蓝)。"""
    return get(code)["tone"]


def action_of(code: str) -> str:
    """取某原因码默认对应的动作 (写入方可以覆盖, 例如 exit 的 EXIT_TRIGGERED
    在实时路径上由成交回报写、动作固定 SELL)。"""
    return get(code)["action"]


# ── 分类: ETF 轮动 ─────────────────────────────────────────────


def classify_rotation(detail: dict, *, skip_kind: str = "") -> str:
    """从 ETF 轮动审计的结构化明细复原原因码 (纯函数, 实时与回填共用)。

    参数:
        detail   —— ``rotation_summary`` 的 detail_json, 形如
                    ``{"tranche":2, "anchor":"wed", "target":"513100.SH",
                       "pool":324554.94, "momentum":{...}, "stop_off":False,
                       "signal":{...}}``;
                    或 ``rotation_skip`` 的 detail (只有 tranche/anchor);
        skip_kind —— 只在跳过路径用: ``"busy"`` (上一轮还在跑) /
                    ``"first_sig"`` (首信号未算) / ``"skip_session"``
                    (非连续竞价时段) / ``"data_missing"`` (行情数据不足) /
                    ``""`` (等信号日)。

    判定顺序 (与 trade/rotation.py 里 _execute 的分支顺序一致, 改那边务必回看这里):
        1. 日频移动止损触发 → 卖风险腿切避险;
        2. 行情数据不足 → 维持现状 (fail-safe);
        3. 目标为空 (避险篮子): 两腿动量都 ≤0 → 买黄金; 否则维持;
        4. 有目标腿: 有动量数据 = 今天重算过 → 换腿; 没动量 = 执行既有目标 → 维持。

    **老格式兼容** (只为历史回填, 实时路径永远写新格式, 所以下面两条分支在实时
    路径上是死代码, 但它们让"翻历史"翻得准):
        1b. 2026-08-17 ~ 08-20 的"单篮子"格式 (只有 state/changed, 没有 target);
        4b. 2026-08-21 的格式 (动量嵌在 signal.momentum 里, 顶层没有)。
    """
    if skip_kind == "busy":
        return "ROT_BUSY"
    if skip_kind == "first_sig":
        return "ROT_HOLD_FIRST_SIG"
    if skip_kind == "skip_session":
        return "ROT_SKIP_SESSION"
    if skip_kind == "data_missing":
        return "ROT_DATA_MISSING"
    if skip_kind == "wait_signal":
        return "ROT_NOT_SIGNAL_DAY"

    if detail.get("stop_off"):
        return "ROT_TRAILING_STOP"

    signal = detail.get("signal") or {}

    # 老格式兼容① (2026-08-17 ~ 08-20 的"单篮子"时代): 明细里没有 target,
    # 只有 state (full_cyb / full_gold) 与 changed。那时"换档"就是换腿 ——
    # changed=True 表示篮子变了 (含首次判定, 那时 derived=None)。
    # 回填时不认这个格式, 那 4 个交易日会全被记成"维持持仓", 与原文
    # "换档 True" 直接矛盾 (2026-09-18 回填前核对实测数据时发现)。
    # 判断放在 stop_off / insufficient 之后: 那两条是更强的失败信号, 优先。
    if "changed" in detail and "target" not in detail:
        return "ROT_SWITCH" if detail.get("changed") else "ROT_HOLD_KEEP"

    if isinstance(signal, dict) and signal.get("insufficient"):
        return "ROT_DATA_MISSING"

    target = detail.get("target")
    momentum = detail.get("momentum")
    # 老格式兼容② (2026-08-21): 那一天有 target 也有动量, 但动量嵌在
    # signal.momentum 里、顶层**没有** momentum 这个键。不回落的话, 那天明明
    # 算出了新目标腿, 却会被记成"维持持仓"。
    # 判据是"键缺失"而**不是**"值为 None": 实时格式里 momentum=None 是有意义
    # 的 —— 它表示"今天没重算, 在执行既有目标"(08-24 起就是这种), 那时即使
    # signal 里带了动量也不能算换腿。
    if "momentum" not in detail and isinstance(signal, dict):
        momentum = signal.get("momentum")
    if target in (None, "", "skip"):
        # 目标为空 = 避险篮子 (黄金); 若两腿动量都 ≤0, 那是"两条腿都跌"主动避险
        values = _momentum_values(momentum)
        if values and all(v <= 0 for v in values):
            return "ROT_HEDGE_BOTH_NEG"
        return "ROT_HOLD_KEEP"
    if momentum:
        return "ROT_SWITCH"
    return "ROT_HOLD_KEEP"


# 跳过文案 → **原因码**。顺序敏感: 先判最具体的说法, 别让泛词先吃掉。
#
# 为什么需要它 (2026-09-18 回填前核对实测数据时补): ``rotation_skip`` 这条审计
# **不带 skip_kind 字段**, 只有一个 note 或一句 message。回填只能读那句话来还原,
# 而计划书 §3.6 的映射表当时只想到「首个信号未算」和「上一轮还在跑」两种 ——
# 实测 09-10 那天写的是「非连续竞价时段, 跳过调仓」, 属于第三种
# (ROT_SKIP_SESSION), 照原表会被误记成 ROT_NOT_SIGNAL_DAY, 页面上的解释就错了。
# 把这个判断放进原因码模块 (而不是回填工具里), 是为了让它与其他分类函数待在一起,
# 将来 rotation 改了跳过文案, 改的地方只有一个。
_SKIP_TEXT_RULES: tuple[tuple[str, str], ...] = (
    ("非连续竞价", "ROT_SKIP_SESSION"),
    ("跳过调仓", "ROT_SKIP_SESSION"),
    ("首个周频信号", "ROT_HOLD_FIRST_SIG"),
    ("首个信号", "ROT_HOLD_FIRST_SIG"),
    ("数据不足", "ROT_DATA_MISSING"),
    ("还在跑", "ROT_BUSY"),
    ("上一轮", "ROT_BUSY"),
    ("等信号日", "ROT_NOT_SIGNAL_DAY"),
)


def classify_rotation_skip(message: str) -> str:
    """从 ``rotation_skip`` 的 message 原文认出**原因码** (回填专用)。

    返回的是可以直接落库的原因码 (与 ``classify_auto_buy`` 同款), 不是中间量 ——
    2026-09-18 这里最初返回的是内部用的 skip_kind (``"skip_session"``), 调用方
    顺手把它当原因码写进了台账, 页面上就会冒出一个灰字兜底的 ``skip_session``。
    用例 ``test_rotation_skip_returns_reason_codes`` 现在盯着这一点。

    认不出来时保守给 ``ROT_NOT_SIGNAL_DAY`` (「今天不轮到这份重算」) —— 这是四个
    跳过原因里最中性、最不容易说错的一个。宁可含糊, 不能编一个"上一轮还在跑"出来。
    """
    text = str(message or "")
    for needle, code in _SKIP_TEXT_RULES:
        if needle in text:
            return code
    return "ROT_NOT_SIGNAL_DAY"


def _momentum_values(momentum) -> list[float]:
    """把动量字典里的数字取出来 (损坏/缺失一律当"没有", 不猜)。"""
    if not isinstance(momentum, dict):
        return []
    out = []
    for v in momentum.values():
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


# ── 分类: 尾盘选股买入 ─────────────────────────────────────────


def classify_auto_buy(kind: str, detail: dict) -> str:
    """从尾盘选股审计的 (kind, detail) 复原原因码 (纯函数, 实时与回填共用)。

    ``kind`` 是审计类型; ``detail`` 是它的 detail_json。三种情况:
      - ``auto_buy_skip_regime`` —— 弱市闸门拦下 (大盘没站上年线);
      - ``auto_buy_error``       —— 取数/选股流程报错;
      - ``auto_buy_summary``     —— 跑完了: 买到了 → 买入; 一只没选出 → 公式没信号;
        选出来了但一张单没下成 → 全被过滤。
    """
    if kind == "auto_buy_skip_regime":
        return "PICK_REGIME_BLOCK"
    if kind == "auto_buy_error":
        return "PICK_ERROR"
    if kind == "auto_buy_summary":
        if _int(detail.get("bought")) > 0:
            return "PICK_BUY"
        if _int(detail.get("selected")) == 0:
            return "PICK_NO_SIGNAL"
        return "PICK_ALL_FILTERED"
    return "PICK_NO_SIGNAL"


def _int(v) -> int:
    """宽松取整 (审计里的老数据可能是字符串), 取不到当 0。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "CODES", "ACTION_RANK",
    "ACTION_BUY", "ACTION_SELL", "ACTION_HOLD", "ACTION_FAIL", "ACTION_INFO",
    "TONE_UP", "TONE_DOWN", "TONE_MUTED", "TONE_WARN", "TONE_INFO",
    "get", "label_of", "tone_of", "action_of",
    "classify_rotation", "classify_rotation_skip", "classify_auto_buy",
]
