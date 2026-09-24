# -*- coding: utf-8 -*-
"""notes_gen/morning.py — 隔夜简报（2026-09-17, M7 补做）。

**为什么有它**（计划书 §17.4，用户 2026-09-17 拍板选 (b)）：
体温表是**收盘价**算出来的，天生属于「收盘后」，硬拖到早上不会变 ——
实测跑 `expected_last_trading_day` 证明 15:50 与次日 09:05 拿到的是**同一天、逐字段相同**的数据，
早上那张卡一个新数字都没有。所以早上要发就发**过了一夜新发生的事**：
美股隔夜收盘 / 港股 / 南向资金 / 隔夜消息，**体温表只作一句背景**。

**两条硬规则**（计划书 §17.4）：
1. **没有隔夜新信息就不发空卡**（美股休市、港股休市、夜里无消息 → 不发）。
   依据：飞书推送验证计划书的「告警疲劳」结论 —— 空卡会训练用户忽略推送。
2. **昨天 15:55 那条没推成时，简报里附带补发**（判定依据 `data/scheduler_state.json`）。

**接口（4 个）**：`build_brief()` / `brief_md()` / `push_brief()` / `run_morning_brief()`。
**依赖单向**：本模块 import `brain.market_panel`（盘面快照）与
`core.market_position_runner`（昨日位置一句话），**绝不 import trade 做仓位动作**。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.logger import get_logger
from utils.sysutil import project_root

#: 数字/百分比怎么印（None 印「—」等规则）**复用**复盘报告那一份，不写第二份
#: （两个都是报告层模块，同一包、无环：daily 不 import morning）
from notes_gen.daily import _pct

_logger = get_logger("notes_gen.morning")


def _num(v, nd: int = 2) -> str:
    """数字 → 字符串（None/非数返「—」）。**不编 0**：缺失就印破折号。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "—"
    if x != x:
        return "—"
    return f"{x:,.{nd}f}"

_ROOT = project_root()
STATE_PATH = _ROOT / "data" / "scheduler_state.json"
#: 昨天 15:55 的 job 名（补发判定用；与 scheduler/__main__.py 注册的一致）
EVENING_JOB = "market_position_push"


# ───────────────────── 取数（全部 fail-soft） ─────────────────────


def _overnight_facts() -> dict:
    """取**结构化**的隔夜盘面事实（美股/港股/南向）。

    **2026-09-17 M7 实测改口**：原来这里直接转 `brain.market_panel.market_snapshot()`
    的返回文本，而那份文本是**给大脑看的原始数据包**（`df.to_string()` 直接倒出来）——
    实测转出来的卡片里有：错位的列、`NaN`、`133558676` 这种没单位的原始数字、
    甚至一行被截断的「福莱蒽特 10.011」。**直接把机器数据包塞给用户 = 违反
    「所有给用户的内容都要大白话」那条规则**。改成取结构化数字，人话由本模块自己组织
    （与 `notes_gen/daily.py` 的 `_*_plain` 模板同一分工：报告层负责说人话）。
    """
    try:
        from brain.market_panel import overnight_facts
        return overnight_facts() or {}
    except Exception as e:
        _logger.warning("取隔夜盘面事实失败: %s", e)
        return {}


def _pct_word(pct) -> str:
    """涨跌幅 → 人话幅度词（不写"显著"这种没标准的字眼）。"""
    a = abs(float(pct))
    if a < 0.5:
        return "几乎没动"
    if a < 1.5:
        return "小幅"
    if a < 3.0:
        return "明显"
    return "大幅"


def _overnight_plain(f: dict) -> list[str]:
    """结构化事实 → **人话**（每条先说意思、再给数字，数字带单位）。

    全部由数字生成，不写死句子 —— 写死的话明天涨了就成了假话。
    """
    out: list[str] = []
    us = [r for r in (f.get("us") or []) if r.get("pct") is not None]
    if us:
        move = [float(r["pct"]) for r in us]
        if all(m > 0 for m in move):
            head = "**三大指数一起涨**"
        elif all(m < 0 for m in move):
            head = "**三大指数一起跌**"
        else:
            head = "**三大指数涨跌不一**"
        parts = [f"{r['name']} {_num(r['close'], 1)} 点（{_pct(r['pct'])}）" for r in us]
        worst = min(move)
        out.append(f"昨天夜里美股收盘：{head} —— " + "、".join(parts) + "。")
        out.append(f"说人话：{_pct_word(worst)}的波动"
                   + ("（跌得最多的那个也没跌多少）" if abs(worst) < 0.5 else "")
                   + "。美股跟 A 股不是同一批人炒，**它跌不代表 A 股今天会跌**，"
                     "只能说明外围情绪偏冷或偏热。")
    hk = [r for r in (f.get("hk") or []) if r.get("close") is not None]
    if hk:
        # 恒生指数排第一（它是"港股"这张脸），其余按原名次跟在后面
        hk.sort(key=lambda r: (0 if r["name"] in ("恒生指数", "恒生指數") else 1))
        parts = [f"{r['name']} {_num(r['close'], 2)} 点（{_pct(r['pct'])}）"
                 for r in hk[:3]]
        out.append(f"港股上一个交易日收盘：" + "、".join(parts) + "。")
        out.append("说人话：港股里内地公司占大头，所以它跟 A 股是「同一条街上的邻居」，"
                   "方向经常一起走 —— 可以当个参考，但它不是 A 股今天的答案。")
    sb = f.get("southbound") or None
    if sb and sb.get("net_buy_yi") is not None:
        v = float(sb["net_buy_yi"])
        who = "**净买入**" if v > 0 else "**净卖出**"
        out.append(f"南向资金（内地资金通过港股通买卖港股的钱）{sb.get('date')} "
                   f"{who} {_num(abs(v), 2)} 亿港元。")
        out.append("说人话：这个数是「内地这边的钱在往港股那边流，还是往外撤」。"
                   + ("往那边流，说明有内地资金在找港股的机会。" if v > 0 else
                      "往回撤，说明内地资金在收缩港股仓位。"))
    return out


def _yesterday_line() -> str:
    """昨日位置**只作一句背景**（不重复整张体温表）。

    2026-09-17 M7：称呼**由数据生成**（与体温表 `day_word` 同一纪律）——
    录像最新一条落在哪天就写哪天，不许一律叫「昨天」。实测踩到：今天 9月17日，
    而日线缓存最新有效日是 **9月15日**（9月16日的还没补上），
    写「昨天（9月15日）收盘」是错的（那是前天）。
    """
    try:
        from core import market_position_runner as mpr
        rec = mpr.latest()
        if not rec:
            return ""
        b = rec.get("breadth") or {}
        sh = ((rec.get("indices") or {}).get("shanghai")) or {}
        bits = []
        p = sh.get("pct_10y")
        if p is not None:
            bits.append(f"上证在十年 {p:.1f}% 位置")
        w = b.get("above_ma20_pct")
        if w is not None:
            bits.append(f"{w:.1f}% 的股票站在 20 日均线上方")
        if not bits:
            return ""
        d = str(rec.get("date") or "")
        try:
            mo, dy = int(d[5:7]), int(d[8:10])
            day = f"{mo}月{dy}日"
        except Exception:
            day = d or "最新一天"
        when = (f"最新一天的盘面（{day}，**个股日线还没更新到今天**）"
                if rec.get("stale") else f"上一交易日（{day}）")
        return f"{when}收盘：{'、'.join(bits)}。位置和趋势怎么读，看 15:55 那份复盘。"
    except Exception as e:
        _logger.warning("取昨日位置失败: %s", e)
        return ""


def _missed_evening_push() -> dict | None:
    """昨天 15:55 那条推成功了吗？没成功就返 `{"date": ..., "reason": ...}`。

    **不知道就返 None**（当成"没漏"）—— 宁可漏一次补发，也不要在推成功的情况下
    又补发一遍（那会变成重复推送，比漏发更烦人）。
    """
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None                     # 还没有状态文件 = 调度器还没跑过, 不补
    except Exception as e:
        _logger.warning("读 %s 失败（按没漏处理）: %s", STATE_PATH, e)
        return None
    try:
        job = (state.get("jobs") or {}).get(EVENING_JOB) or {}
        last = job.get("last_run") or job.get("last") or ""
        if not last:
            return None
        return None if not job.get("last_error") else {
            "date": str(last)[:10], "reason": str(job.get("last_error"))[:120]}
    except Exception:
        return None


# ───────────────────── 公开接口 ─────────────────────


def build_brief() -> dict:
    """组装隔夜简报 payload（只读，fail-soft）。

    返回键：`ok` / `plain`（人话段落）/ `yesterday` / `missed` / `reason`（ok=False 时）。
    """
    facts = _overnight_facts()
    plain = _overnight_plain(facts)
    yday = _yesterday_line()
    missed = _missed_evening_push()
    # **硬规则 1**: 没有隔夜新信息就不发。判据 = 人话段落一条都没生成
    # （= 美股/港股/南向三项全都没取到）。
    # （昨日位置那句**不算**"新信息" —— 它是背景，单靠它不足以构成一封简报。）
    if not plain:
        return {"ok": False, "reason": "没有取到隔夜盘面信息（美股/港股/南向都没有）—— "
                                       "按设计不发空卡，免得训练你忽略推送。",
                "plain": [], "yesterday": yday, "missed": missed}
    return {"ok": True, "plain": plain, "yesterday": yday, "missed": missed}


def brief_md(brief: dict) -> str:
    """payload → Markdown（纯函数）。"""
    b = brief or {}
    out = ["# 隔夜简报", "",
           "**这是「过了一夜新发生的事」**，不是昨天收盘那张体温表 —— "
           "收盘数据不会因为过了夜就变，所以早上不重复推它。", ""]
    if b.get("missed"):
        m = b["missed"]
        out += [f"> **补发**：昨天 15:55 那条盘后复盘没发出去"
                f"（{m.get('date')}，原因：{m.get('reason')}）。"
                "那份报告已经重新生成过了，需要的话在「大盘位置」页签点「生成今日复盘」看。", ""]
    if b.get("plain"):
        out += ["## 隔夜盘面", ""] + [f"- {x}" for x in b["plain"]] + [""]
    if b.get("yesterday"):
        out += ["## 一句话背景", "", b["yesterday"], ""]
    out += ["---",
            "只读参考，**不联入任何仓位调度**（业务铁律 1）。"
            "要动手，你自己判断。"]
    return "\n".join(out)


def push_brief(brief: dict, *, feishu: bool = True) -> dict:
    """推飞书（复用 `tools/send_report_feishu.py`）。fail-soft，绝不抛。"""
    if not brief.get("ok"):
        return {"ok": False, "reason": brief.get("reason") or "没有内容可发", "sent": False}
    if not feishu:
        return {"ok": True, "sent": False, "reason": "已关掉飞书推送"}
    md = brief_md(brief)
    try:
        from tools.send_report_feishu import (chunk_sections, load_webhook,
                                             md_to_lark, send_card)
        try:
            webhook = load_webhook()
        except SystemExit:
            return {"ok": False, "sent": False,
                    "reason": "未配置 FEISHU_WEBHOOK_URL (.env)"}
        chunks = chunk_sections(md_to_lark(md))
        codes = []
        for i, c in enumerate(chunks, 1):
            r = send_card(webhook, "隔夜简报", c, i, len(chunks))
            codes.append(r.get("code", r.get("StatusCode")))
        return {"ok": True, "sent": True, "cards": len(chunks), "codes": codes}
    except Exception as e:
        return {"ok": False, "sent": False, "reason": f"推送失败: {e}"}


def run_morning_brief(*, push: bool = True, write: bool = True) -> dict:
    """编排入口：组装 → （可选落盘）→ 推送。供调度器 09:05 的 job 调。"""
    brief = build_brief()
    out = {"ok": bool(brief.get("ok")), "reason": brief.get("reason")}
    if not brief.get("ok"):
        _logger.info("隔夜简报不推送: %s", brief.get("reason"))
        return out
    if write:
        try:
            d = _ROOT / "data" / "morning_brief"
            d.mkdir(parents=True, exist_ok=True)
            import datetime as _dt
            p = d / f"{_dt.date.today().isoformat()}.md"
            tmp = p.with_suffix(".md.tmp")
            tmp.write_text(brief_md(brief), encoding="utf-8")
            import os
            os.replace(tmp, p)
            out["path"] = str(p)
        except Exception as e:
            _logger.warning("隔夜简报落盘失败: %s", e)
    if push:
        out["push"] = push_brief(brief)
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m notes_gen.morning",
                                 description="隔夜简报（只读，不联仓位）")
    ap.add_argument("--stdout", action="store_true", help="打印而不推送")
    ap.add_argument("--no-push", action="store_true", help="不推飞书")
    args = ap.parse_args(argv)
    if args.stdout:
        b = build_brief()
        print(brief_md(b) if b.get("ok") else f"（不发：{b.get('reason')}）")
        return 0
    res = run_morning_brief(push=not args.no_push)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
