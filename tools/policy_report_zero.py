"""tools/policy_report_zero.py — 零号报告: 持仓/交易 × 十五五档位复盘 (纯统计, 无 LLM)。

P3 阶段复盘报告的最小原型 (讨论稿"零号报告"方案): 只用现成原料 ——
trade.db 成交记录 + A 层 policy_kb 行业档位, FIFO 配对出已平仓战绩。
零 LLM / 零爬虫 / 零新依赖, 全程只读。

口径铁律 (计划书 M-9): 报告只允许统计陈述 + 引用具体成交/日期,
禁模糊归因; 每条统计必须带样本量, n < min_n 的档位标"样本不足"。

用法:
    python tools/policy_report_zero.py [--db data/trade/trade.db]
        [--out output/policy_report] [--min-n 5] [--since YYYYMMDD]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 仓库根, 供 trade/policy_kb 导入

from trade.book import DIRECTION_BUY, DIRECTION_SELL  # 单一来源, 不写第二份定义

_TIERS = ("P1", "P2", "P3", "AVOID", "UNTAGGED")
_PRIORITY_JSON = Path(__file__).resolve().parent.parent / "policy_kb" / "tongdaxin_priority.json"


# ── 数据结构 ─────────────────────────────────────────────────

@dataclass
class ClosedLot:
    """一笔已平仓 (FIFO 配对的买→卖)。"""
    code: str
    qty: int
    buy_price: float
    sell_price: float
    buy_ts: float
    sell_ts: float

    @property
    def pnl(self) -> float:
        return (self.sell_price - self.buy_price) * self.qty

    @property
    def ret(self) -> float:
        return self.sell_price / self.buy_price - 1.0

    @property
    def hold_days(self) -> int:
        return max(0, int((self.sell_ts - self.buy_ts) / 86400))


@dataclass
class OpenLot:
    """一笔记在持 (FIFO 配对后剩余的买入)。"""
    code: str
    qty: int
    price: float
    ts: float


@dataclass
class PairResult:
    closed: list[ClosedLot] = field(default_factory=list)
    open_lots: list[OpenLot] = field(default_factory=list)
    unmatched_sell_qty: int = 0  # 无买可配的卖出 (DB 历史起点前的底仓)


# ── 数据加载 ─────────────────────────────────────────────────

def load_trades(db_path: str | Path, since: str | None = None) -> list[dict]:
    """只读连 trade.db 拉全部成交 (WAL 下 mode=ro 与写连接不互堵)。

    since: 可选 'YYYYMMDD', 只取该日 (含) 之后的成交。
    """
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        sql = "SELECT code, direction, price, qty, ts FROM trades"
        params: tuple = ()
        if since:
            day_start = dt.datetime.strptime(since, "%Y%m%d").timestamp()
            sql += " WHERE ts >= ?"
            params = (day_start,)
        sql += " ORDER BY ts"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [{"code": c, "direction": d, "price": p, "qty": q, "ts": t}
            for c, d, p, q, t in rows]


def pair_fifo(trades: list[dict]) -> PairResult:
    """按票 FIFO 配对买卖。卖出超过持仓的部分记 unmatched (底仓早于 DB 起点)。"""
    books: dict[str, deque] = {}
    res = PairResult()
    for t in trades:
        code = t["code"]
        book = books.setdefault(code, deque())
        if t["direction"] == DIRECTION_BUY:
            book.append(OpenLot(code=code, qty=t["qty"], price=t["price"], ts=t["ts"]))
        elif t["direction"] == DIRECTION_SELL:
            remain = t["qty"]
            while remain > 0 and book:
                lot = book[0]
                take = min(lot.qty, remain)
                res.closed.append(ClosedLot(
                    code=code, qty=take, buy_price=lot.price,
                    sell_price=t["price"], buy_ts=lot.ts, sell_ts=t["ts"]))
                lot.qty -= take
                remain -= take
                if lot.qty == 0:
                    book.popleft()
            res.unmatched_sell_qty += remain
    for book in books.values():
        res.open_lots.extend(book)
    return res


def load_tiers(codes: list[str]) -> dict[str, dict] | None:
    """票 → {tier, sector_code, sector_name}。失败返 None (松耦合, 全记 UNTAGGED)。

    复用 A 层现成件: build_stock_sector_index (票→行业) + tongdaxin_priority.json
    (行业→档位/名称), 不写第二份规则定义。
    """
    try:
        from policy_kb.build_sector_index import build_stock_sector_index
        index = build_stock_sector_index()
        with open(_PRIORITY_JSON, "r", encoding="utf-8") as f:
            sectors = {s["code"]: s for s in json.load(f).get("sectors", [])}
        out: dict[str, dict] = {}
        for code in codes:
            sc = index.get(code)
            s = sectors.get(sc) if sc else None
            out[code] = {
                "tier": s["priority"] if s else "UNTAGGED",
                "sector_code": sc or "",
                "sector_name": s.get("name", "") if s else "",
            }
        return out
    except Exception as e:  # 松耦合: TDX 不在线/JSON 缺失都不阻塞报告
        print(f"[warn] 行业标签不可用, 全部记 UNTAGGED: {e}", file=sys.stderr)
        return None


# ── 报告生成 ─────────────────────────────────────────────────

def _fmt_day(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _money(x: float) -> str:
    return f"{x:,.0f}"


def _tier_of(tiers: dict[str, dict] | None, code: str) -> str:
    return tiers[code]["tier"] if tiers else "UNTAGGED"


def _stats_lines(rows: list[tuple[str, list[ClosedLot]]], min_n: int) -> list[str]:
    """规律候选: 仅 n≥min_n 的档位给统计句, 其余明说样本不足。"""
    lines: list[str] = []
    qualified: list[tuple[str, int, float]] = []  # (tier, n, win_rate)
    for tier, lots in rows:
        n = len(lots)
        if n < min_n:
            lines.append(f"- **{tier}**: 样本不足 (n={n} < {min_n}), 不下结论。")
            continue
        wins = sum(1 for l in lots if l.pnl > 0)
        wr = wins / n
        avg_ret = sum(l.ret for l in lots) / n
        avg_days = sum(l.hold_days for l in lots) / n
        lines.append(
            f"- **{tier}**: 胜率 {_pct(wr)} ({wins}/{n}), "
            f"平均收益 {_pct(avg_ret)}, 平均持有 {avg_days:.0f} 天。")
        qualified.append((tier, n, wr))
    if len(qualified) >= 2:
        best = max(qualified, key=lambda x: x[2])
        worst = min(qualified, key=lambda x: x[2])
        if best[0] != worst[0]:
            lines.append(
                f"- 档位对比: {best[0]} 胜率 {_pct(best[2])} (n={best[1]}) "
                f"vs {worst[0]} {_pct(worst[2])} (n={worst[1]}); "
                f"样本量见各行, 差异是否稳定需更多成交验证。")
    else:
        lines.append(f"- 满足 n≥{min_n} 的档位不足两个, 不做档位对比结论。")
    return lines


def build_report(res: PairResult, tiers: dict[str, dict] | None,
                 min_n: int, db_path: str, since: str | None) -> str:
    """生成 markdown 报告。四块: 仓位体检 / 分档战绩 / 偏离清单 / 规律候选。"""
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    L: list[str] = [
        "# 零号报告：持仓/交易 × 十五五档位复盘",
        "",
        f"> 生成: {now} | 数据: `{db_path}` | 区间: {('自 ' + since) if since else '全部成交'}",
        "> 口径: 纯统计陈述 + 引用具体成交 (M-9, 无归因叙事); FIFO 配对, 已平仓才计战绩。",
    ]
    if tiers is None:
        L.append("> ⚠ 行业标签不可用 (松耦合降级), 全部按 UNTAGGED 统计。")
    if res.unmatched_sell_qty:
        L.append(f"> ⚠ 有 {res.unmatched_sell_qty} 股卖出无买入可配 "
                 f"(底仓早于 DB 起点), 未计入战绩。")
    L.append("")

    # ── 1. 仓位体检 (在持) ──
    L += ["## 1. 仓位体检（在持）", ""]
    if not res.open_lots:
        L += ["当前无在持仓位。", ""]
    else:
        by_tier: dict[str, list[OpenLot]] = {}
        for lot in res.open_lots:
            by_tier.setdefault(_tier_of(tiers, lot.code), []).append(lot)
        total_cost = sum(l.qty * l.price for l in res.open_lots)
        L += ["| 档位 | 票数 | 成本市值(元) | 占比 |", "|---|---|---|---|"]
        for tier in _TIERS:
            lots = by_tier.get(tier)
            if not lots:
                continue
            cost = sum(l.qty * l.price for l in lots)
            codes = {l.code for l in lots}
            L.append(f"| {tier} | {len(codes)} | {_money(cost)} | "
                     f"{_pct(cost / total_cost) if total_cost else '-'} |")
        L += ["", "| 代码 | 行业 | 档位 | 数量 | 成本价 | 成本市值(元) | 买入日 |",
              "|---|---|---|---|---|---|---|"]
        for lot in sorted(res.open_lots, key=lambda l: l.ts):
            t = tiers.get(lot.code, {}) if tiers else {}
            L.append(f"| {lot.code} | {t.get('sector_name', '-')} | "
                     f"{_tier_of(tiers, lot.code)} | {lot.qty} | {lot.price:.3f} | "
                     f"{_money(lot.qty * lot.price)} | {_fmt_day(lot.ts)} |")
        L.append("")

    # ── 2. 分档战绩 (已平仓) ──
    L += ["## 2. 分档战绩（已平仓）", ""]
    if not res.closed:
        L += ["无已平仓成交。", ""]
    else:
        by_tier_c: dict[str, list[ClosedLot]] = {}
        for lot in res.closed:
            by_tier_c.setdefault(_tier_of(tiers, lot.code), []).append(lot)
        L += ["| 档位 | n | 胜率 | 平均收益 | 平均持有(天) | 合计盈亏(元) |",
              "|---|---|---|---|---|---|"]
        for tier in _TIERS:
            lots = by_tier_c.get(tier)
            if not lots:
                continue
            n = len(lots)
            wr = sum(1 for l in lots if l.pnl > 0) / n
            avg_ret = sum(l.ret for l in lots) / n
            avg_days = sum(l.hold_days for l in lots) / n
            pnl = sum(l.pnl for l in lots)
            L.append(f"| {tier} | {n} | {_pct(wr)} | {_pct(avg_ret)} | "
                     f"{avg_days:.0f} | {_money(pnl)} |")
        n_all = len(res.closed)
        wr_all = sum(1 for l in res.closed if l.pnl > 0) / n_all
        pnl_all = sum(l.pnl for l in res.closed)
        L.append(f"| **合计** | {n_all} | {_pct(wr_all)} | - | - | {_money(pnl_all)} |")
        L.append("")

    # ── 3. 偏离清单 (P3/AVOID) ──
    L += ["## 3. 偏离清单（P3/AVOID 档）", ""]
    dev_closed = [l for l in res.closed if _tier_of(tiers, l.code) in ("P3", "AVOID")]
    dev_open = [l for l in res.open_lots if _tier_of(tiers, l.code) in ("P3", "AVOID")]
    if not dev_closed and not dev_open:
        L += ["无 P3/AVOID 档的已平仓或在持记录。", ""]
    else:
        if dev_closed:
            L += ["### 已平仓", "",
                  "| 代码 | 行业 | 档位 | 买入日 | 卖出日 | 持有(天) | 收益率 | 盈亏(元) |",
                  "|---|---|---|---|---|---|---|---|"]
            for l in sorted(dev_closed, key=lambda x: x.sell_ts):
                t = tiers.get(l.code, {}) if tiers else {}
                L.append(f"| {l.code} | {t.get('sector_name', '-')} | "
                         f"{_tier_of(tiers, l.code)} | {_fmt_day(l.buy_ts)} | "
                         f"{_fmt_day(l.sell_ts)} | {l.hold_days} | {_pct(l.ret)} | "
                         f"{_money(l.pnl)} |")
            L.append("")
        if dev_open:
            L += ["### 在持", "",
                  "| 代码 | 行业 | 档位 | 数量 | 成本价 | 成本市值(元) | 买入日 |",
                  "|---|---|---|---|---|---|---|"]
            for l in sorted(dev_open, key=lambda x: x.ts):
                t = tiers.get(l.code, {}) if tiers else {}
                L.append(f"| {l.code} | {t.get('sector_name', '-')} | "
                         f"{_tier_of(tiers, l.code)} | {l.qty} | {l.price:.3f} | "
                         f"{_money(l.qty * l.price)} | {_fmt_day(l.ts)} |")
            L.append("")

    # ── 4. 规律候选 (仅统计) ──
    L += ["## 4. 规律候选（仅统计陈述）", ""]
    if not res.closed:
        L += ["无已平仓成交, 无统计可述。", ""]
    else:
        rows = [(tier, [l for l in res.closed if _tier_of(tiers, l.code) == tier])
                for tier in _TIERS]
        rows = [(t, lots) for t, lots in rows if lots]
        L += _stats_lines(rows, min_n)
        L.append("")
    return "\n".join(L)


# ── CLI ──────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="零号报告: 持仓/交易 × 十五五档位复盘")
    ap.add_argument("--db", default="data/trade/trade.db", help="trade.db 路径")
    ap.add_argument("--out", default="output/policy_report", help="报告输出目录")
    ap.add_argument("--min-n", type=int, default=5, help="规律候选的最小样本量")
    ap.add_argument("--since", default=None, help="只统计 YYYYMMDD (含) 之后的成交")
    args = ap.parse_args(argv)

    trades = load_trades(args.db, args.since)
    res = pair_fifo(trades)
    codes = sorted({l.code for l in res.closed} | {l.code for l in res.open_lots})
    tiers = load_tiers(codes) if codes else {}  # 无成交则不打标签, 也不报降级警告

    report = build_report(res, tiers, args.min_n, args.db, args.since)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"zero_report_{dt.datetime.now().strftime('%Y%m%d')}.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"报告已生成: {out_path} "
          f"(已平仓 {len(res.closed)} 笔, 在持 {len(res.open_lots)} 笔)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
