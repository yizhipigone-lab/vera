"""
gs_txt 5m 止盈止损全参数扫描驱动 — 任意公式 (2026-07-18)

把 quantqq_5m_sweep.py 的 5m 全参数扫描能力, 从 QUANTQQ 单公式扩展到任意 gs_1 公式.
不修改原 quantqq_5m_sweep.py (规则3: 不破坏已验证功能), combo 网格函数直接 import 复用.

口径铁律 (沿用 QUANTQQ, 已验证): 信号日 T 最后一根 5m bar (15:00) 收盘买入.
资金 300万 / 单票上限 2万 / 前复权 / 5m / priority=移动止盈优先.
达标 (用户口径, 比 QUANTQQ 23% 严): 年化>30% 且 回撤≤15% 且 交易≥1000笔.

用法:
    python tools/gs_5m_sweep.py prep 黑马绝技 --start 20240801 --end 20260717
    python tools/gs_5m_sweep.py run 黑马绝技 --shard 0 --nshards 6 --limit 3
    python tools/gs_5m_sweep.py report 黑马绝技

缓存按公式名隔离: output/gs_5m_sweep/<safe_name>/cache + sweep_*.csv
"""
import argparse
import json
import os
import re
import sys
import time

import numpy as np
import pandas as pd

# 项目根 + tools/ (sibling import quantqq_5m_sweep 的 combo 函数)
_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS)
sys.path.insert(0, _ROOT)
sys.path.insert(0, _THIS)

from quantqq_5m_sweep import (  # noqa: E402
    CSV_COLUMNS,
    combo_key,
    combo_stop_config,
    gen_coarse_combos,
)

from utils.config_loader import ConfigLoader  # noqa: E402
from utils.logger import get_logger  # noqa: E402

logger = get_logger("gs_5m_sweep")

WINDOW_TD = 60            # 稀疏窗口交易日: > max_hold_days(40) + 15 缓冲
DEFAULT_CAPITAL = 3_000_000.0
DEFAULT_MAX_BUY = 20_000.0
# 2026-09-11: 达标口径收口到 core/farm_rules (单一真相源)。
# 用户拍板 15%: 年化≥15% 且 |最大回撤|≤15% 且 笔数≥20 (不足 20 笔记「样本不足」)。
# 此前本文件硬编码 0.30/0.15/1000, 与 09-09 批农场粗扫实际在用的 15% 冲突 —— 两套口径
# 并存是 2026-09-11 审计认定的缺陷 (详见 docs/plan/2026-09-11_公式农场粗扫报告修复_计划书.md)。
from core import farm_rules  # noqa: E402

TARGET_ANN = farm_rules.TARGET_ANN
TARGET_MAXDD = farm_rules.TARGET_MAXDD
MIN_TRADES = farm_rules.MIN_TRADES


def _safe_dir(name: str) -> str:
    """公式名 -> Windows 安全目录名 (替换禁用字符, strip 首尾空格/点)."""
    s = re.sub(r'[\\/:*?"<>|]', "_", name).strip().rstrip(".")
    return s or "UNNAMED"


BASE = "output/gs_5m_sweep"
PRIORITY = "trailing_first"  # 默认移动止盈优先; stop_first=止损优先


def _formula_dir(formula: str) -> str:
    # 2026-09-09: 样本外分段验证 — SWEEP_TAG 环境变量隔离目录 (仿 quantqq_5m_sweep_2010)
    tag = os.environ.get("SWEEP_TAG", "")
    base = os.path.join(BASE, tag) if tag else BASE
    return os.path.join(base, _safe_dir(formula))


def _cache_dir(formula: str, window_td: int) -> str:
    base = _formula_dir(formula)
    return os.path.join(base, "cache" if window_td == WINDOW_TD else f"cache_w{window_td}")


# ---------------------------------------------------------------- prep

def do_prep(args):
    """选股(formula) + 5m 窗口取数 + 矩阵落盘. 幂等: 有缓存则跳过."""
    from backtest.engine import (
        ENGINE_VERSION,
        BacktestEngine,
    )
    from selection.selector import StockSelector

    formula = args.formula
    win_td = args.window_td
    cache_dir = _cache_dir(formula, win_td)
    os.makedirs(cache_dir, exist_ok=True)
    sel_path = os.path.join(cache_dir, "selections.csv")
    meta_path = os.path.join(cache_dir, "meta.json")

    capital = args.capital
    max_buy = args.max_buy

    # 1. 选股 (formula, 全A, 1d, 前复权)
    if os.path.exists(sel_path):
        selections = pd.read_csv(sel_path, dtype={"stock_code": str})
        logger.info("[prep:%s] 选股缓存命中: %d 信号", formula, len(selections))
    else:
        defaults = ConfigLoader.load_defaults()
        sel_tpl = defaults.get("selection", {})
        uni = sel_tpl.get("universe", {"type": "50", "exclude_st": True})
        if getattr(args, "universe_type", None):  # 2026-09-06: 公式农场批量可加池, 默认不变
            uni = dict(uni, type=str(args.universe_type))
        sel_cfg = {
            "formula_name": formula,
            "formula_arg": args.formula_arg if args.formula_arg is not None else "",
            "universe": uni,
            "period": "1d",
            "dividend_type": 1,
        }
        t0 = time.time()
        selections = StockSelector(sel_cfg).run(start_time=args.start, end_time=args.end)
        if selections is None or len(selections) == 0:
            # 公式不存在 OR 真零信号 — 无法区分, 统一标记跳过 (不 raise, 批量不断链)
            print(json.dumps({"status": "no_signals", "formula": formula,
                              "hint": "公式名TDX不可解析 OR 区间内零信号"}))
            return
        selections.to_csv(sel_path, index=False)
        logger.info("[prep:%s] 选股完成: %d 信号 %d 股, %.1fs",
                    formula, len(selections), selections["stock_code"].nunique(),
                    time.time() - t0)

    # 2. 引擎配置 (借 helper; 与扫描口径一致)
    bt_cfg = {
        "initial_capital": capital, "commission": 0.0003, "slippage": 0.001,
        "stamp_tax": 0.0005, "enable_realistic_costs": True, "period": "5m",
        "position_sizing": {"min_buy_amount": 2000.0, "max_buy_amount": max_buy,
                            "lot_size": 100, "min_lots": 1},
        "use_kline_cache": True,
    }
    engine = BacktestEngine(bt_cfg)

    # 3. 矩阵准备 (2026-09-19 架构修订批次 3.1: 收编到 engine 公开接缝
    #    prepare_matrices —— 本段原是 engine.run() 准备段的手工复刻,
    #    引擎一改即静默漂移, 详见架构审查 P1-7。口径变化提示: 接缝会把
    #    窗口终点截断到 args.end (2026-07-21 引擎口径), 旧复刻段不截断
    #    → meta 加 prep_seam 标记, 旧缓存加载时告警)
    t0 = time.time()
    prep = engine.prepare_matrices(selections, args.start, args.end, win_td)
    if prep is None:
        # 池内取数全空 (多为瞬时, 补数据后即有) → 打标返回, 批量层下轮重试
        logger.warning("[prep:%s] 窗口取数为空, 本轮跳过(下轮重试)", formula)
        print(json.dumps({"status": "no_kline", "formula": formula}))
        return
    logger.info("[prep:%s] 窗口取数+矩阵准备完成 %.1fs", formula, time.time() - t0)

    close = prep["close"]
    idx, cols = prep["idx"], prep["cols"]
    high_np, low_np, open_np = prep["high"], prep["low"], prep["open"]
    tradable_np, last_tradable_idx = prep["tradable"], prep["last_tradable_idx"]
    # 涨停预过滤是 sweep 特有步骤 (stop 无关, 只滤一次), 不在接缝内
    entries = engine._filter_limit_up(prep["entries"], close)

    np.save(os.path.join(cache_dir, "close.npy"), close.values.astype(np.float64))
    np.save(os.path.join(cache_dir, "high.npy"), high_np)
    np.save(os.path.join(cache_dir, "low.npy"), low_np)
    np.save(os.path.join(cache_dir, "open.npy"), open_np)
    np.save(os.path.join(cache_dir, "entries.npy"), entries.values.astype(bool))
    np.save(os.path.join(cache_dir, "tradable.npy"), tradable_np.astype(bool))
    np.save(os.path.join(cache_dir, "last_tradable_idx.npy"),
            np.asarray(last_tradable_idx, dtype=np.int64))
    meta = {
        "index": [str(t) for t in idx], "columns": [str(c) for c in cols],
        "start": args.start, "end": args.end, "formula": formula,
        "window_td": win_td, "capital": capital, "max_buy": max_buy,
        "engine_version": ENGINE_VERSION,
        "prep_seam": "engine_prepare_matrices@2026-09-19",
        "n_signals": int(entries.values.sum()), "shape": [int(len(idx)), int(len(cols))],
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    gb = 4 * np.prod(meta["shape"]) * 8 / 1e9
    logger.info("[prep:%s] 矩阵落盘: shape=%s, 信号=%d, 价格矩阵≈%.2fGB",
                formula, meta["shape"], meta["n_signals"], gb)
    # 精简 print (不打印 index 数组, 避免输出爆炸)
    print(json.dumps({"status": "ok", "formula": formula, "shape": meta["shape"],
                      "n_signals": meta["n_signals"], "start": args.start,
                      "end": args.end, "matrix_gb": round(gb, 2)}))


# ---------------------------------------------------------------- run

def _load_cache(formula, window_td=WINDOW_TD):
    cache_dir = _cache_dir(formula, window_td)
    with open(os.path.join(cache_dir, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    idx = pd.DatetimeIndex(pd.to_datetime(meta["index"]))
    cols = meta["columns"]
    ld = lambda n, mmap=None: np.load(os.path.join(cache_dir, n), mmap_mode=mmap)
    close_df = pd.DataFrame(ld("close.npy", "r"), index=idx, columns=cols)
    mats = {
        "close_df": close_df,
        "entries_df": pd.DataFrame(ld("entries.npy"), index=idx, columns=cols),
        "high_np": ld("high.npy", "r"), "low_np": ld("low.npy", "r"),
        "open_np": ld("open.npy", "r"),
        "tradable_np": ld("tradable.npy"),
        "last_tradable_idx": ld("last_tradable_idx.npy"),
    }
    return meta, mats


def do_run(args):
    import logging
    logging.getLogger().setLevel(logging.WARNING)
    from backtest.engine import ENGINE_VERSION, BacktestEngine
    from backtest.prepared import PreparedMatrix

    formula = args.formula
    meta, mats = _load_cache(formula, args.window_td)
    if meta.get("engine_version") and meta["engine_version"] != ENGINE_VERSION:
        logger.warning("[%s] 引擎版本漂移: prep=%s 现在=%s, Top 组合须 run() 复跑核对",
                       formula, meta["engine_version"], ENGINE_VERSION)
    # 2026-09-19 批次 3.1: 旧缓存是"手工复刻配方"产物 (窗口不截断到 end),
    # 接缝口径是截断的 —— 缺标记的旧缓存告警, 提示重 prep
    if meta.get("prep_seam") != "engine_prepare_matrices@2026-09-19":
        logger.warning("[%s] 缓存为旧复刻配方产物 (窗口口径不同), 建议重跑 prep",
                       formula)
    sel_path = os.path.join(_cache_dir(formula, args.window_td), "selections.csv")
    selections = pd.read_csv(sel_path, dtype={"stock_code": str})

    engine = BacktestEngine({
        "initial_capital": meta.get("capital", DEFAULT_CAPITAL),
        "commission": 0.0003, "slippage": 0.001, "stamp_tax": 0.0005,
        "enable_realistic_costs": True, "period": "5m",
        "position_sizing": {"min_buy_amount": 2000.0,
                            "max_buy_amount": meta.get("max_buy", DEFAULT_MAX_BUY),
                            "lot_size": 100, "min_lots": 1},
    })

    max_t = args.window_td - 15
    if args.stage == "coarse":
        combos = gen_coarse_combos()
    else:
        with open(args.combos_file, encoding="utf-8") as f:
            combos = json.load(f)
    combos = [c for c in combos if int(c["time_days"]) <= max_t]
    combos = [c for i, c in enumerate(combos) if i % args.nshards == args.shard]
    if args.limit:
        combos = combos[:args.limit]

    out_path = args.out or os.path.join(
        _formula_dir(formula), f"sweep_{args.stage}_shard{args.shard}of{args.nshards}.csv")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    done = set()
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
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
                    prepared, combo_stop_config(c, PRIORITY),
                    ladder_profits, ladder_ratios, len(levels),
                    filter_limit_up=False)
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
                row = {col: None for col in CSV_COLUMNS}
                row.update({"key": key, "cost": c["cost"], "act": c["act"],
                            "dd": c["dd"], "ladder": c["ladder"],
                            "time_days": c["time_days"], "cond_days": c["cond_days"],
                            "cond_profit": c["cond_profit"],
                            "elapsed": round(time.time() - t0, 2),
                            "error": f"{type(e).__name__}: {str(e)[:80]}"})
            pd.DataFrame([row], columns=CSV_COLUMNS).to_csv(fout, header=header, index=False)
            header = False
            fout.flush()
            n_done += 1
            if n_done % 25 == 0:
                rate = n_done / (time.time() - t_start)
                eta = (len(combos) - n_done) / rate / 60 if rate > 0 else -1
                print(f"[{formula} shard {args.shard}] {n_done}/{len(combos)} "
                      f"({rate:.2f}/s, ETA {eta:.0f}min)", flush=True)
    print(json.dumps({"status": "ok", "formula": formula, "shard": args.shard,
                      "done": n_done, "out": out_path,
                      "minutes": round((time.time() - t_start) / 60, 1)}))


# ---------------------------------------------------------------- report

def do_report(args):
    formula = args.formula
    fdir = _formula_dir(formula)
    frames = []
    for f in os.listdir(fdir):
        if f.startswith("sweep_") and f.endswith(".csv"):
            frames.append(pd.read_csv(os.path.join(fdir, f)))
    if not frames:
        print(json.dumps({"status": "no_results", "formula": formula}))
        return
    df = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["key"], keep="last")
    n_err = int(df["error"].fillna("").ne("").sum())
    df = df[df["annret"].notna()]
    n_pass = int(df.apply(farm_rules.is_pass, axis=1).sum())
    n_thin = int(sum(1 for _, r in df.iterrows()
                     if farm_rules.verdict(r["annret"], r["maxdd"],
                                           r["trades"])["code"] == farm_rules.THIN))
    print(f"[{formula}] 总组合:{len(df)} 失败:{n_err} "
          f"达标:{n_pass} 样本不足:{n_thin} | {farm_rules.describe()}")
    cols = ["cost", "act", "dd", "ladder", "time_days", "cond_days", "cond_profit",
            "annret", "maxdd", "calmar", "sharpe", "winrate", "trades"]
    passed = df[df.apply(farm_rules.is_pass, axis=1)]       # 2026-09-11: 原来是 tgt (已并入 n_pass)
    if len(passed):
        print("\n=== 达标 Top (按 Calmar) ===")
        print(passed.sort_values("calmar", ascending=False).head(15)[cols].to_string(index=False))
    print("\n=== 全体 Top 5 (按年化, 不看约束) ===")
    print(df.sort_values("annret", ascending=False).head(5)[cols].to_string(index=False))
    df.to_csv(os.path.join(fdir, "report_merged.csv"), index=False)


def main():
    ap = argparse.ArgumentParser(description="gs_txt 5m 全参数扫描 (任意公式)")
    ap.add_argument("cmd", choices=["prep", "run", "report"])
    ap.add_argument("formula", help="TDX 公式名 (如 黑马绝技 / GUPIAO_001)")
    ap.add_argument("--start", default="20240801")
    ap.add_argument("--end", default="20260717")
    ap.add_argument("--formula-arg", default=None, help="公式参数 (多数留空)")
    ap.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    ap.add_argument("--max-buy", type=float, default=DEFAULT_MAX_BUY)
    ap.add_argument("--window-td", type=int, default=WINDOW_TD)
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--nshards", type=int, default=1)
    ap.add_argument("--stage", choices=["coarse", "refine"], default="coarse")
    ap.add_argument("--combos-file", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--priority", default="trailing_first",
                    choices=["trailing_first", "stop_first"],
                    help="trailing_first=移动止盈优先(默认), stop_first=止损优先")
    ap.add_argument("--universe-type", default=None,
                    help="股票池 type 覆盖(默认取 config; 23=沪深300 50=全A), 公式农场批量加速用")
    args = ap.parse_args()

    global BASE, PRIORITY
    PRIORITY = args.priority
    if PRIORITY != "trailing_first":
        BASE = f"output/gs_5m_sweep_{PRIORITY}"  # 止损优先输出新目录, 不覆盖

    if args.cmd == "prep":
        do_prep(args)
    elif args.cmd == "run":
        do_run(args)
    else:
        do_report(args)


if __name__ == "__main__":
    main()
