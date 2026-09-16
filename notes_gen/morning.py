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

_logger = get_logger("notes_gen.morning")

_ROOT = project_root()
STATE_PATH = _ROOT / "data" / "scheduler_state.json"
#: 昨天 15:55 的 job 名（补发判定用；与 scheduler/__main__.py 注册的一致）
EVENING_JOB = "market_position_push"


# ───────────────────── 取数（全部 fail-soft） ─────────────────────


def _market_snapshot() -> str:
    """调既有 `brain.market_panel.market_snapshot()` 拿"隔夜/隔日"盘面文本。

    复用而不是重写：那份快照已经含「美股三大指数（隔夜收盘）/ 港股指数 / 南向资金」三块
    （CLAUDE.md 已登记）。取不到就返空串 —— 由调用方决定"没内容就不发"。
    """
    try:
        from brain.market_panel import market_snapshot
        return str(market_snapshot() or "").strip()
    except Exception as e:
        _logger.warning("取盘面快照失败: %s", e)
        return ""


def _yesterday_line() -> str:
    """昨日位置**只作一句背景**（不重复整张体温表）。"""
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
        return f"昨天（{rec['date']}）收盘：{'、'.join(bits)}。"
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

    返回键：`ok` / `snapshot` / `yesterday` / `missed` / `reason`（ok=False 时）。
    """
    snap = _market_snapshot()
    yday = _yesterday_line()
    missed = _missed_evening_push()
    # **硬规则 1**: 没有隔夜新信息就不发。判据 = 盘面快照取不到任何内容。
    # （昨日位置那句**不算**"新信息" —— 它是背景，单靠它不足以构成一封简报。）
    if not snap:
        return {"ok": False, "reason": "没有取到隔夜盘面信息（美股/港股/南向都没有）—— "
                                       "按设计不发空卡，免得训练你忽略推送。",
                "snapshot": "", "yesterday": yday, "missed": missed}
    return {"ok": True, "snapshot": snap, "yesterday": yday, "missed": missed}


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
    if b.get("snapshot"):
        out += ["## 隔夜盘面", "", b["snapshot"], ""]
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
