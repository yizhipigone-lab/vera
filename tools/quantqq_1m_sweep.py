"""
QUANTQQ 1m 止盈止损参数扫描驱动 (2026-08-14)

背景:
  用户拍板口径: 基于 1 分钟线, 2026-01-28 至今 (1m 缓存起点 2026-01-26, 数据
  到 2026-08-13), QUANTQQ 公式, 沪深A股池 (universe type "50", 排 ST/次新 60 天,
  与实盘 auto_buy 同口径), 本金 100 万, 单票上限 2 万 (与实盘一致)。
  移动止盈启用时 confirm="real" (条件单语义: 创新高 bar 不触发/跳空按开盘价/
  触线按线价 —— 实盘可 1:1 复现, 用户铁律)。
  目标: 年化 ≥ 15%。

  与 quantqq_5m_sweep.py 的差异: period 1m (240 根/日, 非标准 bar 过滤走
  STD_BAR_TIMES["1m"])、区间/本金/股票池按上口径、trailing confirm="real"、
  达标线 23%→15%。

设计 (同 5m 版):
  prep   — 选股(QUANTQQ, 1d) + 1m 稀疏窗口取数(本地缓存) + 构建矩阵落盘 (.npy mmap)。
  run    — mmap 加载矩阵, 按分片跑组合, 逐行追加 CSV (断点续跑)。
  report — 汇总 CSV, 按约束筛达标组合, 输出 Top 表。

用法:
    python tools/quantqq_1m_sweep.py prep
    python tools/quantqq_1m_sweep.py run --shard 0 --nshards 6 --stage coarse
    python tools/quantqq_1m_sweep.py report
"""
import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.logger import get_logger
from core.farm_rules import TARGET_ANN, TARGET_MAXDD
# 2026-09-20 审计: _load_cache (模块级函数) 要用这两个名字, 必须模块级导入 ——
# 原先只在 do_prep 内局部导入 → 调用即 NameError。
from backtest.engine import PREP_SEAM, check_prep_caliber

logger = get_logger(__name__)

START = "20260128"
END = "20260813"          # 1m 缓存最新完整交易日 (2026-08-14 盘中数据不全)
FORMULA = "QUANTQQ"
WINDOW_TD = 60            # 稀疏窗口交易日: > max_hold_days(40) + 15 缓冲 (engine 铁律)
CAPITAL = 1_000_000.0     # 用户拍板: 100 万 (贴近实盘资产量级)
MAX_BUY = 20_000.0        # 单票上限 2 万 (与实盘 auto_buy 一致)
UNIVERSE = {"type": "50", "exclude_st": True, "exclude_new_listings_days": 60}
TARGET_ANNRET = TARGET_ANN  # 达标口径单一真相源 core/farm_rules.py: 年化≥15% 且 回撤≤15%
TARGET_MAXDD_LIM = TARGET_MAXDD  # |最大回撤| 上限 (P0-7 补回撤腿)
MIN_TRADES = 300          # 6.5 个月区间的统计显著性下限 (5m 版 2 年用 1000 折算)


def _cache_dir(window_td: int) -> str:
    base = os.path.join("output", "quantqq_1m_sweep")
    return os.path.join(base, "cache" if window_td == WINDOW_TD else f"cache_w{window_td}")


RESULTS_DIR = os.path.join("output", "quantqq_1m_sweep")

# === 粗扫网格 (priority 固定 trailing_first — 沿用 5m 版口径) ===
# 1m 单组合耗时 ≈ 5m 的 5 倍, 粗扫先收敛到 324 组合, 命中区再细化
COARSE = {
    "cost": [-0.08, -0.12],
    "activation": [0.035, 0.05, 0.08],
    "drawdown": [0.005, 0.01, 0.02],
    "ladder": [
        ("off", []),
        ("s8_100", [(0.08, 1.0)]),
        ("m2_6-15_30", [(0.06, 0.30), (0.15, 0.30)]),
    ],
    "time_days": [12, 20, 40],
    "cond": [None, (7, 0.015)],
}
# 2×3×3×3×3×2 = 324 组合


def gen_coarse_combos():
    combos = []
    for cost in COARSE["cost"]:
        for act in COARSE["activation"]:
            for dd in COARSE["drawdown"]:
                for lname, levels in COARSE["ladder"]:
                    for tdays in COARSE["time_days"]:
                        for cond in COARSE["cond"]:
                            combos.append({
                                "cost": cost, "act": act, "dd": dd,
                                "ladder": lname, "levels": levels,
                                "time_days": tdays,
                                "cond_days": cond[0] if cond else 0,
                                "cond_profit": cond[1] if cond else 0.0,
                            })
    return combos


def combo_key(c):
    return (f"c{c['cost']}_a{c['act']}_d{c['dd']}_L{c['ladder']}"
            f"_t{c['time_days']}_cd{c['cond_days']}_cp{c['cond_profit']}")


def combo_stop_config(c, priority="trailing_first"):
    levels = [{"profit": p, "sell_ratio": r}
              for p, r in sorted(c["levels"], key=lambda x: x[0])]  # L1: 升序保证
    return {
        "priority": priority,
        "cost_stop": {"enabled": True, "threshold": float(c["cost"])},
        # 用户铁律: 移动止盈启用时必须条件单语义 (confirm="real")
        "trailing_stop": {"enabled": True, "activation": float(c["act"]),
                          "drawdown": float(c["dd"]), "confirm": "real"},
        "ladder_tp": {"enabled": bool(levels), "levels": levels},
        "time_stop": {"enabled": True, "max_hold_days": int(c["time_days"])},
        "cond_time_stop": {"enabled": c["cond_days"] > 0,
                           "days": int(c["cond_days"]) if c["cond_days"] > 0 else 7,
                           "profit": float(c["cond_profit"]) if c["cond_days"] > 0 else 0.01},
        "first_day": {"enabled": False},
    }


# ---------------------------------------------------------------- prep

def do_prep(args):
    """选股 + 1m 窗口取数 + 矩阵落盘。幂等: 有缓存则跳过。"""
    from backtest.engine import ENGINE_VERSION, BacktestEngine
    from selection.selector import StockSelector

    win_td = args.window_td
    cache_dir = _cache_dir(win_td)
    os.makedirs(cache_dir, exist_ok=True)
    sel_path = os.path.join(cache_dir, "selections.csv")
    meta_path = os.path.join(cache_dir, "meta.json")

    # 1. 选股 (QUANTQQ, 沪深A股, 1d, 前复权 — 与引擎 front 口径一致)
    if os.path.exists(sel_path):
        selections = pd.read_csv(sel_path, dtype={"stock_code": str})
        logger.info("[prep] 选股缓存命中: %d 信号", len(selections))
    else:
        sel_cfg = {
            "formula_name": FORMULA,
            "formula_arg": "",
            "universe": dict(UNIVERSE),
            "period": "1d",
            "dividend_type": 1,
        }
        t0 = time.time()
        selections = StockSelector(sel_cfg).run(start_time=START, end_time=END)
        if selections is None or len(selections) == 0:
            raise RuntimeError("QUANTQQ 选股零信号, 无法继续")
        selections.to_csv(sel_path, index=False)
        logger.info("[prep] 选股完成: %d 信号 %d 股, %.1fs",
                    len(selections), selections["stock_code"].nunique(), time.time() - t0)

    # 2. 引擎实例 (仅借其 helper; 配置与扫描口径一致)
    bt_cfg = {
        "initial_capital": CAPITAL, "commission": 0.0003, "slippage": 0.001,
        "stamp_tax": 0.0005, "enable_realistic_costs": True, "period": "1m",
        "position_sizing": {"min_buy_amount": 2000.0, "max_buy_amount": MAX_BUY,
                            "lot_size": 100, "min_lots": 1},
        "use_kline_cache": True,
    }
    engine = BacktestEngine(bt_cfg)

    # 3. 矩阵准备 (2026-09-19 架构修订批次 3.1: 收编到 engine 公开接缝
    #    prepare_matrices —— 本段原是 engine.run() 准备段的手工复刻,
    #    引擎一改即静默漂移, 详见架构审查 P1-7。口径变化: 接缝会把窗口
    #    终点截断到 END (2026-07-21 引擎口径), 旧复刻段不截断)
    t0 = time.time()
    prep = engine.prepare_matrices(selections, START, END, win_td)
    if prep is None:
        raise RuntimeError("QUANTQQ 1m 窗口取数为空, 无法继续")
    logger.info("[prep] 窗口取数+矩阵准备完成 %.1fs", time.time() - t0)

    close = prep["close"]
    idx, cols = prep["idx"], prep["cols"]
    high_np, low_np, open_np = prep["high"], prep["low"], prep["open"]
    tradable_np, last_tradable_idx = prep["tradable"], prep["last_tradable_idx"]
    # 涨停预过滤是 sweep 特有步骤 (stop 无关, 只滤一次), 不在接缝内
    entries = engine._filter_limit_up(prep["entries"], close)

    # 4. 落盘 (float64 mmap; bool/int 小数组同目录)
    np.save(os.path.join(cache_dir, "close.npy"), close.values.astype(np.float64))
    np.save(os.path.join(cache_dir, "high.npy"), high_np)
    np.save(os.path.join(cache_dir, "low.npy"), low_np)
    np.save(os.path.join(cache_dir, "open.npy"), open_np)
    np.save(os.path.join(cache_dir, "entries.npy"), entries.values.astype(bool))
    np.save(os.path.join(cache_dir, "tradable.npy"), tradable_np.astype(bool))
    np.save(os.path.join(cache_dir, "last_tradable_idx.npy"),
            np.asarray(last_tradable_idx, dtype=np.int64))
    meta = {
        "index": [str(t) for t in idx],
        "columns": [str(c) for c in cols],
        "start": START, "end": END, "formula": FORMULA,
        "window_td": win_td, "capital": CAPITAL, "max_buy": MAX_BUY,
        "universe": UNIVERSE, "period": "1m", "trailing_confirm": "real",
        "engine_version": ENGINE_VERSION,
        "prep_seam": PREP_SEAM,
        "n_signals": int(entries.values.sum()),
        "shape": [int(len(idx)), int(len(cols))],
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    gb = 4 * np.prod(meta["shape"]) * 8 / 1e9
    logger.info("[prep] 矩阵落盘完成: shape=%s, 信号=%d, 价格矩阵≈%.2fGB",
                meta["shape"], meta["n_signals"], gb)
    print(json.dumps({"status": "ok", **meta}))


# ---------------------------------------------------------------- run

def _load_cache(window_td=WINDOW_TD):
    cache_dir = _cache_dir(window_td)
    with open(os.path.join(cache_dir, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    check_prep_caliber(meta, where="quantqq_1m_sweep")
    idx = pd.DatetimeIndex(pd.to_datetime(meta["index"]))
    cols = meta["columns"]
    ld = lambda n, mmap=None: np.load(os.path.join(cache_dir, n), mmap_mode=mmap)
    close_df = pd.DataFrame(ld("close.npy", "r"), index=idx, columns=cols)
    mats = {
        "close_df": close_df,
        "entries_df": pd.DataFrame(ld("entries.npy"), index=idx, columns=cols),
        "high_np": ld("high.npy", "r"),
        "low_np": ld("low.npy", "r"),
        "open_np": ld("open.npy", "r"),
        "tradable_np": ld("tradable.npy"),
        "last_tradable_idx": ld("last_tradable_idx.npy"),
    }
    return meta, mats


# CSV 固定 schema (审计 H1: 成功/异常行同列集, error 列放最后, 防列错位)
CSV_COLUMNS = ["key", "cost", "act", "dd", "ladder", "time_days",
               "cond_days", "cond_profit", "cumret", "annret", "maxdd",
               "sharpe", "calmar", "winrate", "trades", "profit_factor",
               "avg_hold", "elapsed", "error"]


def do_run(args):
    import logging
    logging.getLogger().setLevel(logging.WARNING)  # 引擎每组合 INFO 刷屏, 扫描期压掉
    from backtest.engine import ENGINE_VERSION, BacktestEngine
    from backtest.prepared import PreparedMatrix

    meta, mats = _load_cache(args.window_td)
    if meta.get("engine_version") and meta["engine_version"] != ENGINE_VERSION:
        logger.warning("引擎版本漂移: prep 时=%s 现在=%s — 并行会话在改引擎, "
                       "Top 组合须用 engine.run() 复跑核对",
                       meta["engine_version"], ENGINE_VERSION)
    sel_path = os.path.join(_cache_dir(args.window_td), "selections.csv")
    selections = pd.read_csv(sel_path, dtype={"stock_code": str})

    engine = BacktestEngine({
        "initial_capital": CAPITAL, "commission": 0.0003, "slippage": 0.001,
        "stamp_tax": 0.0005, "enable_realistic_costs": True, "period": "1m",
        "position_sizing": {"min_buy_amount": 2000.0, "max_buy_amount": MAX_BUY,
                            "lot_size": 100, "min_lots": 1},
    })

    max_t = args.window_td - 15   # 窗口铁律: max_hold_days ≤ win_td - 15
    if args.stage == "coarse":
        combos = gen_coarse_combos()
    else:
        with open(args.combos_file, encoding="utf-8") as f:
            combos = json.load(f)
    skipped = [c for c in combos if int(c["time_days"]) > max_t]
    if skipped:
        logger.warning("跳过 %d 个组合: time_stop 超过窗口铁律 (win_td=%d → max %d 天)",
                       len(skipped), args.window_td, max_t)
        combos = [c for c in combos if int(c["time_days"]) <= max_t]
    # 分片: 按组合序号取模
    combos = [c for i, c in enumerate(combos) if i % args.nshards == args.shard]
    if args.limit:
        combos = combos[:args.limit]

    out_path = args.out or os.path.join(
        RESULTS_DIR, f"sweep_{args.stage}_shard{args.shard}of{args.nshards}.csv")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    done = set()
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:  # L2: 0字节守卫
        # M1: 只有成功行(annret 非空)算"已完成", 异常行重跑时重试
        prev = pd.read_csv(out_path)
        done = set(prev.loc[prev["annret"].notna(), "key"])
    header = not os.path.exists(out_path) or os.path.getsize(out_path) == 0

    n_done = 0
    t_start = time.time()
    with open(out_path, "a", encoding="utf-8", newline="") as fout:
        for c in combos:
            key = combo_key(c)
            if key in done:
                continue
            levels = c["levels"]
            ladder_profits = np.array([p for p, _ in levels], dtype=np.float64)
            ladder_ratios = np.array([r for _, r in levels], dtype=np.float64)
            t0 = time.time()
            try:
                prepared = PreparedMatrix(
                    close=mats["close_df"], entries=mats["entries_df"],
                    high_np=mats["high_np"], low_np=mats["low_np"],
                    open_np=mats["open_np"], tradable_np=mats["tradable_np"],
                    last_tradable_idx=mats["last_tradable_idx"])
                res = engine.run_cached(
                    prepared, combo_stop_config(c),
                    ladder_profits, ladder_ratios, len(levels),
                    filter_limit_up=False)   # prep 已预过滤
                m = res["metrics"]
                row = {
                    "key": key, "cost": c["cost"], "act": c["act"], "dd": c["dd"],
                    "ladder": c["ladder"], "time_days": c["time_days"],
                    "cond_days": c["cond_days"], "cond_profit": c["cond_profit"],
                    "cumret": m.get("cumulative_return", 0),
                    "annret": m.get("annualized_return", 0),
                    "maxdd": m.get("max_drawdown", 0),
                    "sharpe": m.get("sharpe_ratio", 0),
                    "calmar": m.get("calmar_ratio", 0),
                    "winrate": m.get("win_rate", 0),
                    "trades": m.get("total_trades", 0),
                    "profit_factor": m.get("profit_factor", 0),
                    "avg_hold": m.get("avg_hold_days", 0),
                    "elapsed": round(time.time() - t0, 2),
                    "error": "",
                }
            except Exception as e:
                # H1: 与成功行同 schema (全指标 None), error 放最后
                row = {col: None for col in CSV_COLUMNS}
                row.update({"key": key, "cost": c["cost"], "act": c["act"],
                            "dd": c["dd"], "ladder": c["ladder"],
                            "time_days": c["time_days"], "cond_days": c["cond_days"],
                            "cond_profit": c["cond_profit"],
                            "elapsed": round(time.time() - t0, 2),
                            "error": f"{type(e).__name__}: {str(e)[:80]}"})
            pd.DataFrame([row], columns=CSV_COLUMNS).to_csv(
                fout, header=header, index=False)
            header = False
            fout.flush()
            n_done += 1
            if n_done % 10 == 0:
                rate = n_done / (time.time() - t_start)
                eta = (len(combos) - n_done) / rate / 60 if rate > 0 else -1
                print(f"[shard {args.shard}] {n_done}/{len(combos)} "
                      f"({rate:.2f}/s, ETA {eta:.0f}min)", flush=True)
    print(json.dumps({"status": "ok", "shard": args.shard, "done": n_done,
                      "out": out_path,
                      "minutes": round((time.time() - t_start) / 60, 1)}))


# ---------------------------------------------------------------- report

def do_report(args):
    frames = []
    for f in os.listdir(RESULTS_DIR):
        if f.startswith("sweep_") and f.endswith(".csv"):
            frames.append(pd.read_csv(os.path.join(RESULTS_DIR, f)))
    if args.combos_csv:
        frames.append(pd.read_csv(args.combos_csv))
    df = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["key"], keep="last")
    n_err = int(df["error"].fillna("").ne("").sum())
    df = df[df["annret"].notna()]
    tgt = df[(df["annret"] >= TARGET_ANNRET) & (df["trades"] >= MIN_TRADES)
             & (df["maxdd"].abs() <= TARGET_MAXDD_LIM)]
    print(f"总组合: {len(df)}  失败: {n_err}  "
          f"达标(年化≥{TARGET_ANNRET:.0%} 且 回撤≤{TARGET_MAXDD_LIM:.0%} "
          f"且 交易≥{MIN_TRADES}): {len(tgt)}")
    cols = ["cost", "act", "dd", "ladder", "time_days", "cond_days", "cond_profit",
            "annret", "maxdd", "calmar", "sharpe", "winrate", "trades"]
    print("\n=== 达标 Top 20 (按 Calmar) ===")
    print(tgt.sort_values("calmar", ascending=False).head(20)[cols].to_string(index=False))
    print("\n=== 全体 Top 20 (按年化, 不看约束) ===")
    print(df.sort_values("annret", ascending=False).head(20)[cols].to_string(index=False))
    out = os.path.join(RESULTS_DIR, "report_merged.csv")
    df.to_csv(out, index=False)
    print(f"\n合并结果: {out}")


def main():
    ap = argparse.ArgumentParser(description="QUANTQQ 1m 止盈止损参数扫描")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pp0 = sub.add_parser("prep")
    pp0.add_argument("--window-td", type=int, default=WINDOW_TD)
    pr = sub.add_parser("run")
    pr.add_argument("--shard", type=int, default=0)
    pr.add_argument("--nshards", type=int, default=1)
    pr.add_argument("--stage", choices=["coarse", "refine"], default="coarse")
    pr.add_argument("--combos-file", default=None)
    pr.add_argument("--out", default=None)
    pr.add_argument("--limit", type=int, default=0)
    pr.add_argument("--window-td", type=int, default=WINDOW_TD)
    pp = sub.add_parser("report")
    pp.add_argument("--combos-csv", default=None)
    args = ap.parse_args()
    if args.cmd == "prep":
        do_prep(args)
    elif args.cmd == "run":
        do_run(args)
    else:
        do_report(args)


if __name__ == "__main__":
    main()
