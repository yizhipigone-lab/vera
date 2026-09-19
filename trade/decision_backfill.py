"""trade/decision_backfill.py — 决策台账历史回填编排 (2026-09-18)。

一次性动作, **不进运行时**: 由 ``tools/backfill_daily_decision.py`` 这个离线 CLI
调用, 把 2026 年 7 月 27 日以来的历史决策从 ``audit`` 表里翻出来, 补进
``daily_decision`` 台账。

为什么要翻
----------
台账是 2026 年 9 月 18 日才上线的, 之前的决策原因只散在审计表里。用户要的是
「方便地看到历史原因」—— 不翻的话, 点开 8 月的任何一天都是空白。

三条设计决定 (都与实施计划书 §3.6 有出入, 逐条说明理由)
------------------------------------------------------

1. **复用 ``DailyDecisionStore`` 作为唯一写入口, 而不是自己写 INSERT**
   —— 计划书写的是「只 import 纯函数 + 历法 + stdlib sqlite3, 刻意不 import
   ``trade.store``」。那句话的本意是"不要付 ``TradeStore`` 构造的代价"
   (它会打开原始回报 JSONL 并起一个写盘线程), 这个顾虑是对的、已满足: 本模块
   全程只构造 ``DailyDecisionStore``, 自建一个裸 sqlite3 连接交给它用。

   但"不 import ``trade.store``"字面执行会带来一个更糟的后果: **来源可信度保护
   与动作强弱合并这两条规则要在这里手写第二份**。项目里"同一规则手写两份"的坑
   踩过不止一次, 而这两条恰恰是台账正确性的根 —— 手写第二份就意味着
   「回填不许覆盖 live」这件事有两处实现, 迟早分叉。所以这里改成 import
   ``DAILY_DECISION_DDL``(单一真相源) + ``DailyDecisionStore``(唯一写入口),
   两边行为由构造保证一致。

2. **日期不需要自己判交易日**: 守卫在写入口上, 且用的是行自己的日期 ——
   回填到周末会被自动拦下。这里只做"提前跳过"省点 IO, 不作为正确性依据。

3. **整天空白的交易日写一行 system 级 ``NO_RUN``; 部分策略缺失的写该策略级的
   ``NO_RUN``**。计划书 §3.6 只给了整体空白那一种, 但实测 40 个交易日里
   「某一条策略当天没记录」是常态 (例如 8 月 24 日起止盈止损再没留过痕),
   不写的话那片区域在页面上永远是含糊的"没有留痕"。

4. **每行都带上审计表里"当时"的时间戳 (``event_ts``)**。计划书没提这件事 ——
   因为实时写入时"写库那一刻"就等于"事情发生那一刻", 两者天然一致, 不需要区分。
   但回填不一样: 7 月 30 日那天监控每 10 秒重试一次卖出, 一共 587 条审计;
   若时间戳取写库时刻, 这一天的明细会全部显示成"9 月 18 日 23:34" —— 用户展开
   明细看到一串和日期对不上的时间, 比不给时间更让人糊涂。审计表的 ``ts`` 就是
   当年的原始时刻, 顺手带过来即是最准的。实时写入的代码路径**完全不受影响**
   (不传 ``event_ts`` 就退回原来的行为)。

幂等
----
主键 ``(日期, 策略, 对象)`` 天然幂等, 且 ``log(..., overwrite_live=False)``
保证改不动 ``live`` 行 —— 中断了直接再跑一遍即可。
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import threading
from collections import defaultdict

from utils.trading_calendar import is_trading_day as _is_trading_day
from trade.decision_codes import (
    ACTION_BUY, ACTION_FAIL, ACTION_HOLD, ACTION_INFO, ACTION_SELL,
    action_of, classify_auto_buy, classify_rotation, classify_rotation_skip,
)

# 台账起点 = audit / trades 表最早的一天, 再往前没有任何数据
# ("全量回填"能回的就是这么多)。与 trade/decision_api.py 的 _LEDGER_START
# 必须一致, 否则会出现"接口说有 note、台账里却有行"的矛盾。
LEDGER_START = "2026-07-27"

# ETF 轮动最早有审计痕迹的一天 (2026-08-15, 那天是周六 —— 也就是说
# 轮动功能是 8 月中旬才上线的)。这之前的日子, 轮动不是"没跑", 而是"还没有它"。
ROTATION_FIRST_SEEN = "2026-08-15"

# ── 审计类型 → 台账映射 (计划书 §3.6 那张表的可执行形态) ─────────
#
# 来源可信度判据 (计划书没写死, 这里定一条可复述的规则):
#   backfill_exact —— 原因码是**由明细里的结构化字段定下来的** (target/stop_off/
#                     bought/… 或 kind 本身就唯一决定了一个码);
#   backfill_text  —— 只有一个 note 字段/一句 prose, 原因码是**读句子认出来的**。
# 前者数字齐全可复核, 后者只能算"原文照抄", 页面角标也据此不同。

_ROT_SUMMARY = "rotation_summary"
_ROT_SKIP = "rotation_skip"
_ROT_ERROR = "rotation_error"
_ROT_MIGRATE = "rotation_migrate"

_AB_SUMMARY = "auto_buy_summary"
_AB_SKIP_REGIME = "auto_buy_skip_regime"
_AB_ERROR = "auto_buy_error"

_EXIT_SELL_KINDS = ("exit_sell", "exit_sell_market")
_EXIT_FAIL_KINDS = ("exit_skip", "exit_arm_fail", "exit_risk_reject",
                    "exit_fail_closed", "exit_lock_fail")

_LADDER_PLACE = ("ladder_place",)
_LADDER_SKIP = ("ladder_skip", "ladder_skip_etf")

# 全部"决策类"审计种类 (其余一概不进台账: monitor_* / sync_* / risk_reject /
# gapfill_* / trade_backfill / manual_* 都是噪声或非决策)
ROT_KINDS = (_ROT_SUMMARY, _ROT_SKIP, _ROT_ERROR, _ROT_MIGRATE)
AB_KINDS = (_AB_SUMMARY, _AB_SKIP_REGIME, _AB_ERROR)
EXIT_KINDS = _EXIT_SELL_KINDS + _EXIT_FAIL_KINDS
LADDER_KINDS = _LADDER_PLACE + _LADDER_SKIP + ("ladder_skip_disabled",)
DECISION_KINDS = ROT_KINDS + AB_KINDS + EXIT_KINDS + LADDER_KINDS


def _day_of(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _trading_days(start: str, end: str) -> list[str]:
    """``[start, end]`` 之间的交易日 (含两端)。"""
    d0 = _dt.date.fromisoformat(start)
    d1 = _dt.date.fromisoformat(end)
    out = []
    cur = d0
    while cur <= d1:
        if _is_trading_day(cur):
            out.append(cur.isoformat())
        cur += _dt.timedelta(days=1)
    return out


def _load_audit(conn: sqlite3.Connection, start: str, end: str) -> dict[str, list]:
    """读一段区间的审计行 → ``{日期: [(ts, kind, message, detail), ...]}`` (按 ts 升序)。

    只读需要的列与种类, 不整表拉。明细坏了给空字典 (老数据不保证是合法 JSON)。

    **``ts`` 要带着走** (2026-09-18 补): 不改的话, 一行台账的"发生时刻"只能取
    写库那一刻的时间戳 —— 回填 7 月 30 日的数据, 明细里 14 次卖出重试全被标成
    "9 月 18 日 23:34", 用户展开明细会看到一串和日期对不上的时间, 反而更糊涂。
    审计表里本来就存着当年那一刻的真实时间, 顺手带过来就是最准的。
    """
    lo = _dt.datetime.strptime(start, "%Y-%m-%d").timestamp()
    hi = _dt.datetime.strptime(end, "%Y-%m-%d").timestamp() + 86400.0
    marks = ",".join("?" * len(DECISION_KINDS))
    rows = conn.execute(
        f"SELECT ts, kind, message, detail_json FROM audit "
        f"WHERE ts >= ? AND ts < ? AND kind IN ({marks}) ORDER BY ts",
        (lo, hi, *DECISION_KINDS)).fetchall()
    by_day: dict[str, list] = defaultdict(list)
    for ts, kind, message, detail_json in rows:
        try:
            detail = json.loads(detail_json or "{}")
        except (ValueError, TypeError):
            detail = {}
        if not isinstance(detail, dict):
            detail = {}
        by_day[_day_of(ts)].append(
            (float(ts), kind, str(message or ""), detail))
    return by_day


def _load_asset_source(conn: sqlite3.Connection, start: str, end: str) -> dict[str, str]:
    """``{日期: daily_asset.source}`` —— 用来分辨"程序没开机"与"跑了但没留记录"。

    资产表里 ``source='derived'`` 是"程序没开机"的**直接证据** (那天资产是用
    收盘价推算补上的, 见 ``trade/asset_gapfill``), 比"什么都没留下"这句话有力。
    """
    return {r[0]: (r[1] or "eod") for r in conn.execute(
        "SELECT date, source FROM daily_asset WHERE date >= ? AND date <= ?",
        (start, end))}


def _all_audit_days(conn: sqlite3.Connection, start: str, end: str) -> set[str]:
    """任意种类审计出现在哪些天 —— 用来分辨"整天没痕迹"与"跑了但半路退了"。"""
    lo = _dt.datetime.strptime(start, "%Y-%m-%d").timestamp()
    hi = _dt.datetime.strptime(end, "%Y-%m-%d").timestamp() + 86400.0
    return {_day_of(ts) for (ts,) in conn.execute(
        "SELECT DISTINCT ts FROM audit WHERE ts >= ? AND ts < ?", (lo, hi))}


# ── 单条策略的还原 ─────────────────────────────────────────────

def _rotation_rows(day: str, items: list) -> list[dict]:
    """ETF 轮动: 逐份一行 (份1/份2/份3), 跳过与初始化各一行。"""
    out: list[dict] = []
    for ts, kind, message, detail in items:
        if kind == _ROT_SUMMARY:
            tranche = detail.get("tranche")
            subject = f"份{int(tranche) + 1}" if isinstance(tranche, int) else "pool"
            code = classify_rotation(detail)
            out.append(_row(day, "rotation", subject, code, message,
                            evidence=_rot_evidence(detail), event_ts=ts,
                            source="backfill_exact"))
        elif kind == _ROT_SKIP:
            # 注意 `classify_rotation_skip` 返回的是**原因码本身**, 不是内部中间量
            # (2026-09-18 最初把它当成 skip_kind 用, 直接把 "skip_session" 三个字
            #  写进了台账, 页面上会显示一个灰字兜底的怪码)
            code = classify_rotation_skip(message)
            out.append(_row(day, "rotation", "pool", code, message,
                            evidence={"note": message}, event_ts=ts,
                            source="backfill_text"))
        elif kind == _ROT_ERROR:
            out.append(_row(day, "rotation", "pool", "ROT_ERROR", message,
                            evidence={}, event_ts=ts,
                            source="backfill_exact"))
        elif kind == _ROT_MIGRATE:
            out.append(_row(day, "rotation", "pool", "ROT_INIT", message,
                            evidence={"tranches": detail.get("tranches")},
                            event_ts=ts, source="backfill_exact"))
    return out


def _rot_evidence(detail: dict) -> dict:
    """轮动的"凭什么": 只留复核用得上的那几项, 不把整个 detail 灌进去。

    含 state / changed / values 是为了 8 月中旬那批"单篮子"老格式 ——
    那时的判定依据就是"篮子变了没有"加上资金分布, 不带这些等于没留证据。
    """
    out = {}
    for k in ("tranche", "anchor", "target", "pool", "momentum", "stop_off",
              "stop_leg", "signal", "state", "changed", "derived", "values"):
        if k in detail:
            out[k] = detail[k]
    return out


# auto_buy 的审计 message 已经是人话 (例如"399006.SZ 未站上 MA200, 尾盘不买"),
# 直接进 reason_text 比重新拼一句更可信 —— 原文是一手证据。

def _auto_buy_rows(day: str, items: list) -> list[dict]:
    out: list[dict] = []
    for ts, kind, message, detail in items:
        code = classify_auto_buy(kind, detail)
        ev = {k: v for k, v in detail.items() if k in
              ("selected", "bought", "filters", "picks")}
        out.append(_row(day, "auto_buy", "pool", code, message, evidence=ev,
                        event_ts=ts, source="backfill_exact"))
    return out


def _exit_rows(day: str, items: list) -> list[dict]:
    """止盈止损: 真卖出按票一行 SELL, 各种"没卖成"按票一行 FAIL。"""
    out: list[dict] = []
    for ts, kind, message, detail in items:
        code_ = str(detail.get("code") or "")
        if not code_:
            continue                    # 明细没带代码的行不硬猜 (猜错比缺一行更糟)
        if kind in _EXIT_SELL_KINDS:
            ev = {k: detail.get(k) for k in ("price", "qty", "order_id", "reason")}
            out.append(_row(day, "exit", code_, "EXIT_TRIGGERED", message,
                            evidence=ev, trade_ids=detail.get("order_id") or "",
                            event_ts=ts, source="backfill_exact"))
        else:
            # 五种"想卖没卖成"出口统一记 EXIT_ARM_FAIL —— 当时到底卡在哪一步,
            # 审计 message 原文里写着, 全文照抄进 reason_text
            out.append(_row(day, "exit", code_, "EXIT_ARM_FAIL", message,
                            evidence={"code": code_,
                                      "reason": detail.get("reason"),
                                      "audit_message": message},
                            event_ts=ts, source="backfill_exact"))
    return out


def _ladder_rows(day: str, items: list) -> list[dict]:
    """预埋单: 挂成的按票合并档位; 没挂成的按票一行; 开关关着则整池一行。

    「开关关着」只在当天确实一张单都没挂出来时才写 —— 与实时路径
    (``trade_main._ladder_rows_today``) 同一判据, 否则会出现两条自相矛盾的行。
    """
    placed: dict[str, list] = defaultdict(list)
    order_ids: dict[str, list] = defaultdict(list)
    placed_ts: dict[str, list[float]] = defaultdict(list)
    skipped: dict[str, str] = {}
    skipped_ts: dict[str, float] = {}
    disabled_ts: float | None = None
    disabled = False
    for ts, kind, message, detail in items:
        code_ = str(detail.get("code") or "")
        if kind == "ladder_place":
            if not code_:
                continue
            placed[code_].append(detail.get("tier"))
            placed_ts[code_].append(ts)
            if detail.get("order_id"):
                order_ids[code_].append(detail["order_id"])
        elif kind == "ladder_skip_disabled":
            disabled = True
            disabled_ts = ts if disabled_ts is None else min(disabled_ts, ts)
        elif kind in _LADDER_SKIP and code_:
            if code_ not in skipped:
                skipped[code_] = message
                skipped_ts[code_] = ts
    out: list[dict] = []
    for code_ in sorted(placed):
        # 挂多档时取最早那一次 —— 用户想知道的是"什么时候开始挂上的",
        # 而不是最后一档的时间
        ts = min(placed_ts[code_]) if placed_ts[code_] else None
        tiers = [t for t in placed[code_] if isinstance(t, int)]
        ttxt = "、".join(f"第 {t + 1} 档" for t in tiers) or "档位未记录"
        out.append(_row(day, "ladder", code_, "LADDER_PLACED",
                        f"{code_} 的阶梯止盈预埋单已挂出（{ttxt}）",
                        evidence={"code": code_, "tiers": tiers},
                        trade_ids=order_ids.get(code_) or [],
                        event_ts=ts, source="backfill_exact"))
    for code_ in sorted(skipped):
        out.append(_row(day, "ladder", code_, "LADDER_SKIP",
                        skipped[code_], evidence={"code": code_},
                        event_ts=skipped_ts.get(code_),
                        source="backfill_exact"))
    if not placed and disabled:
        out.append(_row(day, "ladder", "pool", "LADDER_DISABLED",
                        "阶梯止盈开关关着，今天没挂预埋单"
                        "（不是没跑，是这个开关关着）",
                        evidence={}, event_ts=disabled_ts,
                        source="backfill_exact"))
    return out


def _row(day: str, strategy: str, subject: str, code: str, text: str,
         *, evidence: dict | None = None, trade_ids=None,
         event_ts: float | None = None,
         source: str = "backfill_exact") -> dict:
    """造一行台账。

    - ``action`` **不写死** —— 由写入口按原因码的默认动作填 (这样"原因码 ↔ 动作"
      的对应只有一处定义)。
    - ``event_ts`` = 这件事**当时**发生的时刻 (审计表里的 ``ts``)。写入口会优先
      用它填 ``updated_ts`` 与明细 ``events[].ts``; 不传则退回"写库那一刻"。
      推断出来的行 (``NO_RUN``) 没有"当时"可言 —— 它们的产生时刻就是写库时刻,
      这样退回是诚实的, 不是凑数。
    """
    row = {"trade_date": day, "strategy": strategy, "subject": subject,
           "reason_code": code, "reason_text": text,
           "evidence": evidence or {}, "trade_ids": trade_ids or "",
           "source": source}
    if event_ts is not None:
        row["event_ts"] = float(event_ts)
    return row


# ── 缺口那几行 (推断, 不是事实) ────────────────────────────────

_SECTION_NAMES = {"rotation": "ETF 轮动", "auto_buy": "尾盘选股买入",
                  "exit": "止盈止损", "ladder": "预埋单"}

# 三条"主"策略 (预埋单不算): 它们全缺 = 程序当天没跑完。预埋单是 09:15 挂的,
# 就算程序中午就退, 它照样能留下痕迹 —— 所以它不能作为"跑过了"的证据。
_PRIMARY_STRATEGIES = frozenset(("rotation", "auto_buy", "exit"))


def _missing_rows(day: str, present: set[str]) -> list[dict]:
    """某条策略当天一条记录都没有 → 写一行 ``NO_RUN`` 说明"为什么没有"。

    **一律 ``inferred``** —— 我们对"那天它没跑"的了解只来自"没有痕迹"这一条
    间接证据, 不是事实。页面会打上「推断」角标, 用户一眼知道这不是当场记的。
    """
    out: list[dict] = []
    for strategy in ("rotation", "auto_buy", "exit", "ladder"):
        if strategy in present:
            continue
        name = _SECTION_NAMES[strategy]
        if strategy == "rotation" and day < ROTATION_FIRST_SEEN:
            text = (f"当天没有 {name} 的记录："
                    f"当时这个策略还没有运行痕迹（它是 8 月中旬才上线的）")
        elif strategy == "exit":
            text = ("当天没有卖出动作；当时系统没记它检查过止盈止损"
                    "（该记录从 2026 年 9 月 18 日上线起才有）")
        else:
            text = (f"当天没有 {name} 的记录"
                    f"（程序可能没跑到它，也可能提前退了）")
        out.append(_row(day, strategy, "pool", "NO_RUN", text, evidence={},
                        source="inferred"))
    return out


def _whole_day_no_run(day: str, asset_source: str | None,
                      has_any_audit: bool) -> dict:
    """整天没有任何决策记录 → 一行 system 级 ``NO_RUN`` 把性质说清楚。

    三种情况性质完全不同, 混成一句会把真故障藏起来:
      - 有别的审计痕迹但没决策记录 → 程序跑了、半路退了;
      - 资产表那天的来源是 ``derived`` → 程序**没开机** (资产是推算补的);
      - 资产负债表里压根没有那天 → 什么都不知道。
    """
    if has_any_audit:
        text = ("这一天程序跑了，但没到收盘就退了"
                "（只留下监控痕迹，没有留下任何决策记录）")
    elif asset_source == "derived":
        text = ("这一天程序没开机（当天的资产是用收盘价推算补上的），"
                "所以没有任何决策记录")
    elif asset_source == "eod":
        text = ("这一天程序跑了，但没有留下决策记录"
                "（这个功能是 2026 年 9 月 18 日才上线的）")
    else:
        text = "这一天没有任何运行痕迹（程序没开机，也没留下别的记录）"
    return _row(day, "system", "all", "NO_RUN", text,
                evidence={"daily_asset_source": asset_source} if asset_source
                else {},
                source="inferred")


# ── 编排 ───────────────────────────────────────────────────────

def collect(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    """把 ``[start, end]`` 之间的历史决策还原成台账行 (只读 audit, 不写库)。

    交易日逐天过: 有痕迹的按四条策略分别还原, 一条策略都没有就补一行 ``NO_RUN``;
    整天一个决策痕迹都没有的, 用 system 级一行说清性质。非交易日直接跳过
    (写入口也会拦, 这里只是省点无谓的工作)。

    **每类审计必须分别喂给对应的还原函数** —— 2026-09-18 干跑时踩到过这个坑:
    把当天全部审计项一股脑交给 ``_auto_buy_rows``, 而 ``classify_auto_buy`` 对
    不认识的类型会**兜底返回 PICK_NO_SIGNAL**, 于是每一条无关审计都凭空生成一行
    "尾盘选股今天没选出票" —— 一天虚增上千行, 而且全是编的。

    返回的行按主键去重 (见 ``_dedupe``): 同一天同一条决策被重复记几百次是常态,
    不去重的话行数虚高、CLI 上的数字没法看, 但**不能顺手做"取最强"的合并** ——
    那正是写入口 ``DailyDecisionStore.log`` 的职责, 这里再压一遍就是第二份规则。
    """
    audit = _load_audit(conn, start, end)
    asset = _load_asset_source(conn, start, end)
    touched = _all_audit_days(conn, start, end)

    rows: list[dict] = []
    for day in _trading_days(start, end):
        items = audit.get(day) or []
        if not items:
            rows.append(_whole_day_no_run(day, asset.get(day),
                                          day in touched))
            continue
        present: set[str] = set()
        # 索引 1 是种类 —— 索引 0 是"当时发生时刻"(见 _load_audit), 别看错
        rot = [it for it in items if it[1] in ROT_KINDS]
        ab = [it for it in items if it[1] in AB_KINDS]
        ex = [it for it in items if it[1] in EXIT_KINDS]
        lad = [it for it in items if it[1] in LADDER_KINDS]
        if rot:
            present.add("rotation")
            rows.extend(_rotation_rows(day, rot))
        if ab:
            present.add("auto_buy")
            rows.extend(_auto_buy_rows(day, ab))
        if ex:
            exit_rows = _exit_rows(day, ex)
            if exit_rows:               # 明细缺 code 会被丢掉, 别虚报"有记录"
                present.add("exit")
                rows.extend(exit_rows)
        if lad:
            present.add("ladder")
            rows.extend(_ladder_rows(day, lad))
        # 「三条主策略一条都没留下痕迹」是"程序没跑完"的信号 (2026-09-11:
        # 09:15 挂预埋单时发现开关关着, 之后再无痕迹, 当天没到收盘就退了)。
        # 不加这一行的话, 那天在页面上就只剩一句"开关关着", 完全看不出程序挂了;
        # 日历格的摘要也会被"预埋单：阶梯止盈开关关着"占掉 —— 那是当天最不重要的事。
        if not (present & _PRIMARY_STRATEGIES):
            rows.append(_whole_day_no_run(day, asset.get(day), day in touched))
        rows.extend(_missing_rows(day, present))
    return _dedupe(rows)


def _dedupe(rows: list[dict]) -> list[dict]:
    """去掉"一模一样"的重复行: 主键 + 原因码 + 大白话三者全同才算重复。

    实测背景 (2026-07-30): 当时监控每 10 秒重试一次卖出失败, 同一只票
    600808.SH 那一天写了 587 条 ``exit_skip`` + 586 条 ``exit_arm_fail``,
    内容只差当前价 (2.61 / 2.6)。逐条送进写入口虽然最终会被主键合并成一行,
    但"还原出行数"这个数字会虚高到没法看, 且白做几千次数据库往返。

    判据只在三者**全同**时才算重复, 所以:
      - 只差价格的两次重试 (文字不同) 会保留 —— 它们进 ``evidence.events``,
        能看到"价格一路在跌", 信息不丢;
      - 三次尾盘选股跑出不同结果 (选中 8 买 0 / 买 4) 会全部保留 ——
        交给写入口的动作强弱合并挑出"买到了"那条, 这里不许替它做主。
    """
    seen: set[tuple] = set()
    out: list[dict] = []
    for r in rows:
        key = (r["trade_date"], r["strategy"], r["subject"], r["reason_code"],
               r["reason_text"])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def reset_backfilled(conn: sqlite3.Connection, start: str, end: str) -> int:
    """删掉区间内**不是当场记录**的台账行, 返回删了几行。

    为什么需要: 回填本身是可重复执行的, 但"重跑一遍"并不总能修正旧结果 ——
    写入口合并时会**继承**旧行已经攒下的 ``events`` (这是对的, 轨迹不该丢),
    于是旧数据里的错时间戳、错文案会跟着留下来。改了回填逻辑要重来时, 得先清干净。

    **只删 ``source != 'live'`` 的行** —— 当场记录的行永远不碰, 这是硬规矩
    (与写入口的来源可信度保护同一条原则, 不因为它是个"清理"函数就放宽)。
    """
    cur = conn.execute(
        "DELETE FROM daily_decision "
        "WHERE trade_date >= ? AND trade_date <= ? AND source <> 'live'",
        (start, end))
    removed = int(cur.rowcount or 0)
    conn.commit()
    return removed


def run(db_path: str, start: str = LEDGER_START, end: str | None = None,
        *, dry_run: bool = False, overwrite_live: bool = False,
        reset: bool = False) -> dict:
    """执行回填。返回统计字典供 CLI 打印。

    统计里有三个数, 别混:
      - ``rows``  —— 还原出的行对象数。**同一条决策可能有多行** (当时监控每 10 秒
        重试失败就写一条审计), 所以这个数会大于台账里实际的行数;
      - ``keys``  —— 按主键 ``(日期, 策略, 对象)`` 去重后的格数, **这才是台账里
        会有多少行**;
      - ``written`` —— 写入口报告"产生了影响"的行数 (被可信度保护拦下的不计)。
        它按行对象计数, 所以也大于 ``keys``。

    ``end`` 缺省 = 今天。``dry_run=True`` 只还原、不落库 (先把结果看一眼)。
    ``overwrite_live=True`` 是"修数据"的后门, 默认关 —— 回填永远不该改写当场
    记录的行。

    ``reset=True`` 先把区间内**非当场记录**的旧台账行删掉再写 (见
    ``reset_backfilled`` 说明为什么重跑不一定能自我修正)。返回值里的 ``removed``
    是删掉的条数。
    """
    # 延迟 import: 让本模块在被 CLI import 时不牵连 trade.store 的依赖链,
    # 真正要写库时才拉 (与 trade_main 延迟 import trade.signals 同一手法)
    from trade.store import DAILY_DECISION_DDL, DailyDecisionStore

    if end is None:
        end = _dt.date.today().isoformat()
    if start > end:
        raise ValueError(f"起点晚于终点: {start} > {end}")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        _require_audit_table(conn, db_path)
        rows = collect(conn, start, end)
        per_day: dict[str, int] = defaultdict(int)
        for r in rows:
            per_day[r["trade_date"]] += 1
        result = {"start": start, "end": end, "days": len(per_day),
                  "rows": len(rows), "written": 0, "removed": 0,
                  "per_day": dict(per_day), "dry_run": bool(dry_run),
                  "keys": len({(r["trade_date"], r["strategy"], r["subject"])
                               for r in rows})}
        if dry_run or (not rows and not reset):
            return result
        conn.executescript(DAILY_DECISION_DDL)   # 与实时同一份 DDL (单一真相源)
        if reset:
            result["removed"] = reset_backfilled(conn, start, end)
        if not rows:
            return result
        writer = DailyDecisionStore(conn, threading.Lock())
        result["written"] = writer.log(rows, overwrite_live=overwrite_live)
        return result
    finally:
        conn.close()


def _require_audit_table(conn: sqlite3.Connection, db_path: str) -> None:
    """库里没有 ``audit`` 表 → 直接报错退出, **不要**当成"每天都没跑"。

    这是个很容易踩的坑: 指错库 (比如空文件) 时, ``audit`` 一次都查不到, 程序会
    老老实实地给每一个交易日写一行"这一天程序没开机" —— 一库假数据, 而且看起来
    还挺像真的 (2026-09-18 补测试时被一个空库钓出来)。
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='audit'"
    ).fetchone()
    if row is None:
        raise ValueError(
            f"这个库里没有 audit 表, 不像是交易库: {db_path}"
            "（回填要从审计表里翻历史决策，指错库会写出一堆假记录）")
