"""trade/asset_gapfill.py — 停机日资产补算 (2026-09-10)。

**问题** (2026-09-09 事件): VERA 没开机/没归档的交易日, `daily_asset` 缺行 →
分析页日历那格退化成「无成交」, 而**下一格**把缺失日的涨跌一起吞进去当单日
(9/10 那格显示 -3,365.60, 实为 9/9 的 +240.40 与 9/10 的 -3,606.00 之和)。

**算法** (大白话): 每天资产 = 现金 + 持仓股数 × 当日收盘价。程序没开机只是
"没人记账", 市场照常报价、现金也不会自己变 (除非停机期间有人手工买卖),
所以拿"最近一个有实测的日子"当锚点, 用缺口内的成交回放现金与股数、用每天的
收盘价逐日计价, 就能把缺的日子算出来 —— **一天、两天、十天都是同一个算法**。

**两条硬规矩**:

1. **两端夹逼**: 缺口后第一个实测行是尺子 —— 从推算结果继续推进到那天,
   预测值与实测值的差 = 残差。残差 0 最好; 在容差 (默认 0.5%) 内则写入并把
   残差**按日均摊** (否则误差会全砸在右端那格, 又是一次"单日数字含多日"的
   翻版); 超容差 → 拒写并报警 (实证: 8/31-9/1 两天停机, 差 1,847.20 ——
   停机期间在券商端手工买过 200 股 518880, 成交表里没有这一笔)。
2. **fail-closed**: 缺收盘价 / 右端无实测行 / 无锚点 → 不写该缺口, 如实报原因。
   宁可缺行, 不写错数 (与"行情故障宁可不卖"同一条铁律)。

**口径**: 持仓估值一律用**不复权**收盘价 (与实盘成本/市值同维度, 前复权
历史价在除权后与真实市值不可比)。税费/分红未计 (与 trade/ 模块 P2 口径一致)。

**写入**: 推算行带 `source='derived'`; 实测行 (`source='eod'`) 永不覆盖
(存储层还有一道保险, 见 `DailyAssetStore.save`)。

接口: `plan_gaps` (纯函数, 全部数学, 可用 dict 单测) /
`fill_gaps` (编排: 读库+写行+留痕) / `make_close_source` (取价降级链) /
`orchestrate_gapfill` (生产编排: 持仓快照组装+时段闸+调用 fill_gaps,
2026-09-15 自 trade_main 组合根下沉)。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from trade.book import DIRECTION_BUY
from utils.logger import get_logger

logger = get_logger(__name__)

#: 两端夹逼容差: 残差占左端实测资产的比例超过它 → 拒写 (只报警)。
TOLERANCE = 0.005

#: 取价窗口 (自然日): 逐日问价时按"首个请求日往前这么多天"整段取一次。
_PRICE_WINDOW_DAYS = 400


@dataclass(frozen=True)
class GapDay:
    """推算出来的一天 (准备写入 daily_asset 的一行)。"""
    date: str
    total_asset: float
    available: float          # 现金 (已含均摊的未记录资金调整)
    market_value: float
    adjust: float = 0.0       # 摊到该日的未记录资金调整 (0 = 两端夹逼分毫不差)


@dataclass(frozen=True)
class GapRun:
    """一个缺口 (连续缺失交易日) 的推算结果。"""
    left_date: str            # 左端实测行日期 (锚点)
    right_date: str | None    # 右端实测行日期 (尺子); None = 尾部缺口
    dates: tuple              # 缺失的交易日 (升序)
    days: tuple               # 计划写入的行; 拒写/跳过时为空
    residual: float           # 右端夹逼残差 = 预测 - 实测
    left_residual: float      # 左端诊断 = 反推持仓按左端收盘价算 - 左端实测市值
    status: str               # 'write' | 'reject' | 'skip'
    reason: str               # 大白话说明 (直接给界面/审计看)


# ────────────────────────── 纯计算 ──────────────────────────

def plan_gaps(rows, *, positions, trades_by_date, close_at, is_trading_day,
              today, tolerance: float = TOLERANCE) -> list[GapRun]:
    """算出每个缺口的推算计划 (**不写任何东西**)。

    rows: daily_asset 行 [{date,total_asset,available,market_value,source}, ...]
        —— 其中 source='derived' 的行**不作为锚点** (重跑时重算, 可被补录的
        成交修正); 缺口 = 两个实测行之间缺掉的交易日。
    positions: {code: 股数} 最新已知持仓 (QMT 实时持仓或最近一次 EOD 快照)。
    trades_by_date: {日期: [(code, direction, qty, amount), ...]} 成交回放源。
    close_at: callable(code, 'YYYY-MM-DD') -> 不复权收盘价 | None。
    is_trading_day: callable('YYYY-MM-DD') -> bool。
    today: 'YYYY-MM-DD'; **今天不补** —— 今天的实测由 15:05 EOD 负责,
        没归档就是还没到点或归档失败, 不能拿盘中价冒充日终。

    返回按日期升序的 GapRun 列表; 没有缺口返回 []。
    """
    anchors = sorted((_norm(r) for r in (rows or [])
                      if (r.get("source") or "eod") != "derived"),
                     key=lambda r: r["date"])
    if not anchors:
        return []                                  # 账户首月无锚点, 不推
    today_d = _parse(today)
    first_d = _parse(anchors[0]["date"])
    if today_d <= first_d:
        return []
    have = {r["date"] for r in anchors}
    # 候选时间轴: [首行, 今天) 内的「实测锚点 / 缺失交易日」 (非交易日不上轴)
    timeline: list[tuple[str, str]] = []
    cur = first_d
    while cur < today_d:
        ds = cur.isoformat()
        if ds in have:
            timeline.append(("anchor", ds))
        elif _safe_call(is_trading_day, ds):
            timeline.append(("missing", ds))
        cur += timedelta(days=1)

    runs: list[GapRun] = []
    by_date = {r["date"]: r for r in anchors}
    dates_sorted = [r["date"] for r in anchors]
    i = 0
    while i < len(timeline):
        kind, ds = timeline[i]
        if kind == "anchor":
            i += 1
            continue
        dates: list[str] = []
        while i < len(timeline) and timeline[i][0] == "missing":
            dates.append(timeline[i][1])
            i += 1
        # 左端 = 缺口前最后一个实测行; 右端 = 缺口后第一个实测行 (可能就在今天,
        # 今天的归档行不参与"待补日", 但正是它当尺子)。
        left = by_date[[d for d in dates_sorted if d < dates[0]][-1]]
        nxt = [d for d in dates_sorted if d > dates[-1]]
        runs.append(_plan_run(dates, left, by_date[nxt[0]] if nxt else None,
                              positions, trades_by_date, close_at, tolerance))
    return runs


def _plan_run(dates, left, right, positions, trades_by_date, close_at,
              tolerance) -> GapRun:
    """单个缺口: 反推左端持仓 → 逐日回放计价 → 右端夹逼 → 写/拒/跳。"""
    left_total = float(left["total_asset"])
    # 1) 反推左端持仓: 从最新持仓里把左端之后的成交倒回去
    #    (买入撤掉股数, 卖出补回股数) —— 停机期间的手工成交若没进成交表,
    #    这里会多出"幽灵持仓", 左端诊断会把它报出来。
    held = {c: int(q) for c, q in (positions or {}).items() if q}
    for ds in sorted(trades_by_date or {}):
        if ds <= left["date"]:
            continue
        for code, direction, qty, _amt in trades_by_date[ds]:
            held[code] = held.get(code, 0) + (-int(qty)
                                              if direction == DIRECTION_BUY
                                              else int(qty))
            if not held[code]:
                held.pop(code, None)
    # 2) 左端诊断 (解释残差从哪来, 不参与写入)
    left_mv, left_err = _value_at(held, left["date"], close_at)
    left_residual = 0.0 if left_err else round(left_mv - float(left["market_value"]), 2)

    # 3) 逐日回放: 成交动现金/股数, 收盘价动市值
    cash = float(left["available"])
    cur_held = dict(held)
    states: list[tuple[str, float, float]] = []
    for ds in dates:
        for code, direction, qty, amt in (trades_by_date or {}).get(ds, ()):
            qty = int(qty)
            if direction == DIRECTION_BUY:
                cash -= float(amt)
                cur_held[code] = cur_held.get(code, 0) + qty
            else:
                cash += float(amt)
                cur_held[code] = cur_held.get(code, 0) - qty
            if not cur_held[code]:
                cur_held.pop(code, None)
        mv, err = _value_at(cur_held, ds, close_at)
        if err:
            return _verdict("reject", left, right, dates, left_residual,
                            f"{err}, 该缺口不推算 (宁可不写, 不写错数)")
        states.append((ds, cash, mv))

    # 4) 右端夹逼: 从推算结果继续推进到右端实测日
    if right is None:
        return _verdict("skip", left, None, dates, left_residual,
                        "右端没有实测日 (今天还没归档), 等归档后再补")
    cash_r, held_r = cash, dict(cur_held)
    for code, direction, qty, amt in (trades_by_date or {}).get(right["date"], ()):
        qty = int(qty)
        if direction == DIRECTION_BUY:
            cash_r -= float(amt)
            held_r[code] = held_r.get(code, 0) + qty
        else:
            cash_r += float(amt)
            held_r[code] = held_r.get(code, 0) - qty
        if not held_r[code]:
            held_r.pop(code, None)
    right_mv, err = _value_at(held_r, right["date"], close_at)
    if err:
        return _verdict("reject", left, right, dates, left_residual,
                        f"{err}, 该缺口不推算 (宁可不写, 不写错数)")
    residual = round(cash_r + right_mv - float(right["total_asset"]), 2)
    limit = tolerance * max(left_total, 1.0)
    if abs(residual) > limit:
        pct = abs(residual) / max(left_total, 1.0)
        return _verdict(
            "reject", left, right, dates, left_residual,
            f"与右端实测 ({right['date']}) 对不上 ¥{residual:,.2f} "
            f"({pct:.2%} > 容差 {tolerance:.2%}), 不写数 —— 缺口内可能有"
            f"未记录的成交或资金变动, 补录后重跑即可",
            residual=residual)

    # 5) 容差内: 残差按日均摊进各日现金 (余数落最后一天, 保证合计精确),
    #    这样右端那格的单日涨跌仍是干净真值。
    n = len(states)
    per = round(-residual / n, 2) if n else 0.0
    adjusts = [per] * n
    if n:
        adjusts[-1] = round(-residual - per * (n - 1), 2)
    days = []
    for (ds, c, mv), adj in zip(states, adjusts):
        available = round(c + adj, 2)
        market_value = round(mv, 2)
        days.append(GapDay(date=ds, total_asset=round(available + market_value, 2),
                           available=available, market_value=market_value,
                           adjust=adj))
    notes = []
    if left_residual:
        notes.append(f"左端持仓回放比实测市值多 ¥{left_residual:,.2f}")
    if residual:
        notes.append(f"两端夹逼差 ¥{residual:,.2f} "
                     f"({residual / max(left_total, 1.0):+.2%}), 已按日均摊到各日")
    else:
        notes.append(f"两端夹逼一致 (右端 {right['date']} 分毫不差)")
    return GapRun(left_date=left["date"], right_date=right["date"],
                  dates=tuple(dates), days=tuple(days), residual=residual,
                  left_residual=left_residual, status="write",
                  reason="; ".join(notes))


def _verdict(status, left, right, dates, left_residual, reason,
             residual: float = 0.0) -> GapRun:
    return GapRun(left_date=left["date"],
                  right_date=right["date"] if right else None,
                  dates=tuple(dates), days=(), residual=residual,
                  left_residual=left_residual, status=status, reason=reason)


def _value_at(held, ds, close_at) -> tuple[float, str]:
    """持仓按 ds 当日不复权收盘价计价 → (市值, 错误说明)。缺价 → ("", 说明)。"""
    mv = 0.0
    for code, qty in sorted(held.items()):
        if not qty:
            continue
        px = _safe_close(close_at, code, ds)
        if px is None:
            return 0.0, f"缺 {code} 在 {ds} 的收盘价"
        mv += qty * px
    return mv, ""


def _safe_close(close_at, code, ds):
    """取价回调异常/脏值一律当"没价" (fail-closed, 不让行情故障炸穿补算)。"""
    try:
        px = close_at(code, ds)
    except Exception as e:                       # noqa: BLE001
        logger.warning("取收盘价异常 (%s %s): %s", code, ds, e)
        return None
    if px is None:
        return None
    try:
        px = float(px)
    except (TypeError, ValueError):
        return None
    return px if px > 0 and px == px else None


def _safe_call(fn, ds: str) -> bool:
    """交易日判定。日历抛异常 → 回落"周一到周五算交易日" (与 trade/monitor 的
    `is_trading_day_cached` 同款 fail-open)。**不能静默当"全不是交易日"** ——
    那会让补算一声不响什么都没干 (2026-09-10 接线测试抓到的正是这个)。"""
    try:
        return bool(fn(ds))
    except Exception as e:                       # noqa: BLE001
        logger.warning("交易日历异常 (%s), 回落周末判定: %s", ds, e)
        return _parse(ds).weekday() < 5


def _norm(r) -> dict:
    return {"date": r["date"], "total_asset": float(r["total_asset"]),
            "available": float(r["available"]),
            "market_value": float(r["market_value"]),
            "source": (r.get("source") or "eod")}


def _parse(ds: str):
    """'YYYY-MM-DD' → date (只按日粒度比较, 绝不用 datetime 混进 isoformat)。"""
    return datetime.strptime(ds, "%Y-%m-%d").date()


# ────────────────────────── 编排 (读库 + 写行 + 留痕) ──────────────────────────

def fill_gaps(store, *, positions, close_at, is_trading_day=None,
              today: str | None = None, tolerance: float = TOLERANCE,
              dry_run: bool = False) -> dict:
    """跑一轮补算。**fail-soft**: 任何异常都不抛给调用方 (交易链零影响)。

    返回报告 (也被 trade_main 落 audit 供界面读):
      {"ok", "dry_run", "tolerance", "written":[日期...], "runs":[{...}],
       "missing": {日期: {reason, residual, status}}, "error"}

    written = 本次(会)写入的推算日; dry_run=True 时只算不写。
    missing = 没补上的缺口日 → 界面据此把「无成交」改成「未归档·差¥X」。
    """
    report: dict = {"ok": False, "dry_run": bool(dry_run),
                    "tolerance": tolerance, "written": [], "runs": [],
                    "missing": {}, "error": ""}
    try:
        rows = store.daily_asset.get()
        today = today or datetime.now().strftime("%Y-%m-%d")
        anchors = [r for r in rows if (r.get("source") or "eod") != "derived"]
        trades = _trades_by_date(store, anchors[0]["date"]) if anchors else {}
        runs = plan_gaps(rows, positions=positions, trades_by_date=trades,
                         close_at=close_at,
                         is_trading_day=is_trading_day or _default_is_trading_day,
                         today=today, tolerance=tolerance)
        for run in runs:
            report["runs"].append(_run_view(run))
            if run.status == "write":
                for day in run.days:
                    if not dry_run:
                        store.daily_asset.save(day.date, day.total_asset,
                                               day.available, day.market_value,
                                               source="derived")
                    report["written"].append(day.date)
            else:
                for ds in run.dates:
                    report["missing"][ds] = {"reason": run.reason,
                                             "residual": run.residual,
                                             "status": run.status}
            _audit(store, run, dry_run)
        report["ok"] = bool(report["written"])
    except Exception as e:                       # noqa: BLE001
        logger.exception("停机日补算异常 (交易链不受影响)")
        report["error"] = f"{type(e).__name__}: {e}"
    return report


def _run_view(run: GapRun) -> dict:
    return {"dates": list(run.dates), "left_date": run.left_date,
            "right_date": run.right_date, "status": run.status,
            "reason": run.reason, "residual": run.residual,
            "left_residual": run.left_residual,
            "adjustment": round(sum(d.adjust for d in run.days), 2),
            "days": [{"date": d.date, "total_asset": d.total_asset,
                      "available": d.available, "market_value": d.market_value,
                      "adjust": d.adjust} for d in run.days]}


def _audit(store, run: GapRun, dry_run: bool) -> None:
    label = {"write": "已推算", "reject": "拒写", "skip": "跳过"}.get(run.status,
                                                                  run.status)
    head = ("补算停机日 " + "、".join(run.dates) + f" → {label}")
    try:
        store.write_audit(f"gapfill_{run.status}",
                          f"{head}: {run.reason}",
                          {**_run_view(run), "dry_run": bool(dry_run)})
    except Exception:                            # noqa: BLE001
        logger.warning("补算审计写入失败 (不影响补算)", exc_info=True)


def _trades_by_date(store, since_date: str) -> dict:
    """trades 表 → {日期: [(code, direction, qty, amount), ...]}。

    只取首个实测锚点之后的成交: 更早的成交已经体现在锚点的现金/持仓里,
    再回放会重复计。
    """
    out: dict[str, list] = {}
    lo = time.mktime(_parse(since_date).timetuple())
    ro = store.open_readonly()
    try:
        rows = ro.execute(
            "SELECT ts, code, direction, qty, amount FROM trades "
            "WHERE ts >= ? ORDER BY ts ASC", (lo,)).fetchall()
    finally:
        ro.close()
    for ts, code, direction, qty, amount in rows:
        ds = datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
        out.setdefault(ds, []).append((code, direction, qty, amount))
    return out


def _default_is_trading_day(ds: str) -> bool:
    from trade.monitor import is_trading_day_cached
    return bool(is_trading_day_cached(_parse(ds)))


# ────────────────────────── 取价 (QMT 主源 + TDX 兜底) ──────────────────────────

def make_close_source(gateway=None):
    """造一个 `close_at(code, 'YYYY-MM-DD') -> 不复权收盘价 | None` 回调。

    QMT 主源 (与持仓估值/成交价同源, 交易进程必然可用) → TDX 兜底。
    回调是"逐日问价", 故每个代码按首个请求日往前 400 天整段取一次并缓存。
    两级都拿不到 → 返回 None, 由 plan_gaps fail-closed 拒写该缺口。
    """
    cache: dict[str, dict[str, float]] = {}

    def _load(code: str, ds: str) -> dict[str, float]:
        if code in cache:
            return cache[code]
        start = (_parse(ds) - timedelta(days=_PRICE_WINDOW_DAYS)).strftime("%Y%m%d")
        got: dict[str, float] = {}
        if gateway is not None:
            try:
                got = gateway.query_daily_closes_range(code, start, "") or {}
            except Exception as e:               # noqa: BLE001
                logger.warning("QMT 取日线失败 (%s), 降级 TDX: %s", code, e)
        if not got:
            try:
                got = _tdx_closes(code, start)
            except Exception as e:               # noqa: BLE001
                logger.warning("TDX 取日线失败 (%s): %s", code, e)
                got = {}
        cache[code] = got
        return got

    def close_at(code: str, ds: str):
        try:
            return _load(code, ds).get(ds)
        except Exception:                        # noqa: BLE001
            logger.warning("取收盘价失败 (%s %s)", code, ds, exc_info=True)
            return None

    return close_at


def _tdx_closes(code: str, start: str) -> dict[str, float]:
    """TDX 兜底: 不复权(dividend_type='none') 日线 close → {日期: 价}。"""
    from core.data_fetcher import DataFetcher
    end = datetime.now().strftime("%Y%m%d")
    kl = DataFetcher.get_kline([code], start, end, period="1d",
                               dividend_type="none")
    s = (kl or {}).get("Close")
    if s is None or code not in getattr(s, "columns", []):
        return {}
    s = s[code].dropna()
    out: dict[str, float] = {}
    for idx, px in s.items():
        fmt = getattr(idx, "strftime", None)
        if fmt is None:
            continue
        try:
            px = float(px)
        except (TypeError, ValueError):
            continue
        if px == px and px > 0:
            out[idx.strftime("%Y-%m-%d")] = px
    return out


def orchestrate_gapfill(store, book, gateway, *, clock, is_continuous,
                        dry_run: bool = False, source: str = "startup") -> dict:
    """停机日补算生产编排 (2026-09-15 自 trade_main 组合根下沉, 纯移动不改行为)。

    组合根只留一行调用。持仓取 book 快照 (QMT 实时对账过的), 空了回落最近
    一次 EOD 持仓快照; 取价走 QMT 日线 (TDX 兜底)。**全链路 fail-soft** ——
    补算只为分析页补齐, 绝不牵动交易链; 人工触发 (非 startup) 限非连续竞价
    时段, is_continuous: callable → bool (由组合根注入时段判定)。
    """
    if source != "startup" and is_continuous():
        store.write_audit(
            "gapfill_skip", "连续竞价时段不补算 (收盘后 15:05 再点)", {})
        return {"ok": False, "error": "连续竞价时段不补算"}
    positions: dict[str, int] = {}
    try:
        positions = {c: int(p.volume)
                     for c, p in book.snapshot()["positions"].items()
                     if p.volume > 0}
    except Exception as e:                      # noqa: BLE001
        logger.warning("补算取持仓失败: %s", e)
    if not positions:
        try:
            positions = {c: int(v.get("volume", 0))
                         for c, v in store.load_position_snapshot().items()
                         if int(v.get("volume", 0)) > 0}
        except Exception as e:                  # noqa: BLE001
            logger.warning("补算取快照持仓失败: %s", e)
    report = fill_gaps(
        store, positions=positions,
        close_at=make_close_source(gateway),
        today=datetime.fromtimestamp(clock()).strftime("%Y-%m-%d"),
        dry_run=dry_run)
    if report.get("written"):
        logger.info("停机日补算 (%s): 写入 %s", source, report["written"])
    if report.get("error"):
        logger.warning("停机日补算异常: %s", report["error"])
    if source != "startup" and not report.get("runs"):
        try:
            store.write_audit(
                "gapfill_none", "停机日补算: 没有需要补的交易日",
                {"dry_run": bool(dry_run), "source": source})
        except Exception:                       # noqa: BLE001
            pass
    return report
