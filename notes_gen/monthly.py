"""notes_gen/monthly.py — 月度笔记生成 (统计底座 + 可选大脑叙事)。

统计底座: 复用零号报告 tools/policy_report_zero.py 的
load_trades / pair_fifo / load_tiers / build_report 四件套
(tools/ 不是包, 用 importlib 按路径加载, 参照 tests/test_policy_report_zero.py)。
口径: 截至该月末的全部成交 (与零号报告 FIFO 全历史口径一致),
已平仓才计战绩, n < min_n 的档位标"样本不足"。

大脑叙事 (可选增强, 松耦合):
    `try: from brain.claude_cli import ask_brain_sync` —— brain 模块由他人
    并行开发, 接口约定 ask_brain_sync(question, timeout=120) -> dict,
    含 "answer"/"success" 键。模块不存在 / 调用异常 / success=False 时
    跳过叙事段, 只出统计, 绝不因叙事失败弄丢统计底座。

松耦合铁律: 任何外部失败 (db 不存在 / 标签不可用 / brain 缺失)
返回 None 或降级输出并记 warning, 绝不向调用方抛异常。
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from utils.logger import get_logger
from utils.sysutil import load_module_from_path, project_root

_logger = get_logger("notes_gen.monthly")

_PRZ_PATH = project_root() / "tools" / "policy_report_zero.py"


def _load_prz():
    """按路径加载零号报告模块 (tools/ 不是包)。委托 utils.sysutil (已加载则复用 sys.modules)。"""
    return load_module_from_path("policy_report_zero", _PRZ_PATH)


def compute_stats_base(prz, db_path: Path, end_ts: float | None = None):
    """统计底座五连 (evolution/weekly.py 共用): load_trades → pair_fifo →
    codes → load_tiers → build_report。end_ts 非 None 时只保留 ts < end_ts
    的成交 (FIFO 需全历史配对, 只能截尾不能截头)。
    任何失败返 None (松耦合, 记 warning)。"""
    try:
        trades = prz.load_trades(db_path)
        if end_ts is not None:
            trades = [t for t in trades if t["ts"] < end_ts]
        res = prz.pair_fifo(trades)
        codes = sorted({lot.code for lot in res.closed} | {lot.code for lot in res.open_lots})
        tiers = prz.load_tiers(codes) if codes else {}
        stats_md = prz.build_report(res, tiers, min_n=5,
                                    db_path=str(db_path), since=None)
        return res, stats_md
    except Exception as e:
        _logger.warning("统计底座生成失败 (db=%s): %s", db_path, e)
        return None


def _default_month(today: dt.date | None = None) -> str:
    """上一自然月, "YYYY-MM"。"""
    today = today or dt.date.today()
    first = today.replace(day=1)
    prev = first - dt.timedelta(days=1)
    return prev.strftime("%Y-%m")


def _month_end_ts(month: str) -> float:
    """该月结束时刻 (次月 1 日 00:00) 的 epoch 秒。"""
    y, m = int(month[:4]), int(month[5:7])
    nxt = dt.date(y + (m == 12), m % 12 + 1, 1)
    return dt.datetime(nxt.year, nxt.month, nxt.day).timestamp()


def _ask_brain(month: str, stats_md: str) -> str | None:
    """可选大脑叙事。任何失败返 None (松耦合, 只出统计)。"""
    try:
        from brain.claude_cli import ask_brain_sync
    except Exception as e:
        _logger.warning("brain 模块不可用, 跳过叙事段 (只出统计): %s", e)
        return None
    question = (
        f"以下是 {month} 的持仓/交易 × 政策档位复盘统计 (FIFO 配对, 纯统计口径)。"
        "请基于这些数字写一段简短的月度复盘叙事: 哪些档位策略奏效、"
        "哪些偏离值得警惕、下月应验证什么。只许引用文中统计, 不得编造数字。\n\n"
        + stats_md[:6000]
    )
    try:
        resp = ask_brain_sync(question, timeout=120)
    except Exception as e:
        _logger.warning("brain 调用异常, 跳过叙事段: %s", e)
        return None
    if not isinstance(resp, dict) or not resp.get("success"):
        _logger.warning("brain 返回 success=False 或格式异常, 跳过叙事段: %r", resp)
        return None
    answer = str(resp.get("answer", "")).strip()
    return answer or None


def generate_monthly_note(month: str | None = None,
                          db_path: str | Path = "data/trade/trade.db",
                          out_dir: str | Path = "notes") -> Path | None:
    """生成月度笔记 notes/monthly_YYYY-MM.md。失败返 None (记 warning)。

    month: "YYYY-MM", None = 上一自然月。
    """
    month = month or _default_month()
    db_path = Path(db_path)
    if not db_path.exists():
        _logger.warning("trade.db 不存在: %s, 月度笔记跳过", db_path)
        return None

    try:
        prz = _load_prz()
    except Exception as e:
        _logger.warning("零号报告模块加载失败, 月度笔记跳过: %s", e)
        return None

    # 口径: 截至该月末的全部成交 (FIFO 需要全历史配对, 不能截头)
    base = compute_stats_base(prz, db_path, end_ts=_month_end_ts(month))
    if base is None:
        return None
    res, stats_md = base

    # 可选大脑叙事: 失败只丢叙事段, 不丢统计
    narrative = _ask_brain(month, stats_md)

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    L: list[str] = [
        "---",
        f'month: "{month}"',
        f'generated_at: "{now}"',
        f"brain: {'true' if narrative else 'false'}",
        "---",
        "",
        f"# 月度笔记 {month}",
        "",
        "> 统计底座 = 零号报告口径 (截至该月末全部成交, FIFO 配对); "
        "叙事段由 brain 生成 (可选, 缺失不影响统计)。",
        "",
        stats_md,
        "",
    ]
    if narrative:
        L += ["## 大脑复盘叙事", "", narrative, ""]

    try:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        out_path = out / f"monthly_{month}.md"
        out_path.write_text("\n".join(L), encoding="utf-8")
    except Exception as e:
        _logger.warning("月度笔记写盘失败 (out=%s): %s", out_dir, e)
        return None

    _logger.info("月度笔记已生成: %s (已平仓 %d 笔, 在持 %d 笔, 叙事=%s)",
                 out_path, len(res.closed), len(res.open_lots),
                 "有" if narrative else "无")
    return out_path
