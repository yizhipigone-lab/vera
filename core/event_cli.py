# -*- coding: utf-8 -*-
"""core/event_cli.py — 事件录入/查询命令行 (供 Agent 自动扫描落库 + 人工快速录入)。

用法:
  # 加一条事件 (level: epic/major/minor; score 正负号=方向, 正利好负空)
  python core/event_cli.py add --level major --title "央行降准0.5个百分点" \
      --score 0.5 --date 2026-09-15 --logic "释放长期资金约1万亿, 宽松信号" --source agent_scan

  python core/event_cli.py list      # 列出当前生效事件
  python core/event_cli.py status    # 当前事件修正分合计 + 生效数
  python core/event_cli.py prune     # 移除过期事件(写文件)

注意(2026-09-19 用户拍板): add/fedrate 落库的只是**台账**(events.jsonl),
页面读的是**快照**(dashboard.jsonl) —— 录完事件请顺手触发一次仪表盘刷新
(POST /api/market_position/dashboard/refresh 或本地 mdr.refresh_close),
否则事件跟踪页要等下个定时刷新点才显示。
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from core import market_events as me  # noqa: E402


def _cmd_add(args) -> int:
    # 去重: 同标题已在生效期则跳过
    if any(e["title"].strip() == args.title.strip() for e in me.list_active()):
        print(f"SKIP 重复事件(已生效): {args.title}")
        return 0
    e = me.add_event(level=args.level, title=args.title,
                     initial_score=args.score, start_date=args.date,
                     logic=args.logic, source=args.source)
    print(f"ADDED {e['id']} | {e['level_name']} | 分={args.score:+.1f} | {args.title}")
    return 0


def _cmd_list(_args) -> int:
    active = me.list_active()
    if not active:
        print("(无生效事件)")
        return 0
    for e in active:
        print(f"{e['id']} | {e['level_name']} | 分={e['score_now']:+.2f} "
              f"| 剩{e['days_left']}天 | {e['start_date']} | {e['title']}")
    return 0


def _cmd_fedrate(_args) -> int:
    from core import market_event_scan as ms
    r = ms.update_fed_rate_event()
    if not r["updated"]:
        print(f"FEDRATE SKIP: 拉取失败(美债收益率源不可用), 本次不更新。reason={r['reason']}")
        return 0
    ev = r["event"]
    p = r["proxy"]
    flag = "分数变动" if r["changed"] else "读数刷新(分数未变)"
    print(f"FEDRATE {flag}")
    print(f"  美债2年: {p['cur']}% (截至{p['cur_date']})  基准{p['base']}% ({p['base_date']})  "
          f"变动{p['delta_bp']:+.0f}bp  (源:{p['source']})")
    print(f"  事件分: {r['score']:+.2f}  事件id: {ev['id']}  标题: {ev['title']}")
    return 0


def _cmd_status(_args) -> int:
    total = sum(a["score_now"] for a in me.list_active())
    adj = max(-me.TOTAL_ADJ_LIMIT, min(me.TOTAL_ADJ_LIMIT, round(total, 4)))
    print(f"生效事件数={len(me.list_active())}  事件修正分合计={adj:+.2f} (上限±1.00)")
    return 0


def _cmd_remove(args) -> int:
    r = me.remove_event(args.id)
    if not r["found"]:
        print(f"NOT FOUND: {args.id}")
        return 1
    e = r["removed"]
    print(f"REMOVED {e['id']} | {e['level_name']} | 分={e['initial_score']:+.1f} | {e['title']}")
    return 0


def _cmd_prune(_args) -> int:
    r = me.daily_tick()
    print(f"已清理过期事件, 剩余生效={r['count']}, 合计修正分={r['total_adj']:+.2f}")
    if r["removed_ids"]:
        print("移除:", ", ".join(r["removed_ids"]))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="大盘事件 录入/查询 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="录入一条事件")
    a.add_argument("--level", required=True, choices=list(me.EVENT_LEVELS))
    a.add_argument("--title", required=True)
    a.add_argument("--score", type=float, required=True,
                   help="正负号表方向: 正=利好, 负=利空; 绝对值≤该档上限")
    a.add_argument("--date", required=True, help="事件发生日 YYYY-MM-DD")
    a.add_argument("--logic", default="")
    a.add_argument("--source", default="agent_scan")
    a.set_defaults(func=_cmd_add)

    sub.add_parser("list", help="列出当前生效事件").set_defaults(func=_cmd_list)
    sub.add_parser("status", help="事件修正分合计").set_defaults(func=_cmd_status)
    sub.add_parser("fedrate", help="刷新美联储利率预期跟踪事件").set_defaults(func=_cmd_fedrate)

    rm = sub.add_parser("remove", help="按 id 删除一条事件(纠错)")
    rm.add_argument("--id", required=True, help="事件 id, 如 evt_20260915_01")
    rm.set_defaults(func=_cmd_remove)
    sub.add_parser("prune", help="移除过期事件").set_defaults(func=_cmd_prune)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
