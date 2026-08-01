"""evolution/weekly.py — 周度自进化: 复盘 → 教训入册 → 周度报告。

流程 (自进化循环的第一圈):
1. 零号报告统计 (tools/policy_report_zero.py, importlib 按路径加载, 同 notes_gen 手法)
2. 大脑复盘 (channel="evolution", 提示词要求它先读 playbook/LESSONS.md 再复盘,
   并输出至多 3 条 <lesson>标题|内容</lesson> 新教训)
3. 教训解析并追加进 vera_obs_vault/playbook/LESSONS.md (大脑从此"长记性")
4. 周度报告写 notes/weekly_YYYY-Www.md (统计 + 叙事 + 教训清单)

松耦合: db 缺失/无成交 → 报告照出(标注), 不调大脑; 大脑失败 → 只丢
叙事和教训, 统计不丢; playbook 写失败 → 只记日志。
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from notes_gen.monthly import compute_stats_base
from utils.logger import get_logger
from utils.sysutil import load_module_from_path

_logger = get_logger("evolution.weekly")

_ROOT = Path(__file__).resolve().parent.parent
_PRZ_PATH = _ROOT / "tools" / "policy_report_zero.py"
PLAYBOOK_PATH = _ROOT / "vera_obs_vault" / "playbook" / "LESSONS.md"

_LESSON_RE = re.compile(r"<lesson>(.*?)\|(.*?)</lesson>", re.S)
_MAX_LESSONS = 3


def _load_prz():
    """按路径加载零号报告模块。委托 utils.sysutil (同 notes_gen 手法)。"""
    return load_module_from_path("policy_report_zero", _PRZ_PATH)


def parse_lessons(text: str) -> list[tuple[str, str]]:
    """从大脑回答里解析 <lesson>标题|内容</lesson>, 至多 _MAX_LESSONS 条。"""
    out = []
    for title, body in _LESSON_RE.findall(text or ""):
        t, b = title.strip(), body.strip()
        if t and b:
            out.append((t, b))
    return out[:_MAX_LESSONS]


def append_lessons(playbook: Path, lessons: list[tuple[str, str]],
                   day: str, source: str = "周度复盘") -> bool:
    """教训追加进 playbook。成功 True; 失败记日志 False (松耦合)。"""
    if not lessons:
        return True
    try:
        playbook.parent.mkdir(parents=True, exist_ok=True)
        with open(playbook, "a", encoding="utf-8") as f:
            for title, body in lessons:
                f.write(f"\n### {day} {title}\n\n{body}\n\n- 来源: {source}\n")
        _logger.info("playbook 写入 %d 条新教训: %s", len(lessons), playbook)
        return True
    except Exception as e:
        _logger.warning("playbook 写入失败 (松耦合): %s", e)
        return False


def _week_id(d: dt.date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def run_weekly_evolution(db_path: str | Path = "data/trade/trade.db",
                         report_dir: str | Path = "notes",
                         playbook: str | Path | None = None,
                         today: dt.date | None = None) -> Path | None:
    """跑一圈周度进化。返报告路径; 致命失败返 None (松耦合)。"""
    today = today or dt.date.today()
    playbook = Path(playbook) if playbook else PLAYBOOK_PATH
    db_path = Path(db_path)

    # 1. 统计底座 (无成交也出报告, 但跳过大脑)
    if not db_path.exists():
        _logger.warning("trade.db 不存在: %s, 本周进化跳过", db_path)
        return None
    try:
        prz = _load_prz()
    except Exception as e:
        _logger.warning("统计底座失败, 本周进化跳过: %s", e)
        return None
    base = compute_stats_base(prz, db_path)
    if base is None:
        return None
    res, stats_md = base

    # 2. 大脑复盘 (无成交 → 不浪费 token)
    narrative, lessons = None, []
    if res.closed or res.open_lots:
        question = (
            "本周实盘交易统计如下 (FIFO 口径)。请先读你的投资手册 "
            "vera_obs_vault/playbook/LESSONS.md, 然后做周度复盘:\n"
            "① 本周操作与手册里的教训有无冲突;\n"
            "② 亏损主要亏在哪个环节 (选股/入场/止损/档位);\n"
            f"③ 输出至多 {_MAX_LESSONS} 条新教训, 每条格式 "
            "<lesson>标题|事实+以后怎么做</lesson>, 没有新教训就不输出。\n\n"
            + stats_md[:6000]
        )
        try:
            from brain.claude_cli import ask_brain_sync
            r = ask_brain_sync(question, timeout=300, max_turns=20,
                               channel="evolution")
        except Exception as e:
            _logger.warning("大脑复盘调用异常 (松耦合): %s", e)
            r = {"success": False}
        if r.get("success"):
            narrative = r.get("answer", "")
            lessons = parse_lessons(narrative)
            append_lessons(playbook, lessons, today.isoformat())
        else:
            _logger.warning("大脑复盘失败 (松耦合, 只出统计): %s",
                            str(r.get("answer", ""))[:200])
    else:
        _logger.info("本周无成交, 跳过大脑复盘")

    # 3. 周度报告
    wk = _week_id(today)
    L = [
        "---",
        f'week: "{wk}"',
        f'generated_at: "{dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}"',
        f"brain: {'true' if narrative else 'false'}",
        f"lessons_added: {len(lessons)}",
        "---",
        "",
        f"# 周度进化报告 {wk}",
        "",
        stats_md,
        "",
    ]
    if narrative:
        L += ["## 大脑复盘", "", narrative, ""]
    if lessons:
        L += ["## 本周新教训 (已写入 playbook)", ""]
        L += [f"- **{t}**: {b}" for t, b in lessons]
        L.append("")
    try:
        out = Path(report_dir)
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"weekly_{wk}.md"
        path.write_text("\n".join(L), encoding="utf-8")
        _logger.info("周度进化报告: %s (新教训 %d 条)", path, len(lessons))
        return path
    except Exception as e:
        _logger.warning("周度报告写盘失败 (松耦合): %s", e)
        return None
