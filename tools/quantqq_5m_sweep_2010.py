"""
QUANTQQ 5m 止盈止损参数扫描驱动 — 2010~2026H1 长区间验证版 (2026-08-14)

背景:
  用户拍板口径: 换区间验证 —— 1m/2026H1 粗扫最优区年化 ~9%（同期上证 -4%），
  怀疑是区间行情拖累；用 2010-01-01 ~ 2026-06-30 长区间 5m 复扫同一网格,
  看最优参数在跨牛熊样本下能否上年化 15%。
  5m 缓存覆盖 2004 至今 (本地 parquet), 无需联网取数。
  QUANTQQ 公式, 沪深A股池 (universe type "50", 排 ST/次新 60 天, 与实盘同口径),
  本金 100 万, 单票上限 2 万。
  移动止盈 confirm="real" (条件单语义: 创新高 bar 不触发/跳空按开盘价/
  触线按线价 —— 实盘可 1:1 复现, 用户铁律)。
  目标: 年化 ≥ 15%。

  与 quantqq_1m_sweep.py 的差异: period 5m (48 根/日)、区间 2010~2026H1、
  统计显著性下限按 16.5 年提高到 3000 笔。

设计 (同 1m/5m 版):
  prep   — 选股(QUANTQQ, 1d) + 5m 稀疏窗口取数(本地缓存) + 构建矩阵落盘 (.npy mmap)。
  run    — mmap 加载矩阵, 按分片跑组合, 逐行追加 CSV (断点续跑)。
  report — 汇总 CSV, 按约束筛达标组合, 输出 Top 表。

用法:
    python tools/quantqq_5m_sweep_2010.py prep
    python tools/quantqq_5m_sweep_2010.py run --shard 0 --nshards 6 --stage coarse
    python tools/quantqq_5m_sweep_2010.py report
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

logger = get_logger(__name__)

START = "20150101"          # 2026-08-14 改: 用户口径"2015至今"; 2010 长区间矩阵~28GB 超内存, 2015 减负
END = "20260630"          # 用户拍板区间终点
# 2026-08-15: concat 中间产物是终态矩阵的 ~桶数倍 (2 年≈7GB / 11.5 年≈300GB),
# 64G 机器扛不住整段 —— 支持按 2 年一段分段跑, 环境变量覆盖区间+输出子目录:
#   SWEEP_START/SWEEP_END/SWEEP_TAG (如 TAG=y2015_2016 → output/.../y2015_2016/)
START = os.environ.get("SWEEP_START", START)
END = os.environ.get("SWEEP_END", END)
TAG = os.environ.get("SWEEP_TAG", "")
FORMULA = "QUANTQQ"
WINDOW_TD = 60            # 稀疏窗口交易日: > max_hold_days(40) + 15 缓冲 (engine 铁律)
CAPITAL = 1_000_000.0     # 与 1m 版一致: 100 万
MAX_BUY = 50_000.0        # 单票上限 5 万 = 本金 100 万的 5% (用户本次口径; 原 2 万=2%)
UNIVERSE = {"type": "50", "exclude_st": True, "exclude_new_listings_days": 60}
TARGET_ANNRET = 0.15      # 用户拍板: 年化不低于 15%
MIN_TRADES = int(os.environ.get("SWEEP_MIN_TRADES", "3000"))  # 整段 11.5 年用 3000; 分段(2年)用 300


def _cache_dir(window_td: int) -> str:
    base = os.path.join("output", "quantqq_5m_sweep_2010", TAG)
    return os.path.join(base, "cache" if window_td == WINDOW_TD else f"cache_w{window_td}")


RESULTS_DIR = os.path.join("output", "quantqq_5m_sweep_2010", TAG)

# === 粗扫网格 (priority 固定 trailing_first — 与 1m 版同网格, 保证两区间可比) ===
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


# === 宽网格 (2026-08-14 用户要求: 324 之外试更多组合) ===
# 关键扩展 vs COARSE:
#   ① cost 加宽到 -50% (5m 冠军区; COARSE 只有 -8%/-12% 漏了它)
#   ② drawdown 降到 0.3% (冠军区; COARSE 最小 0.5%)
#   ③ time_days 加到 60 (冠军区)
#   ④ 新增 priority 三档 (止损优先/阶梯优先/移动优先) —— COARSE 固定 trailing_first
# 3×4×4×4×3×3×2 = 3456 组合 (COARSE 的 10.7 倍)
WIDE = {
    "priority": ["trailing_first", "stop_first", "ladder_tp_first"],
    "cost": [-0.12, -0.20, -0.35, -0.50],
    "activation": [0.03, 0.06, 0.08, 0.12],
    "drawdown": [0.003, 0.006, 0.01, 0.02],
    "ladder": [
        ("off", []),
        ("s8_100", [(0.08, 1.0)]),
        ("m2_6-15_30", [(0.06, 0.30), (0.15, 0.30)]),
    ],
    "time_days": [10, 30, 45],
    "cond": [None, (7, 0.015)],
}


def gen_wide_combos():
    combos = []
    for pri in WIDE["priority"]:
        for cost in WIDE["cost"]:
            for act in WIDE["activation"]:
                for dd in WIDE["drawdown"]:
                    for lname, levels in WIDE["ladder"]:
                        for tdays in WIDE["time_days"]:
                            for cond in WIDE["cond"]:
                                combos.append({
                                    "priority": pri,
                                    "cost": cost, "act": act, "dd": dd,
                                    "ladder": lname, "levels": levels,
                                    "time_days": tdays,
                                    "cond_days": cond[0] if cond else 0,
                                    "cond_profit": cond[1] if cond else 0.0,
                                })
    return combos


def combo_key(c):
    return (f"p{c.get('priority', 'trailing_first')}"
            f"_c{c['cost']}_a{c['act']}_d{c['dd']}_L{c['ladder']}"
            f"_t{c['time_days']}_cd{c['cond_days']}_cp{c['cond_profit']}")


def combo_stop_config(c, priority="trailing_first"):
    levels = [{"profit": p, "sell_ratio": r}
              for p, r in sorted(c["levels"], key=lambda x: x[0])]  # L1: 升序保证
    return {
        "priority": c.get("priority", priority),
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
    """选股 + 5m 窗口取数 + 矩阵落盘。幂等: 有缓存则跳过。"""
    # 2026-08-14: tqcenter C 扩展在连接掉线/异常时直接调 C 层 exit() 静默杀进程
    # (v1 segfault / v3+v4 exit 1 无输出 / 早间 backfill 无声死亡,
    # Windows 事件查看器 0xc0000005 访问违例, 模块 unknown)。
    # VERA_SWEEP_NO_TDX=1 时禁止 TDX 初始化 —— 只读缓存模式下 prep 全程无需网络,
    # 从根上拔掉引信。无缓存记录的票会因无法拉取而报错暴露 (可接受, 正常不该有)。
    if os.environ.get("VERA_SWEEP_NO_TDX") == "1":
        from core.connector import TdxConnector
        TdxConnector.initialize = classmethod(
            lambda cls: (_ for _ in ()).throw(
                RuntimeError("本进程已禁用 TDX (VERA_SWEEP_NO_TDX)")))
        # 但窗口计算需要交易日历 —— 本地 calendar parquet 只覆盖 2012 年起,
        # 不够 2010 起点; 改用 000001.SZ 的 1d 缓存日期充当交易日历
        # (平安银行 16 年几乎无长期停牌, 零星缺日对窗口步进影响可忽略)。
        from core.data_fetcher import DataFetcher as _DF
        _cal = None

        def _local_trading_days(cls, start_time, end_time, market="SH"):
            nonlocal _cal
            if _cal is None:
                _df = pd.read_parquet("data/kline_cache/1d/000001.SZ.parquet",
                                      columns=["date"])
                _cal = sorted(pd.to_datetime(_df["date"], format="%Y%m%d"))
            s, e = pd.Timestamp(start_time), pd.Timestamp(end_time)
            return [d for d in _cal if s <= d <= e]
        _DF.get_trading_days = classmethod(_local_trading_days)
    from backtest._constants import STD_BAR_TIMES
    from backtest.engine import (
        ENGINE_VERSION,
        BacktestEngine,
        _build_tradable_from_raw,
        recompute_last_tradable_idx,
    )
    from core.data_fetcher import DataFetcher
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
        "stamp_tax": 0.0005, "enable_realistic_costs": True, "period": "5m",
        "position_sizing": {"min_buy_amount": 2000.0, "max_buy_amount": MAX_BUY,
                            "lot_size": 100, "min_lots": 1},
        "use_kline_cache": True,
    }
    engine = BacktestEngine(bt_cfg)

    # 3. 5m 稀疏窗口取数 (本地缓存, 2004 至今覆盖本区间)
    # end_time 截断到区间终点 (2026-08-14 修复): 不截断则窗口尾巴伸到"今天",
    # 每只票都触发缓存末段之后的 TDX 补拉 —— 既慢又踩中偶发段错误路径
    t0 = time.time()
    kline, window_mask = DataFetcher.get_kline_windowed(
        selections, period="5m", window_trading_days=win_td,
        dividend_type="front", fill_data=False, use_cache=True,
        end_time=END,
    )
    logger.info("[prep] 窗口取数完成 %.1fs", time.time() - t0)

    close = engine._ensure_index(kline["Close"])
    high_df = engine._ensure_index(kline["High"])
    low_df = engine._ensure_index(kline["Low"])
    open_df = engine._ensure_index(kline["Open"])

    # 5m 非标准时刻 bar 过滤 (同 engine.run() 口径, 48 根/天不变量)
    close, high_df, low_df, open_df = BacktestEngine._drop_nonstandard_intraday_bars(
        close, high_df, low_df, open_df, STD_BAR_TIMES["5m"])

    entries = engine._build_entry_signals(selections, close)
    cols = sorted(close.columns.intersection(entries.columns))
    cols = sorted(set(cols) & set(high_df.columns) & set(low_df.columns))

    close_raw = close.reindex(index=close.index, columns=cols)
    close = close_raw.ffill()
    entries = entries.reindex(index=close.index, columns=cols, fill_value=False)
    entries = engine._filter_limit_up(entries, close)  # stop 无关, 预过滤一次
    idx = close.index

    high_np = high_df.reindex(index=idx, columns=cols).ffill().values.astype(np.float64)
    low_np = low_df.reindex(index=idx, columns=cols).ffill().values.astype(np.float64)
    # Open 不 ffill (停牌 NaN 保留, P1-1/P1-2)
    open_np = open_df.reindex(index=idx, columns=cols).values.astype(np.float64)

    tradable_np, _ = _build_tradable_from_raw(close_raw, close)
    wm = window_mask.reindex(index=idx, columns=cols, fill_value=False).values.astype(bool)
    tradable_np = tradable_np & wm
    last_tradable_idx = recompute_last_tradable_idx(tradable_np)

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
        "universe": UNIVERSE, "period": "5m", "trailing_confirm": "real",
        "engine_version": ENGINE_VERSION,
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
CSV_COLUMNS = ["key", "priority", "cost", "act", "dd", "ladder", "time_days",
               "cond_days", "cond_profit", "cumret", "annret", "maxdd",
               "sharpe", "calmar", "winrate", "trades", "profit_factor",
               "avg_hold", "elapsed", "error"]


def do_run(args):
    import logging
    logging.getLogger().setLevel(logging.WARNING)  # 引擎每组合 INFO 刷屏, 扫描期压掉
    from backtest.engine import ENGINE_VERSION, BacktestEngine

    meta, mats = _load_cache(args.window_td)
    if meta.get("engine_version") and meta["engine_version"] != ENGINE_VERSION:
        logger.warning("引擎版本漂移: prep 时=%s 现在=%s — 并行会话在改引擎, "
                       "Top 组合须用 engine.run() 复跑核对",
                       meta["engine_version"], ENGINE_VERSION)
    sel_path = os.path.join(_cache_dir(args.window_td), "selections.csv")
    selections = pd.read_csv(sel_path, dtype={"stock_code": str})

    engine = BacktestEngine({
        "initial_capital": CAPITAL, "commission": 0.0003, "slippage": 0.001,
        "stamp_tax": 0.0005, "enable_realistic_costs": True, "period": "5m",
        "position_sizing": {"min_buy_amount": 2000.0, "max_buy_amount": MAX_BUY,
                            "lot_size": 100, "min_lots": 1},
    })

    max_t = args.window_td - 15   # 窗口铁律: max_hold_days ≤ win_td - 15
    if args.stage == "coarse":
        combos = gen_coarse_combos()
    elif args.stage == "wide":
        combos = gen_wide_combos()
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
                res = engine.run_cached(
                    mats["close_df"], mats["entries_df"],
                    mats["high_np"], mats["low_np"],
                    combo_stop_config(c), selections,
                    ladder_profits, ladder_ratios, len(levels),
                    filter_limit_up=False,   # prep 已预过滤
                    open_np=mats["open_np"],
                    tradable_np=mats["tradable_np"],
                    last_tradable_idx=mats["last_tradable_idx"],
                )
                m = res["metrics"]
                row = {
                    "key": key, "priority": c.get("priority", "trailing_first"),
                    "cost": c["cost"], "act": c["act"], "dd": c["dd"],
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
                row.update({"key": key, "priority": c.get("priority", "trailing_first"),
                            "cost": c["cost"], "act": c["act"],
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
    tgt = df[(df["annret"] >= TARGET_ANNRET) & (df["trades"] >= MIN_TRADES)]
    print(f"总组合: {len(df)}  失败: {n_err}  "
          f"达标(年化≥{TARGET_ANNRET:.0%} 且 交易≥{MIN_TRADES}): {len(tgt)}")
    cols = ["priority", "cost", "act", "dd", "ladder", "time_days", "cond_days", "cond_profit",
            "annret", "maxdd", "calmar", "sharpe", "winrate", "trades"]
    print("\n=== 达标 Top 20 (按 Calmar) ===")
    print(tgt.sort_values("calmar", ascending=False).head(20)[cols].to_string(index=False))
    print("\n=== 全体 Top 20 (按年化, 不看约束) ===")
    print(df.sort_values("annret", ascending=False).head(20)[cols].to_string(index=False))
    out = os.path.join(RESULTS_DIR, "report_merged.csv")
    df.to_csv(out, index=False)
    print(f"\n合并结果: {out}")


def main():
    ap = argparse.ArgumentParser(description="QUANTQQ 5m 止盈止损参数扫描 (2010~2026H1 长区间)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pp0 = sub.add_parser("prep")
    pp0.add_argument("--window-td", type=int, default=WINDOW_TD)
    pr = sub.add_parser("run")
    pr.add_argument("--shard", type=int, default=0)
    pr.add_argument("--nshards", type=int, default=1)
    pr.add_argument("--stage", choices=["coarse", "wide", "refine"], default="coarse")
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
