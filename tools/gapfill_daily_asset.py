"""tools/gapfill_daily_asset.py — 停机日资产补算 (命令行入口, 2026-09-10)。

VERA 没开机/没归档的交易日, `daily_asset` 缺行 → 分析页日历那格显示"无成交",
下一格还会把缺失日的涨跌一起吞进去当单日 (2026-09-09 事件)。本工具用
「最后一个实测锚点 + 缺口内成交回放 + 每日不复权收盘价」把缺的交易日推算出来。

与线上路径的关系:
    - 线上自动路径 = trade_main 启动时自动补 (trade_main._startup_catchup);
    - 线上手工路径 = 分析页「补算停机日」按钮 (走 8081 API, 消费者线程执行);
    - **本工具 = 离线路径** (trade_main 没在跑、或想先看预览再决定写不写)。
    三条路径共用同一个深模块 trade/asset_gapfill, 口径完全一致。

取价降级: TDX (core.data_fetcher, 不复权) → 东方财富 HTTP (stdlib, 免依赖)。

用法:
    python tools/gapfill_daily_asset.py                 # 预览 (默认 dry-run)
    python tools/gapfill_daily_asset.py --apply         # 实际写入
    python tools/gapfill_daily_asset.py --apply --tolerance 0.01 --today 2026-09-10
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from trade.asset_gapfill import TOLERANCE, fill_gaps  # noqa: E402
from trade.store import TradeStore  # noqa: E402


def _tdx_closes(code: str, start: str) -> dict:
    from core.data_fetcher import DataFetcher
    end = datetime.now().strftime("%Y%m%d")
    kl = DataFetcher.get_kline([code], start, end, period="1d",
                               dividend_type="none")
    s = (kl or {}).get("Close")
    if s is None or code not in getattr(s, "columns", []):
        return {}
    out = {}
    for idx, px in s[code].dropna().items():
        try:
            out[idx.strftime("%Y-%m-%d")] = float(px)
        except Exception:                            # noqa: BLE001
            continue
    return out


def _em_closes(code: str, start: str) -> dict:
    """东方财富日线 (不复权), 免依赖兜底。"""
    num, _, ex = code.partition(".")
    secid = ("1." if ex.upper() == "SH" else "0.") + num
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={secid}&fields1=f1,f2,f3&fields2=f51,f53"
           "&klt=101&fqt=0&end=20500101&lmt=1000")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
    out = {}
    for k in ((raw.get("data") or {}).get("klines") or []):
        p = k.split(",")
        if len(p) >= 2 and p[0] >= start[:4] + "-" + start[4:6]:
            try:
                out[p[0]] = float(p[1])
            except ValueError:
                continue
    return out


def make_close_source():
    """(code, date) → 不复权收盘价 | None: TDX 主源, 东财 HTTP 兜底, 逐码缓存。"""
    cache: dict[str, dict] = {}

    def _load(code: str, ds: str) -> dict:
        if code not in cache:
            start = (datetime.strptime(ds, "%Y-%m-%d")
                     - timedelta(days=400)).strftime("%Y%m%d")
            got = {}
            try:
                got = _tdx_closes(code, start)
            except Exception as e:                   # noqa: BLE001
                print(f"  [warn] TDX 取 {code} 失败: {e}")
            if not got:
                try:
                    got = _em_closes(code, start)
                except Exception as e:               # noqa: BLE001
                    print(f"  [warn] 东财取 {code} 失败: {e}")
            cache[code] = got
            print(f"  {code}: 取到 {len(got)} 根日线")
        return cache[code]

    return lambda code, ds: _load(code, ds).get(ds)


def main() -> int:
    ap = argparse.ArgumentParser(description="停机日资产补算 (缺归档的交易日)")
    ap.add_argument("--db", default=str(PROJECT_ROOT / "data" / "trade" / "trade.db"))
    ap.add_argument("--apply", action="store_true", help="实际写入 (默认只预览)")
    ap.add_argument("--tolerance", type=float, default=TOLERANCE,
                    help=f"两端夹逼容差 (占左端资产比例, 默认 {TOLERANCE})")
    ap.add_argument("--today", default="", help="YYYY-MM-DD (默认系统今天)")
    ap.add_argument("--raw-log", default="", help="原始回报 JSONL 路径 (默认同目录)")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"错误: 找不到 {db}")
        return 2
    raw_log = args.raw_log or str(db.parent / "gapfill_raw.jsonl")

    store = TradeStore(str(db), raw_log)
    try:
        snap = store.load_position_snapshot()
        positions = {c: int(v.get("volume", 0)) for c, v in (snap or {}).items()
                     if int(v.get("volume", 0)) > 0}
        print(f"库: {db}")
        print(f"锚点持仓 (最近一次 EOD 快照): "
              f"{ {c: q for c, q in positions.items()} }")
        print("取价:")
        report = fill_gaps(store, positions=positions,
                           close_at=make_close_source(),
                           today=args.today or None,
                           tolerance=args.tolerance,
                           dry_run=not args.apply)
    finally:
        store.close()

    print()
    if report.get("error"):
        print(f"!! 补算异常 (fail-soft): {report['error']}")
    if not report["runs"]:
        print("没有需要补的交易日 (每个交易日都有实测行, 或今天还没归档)。")
    for run in report["runs"]:
        status = {"write": "写入", "reject": "拒写", "skip": "跳过"}.get(
            run["status"], run["status"])
        print(f"[{status}] {run['left_date']} → {run['right_date'] or '(无右端实测)'} "
              f"缺: {'、'.join(run['dates'])}")
        print(f"        右端夹逼差 {run['residual']:+,.2f} / "
              f"左端持仓诊断 {run['left_residual']:+,.2f} / "
              f"资金调整 {run['adjustment']:+,.2f}")
        print(f"        说明: {run['reason']}")
        for day in run["days"]:
            print(f"        {day['date']}  总 {day['total_asset']:>14,.2f} "
                  f"现金 {day['available']:>14,.2f} 市值 {day['market_value']:>14,.2f}")
    print()
    print(f"{'预览 (未写入)' if report['dry_run'] else '已写入'}: "
          f"{report['written'] or '无'}")
    if report["missing"]:
        print(f"未补齐: {list(report['missing'])}")
        for ds, m in report["missing"].items():
            print(f"  {ds}: {m['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
