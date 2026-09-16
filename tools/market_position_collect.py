"""tools/market_position_collect.py — 大盘位置采集 CLI (2026-09-17)。

一条命令跑通「算指标 → 落连续录像 → 出体温表 → 推飞书」。

用法:
    python tools/market_position_collect.py                 日常采集(近300交易日) + 打印体温表
    python tools/market_position_collect.py --push          采集后推飞书体温表
    python tools/market_position_collect.py --push-only     不采集, 直接推当前最新一条
    python tools/market_position_collect.py --backfill      全量历史回填(约10年, 让照镜子可用)
    python tools/market_position_collect.py --mirror        只打印「历史照镜子」
    python tools/market_position_collect.py --shadow        只打印择时规则影子回放
    python tools/market_position_collect.py --md 体温.md    把体温表写成文件(不发飞书)

口径提醒: 采集读本地日线缓存 (data/kline_cache/1d), 不联网、不触发补拉。
调度器每交易日 15:45 已先刷缓存, 本工具排在它之后 (15:50)。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import market_position_runner as mpr  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python tools/market_position_collect.py",
        description="大盘位置: 采集指标落连续录像 + 生成/推送体温表")
    ap.add_argument("--backfill", action="store_true",
                    help="全量历史回填 (bars=0, 约 10 年; 首次必跑, 否则照镜子没有参照系)")
    ap.add_argument("--bars", type=int, default=None,
                    help=f"采集窗口 (交易日)。默认日常 {mpr.DEFAULT_BARS}, --backfill 时 0=全量")
    ap.add_argument("--push", action="store_true", help="采集后把体温表推飞书")
    ap.add_argument("--push-only", action="store_true", help="不采集, 只推当前最新一条")
    ap.add_argument("--mirror", action="store_true", help="只打印历史照镜子结果")
    ap.add_argument("--shadow", action="store_true", help="只打印择时规则影子回放")
    ap.add_argument("--md", metavar="PATH", default=None, help="把体温表 Markdown 写到该文件")
    args = ap.parse_args(argv)

    if args.mirror or args.shadow:
        return _show_only(args)

    if not args.push_only:
        bars = args.bars if args.bars is not None else (
            mpr.BACKFILL_BARS if args.backfill else mpr.DEFAULT_BARS)
        print(f"采集: 窗口 bars={bars} "
              f"({'全量历史' if bars == 0 else str(bars) + ' 个交易日'}) …")
        res = mpr.collect(bars=bars, write=True)
        if not res.get("ok"):
            print(f"采集失败: {res.get('reason')}")
            return 1
        print(f"完成: 数据日期 {res['asof']} (应有 {res['expected']})"
              f"{' 【数据滞后】' if res['stale'] else ''}; "
              f"本次算 {res['records']} 条, 连续录像共 {res['lines']} 条 "
              f"→ {mpr.DAILY_PATH}")

    rec = mpr.latest()
    if rec is None:
        print("没有连续录像可出体温表 (先跑一次采集)")
        return 1

    md_path = None
    if args.md:
        md_path = Path(args.md)
        md_path.write_text(mpr.thermometer_md(rec), encoding="utf-8")
        print(f"体温表已写入 {md_path}")
    else:
        print("\n" + "=" * 72 + "\n")
        print(mpr.thermometer_md(rec))

    if args.push or args.push_only:
        r = mpr.push_thermometer(rec)
        print(f"\n飞书推送: {r}")
        if not r.get("ok"):
            return 1
    return 0


def _show_only(args) -> int:
    """--mirror / --shadow: 只读输出, 不采集不推送。"""
    if args.mirror:
        r = mpr.mirror()
        if not r.get("ok"):
            print(f"照镜子不可用: {r.get('reason')}")
            return 1
        s = r.get("summary") or {}
        print(f"基准日 {r['asof']} —— 今天最像历史上这 {s.get('n')} 天:")
        for m in r.get("matches") or []:
            print(f"  {m['date']}  距离 {m['distance']}  "
                  f"之后20日(沪深300) {m.get('fwd_20_hs300_pct')}%  "
                  f"之后60日 {m.get('fwd_60_hs300_pct')}%")
        print(f"\n汇总: 20日中位数 {s.get('fwd_20_median')}%, "
              f"上涨占比 {s.get('fwd_20_up_ratio')}%; "
              f"60日中位数 {s.get('fwd_60_median')}%")
        print(f"\n{r.get('warning')}")
    if args.shadow:
        r = mpr.shadow_replay()
        if not r.get("ok"):
            print(f"影子回放不可用: {r.get('reason')}")
            return 1
        print(f"影子回放 {r['start']} ~ {r['end']}  口径: {r['caliber']}")
        print(f"{'规则':<16}{'年化':>10}{'最大回撤':>10}{'夏普':>8}{'卡玛':>8}{'持仓占比':>10}")
        for row in (r.get("rows") or []) + [r.get("buy_hold") or {}]:
            if not row:
                continue
            print(f"{str(row.get('rule')):<16}{str(row.get('annualized_pct')):>10}"
                  f"{str(row.get('max_drawdown_pct')):>10}{str(row.get('sharpe')):>8}"
                  f"{str(row.get('calmar')):>8}{str(row.get('exposure_pct')):>10}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
