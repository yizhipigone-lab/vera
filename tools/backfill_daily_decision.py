#!/usr/bin/env python
"""tools/backfill_daily_decision.py — 决策台账历史回填 CLI (2026-09-18)。

一次性动作, 不进运行时。把 2026 年 7 月 27 日以来的历史决策从 ``audit`` 表翻出来,
补进 ``daily_decision`` 台账 —— 这样点开交易记录页的决策日历, 8 月的每一天都能
看到"当时为什么动 / 为什么没动"。

用法
----
    # 先看一眼 (不落库), 确认要写多少行
    python tools/backfill_daily_decision.py --from 2026-07-27 --to today --dry-run

    # 真回填 (建议非交易时段跑)
    python tools/backfill_daily_decision.py --from 2026-07-27 --to today

    # 换库 (默认 data/trade/trade.db, 与 trade/config.py 的 db_path 一致)
    python tools/backfill_daily_decision.py --db /path/to/trade.db

两个要点
--------
1. **可重复执行**: 主键 ``(日期, 策略, 对象)`` 天然幂等, 中断了直接再跑一遍。
2. **绝不覆盖当场记录的行**: 写入口按来源可信度保护, ``backfill_*`` / ``inferred``
   改不动 ``live``。所以就算在交易时段跑, 也不会把今天上午的真实决策改成推断。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade.decision_backfill import LEDGER_START, run  # noqa: E402

_DEFAULT_DB = "data/trade/trade.db"
_DEFAULT_API = "http://127.0.0.1:8081"


def _resolve_api_base(cli_value: str | None) -> str:
    """探活地址的解析顺序 (2026-09-20 审计 P3-4)。

    病根: 地址原先只有"命令行缺省 8081"一条来源 —— 谁把 `trade_main --api-port`
    改了, 探针照旧捅 8081, 那里没人 listen → 判"进程已死" → **fail-open** 直接
    并发写 trade.db (第二写者铁律破口)。现在按"显式 > 环境 > trade 配置 > 默认"
    解析, 配置改了它跟着改:
      ① `--api-base` 显式给 → 用它 (最高优先);
      ② 环境变量 `VERA_TRADE_API_BASE` (部署脚本可注入);
      ③ `.env` 的 `VERA_TRADE_API_PORT` / `TRADE_API_PORT` (纯 stdlib 读取, 不拉
         trade 包依赖 —— 本工具是离线 CLI, 不该为读一个端口 import 交易栈);
      ④ 兜底 8081 (`trade_main` 的 argparse 缺省值)。
    """
    import os
    if cli_value:
        return cli_value
    env_base = os.environ.get("VERA_TRADE_API_BASE")
    if env_base:
        return env_base
    for key in ("VERA_TRADE_API_PORT", "TRADE_API_PORT"):
        port = (os.environ.get(key) or "").strip()
        if port.isdigit():
            return f"http://127.0.0.1:{port}"
    env_file = Path(__file__).resolve().parents[1] / ".env"
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k == "VERA_TRADE_API_BASE" and v:
                    return v
                if k in ("VERA_TRADE_API_PORT", "TRADE_API_PORT") and v.isdigit():
                    return f"http://127.0.0.1:{v}"
        except OSError:
            pass
    return _DEFAULT_API


def _trade_api_alive(base: str = _DEFAULT_API, timeout: float = 1.5) -> bool:
    """交易进程 (8081) 是否活着 —— 2026-09-19 批次 4.3 的并发守卫探针。

    背景(架构审查 P0-5): 本 CLI 直写 daily_decision 表, 是 trade.db 的**第二
    写者**, 与运行中的 trade_main (唯一写者铁律) 并发, 原来只靠 WAL busy 重试
    兜底。现在活进程在跑就拒绝执行 (除非 --force)。

    判据用 **TCP 连通性** (端口有没有人 listen), 不要 HTTP 请求:
      ① 本函数只回答"进程在不在", 更简单的判据更可靠;
      ② tests/conftest.py 会 session 级焊死 urllib.request.urlopen (假 200),
         走 HTTP 的话测试根本测不出真实行为 (实测踩坑: 死端口被判成 True)。

    2026-09-20 审计 P3-4: 地址不再写死 —— 由 `_resolve_api_base()` 解析
    (显式 → 环境 → trade 配置 → 8081), 端口改了守卫跟着改, 不 fail-open。
    """
    u = urlparse(base if "://" in base else "http://" + base)
    host = u.hostname or "127.0.0.1"
    port = u.port or 8081
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _parse_day(text: str, *, default: str) -> str:
    s = (text or "").strip()
    if not s:
        return default
    if s.lower() == "today":
        return _dt.date.today().isoformat()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return _dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    raise SystemExit(f"日期看不懂: {text!r}（要 2026-07-27 或 20260727 这种写法）")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="把历史决策从审计表回填进 daily_decision 台账（可重复执行）")
    ap.add_argument("--from", dest="start", default=LEDGER_START,
                    help=f"起点日期（缺省 {LEDGER_START}，即台账能回的最早一天）")
    ap.add_argument("--to", dest="end", default="today",
                    help="终点日期（缺省 today）")
    ap.add_argument("--db", default=_DEFAULT_DB, help=f"数据库路径（缺省 {_DEFAULT_DB}）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只还原、不落库（先看看会写多少行）")
    ap.add_argument("--reset", action="store_true",
                    help="先删掉区间内旧的回填行再写（改了回填逻辑要重做时用；"
                         "当场记录的行永远不删）")
    ap.add_argument("--overwrite-live", action="store_true",
                    help="连当场记录的行也覆盖（修数据用，平时别开）")
    ap.add_argument("--api-base", default=None,
                    help="交易进程地址（探活用；缺省按 VERA_TRADE_API_BASE / "
                         "VERA_TRADE_API_PORT / .env / 8081 依次解析）")
    ap.add_argument("--force", action="store_true",
                    help="交易进程在跑也照样写（危险：绕过第二写者守卫，"
                         "仅在你确信当前无委托/无成交时用）")
    args = ap.parse_args(argv)
    api_base = _resolve_api_base(args.api_base)

    # 2026-09-19 批次 4.3: 活进程守卫 —— daily_decision 直写是 trade.db 的
    # 第二写者, 与 trade_main (唯一写者) 并发有风险。dry-run 只读, 不拦。
    if not args.dry_run and not args.force and _trade_api_alive(api_base):
        print(f"[!] 交易进程活着 ({api_base} 有响应), 拒绝直写 trade.db:"
              "\n    它是「QMT/账本唯一写者」铁律下的另一个进程, 并发写有风险。"
              "\n    做法二选一: ①收盘后停掉 trade_main 再跑本命令;"
              "\n                ②--dry-run 先看结果 (只读不写)。"
              "\n    确实要带病写: 加 --force (自担风险)。")
        return 3

    today = _dt.date.today().isoformat()
    start = _parse_day(args.start, default=LEDGER_START)
    end = _parse_day(args.end, default=today)
    db = Path(args.db)
    if not db.exists():
        print(f"[!] 数据库不存在: {db}")
        return 2

    mode = "试算（不落库）" if args.dry_run else "回填"
    print(f"[{mode}] {db}")
    print(f"  区间: {start} ~ {end}")
    if args.overwrite_live:
        print("  [!] --overwrite-live 开着: 含 live 行也会被覆盖")
    if args.reset:
        print("  [!] --reset 开着: 区间内的旧回填行会先删掉（当场记录的行不删）")

    try:
        result = run(str(db), start, end, dry_run=args.dry_run,
                     overwrite_live=args.overwrite_live, reset=args.reset)
    except ValueError as e:
        print(f"[!] 参数不对: {e}")
        return 2

    print(f"  交易日里有记录的天数: {result['days']}")
    print(f"  还原出的台账行数: {result['rows']}")
    print(f"  台账格数（按“某天×某策略×某对象”去重）: {result['keys']}"
          "  ← 这就是页面上能看到的记录条数")
    if args.dry_run:
        print("  没有写库（去掉 --dry-run 才真写）")
    else:
        if result.get("removed"):
            print(f"  先删掉了旧回填行: {result['removed']} 条")
        print(f"  实际写入行数: {result['written']}"
              "（被拒的行 = 已有更可信的记录，属于正常）")

    per_day = result["per_day"]
    if per_day:
        days = sorted(per_day)
        print(f"\n  逐日行数（前 5 天 / 后 5 天，共 {len(days)} 天）:")
        shown = days[:5] + (["..."] if len(days) > 10 else []) + days[-5:]
        last = None
        for d in shown:
            if d == "...":
                print("    ...")
                continue
            if d == last:
                continue
            last = d
            print(f"    {d}  {per_day[d]:2d} 行")

    if not args.dry_run:
        print("\n  下一步: 打开交易记录页 → 顶部「决策日历」翻到 8 月，"
              "点任意一天看原因。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
