# -*- coding: utf-8 -*-
"""塘划分轴对照实验 (2026-07-26, CLAUDE.md「战略方向」前置研究)。

问题: 两层架构第一层"择塘", 塘按什么划分?
  A 行业轴 (128 个 881 板块, 动量 Top3)
  B 板轴   (主板/创业板/科创板/北交所, 动量 Top1)
  C 指数轴 (沪深300/中证500/中证1000/中证A500, 动量 Top1)
  D 全A基线 (不择塘)
  E 随机行业 Top3 × N 次 (对照)
  + 板/指数各塘静态全期 (地形态参考)

方法 (性能关键): 选股在全市场只跑一次 (TDX 公式为单票时序, 与池无关),
各组信号按"窗口池"过滤 —— 与直接在池内选股严格等价。
窗口: 每 REBAL 个交易日调仓, 排名用调仓日前一收盘的 MOM 日动量 (因果)。
信号先过 30 日首信号 (per-stock 操作, 与池过滤可交换顺序)。

判定 (事先写死): 选塘增益 (vs D) 最大且两段 (24-25/26H1) 皆正的轴胜;
A 轴还须 > E 均值+2σ, 否则第一层砍掉。

用法: python tools/pond_axis_experiment.py --formulas GP1014,超赢王牛股
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent

from backtest.engine import BacktestEngine  # noqa: E402
from backtest.stop_config import load_stop_config  # noqa: E402
from core.connector import TdxConnector  # noqa: E402
from core.data_fetcher import DataFetcher  # noqa: E402
from selection.selector import StockSelector  # noqa: E402
from selection.signal_rules import filter_first_signal_in_window  # noqa: E402
from utils.config_loader import ConfigLoader  # noqa: E402

PAD_DAYS = 70
SPLIT_DATE = "20260101"   # 两段切分: 2024-25 vs 2026H1

# 板轴: 塘名 → (动量指数, 池构造)
BOARD_INDEX = {"主板": "shanghai", "创业板": "chuangyeban",
               "科创板": "kechuang50", "北交所": "899050.BJ"}
# 指数轴: 塘名 → (list_type, 动量指数)
INDEX_PONDS = {"沪深300": ("23", "hs300"), "中证500": ("24", "zz500"),
               "中证1000": ("25", "000852.SH"), "中证A500": ("28", "zhongzhengA500")}


def get_index_close(name_or_code: str, start: str, end: str) -> pd.Series:
    """指数收盘价序列 (失败返回空)。"""
    try:
        df = DataFetcher.get_index_data(name_or_code, start, end,
                                        dividend_type="none")
        if df is None or len(df) == 0:
            return pd.Series(dtype=float)
        col = "Close" if "Close" in df.columns else "close"
        return df[col]
    except Exception as e:
        print(f"  [WARN] 指数 {name_or_code} 拉取失败: {e}")
        return pd.Series(dtype=float)


def momentum_rank(closes: dict, date: pd.Timestamp, mom: int) -> list:
    """各塘在 date 前一收盘的 mom 日动量排名 (降序返回塘名)。"""
    scores = {}
    for name, s in closes.items():
        if len(s) == 0:
            continue
        pos = s.index.searchsorted(date) - 1
        if pos < mom:
            continue
        scores[name] = s.iloc[pos] / s.iloc[pos - mom] - 1.0
    return sorted(scores, key=scores.get, reverse=True)


def run_engine(sel, start, end, bt_cfg, stop_config):
    if sel is None or len(sel) == 0:
        return None
    engine = BacktestEngine(bt_cfg)
    res = engine.run(selections=sel, start_time=start, end_time=end,
                     stop_config=stop_config)
    m = res.get("metrics") or {}
    trades = res.get("trades")
    n_trades = int(len(trades)) if trades is not None else 0
    eq = res.get("equity_curve")
    seg1 = seg2 = None
    if eq is not None and len(eq) > 1:
        dates = pd.to_datetime(eq["date"] if "date" in eq.columns else eq.index)
        vals = eq["equity"].values if "equity" in eq.columns else eq.iloc[:, 0].values
        cut = pd.Timestamp(SPLIT_DATE)
        i_cut = int(np.searchsorted(dates.values, np.datetime64(cut)))
        if 0 < i_cut < len(vals):
            seg1 = vals[i_cut - 1] / vals[0] - 1.0
            seg2 = vals[-1] / vals[i_cut - 1] - 1.0
    return {"cumulative_return": m.get("cumulative_return"),
            "win_rate": m.get("win_rate"),
            "max_drawdown": m.get("max_drawdown"),
            "sharpe": m.get("sharpe_ratio"),
            "n_trades": n_trades,
            "seg_2024_2025": seg1, "seg_2026H1": seg2,
            "signals": int(len(sel))}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--formulas", required=True)
    ap.add_argument("--start", default="20240101")
    ap.add_argument("--end", default=pd.Timestamp.now().strftime("%Y%m%d"))
    ap.add_argument("--rebal", type=int, default=5, help="调仓周期 (交易日)")
    ap.add_argument("--mom", type=int, default=20, help="动量窗口 (交易日)")
    ap.add_argument("--topk-industry", type=int, default=3)
    ap.add_argument("--random-runs", type=int, default=20)
    ap.add_argument("--seed", type=int, default=20260726)
    ap.add_argument("--max-buy-amount", type=float, default=100000.0)
    ap.add_argument("--cooldown-days", type=int, default=20)
    ap.add_argument("--first-signal-window", type=int, default=30)
    ap.add_argument("--outdir", default=str(ROOT / "output" / "pond_axis"))
    args = ap.parse_args()

    outdir = Path(args.outdir)
    sel_dir = outdir / "selections"
    sel_dir.mkdir(parents=True, exist_ok=True)
    formulas = [s.strip() for s in args.formulas.split(",") if s.strip()]
    TdxConnector.initialize()

    # ── 1. 全市场股票池 (剔ST) + 交易日历 ──
    stocks = StockSelector({"formula_name": "_", "universe": {
        "type": "5", "exclude_st": True}}).resolve_universe()
    u_set = set(stocks)
    print(f"[INFO] 全A池 {len(stocks)} 只")
    padded_start = (pd.Timestamp(args.start) - pd.Timedelta(days=PAD_DAYS)
                    ).strftime("%Y%m%d")
    calendar = pd.DatetimeIndex(pd.to_datetime(DataFetcher.get_trading_dates(
        "SH", start_time=padded_start, end_time=args.end)))
    win_days = [d for d in calendar if pd.Timestamp(args.start) <= d
                and d <= pd.Timestamp(args.end)]

    # ── 2. 塘定义 ──
    print("[INFO] 构造塘池...")
    pools = {}   # 塘名 → set(codes)  (∩ 全A池)
    uni = {}
    for lt in ("50", "51", "52", "53", "23", "24", "25", "28"):
        uni[lt] = set(DataFetcher.get_stock_universe(lt))
    pools["主板"] = (uni["50"] - uni["51"] - uni["52"]) & u_set
    pools["创业板"] = uni["51"] & u_set
    pools["科创板"] = uni["52"] & u_set
    pools["北交所"] = uni["53"] & u_set
    for name, (lt, _) in INDEX_PONDS.items():
        pools[name] = uni[lt] & u_set
    sectors = DataFetcher.get_sector_list()
    for s in sectors:
        pools[s["name"]] = set(DataFetcher.get_sector_stocks(s["code"])) & u_set
    industry_names = [s["name"] for s in sectors]
    print(f"[INFO] 塘共 {len(pools)} 个 (行业 {len(industry_names)})")

    # ── 3. 各塘动量指数 ──
    print("[INFO] 拉动量指数...")
    closes = {}
    for name, idx in BOARD_INDEX.items():
        closes[name] = get_index_close(idx, padded_start, args.end)
    for name, (_, idx) in INDEX_PONDS.items():
        closes[name] = get_index_close(idx, padded_start, args.end)
    for s in sectors:
        closes[s["name"]] = get_index_close(s["code"], padded_start, args.end)
    n_ok = sum(1 for v in closes.values() if len(v) > 0)
    print(f"[INFO] 指数就绪 {n_ok}/{len(closes)}")

    # ── 4. 回测配置 (全部组同参, 单票10万避免一手偏差) ──
    defaults = ConfigLoader.load_defaults()
    bt_cfg = dict(defaults.get("backtest", {}))
    ps = dict(bt_cfg.get("position_sizing", {}))
    ps["max_buy_amount"] = float(args.max_buy_amount)
    bt_cfg["position_sizing"] = ps
    bt_cfg["matrix_cache"] = False
    bt_cfg["sell_cooldown_days"] = int(args.cooldown_days)
    stop_config = load_stop_config()

    # ── 5. 窗口 → 各轴塘选择 ──
    rebal_dates = win_days[:: args.rebal]
    def window_of(d):
        i = int(np.searchsorted(np.array(rebal_dates, dtype="datetime64[ns]"),
                                np.datetime64(d), side="right")) - 1
        return max(i, 0)

    win_pick = {"A_行业Top3": [], "B_板Top1": [], "C_指数Top1": []}
    for r in rebal_dates:
        rank_all = momentum_rank(closes, r, args.mom)
        ind_rank = [n for n in rank_all if n in industry_names]
        brd_rank = [n for n in rank_all if n in BOARD_INDEX]
        idx_rank = [n for n in rank_all if n in INDEX_PONDS]
        win_pick["A_行业Top3"].append(ind_rank[: args.topk_industry])
        win_pick["B_板Top1"].append(brd_rank[:1])
        win_pick["C_指数Top1"].append(idx_rank[:1])

    rng = np.random.default_rng(args.seed)
    rand_picks = [[list(rng.choice(industry_names, size=args.topk_industry,
                                   replace=False)) for _ in rebal_dates]
                  for _ in range(args.random_runs)]

    def filter_by_picks(sel, picks_per_win):
        """sel → 只保留 当日窗口被选中塘 的信号。"""
        widx = np.array([window_of(d) for d in sel["select_date"]])
        keep = np.zeros(len(sel), dtype=bool)
        codes = sel["stock_code"].values
        for wi in range(len(rebal_dates)):
            pond_set = set().union(*(pools.get(p, set()) for p in picks_per_win[wi])) \
                if picks_per_win[wi] else set()
            if not pond_set:
                continue
            mask = widx == wi
            keep[mask] = np.array([c in pond_set for c in codes[mask]])
        return sel[keep]

    # ── 6. 逐公式: 选股 (缓存 parquet) → 分组回测 ──
    report = {}
    for f in formulas:
        sp = sel_dir / f"{f}.parquet"
        if sp.exists():
            sel = pd.read_parquet(sp)
            print(f"[INFO] {f} 选股命中缓存 {len(sel)} 条")
        else:
            print(f"[INFO] {f} 全市场选股中...")
            cfg = {"formula_name": f, "formula_arg": "",
                   "universe": {"type": "5", "exclude_st": True},
                   "period": "1d", "dividend_type": 1}
            raw = StockSelector(cfg).run(start_time=padded_start,
                                         end_time=args.end, stock_list=stocks)
            sel = filter_first_signal_in_window(
                raw, window_td=args.first_signal_window,
                real_start=args.start, calendar=calendar)
            sel.to_parquet(sp)
            print(f"[INFO] {f} 信号 {len(sel)} 条 (已缓存)")
        sel["select_date"] = pd.to_datetime(sel["select_date"])

        groups = {}
        t0 = time.time()
        print(f"[INFO] {f} D_全A基线...")
        groups["D_全A"] = run_engine(sel, args.start, args.end, bt_cfg, stop_config)
        for gname, picks in win_pick.items():
            print(f"[INFO] {f} {gname}...")
            groups[gname] = run_engine(filter_by_picks(sel, picks),
                                       args.start, args.end, bt_cfg, stop_config)
        # 静态塘 (板+指数, 地形态参考)
        for pond in list(BOARD_INDEX) + list(INDEX_PONDS):
            sub = sel[sel["stock_code"].isin(pools.get(pond, set()))]
            groups[f"静态_{pond}"] = run_engine(sub, args.start, args.end,
                                                bt_cfg, stop_config)
        # 随机行业对照
        rand_rets = []
        for k in range(args.random_runs):
            r = run_engine(filter_by_picks(sel, rand_picks[k]),
                           args.start, args.end, bt_cfg, stop_config)
            if r:
                rand_rets.append(r["cumulative_return"])
            print(f"  [{k+1}/{args.random_runs}] 随机行业 "
                  f"{r['cumulative_return']*100:+.1f}%" if r else f"  [{k+1}] 空")
        groups["E_随机行业Top3"] = {
            "mean": float(np.mean(rand_rets)) if rand_rets else None,
            "std": float(np.std(rand_rets)) if rand_rets else None,
            "n": len(rand_rets)}
        report[f] = groups
        print(f"[INFO] {f} 完成 ({time.time()-t0:.0f}s)")

    (outdir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    TdxConnector.close()

    # ── 7. 判定表 ──
    print("\n=== 轴间对照 (全期收益 | 24-25段 | 26H1段 | 胜率 | 回撤) ===")
    for f, groups in report.items():
        print(f"\n--- {f} ---")
        for g, r in groups.items():
            if g.startswith("E_"):
                print(f"  {g}: 均值={r['mean']*100:+.1f}% ±{r['std']*100:.1f} (n={r['n']})")
            elif r:
                s1 = f"{r['seg_2024_2025']*100:+.1f}%" if r['seg_2024_2025'] is not None else "—"
                s2 = f"{r['seg_2026H1']*100:+.1f}%" if r['seg_2026H1'] is not None else "—"
                print(f"  {g}: {r['cumulative_return']*100:+.1f}% | {s1} | {s2} "
                      f"| 胜{r['win_rate']*100:.1f}% | 撤{r['max_drawdown']*100:.1f}% "
                      f"| 信号{r['signals']}")
            else:
                print(f"  {g}: 无信号/空")
    print(f"[OK] 落盘 {outdir / 'report.json'}")


if __name__ == "__main__":
    main()
