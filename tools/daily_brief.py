"""tools/daily_brief.py — 今日热点追踪简报流水线（2026-08-12，P1）。

把「取数」和「成文」拆开：Python 侧一次性把数据取全（brain.data_tools），
LLM 只做一次调用按模板成文 —— 对照 TradingAgentsCN 大盘/板块分析师的
「自包含」模式（取数 + 单次 LLM 分析，不跑 agent 多轮循环）。
解决旧打法（LLM 现场搜数据）的三大事故：300s 超时（2026-07-30 两次）、
max-turns 卡死（2026-08-01）、provider 断流续跑。

产物：docs/brief/YYYY-MM-DD_今日热点追踪简报.md（大脑 Write 落盘）
      + 对话沉淀归档（ask_brain_sync 自带）。

用法：
    python tools/daily_brief.py              # 取数 + 调大脑成文 + 写 docs/brief/
    python tools/daily_brief.py --pack-only  # 只取数打印数据包（自检/调试，不调 LLM）
    python tools/daily_brief.py --date 20260811
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 仓库根, 供 brain/utils 导入

from utils.sysutil import ensure_utf8_stdout, project_root

_TEMPLATE_PATH = project_root() / "brain" / "templates" / "daily_brief.md"


def build_data_pack(trade_date: str) -> str:
    """市场快照 + 财新要闻 → 成文数据包。"""
    from brain import data_tools  # 延迟 import（akshare 加载慢）
    return (data_tools.market_snapshot(trade_date)
            + "\n\n" + data_tools.stock_news(20))


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdout()
    ap = argparse.ArgumentParser(prog="python tools/daily_brief.py",
                                 description="今日热点追踪简报流水线")
    ap.add_argument("--date", default=None,
                    help="交易日期 YYYYMMDD（缺省今天；非交易日数据源内自动回退）")
    ap.add_argument("--pack-only", action="store_true",
                    help="只取数打印数据包，不调 LLM（自检/调试）")
    ap.add_argument("--timeout", type=int, default=300, help="大脑成文超时秒数")
    args = ap.parse_args(argv)

    trade_date = args.date or dt.datetime.now().strftime("%Y%m%d")
    day = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
    pack = build_data_pack(trade_date)
    if args.pack_only:
        print(pack)
        return 0

    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    question = (
        template
        + "\n\n---\n\n# 数据包（Python 已取好，直接使用，不要再去搜网页）\n\n"
        + pack
        + f"\n\n---\n\n成文后把简报全文用 Write 写入 "
          f"`docs/brief/{day}_今日热点追踪简报.md`，"
          "并在回答里输出文件路径 + 简报全文。"
    )
    from brain.claude_cli import ask_brain_sync
    # channel 按日期独立：每天的简报会话互相隔离，不拖着历史涨成本
    r = ask_brain_sync(question, timeout=args.timeout, max_turns=6,
                       channel=f"daily_brief_{trade_date}")
    print(r["answer"])
    for w in r["warnings"]:
        print(f"[warn] {w}")
    if r["low_confidence"]:
        print("[note] 本回答低置信（缺反证段或引用依据）")
    return 0 if r["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
