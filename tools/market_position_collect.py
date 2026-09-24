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
    ap.add_argument("--regime", action="store_true",
                    help="只打印牛熊区间与时长 (两条口径并列) + 指标体检")
    ap.add_argument("--validity", action="store_true", help="只打印指标体检 (维度有效性)")
    ap.add_argument("--md", metavar="PATH", default=None, help="把体温表 Markdown 写到该文件")
    args = ap.parse_args(argv)

    if args.mirror or args.shadow or args.regime or args.validity:
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
        print(f"基准日 {r['asof']} —— 今天最像历史上这 {s.get('n')} 天"
              f"(距离最近的一档 = 前 {(r.get('band_quantile') or 0) * 100:.0f}%;"
              f" 可当参照的历史日 {r.get('eligible')} 天,"
              f" 已排除最近 {r.get('exclude_recent')} 个交易日):")
        for m in r.get("matches") or []:
            print(f"  {m['date']}  距离 {m['distance']}  "
                  f"之后20日(沪深300) {m.get('fwd_20_hs300_pct')}%  "
                  f"之后60日 {m.get('fwd_60_hs300_pct')}%")
        print(f"\n汇总(基于分位带 {s.get('n')} 天): "
              f"20日中位数 {s.get('fwd_20_median')}%, "
              f"平均 {s.get('fwd_20_mean')}%, "
              f"上涨占比 {s.get('fwd_20_up_ratio')}%; "
              f"60日中位数 {s.get('fwd_60_median')}%")
        print(f"有效独立样本: 20日约 {s.get('n_eff_20')} 份 / "
              f"60日约 {s.get('n_eff_60')} 份 (命中日高度重叠, 不能按天数读)")
        if r.get("years"):
            print("\n按年份拆解:")
            for y in r["years"]:
                print(f"  {y['year']} 年  {y['n']:>3} 天  "
                      f"之后20日中位 {y['median_pct']}%  "
                      f"上涨占比 {y['up_ratio_pct']}%")
        print(f"\n{r.get('warning')}")
    if args.shadow:
        r = mpr.shadow_replay()
        if not r.get("ok"):
            print(f"影子回放不可用: {r.get('reason')}")
            return 1
        print(f"影子回放 {r['start']} ~ {r['end']}  口径: {r['caliber']}")
        print(f"{'规则':<16}{'毛年化':>9}{'净年化':>9}{'净95%区间':>18}"
              f"{'净回撤':>9}{'建仓次数':>9}{'在场占比':>9}")
        for row in (r.get("rows") or []) + [r.get("buy_hold") or {}]:
            if not row:
                continue
            net = row.get("net") or {}
            ci = (f"{net.get('ci_low_pct')} ~ {net.get('ci_high_pct')}"
                  if net.get("ci_low_pct") is not None else "【缺】")
            print(f"{str(row.get('rule')):<16}"
                  f"{str(row.get('annualized_pct')):>9}"
                  f"{str(net.get('annualized_pct')):>9}{ci:>18}"
                  f"{str(net.get('max_drawdown_pct')):>9}"
                  f"{str(row.get('round_trips')):>9}"
                  f"{str(row.get('exposure_pct')):>9}")
        print("\n双窗口一致性 (前一半 / 后一半, 同向才算数):")
        for row in (r.get("rows") or []) + [r.get("buy_hold") or {}]:
            if not row:
                continue
            w = row.get("windows") or {}
            print(f"  {str(row.get('rule')):<16}"
                  f"前 {(w.get('in') or {}).get('annualized_pct')}%  "
                  f"后 {(w.get('out') or {}).get('annualized_pct')}%  "
                  f"{'同向' if w.get('consistent') else '不一致(待复核)'}")
        c = r.get("cost") or {}
        if c:
            print(f"\n成本口径: 单次往返 {c.get('round_trip_pct')}% (佣金+印花税), "
                  f"未计滑点; 含滑点则 {c.get('round_trip_with_slippage_pct')}%")
        print("\n措辞纪律: 净口径 95% 区间跨过 0 → 只能说「看不出显著的优势或劣势」, "
              "不写「无效」也不写「跑输」。")
    if args.regime:
        rg = mpr._regime_all()
        cn = {"bull": "牛", "bear": "熊", "range": "震荡"}
        for key, name, code in mpr.INDEX_SPECS:
            it = rg.get(key) or {}
            print(f"\n=== {name}({code}) ===")
            for ck in ("ma250", "pct20"):
                s = it.get(ck)
                if not s:
                    print("  【缺】")
                    continue
                print(f"  {s['caliber']}: 现在={cn.get(s['state'], s['state'])} "
                      f"起点={s['since']} 已走 {s['months']} 月 涨跌 {s['ret_pct']}%"
                      f" | 历史同状态 {s['n_same_state']} 段, 中位 {s['median_months']} 月"
                      f" / {s['median_ret_pct']}%, 月数百分位 {s['months_percentile']}%"
                      f" (全部 {s['n_episodes']} 段, {s['flips_per_year']} 段/年)")
                if s["flicker_note"]:
                    print(f"    ⚠ {s['flicker_note']}")
                for r in s["longest_rows"]:
                    print(f"      {r['start']} ~ {r['end']}  {r['months']:>5} 月  "
                          f"{r['ret_pct']:>7}%")
    if args.validity:
        r = mpr._dimension_validity()
        if not r.get("ok"):
            print(f"指标体检不可用: {r.get('reason')}")
            return 1
        print(f"指标体检: 月频 {r['n_months']} 个月 ({r['start']} ~ {r['end']}), "
              f"共检验 {r['n_tests']} 个组合 / {r['n_families']} 个族")
        print(f"\n{'指标':<20}{'族':<8}{'持有期':<8}{'n':>5}{'N_eff':>7}"
              f"{'rho':>8}{'p':>9}{'前段':>8}{'后段':>8}{'五分位差':>10}  判定")
        for row in r["rows"]:
            print(f"{row['name']:<20}{row['family']:<8}{row['horizon']:<8}{row['n']:>5}"
                  f"{row['n_eff']:>7}{str(row['rho']):>8}{str(row['p']):>9}"
                  f"{str(row['rho_in']):>8}{str(row['rho_out']):>8}"
                  f"{str(row['quintile_spread_pct']):>10}  {row['verdict']}")
        print("\n诚实限制:")
        for x in r["limitations"]:
            print("  *", x)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
