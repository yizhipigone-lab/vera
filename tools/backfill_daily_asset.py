"""tools/backfill_daily_asset.py — 一次性脚本: 从 trades 表回放构造 daily_asset 历史。

用法:
    python tools/backfill_daily_asset.py [--initial-capital 1000000]

说明:
    daily_asset 表从 EOD 才开始写入, 历史日期为空。本脚本从 trades 表
    逐日回放构造资产序列, 写入 daily_asset 表, 供分析 Tab 展示历史业绩。

算法:
    1. 从最早成交日开始, 初始资产 = --initial-capital (默认 1000000)
    2. 逐日: 当日资产 = 前日资产 + 当日净现金流入(卖出-买入) + 持仓市值变动
    3. 持仓市值 = 当日持仓 × 最近一次成交价(卖出优先, 买入次之)
    4. 非交易日: carry-forward 前一日值

限制:
    - 不拉取历史日线做 mark-to-market (仅用成交价近似, 精度有限)
    - 不处理分红/除权/税费 (与 trade/ 模块 P2 口径一致)
    - 写出前会提示确认, 不会静默覆盖已有数据
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "trade.db"


def fmt_date(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d")


def backfill(db_path: Path, initial_capital: float, dry_run: bool = False):
    if not db_path.exists():
        print(f"错误: trade.db 不存在 ({db_path})")
        print("请先确保 trade 模块已运行过至少一次。")
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # 1. 读取全部成交
    trades = conn.execute(
        "SELECT ts, code, direction, price, qty, amount "
        "FROM trades ORDER BY ts ASC"
    ).fetchall()

    if not trades:
        print("trades 表为空, 无需回填。")
        conn.close()
        return

    first_date = fmt_date(trades[0]["ts"])
    last_date = fmt_date(trades[-1]["ts"])
    print(f"成交范围: {first_date} ~ {last_date}, 共 {len(trades)} 笔")

    # 2. 检查已有 daily_asset 行
    existing = set()
    for r in conn.execute("SELECT date FROM daily_asset ORDER BY date ASC"):
        existing.add(r["date"])
    if existing:
        first_existing = min(existing)
        print(f"daily_asset 已有 {len(existing)} 行 (最早 {first_existing})")

    # 3. 每日聚合: 成交金额 + 持仓变动
    # 按日期存储当日成交
    daily_trades: dict[str, list] = defaultdict(list)
    for t in trades:
        d = fmt_date(t["ts"])
        daily_trades[d].append(t)

    # 构建所有需要的日期 (从首笔成交到最后一天, 包含非交易日)
    start_d = parse_date(first_date)
    end_d = parse_date(last_date)
    all_dates: list[str] = []
    cur = start_d
    while cur <= end_d:
        all_dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)

    # 4. 逐日回放
    equity = initial_capital
    cash = initial_capital
    positions: dict[str, dict] = {}  # code -> {qty, last_price}

    rows_to_write: list[tuple] = []
    last_trading_date: str | None = None

    for date_str in all_dates:
        day_trades = daily_trades.get(date_str, [])

        # 处理当日成交: 更新现金和持仓
        day_cash_flow = 0.0
        for t in day_trades:
            code = t["code"]
            price = t["price"]
            qty = t["qty"]
            amount = t["amount"]
            direction = t["direction"]  # 23=buy, 24=sell

            if direction == 23:  # 买入
                day_cash_flow -= amount
                if code not in positions:
                    positions[code] = {"qty": 0, "last_price": price}
                # 加权平均成本
                old_qty = positions[code]["qty"]
                old_cost = positions[code].get("avg_cost", price)
                new_qty = old_qty + qty
                if new_qty > 0:
                    positions[code]["avg_cost"] = (old_cost * old_qty + price * qty) / new_qty
                positions[code]["qty"] = new_qty
                positions[code]["last_price"] = price
            elif direction == 24:  # 卖出
                day_cash_flow += amount
                if code in positions:
                    positions[code]["qty"] = max(0, positions[code]["qty"] - qty)
                    positions[code]["last_price"] = price
                    if positions[code]["qty"] == 0:
                        del positions[code]

        cash += day_cash_flow

        # 市值 = 现金 + 持仓市值(数量 × 最新价)
        market_value = 0.0
        for p in positions.values():
            if p["qty"] > 0:
                market_value += p["qty"] * p["last_price"]

        equity = cash + market_value

        # 非交易日: 沿用上一交易日值 (如果当天无成交且昨天有值)
        if not day_trades and last_trading_date is None:
            continue  # 首笔成交前不写

        rows_to_write.append((date_str, round(equity, 2), round(cash, 2),
                              round(market_value, 2), 0.0))

        if day_trades:
            last_trading_date = date_str

    # 5. 确认并写入
    new_count = sum(1 for d, _, _, _, _ in rows_to_write if d not in existing)
    if new_count == 0:
        print("所有日期已有数据, 无需写入。")
        conn.close()
        return

    print(f"\n将写入 {new_count} 条新记录 (已有 {len(existing)} 条)")
    print(f"初始资金: {initial_capital:,.0f}")
    print(f"最终资产: {rows_to_write[-1][1]:,.2f}")
    if initial_capital > 0:
        total_return = (rows_to_write[-1][1] / initial_capital - 1) * 100
        print(f"累计收益: {total_return:+.2f}%")

    if dry_run:
        print("\n[dry-run] 未实际写入。加 --no-dry-run 执行写入。")
        conn.close()
        return

    # 确认
    resp = input("\n确认写入? [y/N] ").strip().lower()
    if resp != "y":
        print("已取消。")
        conn.close()
        return

    conn.execute("BEGIN")
    try:
        for date_str, total_asset, available, market_val, ts in rows_to_write:
            if date_str not in existing:
                conn.execute(
                    "INSERT INTO daily_asset (date, total_asset, available, market_value, ts) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (date_str, total_asset, available, market_val, datetime.now().timestamp()),
                )
        conn.execute("COMMIT")
        print(f"已写入 {new_count} 条。")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"写入失败: {e}")
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="回填 daily_asset 历史数据")
    parser.add_argument("--initial-capital", type=float, default=1_000_000,
                        help="初始资金 (默认 1000000)")
    parser.add_argument("--db", type=str, default=str(DB_PATH),
                        help="trade.db 路径")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="预览不写入 (默认)")
    parser.add_argument("--no-dry-run", action="store_false", dest="dry_run",
                        help="实际写入")
    args = parser.parse_args()

    backfill(Path(args.db), args.initial_capital, args.dry_run)


if __name__ == "__main__":
    main()
