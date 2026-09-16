"""notes_gen/daily.py — 盘后复盘报告（2026-09-17, M6）。

**一句话**：收盘后把「今天大盘在什么位置 + 我的账户怎么样 + 有什么需要我留意」
拼成一份报告，落盘 + 推飞书（可选邮件）。

三段结构（计划书 §9 验收 1）：
    ① 市场盘面   —— **复用** `core.market_position_runner.thermometer_md()`，不写第二份文案
    ② 我的账户   —— **复用** `daily_report` 表里已有的 payload，**不重算**（重算必然与飞书日报漂移）
    ③ 需要你留意的清单 —— 只陈述规则事实，不做预测、不调 LLM
    ＋「今天做对的」/「我的交易行为」/「累计统计」（外部最佳实践 §12.2~§12.4）

**依赖方向（单向朝下，绝不成环）**：

    notes_gen/daily.py ──┬─→ core.market_position_runner（盘面；它**绝不** import trade）
                         ├─→ trade.config（止损阈值，取硬止损那一条）
                         └─→ trade.analysis（累计口径，**复用**不重算）

    **反向有 AST 断言**：`trade_main.py` 与 `trade/` 下**不许** import `notes_gen.daily`
    —— 报告线一旦被交易线引用，"报告 → 交易"的反馈环就通了，违反业务铁律 1。
    （守护在 `tests/test_daily_review.py`。）

**落盘**：`data/daily_review/<日期>.md`。
    `data/` 已被 RAG 的 `exclude_dirs` 排除 → **天然不会自我污染检索库**；
    绝不能落 `docs/`（那会被自己的 RAG 索引进去，见 RAG 计划书 §12.6）。

**纪律**：全程**只读** trade.db（`mode=ro` + `PRAGMA query_only=1`）。
    当日没有 EOD 归档时，账户段**明写"当日无交易归档"**，绝不拿昨天的数字冒充今天
    （照 `trade/asset_gapfill.py` 的 fail-closed 纪律）。
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.logger import get_logger
from utils.sysutil import project_root

_logger = get_logger("notes_gen.daily")

_ROOT = project_root()
TRADE_DB = _ROOT / "data" / "trade" / "trade.db"
KLINE_1D_DIR = _ROOT / "data" / "kline_cache" / "1d"
#: 复盘报告落盘目录（**不是** RAG 语料源；改动前先看 RAG 计划书 §12.6）
REVIEW_DIR = _ROOT / "data" / "daily_review"

#: 近端滚动统计的窗口（交易日）
ROLL_WINDOW = 20
#: 行为对比窗口（交易日）：换手/笔数的分位基准
BEHAVIOR_WINDOW = 60
#: 买价落在当日振幅这个位置以上 → 记为"买在当日偏高位置"（§12.3）
HIGH_BUY_RATIO = 0.70


# ───────────────────── 取数（全部只读） ─────────────────────


def _db_ro() -> sqlite3.Connection | None:
    """只读打开 trade.db；不存在/打不开返 None（fail-soft，绝不抛）。"""
    if not TRADE_DB.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{TRADE_DB}?mode=ro", uri=True)
        con.execute("PRAGMA query_only=1")
        con.row_factory = sqlite3.Row
        return con
    except Exception as e:
        _logger.warning("打不开 trade.db（只读）: %s", e)
        return None


def _rows(con: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    """跑一条查询 → list[dict]；失败返 []（fail-soft）。"""
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    except Exception as e:
        _logger.warning("查询失败（返空）: %s — %s", sql[:60], e)
        return []


def _latest_payload(con: sqlite3.Connection, asof: str | None = None) -> dict | None:
    """当日 `daily_report` 的 payload；**只有当日**，拿不到返 None（不拿昨天冒充）。

    留痕（计划书 §12.6 的教训）：列名是 **`payload_json`**，不是 `payload`。
    """
    sql = "SELECT date, payload_json FROM daily_report"
    args: tuple = ()
    if asof:
        sql += " WHERE date = ?"
        args = (asof,)
    sql += " ORDER BY date DESC LIMIT 1"
    got = _rows(con, sql, args)
    if not got:
        return None
    try:
        p = json.loads(got[0]["payload_json"])
        p["_date"] = got[0]["date"]
        return p
    except Exception:
        return None


def _trades_on(con: sqlite3.Connection, date: str) -> list[dict]:
    """当日成交明细（`trades` 表按 ts 落在该日）。

    `trades.ts` 是 Unix 秒；日期边界按**本地日**算（与 EOD 归档同一时区）。
    """
    day = dt.date.fromisoformat(date)
    lo = dt.datetime.combine(day, dt.time.min).timestamp()
    hi = dt.datetime.combine(day, dt.time.max).timestamp()
    return _rows(con, "SELECT code, direction, price, qty, amount, ts, source, "
                      "reason, pnl_amount, pnl_pct FROM trades "
                      "WHERE ts >= ? AND ts <= ? ORDER BY ts", (lo, hi))


def _ohlc_on(code: str, date: str) -> dict | None:
    """某票某日的 OHLC + 成交量；**空壳 bar 一律视为无效**（返 None）。

    空壳 bar 是什么：本项目 2026-09-16 实测过 —— 盘前抓数会造出
    `open=high=low=close` 且 `volume=0` 的假 bar。它会让"买价在当日振幅中的位置"
    直接除零（计划书 §13.1 MED-2）。
    判据与 `core.market_position.last_valid_date` **同一个想法**（那边是全市场版：
    有成交的股票占比 ≥50%；单票版就是 volume > 0），尺度不同、不重复实现。
    """
    p = KLINE_1D_DIR / f"{code}.parquet"
    if not p.exists():
        return None
    try:
        import pandas as pd
        df = pd.read_parquet(p, columns=["date", "open", "high", "low", "close", "volume"])
    except Exception:
        return None
    if len(df) == 0:
        return None
    try:
        d = pd.to_datetime(df["date"]).dt.date.astype(str)
        hit = df[d == date]
        if len(hit) == 0:
            return None
        r = hit.iloc[-1]
        vol = float(r["volume"])
        if not vol > 0:                 # 空壳 bar → 无效
            return None
        return {"open": float(r["open"]), "high": float(r["high"]),
                "low": float(r["low"]), "close": float(r["close"]), "volume": vol}
    except Exception:
        return None


def _positions(con: sqlite3.Connection) -> list[dict]:
    """当前持仓快照（`position_snapshot`）。"""
    return _rows(con, "SELECT code, volume, can_use, avg_cost, ts FROM position_snapshot")


# ───────────────────── 白话格式化（模板，不靠自觉） ─────────────────────


def _money(v) -> str:
    if v is None:
        return "【缺】"
    try:
        return f"{float(v):,.0f} 元"
    except (TypeError, ValueError):
        return "【缺】"


def _pct(v, nd: int = 2) -> str:
    if v is None:
        return "【缺】"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "【缺】"
    return f"{x:+.{nd}f}%"


def _pct_plain(v) -> str:
    """涨跌幅 → 白话（只说方向与量级，不夸张）。"""
    if v is None:
        return "（算不出）"
    x = float(v)
    if abs(x) < 0.05:
        return "基本没动"
    if x > 0:
        return "小涨" if x < 1 else ("上涨" if x < 3 else "明显上涨")
    return "小跌" if x > -1 else ("下跌" if x > -3 else "明显下跌")


# ───────────────────── 各段组装 ─────────────────────


def _account_md(payload: dict | None, asof: str, trades: list[dict]) -> list[str]:
    """② 我的账户（复用 payload；缺当日归档就明写）。"""
    if not payload:
        return ["**今日没有交易归档**（15:05 那次 EOD 没跑成，或今天没开机）。",
                "",
                "**这里不会拿昨天的数字冒充今天** —— 这是本项目对「停机」的一贯处理"
                "（宁可写「没有」，也不给一个看着像今天的旧数）。"
                "要看历史某天的账户，去「分析」页签查那一天。"]
    out = [
        f"- **总资产 {_money(payload.get('total_asset'))}**"
        f"（其中现金 {_money(payload.get('cash'))}、"
        f"持仓市值 {_money(payload.get('market_value'))}）",
        f"- 当日盈亏 **{_money(payload.get('day_pnl'))}**"
        f"（{_pct(payload.get('day_pnl_pct'))}，{_pct_plain(payload.get('day_pnl_pct'))}）"
        f"；浮动盈亏 {_money(payload.get('floating_pnl'))}",
        f"- 已实现盈亏 {_money(payload.get('realized_pnl'))}"
        f"；持仓 {payload.get('position_count')} 只"
        f"；当日买入 {payload.get('buy_count')} 笔 / 卖出 {payload.get('sell_count')} 笔"
        # **单位坑（2026-09-17 核实写方后修正）**: payload 里的 `turnover` 是
        # **成交额（元）**，不是百分比 —— 写方 `trade/daily_report.py:68` 存的是
        # `Σ|amount|`，`trade/notifier.py:282` 也按「成交额 {x:,.2f}」打印。
        # 第一版这里按百分比打印，`.env` 显示 0.0 时看不出错，一旦有成交额就会
        # 把「12 万元」印成「+0.00%」—— 这类单位错必须对着写方核，不许猜。
        f"；当日成交额 {_money(payload.get('turnover'))}",
    ]
    wr = payload.get("win_rate")
    if wr is not None:
        out.append(f"- 当日胜率 {float(wr) * 100:.1f}%"
                   f"（当天卖出的那些票里，赚钱的比例）")
    if not trades:
        out.append("- **今日没有成交**（`trades` 表当日 0 条）——下面的"
                   "「今天做对的」与「买价位置」因此没有数据可算。")
    return out


def _did_well_md(trades: list[dict], ohlc_by_code: dict, *, asof: str) -> list[str]:
    """④ 今天做对的（§12.2）—— 规则事实，不是自我表扬。

    **为什么要有这一段**：只报问题会让人回避看报告（外部最佳实践 SMB 那篇的核心论点）。
    """
    sells = [t for t in trades if (t.get("pnl_amount") is not None)]
    if not trades:
        return ["今日无成交，**没有可算的亮点**（这不是坏事，只是今天没动）。"]
    wins = sorted([s for s in sells if (s.get("pnl_amount") or 0) > 0],
                  key=lambda x: -(x["pnl_amount"] or 0))[:2]
    lines: list[str] = []
    for w in wins:
        lines.append(f"- 已实现盈利最大的一笔：**{w['code']}**，"
                     f"落袋 **{_money(w.get('pnl_amount'))}**"
                     f"（{_pct(w.get('pnl_pct'))}，原因码 {w.get('reason')}）。")
    # 卖在相对高点：卖出价 ≥ 当日最高价 × 0.99
    for t in trades:
        if t.get("direction") not in (1, "1", "sell", "SELL"):
            continue
        o = ohlc_by_code.get(t["code"])
        if not o or o["high"] <= 0:
            continue
        if float(t["price"]) >= o["high"] * 0.99:
            lines.append(f"- **卖在相对高点**：{t['code']} 卖在 {t['price']}，"
                         f"当日最高 {o['high']}（基本是最高价附近出的）。")
            break
    # 按规则止损且事后证明对：卖出后当日收盘更低
    for t in trades:
        if t.get("reason") not in ("0", 0, "cost_stop", "stop") and \
                str(t.get("reason")) not in ("cost_stop", "stop"):
            continue
        o = ohlc_by_code.get(t["code"])
        if o and float(t["price"]) > o["close"]:
            lines.append(f"- **按规则止损、事后看是对的**：{t['code']} 卖在 {t['price']}，"
                         f"当日收盘 {o['close']}（卖完还在跌）。守规矩这件事本身值得记住。")
            break
    if not lines:
        lines.append("今天**没有特别值得点出来的亮点**（也没有做错什么明显的）——"
                     "空白也是事实，不硬凑。")
    return lines


def _behavior_md(payload: dict | None, trades: list[dict],
                 ohlc_by_code: dict, pcts: dict) -> list[str]:
    """⑤ 我的交易行为（§12.3）—— **只陈述事实，不写评价词**。

    不写"你今天操作太频繁了"，只写"当日换手处于过去 60 日的第 87 分位"。
    评判留给用户 —— 这与铁律 1（仓位由人把关）同源：**系统描述，人判断**。

    **换手怎么算**（§12.3）：`当日成交额 ÷ 总资产`，与过去 60 个交易日的同一比值比。
    注意 payload 里的 `turnover` 是**成交额（元）**，不是百分比 —— 是这里现算成比率的。
    """
    if not payload:
        return ["【缺】当日没有账户归档，行为指标算不出来。"]
    out: list[str] = []
    items = (("换手率", "turnover", payload.get("turnover_pct")),
             ("成交笔数", "count",
              (payload.get("buy_count") or 0) + (payload.get("sell_count") or 0)))
    for label, key, val in items:
        pct = pcts.get(key)
        shown = f"{val:.2f}%" if (key == "turnover" and val is not None) else val
        if val is None or pct is None:
            out.append(f"- **{label}**：{shown if shown is not None else '【缺】'}"
                       f"（过去 {BEHAVIOR_WINDOW} 个交易日里能比的数据不足 10 天，"
                       "算不出分位）")
        else:
            out.append(f"- **{label} {shown}**，处于过去 {BEHAVIOR_WINDOW} 个交易日的"
                       f"**第 {pct:.0f} 分位**（0 = 最低、100 = 最高）。"
                       f"{'（今天比平时动得多）' if pct >= 80 else ''}"
                       f"{'（今天比平时安静）' if pct <= 20 else ''}")
    buys = [t for t in trades if str(t.get("direction")) in ("0", "buy", "BUY")]
    placed = []
    for t in buys:
        o = ohlc_by_code.get(t["code"])
        if not o:
            continue
        rng = o["high"] - o["low"]
        if rng <= 0:      # 空壳 bar / 一字板 → 没有有效振幅，**不算数**
            placed.append((t["code"], None, "当日无有效振幅"))
            continue
        pos = (float(t["price"]) - o["low"]) / rng
        placed.append((t["code"], pos,
                       "买在当日偏高位置" if pos > HIGH_BUY_RATIO else "买在当日中低位置"))
    if not placed:
        out.append("- **买价位置**：今日无买入成交（或买的那只票当日没有有效行情），"
                   "这一项没有数据。")
    else:
        for code, pos, tag in placed:
            if pos is None:
                out.append(f"- **买价位置**：{code} —— {tag}。")
            else:
                out.append(f"- **买价位置**：{code} 买在当日振幅的 {pos * 100:.0f}% 处"
                           f"（0% = 当日最低、100% = 当日最高），**{tag}**。")
    return out


def _cumulative_md(con: sqlite3.Connection, asof: str, trades: list[dict]) -> list[str]:
    """⑥ 累计统计（§12.4）—— 单日数字看不出趋势，复盘的真正价值在累计。

    **复用** `trade.analysis.build_daily_pnl_view` 的月度口径（同一函数、不重算）；
    近 20 日只做**已有数据的聚合**（`trades.pnl_amount`），不新增取数。
    """
    out: list[str] = []
    wins = [t for t in trades if (t.get("pnl_amount") or 0) > 0]
    loses = [t for t in trades if (t.get("pnl_amount") or 0) < 0]
    if trades:
        out.append(f"- **今天**：卖出 {len(loses) + len(wins)} 笔，"
                   f"赚 {len(wins)} 笔 / 亏 {len(loses)} 笔，"
                   f"已实现合计 {_money(sum((t.get('pnl_amount') or 0) for t in trades))}。")
    # 近 N 个交易日：从 trades 现算
    rows = _rows(con, "SELECT code, pnl_amount, pnl_pct, ts FROM trades "
                      "WHERE pnl_amount IS NOT NULL ORDER BY ts DESC LIMIT 200")
    if rows:
        recent = rows[:200]
        w = sum(1 for r in recent if (r["pnl_amount"] or 0) > 0)
        tot = sum((r["pnl_amount"] or 0) for r in recent)
        worst = min(recent, key=lambda r: (r["pnl_amount"] or 0))
        out.append(f"- **最近 {len(recent)} 笔卖出**（跨若干交易日）："
                   f"胜率 {w / len(recent) * 100:.0f}%，"
                   f"已实现合计 {_money(tot)}，"
                   f"最大单笔亏损 {_money(worst.get('pnl_amount'))}"
                   f"（{worst.get('code')}）。")
    else:
        out.append("- 还没有带盈亏的卖出记录，累计统计暂无数据。")
    # 本月至今 vs 上月：复用 trade.analysis 的月度口径（不重算）
    try:
        from trade.analysis import build_daily_pnl_view
        y, m = int(asof[:4]), int(asof[5:7])
        prefix = f"{y}-{m:02d}"
        month_rows = _rows(con, "SELECT date, total_asset, source FROM daily_asset "
                                "WHERE date LIKE ? ORDER BY date", (prefix + "%",))
        prev_month = _rows(con, "SELECT date, total_asset, source FROM daily_asset "
                                "WHERE date < ? ORDER BY date DESC LIMIT 1", (prefix + "-01",))
        if month_rows:
            view = build_daily_pnl_view(month_rows, prev_month[0] if prev_month else None,
                                        {}, [], y, m)
            mo = view.get("_month") or {}
            base_note = ("与上月最后一天比" if mo.get("baseline_is_prev_month")
                         else "与本月第一天比（前面还没有数据）")
            out.append(f"- **本月至今（{prefix}）**：{mo.get('trading_days')} 个交易日，"
                       f"赚 {mo.get('win_days')} 天 / 亏 {mo.get('loss_days')} 天，"
                       f"区间盈亏 {_money(mo.get('pnl_amount'))}"
                       f"（{_pct((mo.get('pnl_rate') or 0) * 100)}，{base_note}）。")
            if mo.get("derived_days"):
                out.append(f"  （其中 {mo['derived_days']} 天是**停机补算**出来的，"
                           "不是当天实测，看的时候心里有数。）")
    except Exception as e:
        _logger.warning("月度口径复用失败（跳过该行）: %s", e)
        out.append("- 本月口径算不出来（`trade.analysis.build_daily_pnl_view` 不可用）。")
    return out


def _watch_md(con: sqlite3.Connection, payload: dict | None,
              positions: list[dict], asof: str, mp_rec: dict | None,
              ohlc_by_code: dict, stop_threshold: float) -> list[str]:
    """③ 需要你留意的清单 —— 只陈述规则事实，不做预测、不调 LLM。

    **改名理由（计划书 §5）**：原来叫「明日关注」，那承诺了预测，与业务铁律 1 冲突 ——
    推到手机上的报告一旦像建议，就会被当建议读。

    **止损线只报"硬止损"那一条**（计划书 §10 的"读不透就不做"）：
    移动止盈要峰值、阶梯止盈要分档记录、条件时间止盈要当日最高——
    这些本报告不复原，硬凑一个数出来就是编。
    """
    out: list[str] = []
    for p in positions:
        code = p.get("code")
        cost = p.get("avg_cost")
        o = ohlc_by_code.get(code) or _ohlc_on(code, asof)
        if cost is None or not o:
            out.append(f"- 持仓 **{code}**：拿不到当日收盘价（本地日线缓存里没有这一天"
                       f"的有效行情），算不出距止损线的距离。")
            continue
        px = o["close"]
        dist = (px / float(cost) - 1.0) if cost else None
        line = float(cost) * (1.0 + stop_threshold)
        gap = (px / line - 1.0) * 100 if line > 0 else None
        s = (f"- 持仓 **{code}**：成本 {cost}、当日收盘 {px}、"
             f"浮动 {_pct(None if dist is None else dist * 100)}。")
        if gap is not None:
            s += (f"**距硬止损线（成本 {stop_threshold * 100:.0f}% = {line:.2f}）"
                  f"还有 {gap:+.1f}%**")
            s += "（已经很近了，注意）" if gap < 2 else "。"
        out.append(s)
    if not positions:
        out.append("- 当前没有持仓记录（`position_snapshot` 为空）。")
    rot = (payload or {}).get("rotation")
    if rot:
        out.append(f"- **ETF 轮动**：{json.dumps(rot, ensure_ascii=False)[:200]}")
    else:
        out.append("- ETF 轮动：当日归档里没有轮动状态（可能未初始化），跳过。")
    if mp_rec:
        if mp_rec.get("stale"):
            out.append(f"- **大盘数据滞后**：最新有效交易日 {mp_rec.get('date')}，"
                       f"应有 {mp_rec.get('expected_date')} —— 上面那些盘面数字"
                       "不是今天收盘的。")
        else:
            out.append(f"- 大盘盘面数据日期 {mp_rec.get('date')}，是最新的。")
    out.append("- **以上都是事实陈述，不是建议。** 要不要动手，你自己判断"
               "（业务铁律 1：研判只出报告，不接仓位调度）。")
    return out


# ───────────────────── 公开接口（4 个） ─────────────────────


def build_review(*, asof: str | None = None, with_market: bool = True) -> dict:
    """组装复盘 payload（只读，fail-soft）。

    返回键：`asof` / `market_md` / `account` / `did_well` / `behavior` /
    `watch` / `cumulative` / `notes` / `ok`。
    """
    asof = asof or dt.date.today().isoformat()
    notes: list[str] = []
    con = _db_ro()
    if con is None:
        notes.append("trade.db 打不开（只读），账户段与留意清单会缺内容。")
    payload = _latest_payload(con, asof) if con else None
    trades = _trades_on(con, asof) if con else []
    positions = _positions(con) if con else []
    codes = {t["code"] for t in trades} | {p.get("code") for p in positions if p.get("code")}
    ohlc = {c: _ohlc_on(c, asof) for c in sorted(codes)}
    ohlc = {k: v for k, v in ohlc.items() if v}
    # 行为分位：过去 BEHAVIOR_WINDOW 个交易日的**换手率(成交额/总资产)** 与 笔数
    pcts: dict = {}
    if payload is not None:
        ta = payload.get("total_asset")
        tv = payload.get("turnover")
        if ta and tv is not None and float(ta) > 0:
            payload["turnover_pct"] = float(tv) / float(ta) * 100
    if con:
        hist = _rows(con, "SELECT payload_json FROM daily_report ORDER BY date DESC LIMIT ?",
                     (BEHAVIOR_WINDOW,))
        ratios, counts = [], []
        for h in hist:
            try:
                p = json.loads(h["payload_json"])
            except Exception:
                continue
            try:
                if p.get("turnover") is not None and float(p.get("total_asset") or 0) > 0:
                    ratios.append(float(p["turnover"]) / float(p["total_asset"]) * 100)
            except (TypeError, ValueError):
                pass
            if p.get("buy_count") is not None and p.get("sell_count") is not None:
                counts.append(int(p["buy_count"]) + int(p["sell_count"]))
        if payload is not None and len(ratios) >= 10 and payload.get("turnover_pct") is not None:
            cur = float(payload["turnover_pct"])
            pcts["turnover"] = sum(1 for x in ratios if x <= cur) / len(ratios) * 100
        if payload is not None and len(counts) >= 10:
            cur = int(payload.get("buy_count") or 0) + int(payload.get("sell_count") or 0)
            pcts["count"] = sum(1 for x in counts if x <= cur) / len(counts) * 100
    # 止损阈值：复用 trade.config 的既有定义，不新增参数
    stop_threshold = -0.12
    try:
        from trade.config import CostStopConfig
        stop_threshold = float(CostStopConfig().threshold)
    except Exception as e:
        notes.append(f"读不到 trade.config 的硬止损阈值（用默认 -12%）: {e}")
    mp_rec = None
    market_md = ""
    if with_market:
        try:
            from core import market_position_runner as mpr
            mp_rec = mpr.latest()
            market_md = mpr.thermometer_md(mp_rec) if mp_rec else (
                "【缺】还没有大盘连续录像，先跑 "
                "`python tools/market_position_collect.py --backfill`。")
        except Exception as e:
            notes.append(f"盘面段生成失败（跳过）: {e}")
    if con:
        con.close()
    return {
        "ok": True, "asof": asof, "market_md": market_md,
        "account": _account_md(payload, asof, trades),
        "did_well": _did_well_md(trades, ohlc, asof=asof),
        "behavior": _behavior_md(payload, trades, ohlc, pcts),
        "watch": (lambda c: _watch_md(c, payload, positions, asof, mp_rec,
                                      ohlc, stop_threshold))(_db_ro()),
        "cumulative": (lambda c: _cumulative_md(c, asof, trades))(_db_ro()),
        "notes": notes, "payload": payload, "trades": trades,
    }


def review_md(review: dict) -> str:
    """payload → Markdown（**纯函数**，不取数、不推送；便于单测）。"""
    r = review or {}
    asof = r.get("asof") or ""
    out = [f"# 盘后复盘 {asof}", ""]
    if r.get("notes"):
        out += ["【本次有几处没取到数，如实列出】"] + [f"- {n}" for n in r["notes"]] + [""]
    if r.get("market_md"):
        out += [r["market_md"], ""]
    out += ["## 我的账户", ""] + list(r.get("account") or []) + [""]
    out += ["## 今天做对的", ""] + list(r.get("did_well") or []) + [""]
    out += [f"## 我的交易行为（只陈述事实，不评价；评判权在你）", ""] \
        + list(r.get("behavior") or []) + [""]
    out += ["## 需要你留意的清单", ""] + list(r.get("watch") or []) + [""]
    out += ["## 累计统计（单日看不出趋势，看累计）", ""] \
        + list(r.get("cumulative") or []) + [""]
    out += ["---",
            "本报告只读，**不联入任何仓位调度**（业务铁律 1：研判只出报告，最后一步由人来做）。",
            f"落盘路径 `data/daily_review/{asof}.md`（`data/` 不在 RAG 语料范围内，"
            "复盘报告不会污染检索库）。"]
    return "\n".join(out)


def push_review(review: dict, *, also_email: bool = False,
                email_to: str | None = None) -> dict:
    """投递：飞书卡片（复用 `tools/send_report_feishu.py`）+ 可选邮件。

    永远 fail-soft：任何一路失败只返 `{"ok": False, "reason": ...}`，绝不抛 ——
    调度器 job 不该因为推送失败而中断。
    """
    md = review_md(review)
    asof = review.get("asof") or ""
    result: dict = {"ok": False, "feishu": None, "email": None}
    try:
        from tools.send_report_feishu import (chunk_sections, load_webhook,
                                             md_to_lark, send_card)
        try:
            webhook = load_webhook()
        except SystemExit:
            result["feishu"] = {"ok": False, "reason": "未配置 FEISHU_WEBHOOK_URL (.env)"}
            webhook = None
        if webhook:
            chunks = chunk_sections(md_to_lark(md))
            codes = []
            for i, c in enumerate(chunks, 1):
                rr = send_card(webhook, f"盘后复盘 {asof}", c, i, len(chunks))
                codes.append(rr.get("code", rr.get("StatusCode")))
            result["feishu"] = {"ok": True, "cards": len(chunks), "codes": codes}
            result["ok"] = True
    except Exception as e:
        result["feishu"] = {"ok": False, "reason": f"飞书推送器不可用: {e}"}
    if also_email:
        try:
            import subprocess
            import tempfile
            with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                             encoding="utf-8") as f:
                f.write(md)
                tmp = f.name
            cmd = [sys.executable, str(_ROOT / "tools" / "send_email.py"), tmp,
                   "--subject", f"盘后复盘 {asof}"]
            if email_to:
                cmd += ["--to", email_to]
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            result["email"] = {"ok": p.returncode == 0,
                               "msg": (p.stdout or p.stderr or "")[-300:]}
        except Exception as e:
            result["email"] = {"ok": False, "reason": f"邮件投递失败: {e}"}
    return result


def run_daily_review(*, asof: str | None = None, write: bool = True,
                     push: bool = False, also_email: bool = False) -> dict:
    """编排入口：组装 → 落盘 → 推送。供调度器 15:55 的 job 调。"""
    review = build_review(asof=asof)
    md = review_md(review)
    path = None
    if write:
        try:
            REVIEW_DIR.mkdir(parents=True, exist_ok=True)
            path = REVIEW_DIR / f"{review['asof']}.md"
            tmp = path.with_suffix(".md.tmp")
            tmp.write_text(md, encoding="utf-8")
            import os
            os.replace(tmp, path)       # 原子写, 防读到半截
        except Exception as e:
            _logger.warning("复盘落盘失败: %s", e)
            path = None
    out = {"ok": True, "asof": review["asof"], "chars": len(md),
           "path": str(path) if path else None, "notes": review["notes"]}
    if push:
        out["push"] = push_review(review, also_email=also_email)
        out["ok"] = bool(out["push"].get("ok") or out["push"].get("email"))
    return out


def main(argv: list[str] | None = None) -> int:
    """CLI：`python -m notes_gen.daily [--push] [--email] [--no-market] [--stdout]`。"""
    import argparse
    ap = argparse.ArgumentParser(prog="python -m notes_gen.daily",
                                 description="盘后复盘报告（只读，不联仓位）")
    ap.add_argument("--asof", default=None, help="按哪一天做（默认今天）")
    ap.add_argument("--push", action="store_true", help="推飞书")
    ap.add_argument("--email", action="store_true", help="同时发邮件")
    ap.add_argument("--no-market", action="store_true", help="不带盘面段（只要账户）")
    ap.add_argument("--stdout", action="store_true", help="直接打印 Markdown")
    args = ap.parse_args(argv)
    if args.stdout or not (args.push or args.email):
        r = build_review(asof=args.asof, with_market=not args.no_market)
        print(review_md(r))
        if not args.push and not args.email:
            return 0
    res = run_daily_review(asof=args.asof, write=True, push=True,
                           also_email=args.email)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
