"""自动策略迭代循环 — 随机生成策略 → 回测 → 判定绩效 → 不达标换思路重来 (2026-08-10)。

用法:
    python auto_iter/auto_strategy_loop.py                # 默认 50 轮, 股票池 ~2500 只
    python auto_iter/auto_strategy_loop.py --smoke        # 冒烟: 200 只池, 1 轮
    python auto_iter/auto_strategy_loop.py --max-iter 5 --seed 42
    python auto_iter/auto_strategy_loop.py --refine       # 定向变异: 从 iter_log.csv
                                                          # 双榜单播种, 默认 200 轮

流程 (每轮):
    1. 随机二选一思路: 左侧逆势(均值回归) / 右侧趋势突破, 随机因子组合 + 随机参数
    2. 随机采样止盈止损 (成本止损/阶梯止盈/移动止盈/时间止损/优先级)
    3. 向量化算全市场信号 → BacktestEngine.run_cached 回测 2019-01-01~2026-07-31
    4. 达标判定: 年化 >= 25% 且最大回撤 >= -15% (回撤为负值)
    5. 该轮完整可独立运行的复现脚本落盘 output/auto_iter/iter_NNN_*.py
    6. 结果追加 output/auto_iter/iter_log.csv

离线保证: 信号自己从本地 parquet 算 (不走 DataFetcher), 回测走 run_cached
预取矩阵入口 (不触发 KlineCache 的 miss-fetch/缺口补拉/复权探针), 并
enforce_offline() 把 TdxConnector.tq 替换成抛错桩 — 任何残留联网尝试直接炸。
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import pprint
import re
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 允许直接 python 脚本路径跑

from auto_iter.common import (  # noqa: E402
    BT_START, BT_END, DATA_START, DEFAULT_BT_CFG, LEFT_FACTORS, MARKET_FACTORS,
    RIGHT_FACTORS, REPO_ROOT, build_entries_and_bear, build_pool_candidates,
    enforce_offline, filter_pool, load_panel, run_backtest,
    seed_stock_info, shrink_panel,
)

OUT_DIR = REPO_ROOT / "output" / "auto_iter"
LOG_CSV = OUT_DIR / "iter_log.csv"

# 达标门槛: 年化 >= 25% 且最大回撤不超过 15% (max_drawdown 是负值小数)
TARGET_ANNUAL = 0.25
TARGET_MDD = -0.15

# 信号层市场因子 (2026-08-11 v3): mkt_* 因子 AND 进入场信号的叠加概率,
# 可用 --mkt-prob 调。v3 把它当"仓位总闸"用 — 熊市段整体停开新仓,
# 因此 v3 搜索须关掉 --even 月度均匀门槛 (总开关生效期必然信号断粮,
# 两个要求互斥, 用户已拍板回撤适当放宽)。
# 注意 v3 800 轮实测结论: 信号层过滤帮倒忙 (只拦新开仓, 老仓位在熊市
# 被反复止损放血), 真正的总开关是下面的 market_switch (exit 侧强制清仓)。
MKT_PROB = 0.5

# 熊市强制清仓总开关 (2026-08-11 exit 侧): spec["market_switch"] 叠加概率,
# 可用 --switch-prob 调。与信号层 mkt_* 因子互斥 (语义重复, 不同时出现)。
# 与信号层过滤的本质区别: 熊市日通过引擎 formula_exit 绝对优先通道强制
# 清仓老仓位, 不只是拦新开仓。
SWITCH_PROB = 0.5

# 总开关参数家族 (与信号层 mkt_* 的取值范围刻意不同: 总开关看牛熊大级别,
# 窗口放长 — mkt_med_ma n∈[60,250] 半年线~年线级)
SWITCH_PARAM_RANGES = {
    "mkt_med_ma":    {"n": (60, 250, True)},
    "mkt_breadth":   {"n": (20, 120, True), "th": (0.3, 0.7, False)},
    "mkt_not_crash": {"n": (5, 60, True), "x": (0.05, 0.20, False)},
}


def _sample_switch_params(rng, name: str) -> dict:
    """按 SWITCH_PARAM_RANGES 采样一组总开关参数。"""
    out = {"name": name}
    for k, (lo, hi, is_int) in SWITCH_PARAM_RANGES[name].items():
        out[k] = int(rng.integers(lo, hi + 1)) if is_int \
            else round(float(rng.uniform(lo, hi)), 3)
    return out

# 当前运行的窗口/池子口径 (v3): main() 按命令行参数覆写, 复现脚本模板照此落盘,
# 保证独立脚本的取数范围与主循环一致
import auto_iter.common as _common  # noqa: E402
CUR_POOL_FIRST = _common.POOL_FIRST_DATE
CUR_POOL_LAST = _common.POOL_LAST_DATE
CUR_DATA_START = DATA_START

SIDE_NAMES = {"left": "左侧", "right": "右侧"}

LOG_FIELDS = [
    "iter", "side", "factors", "stop_config", "n_signals",
    "annualized_return", "max_drawdown", "sharpe_ratio", "win_rate",
    "profit_factor", "total_trades", "elapsed_sec", "target_met", "note",
    "script_file",
]


# ═══════════════════════════════════════════════════════════════
# 随机策略生成器
# ═══════════════════════════════════════════════════════════════

def _sample_factor_params(rng, name: str) -> dict:
    """各因子的随机参数家族。
    2026-08-10 v2: 阈值范围向"温和档"放宽 — 用户要求信号时间分布均匀,
    太极端的阈值只在崩盘日触发 (信号扎堆), 温和阈值才能日常稳定出信号。"""
    if name == "ret_drop":
        return {"name": name, "n": int(rng.integers(3, 21)),
                "x": round(float(rng.uniform(0.03, 0.25)), 3)}
    if name == "bias_low":
        return {"name": name, "n": int(rng.integers(10, 61)),
                "x": round(float(rng.uniform(0.01, 0.15)), 3)}
    if name == "rsi_low":
        return {"name": name, "n": int(rng.integers(6, 15)),
                "th": round(float(rng.uniform(15, 45)), 1)}
    if name == "vol_shrink":
        return {"name": name, "n": int(rng.integers(5, 21)),
                "r": round(float(rng.uniform(0.3, 0.9)), 2)}
    if name == "bias_ma":
        return {"name": name, "n": int(rng.integers(10, 61)),
                "x": round(float(rng.uniform(0.02, 0.20)), 3)}
    if name == "new_high":
        return {"name": name, "n": int(rng.integers(10, 121))}
    if name == "vol_surge":
        return {"name": name, "n": int(rng.integers(5, 21)),
                "r": round(float(rng.uniform(1.2, 4.0)), 2)}
    if name == "ma_bull":
        fast, mid, slow = [(5, 10, 20), (5, 10, 30), (10, 20, 60), (5, 20, 60)][
            int(rng.integers(0, 4))]
        return {"name": name, "fast": fast, "mid": mid, "slow": slow}
    if name == "break_ma":
        return {"name": name, "n": int(rng.integers(5, 61))}
    # ── 市场状态因子 (左右两侧通用, 每轮最多 1 个) ──
    # v3: 窗口拉长到 250 日 (年线级) — 牛熊总开关需要看得足够远
    if name == "mkt_med_ma":
        return {"name": name, "n": int(rng.integers(20, 251))}
    if name == "mkt_breadth":
        return {"name": name, "n": int(rng.integers(20, 121)),
                "th": round(float(rng.uniform(0.3, 0.7)), 2)}
    if name == "mkt_not_crash":
        return {"name": name, "n": int(rng.integers(3, 21)),
                "x": round(float(rng.uniform(0.03, 0.15)), 3)}
    # ── 2026-08-11 因子库大扩编 (13 个, 区间按 A 股日线常识) ──
    if name == "atr_low":     # ATR/close 常见 0.02~0.04, 低波动取偏窄档
        return {"name": name, "n": int(rng.integers(5, 21)),
                "th": round(float(rng.uniform(0.01, 0.05)), 3)}
    if name == "atr_high":
        return {"name": name, "n": int(rng.integers(5, 21)),
                "th": round(float(rng.uniform(0.03, 0.08)), 3)}
    if name == "range_pos":
        return {"name": name, "th": round(float(rng.uniform(0.6, 0.95)), 2)}
    if name == "lower_shadow":
        return {"name": name, "th": round(float(rng.uniform(0.4, 0.8)), 2)}
    if name == "bull_body":
        return {"name": name, "th": round(float(rng.uniform(0.5, 0.9)), 2)}
    if name == "consec_down":
        return {"name": name, "n": int(rng.integers(2, 6))}
    if name == "price_pos_low":
        return {"name": name, "n": int(rng.integers(60, 251)),
                "th": round(float(rng.uniform(0.05, 0.3)), 2)}
    if name == "price_pos_high":
        return {"name": name, "n": int(rng.integers(60, 251)),
                "th": round(float(rng.uniform(0.7, 0.95)), 2)}
    if name == "roc_up":
        return {"name": name, "n": int(rng.integers(5, 61)),
                "x": round(float(rng.uniform(0.05, 0.5)), 3)}
    if name == "drawdown_from_high":
        return {"name": name, "n": int(rng.integers(20, 251)),
                "x": round(float(rng.uniform(0.1, 0.5)), 3)}
    if name == "amt_surge":
        return {"name": name, "n": int(rng.integers(5, 21)),
                "r": round(float(rng.uniform(1.5, 4.0)), 2)}
    if name == "price_up_vol_down":
        return {"name": name, "n": int(rng.integers(3, 21))}
    if name == "rel_strength":
        return {"name": name, "n": int(rng.integers(20, 121)),
                "th": round(float(rng.uniform(0.05, 0.3)), 3)}
    raise ValueError(f"未知因子: {name}")


def sample_stop_config(rng) -> dict:
    """从参数化家族随机采样止盈止损 (结构对齐 config/default.yaml 的 stop_loss)。"""
    n_levels = int(rng.integers(1, 4))
    profits = sorted(rng.uniform(0.04, 0.30, size=n_levels).tolist())
    levels = [{"profit": round(p, 3),
               "sell_ratio": round(float(rng.uniform(0.2, 0.5)), 2)}
              for p in profits]
    return {
        # 2026-08-10 v2 用户拍板: 固定"止损优先" (cost_stop > ladder_tp > trailing),
        # 不再随机 priority — 先保命再谈赚钱
        "priority": "stop_first",
        "cost_stop": {"enabled": True,
                      "threshold": round(-float(rng.uniform(0.05, 0.20)), 3)},
        "trailing_stop": {
            "enabled": bool(rng.random() > 0.3),   # 70% 启用移动止盈
            "activation": round(float(rng.uniform(0.03, 0.12)), 3),
            "drawdown": round(float(rng.uniform(0.01, 0.08)), 3),
            # 2026-08-10 用户拍板: 移动止盈统一用条件单语义 (confirm="real"):
            # 线必须提前挂好 — 本 bar 自己创峰值不当 bar 判定; 跳空按开盘价,
            # 盘中触线按线价 (隔夜已挂好的条件单, 实盘可达)。见 trailing.py。
            "confirm": "real",
        },
        "ladder_tp": {"enabled": bool(rng.random() > 0.2),  # 80% 启用阶梯止盈
                      "levels": levels},
        "time_stop": {"enabled": True,
                      "max_hold_days": int(rng.integers(5, 41))},
        "cond_time_stop": {"enabled": False},
        "first_day": {"enabled": False},
        "formula_sell": {"enabled": False},
    }


def sample_spec(rng, pool_params: dict) -> dict:
    """一轮完整策略规格: 思路 + 因子组合 + 止盈止损 + 费用/仓位 + 池参数。"""
    side = str(rng.choice(["left", "right"]))
    family = LEFT_FACTORS if side == "left" else RIGHT_FACTORS
    # 2026-08-11 扩编后个股因子 2~5 个 (原 2~4), 因子池大了需要更大组合空间
    n_factors = int(rng.integers(2, min(6, len(family)) + 1))
    names = rng.choice(list(family), size=n_factors, replace=False).tolist()
    factors = [_sample_factor_params(rng, n) for n in names]
    spec = {
        "side": side,
        "factors": factors,
        "stop": sample_stop_config(rng),
        "bt_cfg": DEFAULT_BT_CFG,
        "pool": pool_params,
    }
    # 大盘环境两个用法互斥 (2026-08-11, 语义重复不同时出现):
    # ① market_switch (概率 SWITCH_PROB): 熊市强制清仓总开关, exit 侧
    # ② mkt_* 信号因子 (概率 MKT_PROB): AND 进入场信号, 只拦新开仓
    if rng.random() < SWITCH_PROB:
        spec["market_switch"] = _sample_switch_params(
            rng, str(rng.choice(list(MARKET_FACTORS))))
    elif rng.random() < MKT_PROB:
        factors.append(_sample_factor_params(rng, str(rng.choice(list(MARKET_FACTORS)))))
    return spec


# ═══════════════════════════════════════════════════════════════
# refine 定向变异模式 (--refine)
# ═══════════════════════════════════════════════════════════════
# 思路: 纯随机搜索 250 轮证明"左侧逆势"方向明显占优 (年化前 8 全左侧),
# 但大量轮次浪费在退化配置上。refine 模式改为从 iter_log.csv 双榜单播种
# (回撤合规 top5 + 不限回撤 top5), 每轮在当前最优个体附近变异,
# 保留 30% 概率彻底换思路重新随机 (防陷入局部最优)。

# 各因子数值参数的合法区间 (与 _sample_factor_params 的采样家族一致;
# ma_bull 是离散档位, 不做数值扰动, 只靠结构变异增删)
FACTOR_PARAM_RANGES = {
    "ret_drop":   {"n": (3, 20, True), "x": (0.03, 0.25, False)},
    "bias_low":   {"n": (10, 60, True), "x": (0.01, 0.15, False)},
    "rsi_low":    {"n": (6, 14, True), "th": (15.0, 45.0, False)},
    "vol_shrink": {"n": (5, 20, True), "r": (0.3, 0.9, False)},
    "bias_ma":    {"n": (10, 60, True), "x": (0.02, 0.20, False)},
    "new_high":   {"n": (10, 120, True)},
    "vol_surge":  {"n": (5, 20, True), "r": (1.2, 4.0, False)},
    "ma_bull":    {},
    "break_ma":   {"n": (5, 60, True)},
    "mkt_med_ma":    {"n": (20, 250, True)},
    "mkt_breadth":   {"n": (20, 120, True), "th": (0.3, 0.7, False)},
    "mkt_not_crash": {"n": (3, 20, True), "x": (0.03, 0.15, False)},
    # 2026-08-11 扩编 13 因子 (区间与 _sample_factor_params 一致)
    "atr_low":     {"n": (5, 20, True), "th": (0.01, 0.05, False)},
    "atr_high":    {"n": (5, 20, True), "th": (0.03, 0.08, False)},
    "range_pos":   {"th": (0.6, 0.95, False)},
    "lower_shadow": {"th": (0.4, 0.8, False)},
    "bull_body":   {"th": (0.5, 0.9, False)},
    "consec_down": {"n": (2, 5, True)},
    "price_pos_low":  {"n": (60, 250, True), "th": (0.05, 0.3, False)},
    "price_pos_high": {"n": (60, 250, True), "th": (0.7, 0.95, False)},
    "roc_up":      {"n": (5, 60, True), "x": (0.05, 0.5, False)},
    "drawdown_from_high": {"n": (20, 250, True), "x": (0.1, 0.5, False)},
    "amt_surge":   {"n": (5, 20, True), "r": (1.5, 4.0, False)},
    "price_up_vol_down": {"n": (3, 20, True)},
    "rel_strength": {"n": (20, 120, True), "th": (0.05, 0.3, False)},
}

# 止损数值参数的合法区间
STOP_PARAM_RANGES = {
    "cost_threshold": (-0.25, -0.03, False),
    "activation": (0.02, 0.20, False),
    "drawdown": (0.005, 0.15, False),
    "ladder_profit": (0.02, 0.40, False),
    "sell_ratio": (0.10, 0.80, False),
    "max_hold_days": (3, 60, True),
}


def _jitter(rng, val, lo, hi, is_int):
    """±20% 高斯扰动 (1σ=10%), 取整的取整, 最后夹在 [lo, hi]。"""
    v = float(val) * (1.0 + float(rng.normal(0.0, 0.1)))
    v = min(max(v, lo), hi)
    return int(round(v)) if is_int else round(v, 4)


def mutate_spec(rng, base: dict, pool_params: dict) -> dict:
    """从当前最优个体变异出新个体。

    - 30% 概率彻底换思路: 走 sample_spec 完全重新随机 (含换到另一侧)
    - 70% 在最优附近变异: 数值微调 (±20% 高斯) + 20% 概率结构变异 (增/删/换因子)
    """
    if rng.random() < 0.3:
        return sample_spec(rng, pool_params)
    child = copy.deepcopy(base)
    child["pool"] = dict(pool_params)
    child["bt_cfg"] = DEFAULT_BT_CFG

    # 因子数值微调 (ma_bull 无连续参数, FACTOR_PARAM_RANGES 里为空自动跳过)
    for fac in child["factors"]:
        for k, (lo, hi, is_int) in FACTOR_PARAM_RANGES.get(fac["name"], {}).items():
            if k in fac:
                fac[k] = _jitter(rng, fac[k], lo, hi, is_int)

    # 止损数值微调 + 低概率开关翻转/优先级重抽
    st = child["stop"]
    if "threshold" in st.get("cost_stop", {}):
        st["cost_stop"]["threshold"] = _jitter(
            rng, st["cost_stop"]["threshold"], *STOP_PARAM_RANGES["cost_threshold"])
    tr = st.get("trailing_stop", {})
    if "activation" in tr:
        tr["activation"] = _jitter(rng, tr["activation"], *STOP_PARAM_RANGES["activation"])
    if "drawdown" in tr:
        tr["drawdown"] = _jitter(rng, tr["drawdown"], *STOP_PARAM_RANGES["drawdown"])
    if rng.random() < 0.1:
        tr["enabled"] = not tr.get("enabled", True)
    ld = st.get("ladder_tp", {})
    for lv in ld.get("levels", []):
        lv["profit"] = _jitter(rng, lv["profit"], *STOP_PARAM_RANGES["ladder_profit"])
        lv["sell_ratio"] = _jitter(rng, lv["sell_ratio"], *STOP_PARAM_RANGES["sell_ratio"])
    ld["levels"] = sorted(ld.get("levels", []), key=lambda x: x["profit"])  # 引擎要求升序
    if rng.random() < 0.1:
        ld["enabled"] = not ld.get("enabled", True)
    if "max_hold_days" in st.get("time_stop", {}):
        st["time_stop"]["max_hold_days"] = _jitter(
            rng, st["time_stop"]["max_hold_days"], *STOP_PARAM_RANGES["max_hold_days"])
    # 2026-08-10 v2: priority 固定 stop_first (用户拍板止损优先), 变异不再翻转
    st["priority"] = "stop_first"
    # 移动止盈如启用必须是条件单语义 (历史种子可能带旧口径, 统一改写)
    if tr.get("enabled"):
        tr["confirm"] = "real"

    # 牛熊总开关: 参数 ±20% 抖动 + 10% 概率增删
    sw = child.get("market_switch")
    if sw:
        for k, (lo, hi, is_int) in SWITCH_PARAM_RANGES.get(sw["name"], {}).items():
            if k in sw:
                sw[k] = _jitter(rng, sw[k], lo, hi, is_int)
    if rng.random() < 0.1:
        if sw:
            del child["market_switch"]
        else:
            child["market_switch"] = _sample_switch_params(
                rng, str(rng.choice(list(MARKET_FACTORS))))
            # 互斥: 加总开关时删掉信号层 mkt_* 因子 (语义重复); 若因此删空
            # 则补一个该侧个股因子, 防 entries 退化成全 True 天天买
            child["factors"] = [f for f in child["factors"]
                                if f["name"] not in MARKET_FACTORS]
            if not child["factors"]:
                fam = LEFT_FACTORS if child["side"] == "left" else RIGHT_FACTORS
                child["factors"].append(
                    _sample_factor_params(rng, str(rng.choice(list(fam)))))

    # 结构变异 (20%): 增/删/替换一个因子。个股因子保持在该侧家族内;
    # 市场因子 (mkt_*) 单独成族 — 替换市场因子只换市场因子, add 仅在
    # 当前无市场因子时可引入, 全程守住"每轮最多 1 个市场因子"约束
    if rng.random() < 0.2:
        family = LEFT_FACTORS if child["side"] == "left" else RIGHT_FACTORS
        names = [f["name"] for f in child["factors"]]
        has_mkt = any(n in MARKET_FACTORS for n in names)
        ops = []
        if len(child["factors"]) > 1:
            ops.append("del")
        if len(child["factors"]) < 6:  # 上限 = 5 个股 + 1 市场 (2026-08-11 扩编放宽)
            ops.append("add")
        ops.append("replace")
        op = str(rng.choice(ops))
        if op == "del":
            child["factors"].pop(int(rng.integers(0, len(child["factors"]))))
        elif op == "add":
            fresh = [n for n in family if n not in names]
            # 已有总开关时不再引入信号层 mkt 因子 (互斥)
            if not has_mkt and "market_switch" not in child:
                fresh += [n for n in MARKET_FACTORS]
            if not fresh:
                fresh = list(family)
            child["factors"].append(_sample_factor_params(rng, str(rng.choice(fresh))))
        else:  # replace: 同类替换 (市场↔市场, 个股↔个股)
            idx = int(rng.integers(0, len(child["factors"])))
            if names[idx] in MARKET_FACTORS:
                pool = [n for n in MARKET_FACTORS if n != names[idx]]
            else:
                pool = [n for n in family if n not in names] or list(family)
            child["factors"][idx] = _sample_factor_params(rng, str(rng.choice(pool)))
    return child


def _score(ann: float, mdd: float) -> tuple:
    """个体评分: 回撤合规 (>=-15% 硬门槛) 者按年化排; 不合规按 年化+回撤
    综合排 (两者都是负方向越小越差, 相加越大越接近合规区)。"""
    if mdd >= TARGET_MDD:
        return (1, ann)
    return (0, ann + mdd)


def load_seed_specs(csv_path, pool_params: dict, top_n: int = 5) -> list:
    """双榜单播种: (a) 回撤合规按年化 top5; (b) 不限回撤按年化 top5, 按配置去重。

    返回 [(源日志行, spec)]。spec 的 pool/bt_cfg 统一替换为当前运行参数,
    播种后会全部复评 (同一池子口径才有可比性 — 日志里可能混着 smoke 小池的行)。
    """
    side_rev = {v: k for k, v in SIDE_NAMES.items()}
    with open(csv_path, encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f)
                if r.get("total_trades") and int(r["total_trades"]) > 0]
    compliant = sorted([r for r in rows if float(r["max_drawdown"]) >= TARGET_MDD],
                       key=lambda r: float(r["annualized_return"]), reverse=True)[:top_n]
    overall = sorted(rows, key=lambda r: float(r["annualized_return"]),
                     reverse=True)[:top_n]
    seeds, seen = [], set()
    for r in compliant + overall:
        key = (r["factors"], r["stop_config"])
        if key in seen:
            continue
        seen.add(key)
        stop = json.loads(r["stop_config"])
        ts = stop.get("trailing_stop")
        if isinstance(ts, dict) and ts.get("enabled"):
            # 2026-08-10 用户拍板: 历史日志里的种子缺 confirm 字段 (默认 intraday
            # 偏乐观), 播种时统一改写为条件单语义, 保证复评口径一致。
            ts["confirm"] = "real"
        spec = {"side": side_rev[r["side"]],
                "factors": json.loads(r["factors"]),
                "stop": stop,
                "bt_cfg": DEFAULT_BT_CFG, "pool": dict(pool_params)}
        seeds.append((r, spec))
    return seeds


def _next_iter_index() -> int:
    """refine 模式起始编号: 扫描现有 iter_NNN_*.py 取最大编号+1, 防与历史文件冲突。

    (纯随机模式保持原样从 1 开始 — 历史 5 个批次的文件编号本就重叠,
    靠文件名里的绩效区分, 不在本次改动范围。)
    """
    nums = [int(m.group(1)) for f in OUT_DIR.glob("iter_*.py")
            if (m := re.match(r"iter_(\d+)", f.name))]
    return max(nums, default=0) + 1



# ═══════════════════════════════════════════════════════════════
# 每轮独立复现脚本生成
# ═══════════════════════════════════════════════════════════════

_SCRIPT_TEMPLATE = '''# -*- coding: utf-8 -*-
"""自动迭代第 {it:03d} 轮策略复现脚本 — 由 auto_iter/auto_strategy_loop.py 生成。

思路: {side_name} | 信号 {n_signals} 个 | 回测 {bt_start} ~ {bt_end} (T 收盘买入)
绩效: 年化 {ann:.2%}  最大回撤 {mdd:.2%}  夏普 {sharpe:.2f}  胜率 {win:.2%}  交易 {trades} 笔
达标门槛: 年化 >= 25% 且回撤 >= -15% → {met}

独立运行 (仓库根目录):  python {rel_path}
"""
import sys
from pathlib import Path

# Windows 控制台默认 GBK, 强制 UTF-8 防中文输出乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 本文件位于 output/auto_iter/ 下, 上两级即仓库根
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from auto_iter.common import (
    build_entries_and_bear, build_pool_candidates, enforce_offline, filter_pool,
    load_panel, run_backtest, seed_stock_info, shrink_panel,
)

# 该轮完整参数 (因子/止盈止损/费用仓位/股票池过滤), 勿手改 — 改了就与原轮次对不上
SPEC = {spec_json}


def main():
    enforce_offline()                       # 硬断网: 任何 TDX 连接尝试直接抛错
    codes = build_pool_candidates(first_date='{pool_first}',
                                  last_date='{pool_last}')  # manifest 筛缓存覆盖完整的股票
    panel = load_panel(codes, start='{data_start}')  # 一次性读入内存 (数分钟内的主要耗时)
    codes = filter_pool(panel, start='{bt_start}', end='{bt_end}', **SPEC["pool"])
    panel = shrink_panel(panel, codes)
    seed_stock_info(codes)                  # 涨停过滤的 ST 判定走本地缓存
    # 与主循环 evaluate_spec 同一函数: 含 market_switch 时熊市停开新仓 + 强制清仓
    entries, bear_mask = build_entries_and_bear(panel, SPEC)
    print(f"窗口内信号数: {{int(entries.loc['{bt_start}':'{bt_end}'].sum().sum())}}")
    res = run_backtest(panel, entries, SPEC["stop"], SPEC["bt_cfg"],
                       bt_start='{bt_start}', bt_end='{bt_end}', bear_mask=bear_mask)
    m = res["metrics"]
    print(f"年化 {{m.get('annualized_return', 0):.2%}}  回撤 {{m.get('max_drawdown', 0):.2%}}  "
          f"夏普 {{m.get('sharpe_ratio', 0):.2f}}  胜率 {{m.get('win_rate', 0):.2%}}  "
          f"交易 {{m.get('total_trades', 0)}} 笔")


if __name__ == "__main__":
    main()
'''


def write_round_script(it: int, spec: dict, n_signals: int, metrics: dict,
                       met: bool) -> Path:
    """把本轮策略写成独立可运行脚本, 文件名带思路与绩效。"""
    ann = metrics.get("annualized_return", 0.0)
    mdd = metrics.get("max_drawdown", 0.0)
    fname = (f"iter_{it:03d}_{SIDE_NAMES[spec['side']]}"
             f"_年化{ann * 100:.1f}%_回撤{mdd * 100:.1f}%.py")
    path = OUT_DIR / fname
    content = _SCRIPT_TEMPLATE.format(
        it=it, side_name=SIDE_NAMES[spec["side"]], n_signals=n_signals,
        bt_start=BT_START, bt_end=BT_END,
        ann=ann, mdd=mdd,
        sharpe=metrics.get("sharpe_ratio", 0.0),
        win=metrics.get("win_rate", 0.0),
        trades=metrics.get("total_trades", 0),
        met="TARGET MET" if met else "未达标",
        rel_path=f"output/auto_iter/{fname}",
        pool_first=CUR_POOL_FIRST, pool_last=CUR_POOL_LAST,
        data_start=CUR_DATA_START,
        # 用 pformat 而非 json.dumps: 生成的是 Python 字面量 (True/False),
        # 复现脚本直接 import 即可执行, json 的 true/false 会 NameError
        spec_json=pprint.pformat(spec, width=100, sort_dicts=False),
    )
    path.write_text(content, encoding="utf-8")
    return path


# ═══════════════════════════════════════════════════════════════
# 主循环
# ═══════════════════════════════════════════════════════════════

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="自动策略迭代回测循环 (全程离线)")
    p.add_argument("--max-iter", type=int, default=None,
                   help="最大迭代轮数 (默认: 随机 50 / refine 200)")
    p.add_argument("--seed", type=int, default=None, help="随机种子 (不传则每回不同)")
    p.add_argument("--pool-size", type=int, default=3000,
                   help="股票池上限 (按日均成交额降序截断, 默认 3000)")
    p.add_argument("--amount-quantile", type=float, default=0.2,
                   help="剔除日均成交额最低的分位 (默认 0.2)")
    p.add_argument("--min-signals", type=int, default=30,
                   help="窗口内信号少于此数跳过回测 (默认 30)")
    p.add_argument("--smoke", action="store_true",
                   help="冒烟模式: 池子缩到 200 只, 只跑 1 轮")
    p.add_argument("--refine", action="store_true",
                   help="定向变异模式: 从 iter_log.csv 双榜单播种, 在最优个体附近变异")
    p.add_argument("--refine-from", default=None,
                   help="refine 播种日志 (默认跟随 --tag: iter_log[_tag].csv)")
    # 2026-08-10 用户拍板: 门槛做成可调 — 先冲年化 25%, 回撤可全面放宽 (--target-mdd -1.0)
    p.add_argument("--target-ann", type=float, default=TARGET_ANNUAL,
                   help="年化达标线 (默认 0.25)")
    p.add_argument("--target-mdd", type=float, default=TARGET_MDD,
                   help="回撤达标线, 负值 (默认 -0.15; 全面放宽传 -1.0)")
    # 2026-08-10 v2 用户拍板: 信号要时间分布均匀 (不许崩盘日扎堆、平时装死)
    p.add_argument("--even", action="store_true",
                   help="开启信号均匀度门槛: 不达标轮次直接跳过回测")
    # 2026-08-10 v2.1 用户修订: 不看日覆盖率, 只看"每个自然月都不少于 N 个信号"
    p.add_argument("--min-per-month", type=int, default=10,
                   help="均匀度门槛: 回测窗口内每个自然月至少 N 个信号 (默认 10)")
    p.add_argument("--tag", default="",
                   help="运行批次标签: 日志写到 iter_log_<tag>.csv, 便于多轮实验分开")
    # 2026-08-11 v3 长周期寻优: 窗口/数据起点/池子覆盖期可改写 (默认 None=用 common 常量)
    p.add_argument("--bt-start", default=None, help="回测起点, 如 2012-01-01")
    p.add_argument("--bt-end", default=None, help="回测终点, 如 2026-06-30")
    p.add_argument("--data-start", default=None,
                   help="读数起点 (需早于回测起点约 250 交易日暖机, 如 2011-01-01)")
    p.add_argument("--pool-first", default=None, help="池子缓存覆盖首日, 如 20120101")
    p.add_argument("--pool-last", default=None, help="池子缓存覆盖末日, 如 20260630")
    p.add_argument("--mkt-prob", type=float, default=0.5,
                   help="市场状态因子(信号层过滤)叠加概率 (默认 0.5)")
    p.add_argument("--switch-prob", type=float, default=0.5,
                   help="牛熊总开关(market_switch, 熊市强制清仓)叠加概率 (默认 0.5; "
                        "与信号层 mkt 因子互斥)")
    args = p.parse_args(argv)
    if args.max_iter is None:
        args.max_iter = 200 if args.refine else 50
    return args


def evaluate_spec(panel, spec: dict, min_signals: int, return_entries: bool = False):
    """算信号 + 回测, 返回 (metrics, n_signals)。信号过少跳过回测, metrics 为 {}。

    return_entries=True 时返回 (metrics, n_signals, entries) — v2 信号均匀度
    门槛需要在回测前拿到信号矩阵, 为避免改坏既有调用方做成可选参数。
    spec 含 market_switch 时走 build_entries_and_bear: 熊市日停开新仓 +
    bear_mask 强制清仓 (引擎 formula_exit 通道)。
    """
    entries, bear_mask = build_entries_and_bear(panel, spec)
    n_signals = int(entries.loc[BT_START:BT_END].sum().sum())
    if n_signals < min_signals:
        return ({}, n_signals, entries) if return_entries else ({}, n_signals)
    res = run_backtest(panel, entries, spec["stop"], spec["bt_cfg"],
                       bt_start=BT_START, bt_end=BT_END, bear_mask=bear_mask)
    if return_entries:
        return res["metrics"], n_signals, entries
    return res["metrics"], n_signals


def signal_evenness(entries, min_per_month: int):
    """信号时间分布均匀度判定 (2026-08-10 v2.1 用户修订口径)。

    标准: 回测窗口内【每个自然月】的信号数都不少于 min_per_month 个。
    极端抄底策略在平静年份整月零信号 (如 2023 年全年才 57 个), 直接卡掉;
    崩盘日扎堆策略只要每月凑够数就放行 (用户明确不再限制单日峰值)。
    返回 (是否通过, 最差月信号数, 不达标月数)。
    """
    per_day = entries.loc[BT_START:BT_END].sum(axis=1)
    per_month = per_day.groupby(per_day.index.to_period("M")).sum()
    worst = int(per_month.min()) if len(per_month) else 0
    bad_months = int((per_month < min_per_month).sum())
    return bad_months == 0, worst, bad_months


def run_iteration(it: int, spec: dict, panel, args, writer, log_f, history,
                  note: str = ""):
    """单轮执行 (随机/refine 共用): 评估 → 落盘复现脚本 → 追加日志 → 打印进度。

    返回 (row, met, script_path); met=True 表示双门槛达标。
    """
    t_it = time.time()
    if spec.get("market_switch"):
        # 总开关落 note (不动 CSV 列结构, 保持与老日志可拼接)
        note = (note + " " if note else "") + \
               f"总开关{json.dumps(spec['market_switch'])}"
    even_skip = False
    if getattr(args, "even", False):
        # v2: 先算信号看分布, 不均匀直接跳过回测 (省 1.5s/轮), 不落盘脚本
        metrics, n_signals, entries = evaluate_spec(
            panel, spec, args.min_signals, return_entries=True)
        ok, worst, bad_months = signal_evenness(entries, args.min_per_month)
        if metrics and not ok:
            metrics = {}
            even_skip = True
            note = (note + " " if note else "") + (
                f"信号不均匀跳过(最差月{worst}个/要求每月≥{args.min_per_month}, "
                f"{bad_months}个月不达标)")
    else:
        metrics, n_signals = evaluate_spec(panel, spec, args.min_signals)
    if not metrics and not even_skip:
        note = (note + " " if note else "") + \
               f"信号过少({n_signals}<{args.min_signals}), 跳过回测"
    elapsed = time.time() - t_it

    ann = metrics.get("annualized_return", 0.0)
    mdd = metrics.get("max_drawdown", 0.0)
    met = bool(metrics) and ann >= TARGET_ANNUAL and mdd >= TARGET_MDD
    if even_skip:
        script_path = None
        script_name = ""
    else:
        script_path = write_round_script(it, spec, n_signals, metrics, met)
        script_name = script_path.name

    row = {
        "iter": it, "side": SIDE_NAMES[spec["side"]],
        "factors": json.dumps(spec["factors"], ensure_ascii=False),
        "stop_config": json.dumps(spec["stop"], ensure_ascii=False),
        "n_signals": n_signals,
        "annualized_return": round(ann, 6), "max_drawdown": round(mdd, 6),
        "sharpe_ratio": round(metrics.get("sharpe_ratio", 0.0), 4),
        "win_rate": round(metrics.get("win_rate", 0.0), 4),
        "profit_factor": round(metrics.get("profit_factor", 0.0), 4),
        "total_trades": metrics.get("total_trades", 0),
        "elapsed_sec": round(elapsed, 1),
        "target_met": met, "note": note,
        "script_file": script_name,
    }
    writer.writerow(row)
    log_f.flush()
    history.append(row)

    print(f"iter {it:03d}/{args.max_iter} [{SIDE_NAMES[spec['side']]}] "
          f"年化 {ann:.1%} 回撤 {mdd:.1%} 夏普 {row['sharpe_ratio']:.2f} "
          f"交易 {row['total_trades']} 笔 信号 {n_signals} "
          f"耗时 {elapsed:.1f}s 达标={'是' if met else '否'}"
          + (f" ({note})" if note else ""), flush=True)
    return row, met, script_path


def _print_target_met(it, ann, mdd, script_path):
    print(f"\n*** TARGET MET *** 第 {it} 轮达标: 年化 {ann:.2%} >= {TARGET_ANNUAL:.0%} 且 "
          f"回撤 {mdd:.2%} 在 {TARGET_MDD:.0%} 以内", flush=True)
    print(f"复现脚本: {script_path}", flush=True)


def refine_loop(args, panel, pool_params, rng, writer, log_f, history):
    """定向变异主循环: 播种 → 每轮变异当前最优 → 选择 → 停滞重启 (最多 2 次)。

    维护两个最优: current best (变异起点, 重启时重置) 和 global best
    (全程历史最优, 仅供结束报告)。编号从现有 iter_*.py 最大编号+1 起。
    """
    def reseed():
        """从日志双榜单重新播种并复评 (复评不写日志/不落盘, 只定变异起点)。"""
        seeds = load_seed_specs(args.refine_from, pool_params)
        best = None
        for src, spec in seeds:
            metrics, _ = evaluate_spec(panel, spec, args.min_signals)
            ann = metrics.get("annualized_return", 0.0)
            mdd = metrics.get("max_drawdown", 0.0)
            print(f"[refine] 种子 ← 日志 iter {src['iter']} [{src['side']}] "
                  f"({src['script_file']}): 原年化 {float(src['annualized_return']):.1%} "
                  f"复评年化 {ann:.1%} 回撤 {mdd:.1%}", flush=True)
            cand = {"spec": spec, "score": _score(ann, mdd), "ann": ann, "mdd": mdd}
            if metrics and (best is None or cand["score"] > best["score"]):
                best = cand
        return best

    it = _next_iter_index()
    print(f"[refine] 播种自 {args.refine_from}, 起始编号 iter {it:03d}", flush=True)
    best = reseed()
    if best is None:
        print("[refine] 日志里无可用种子, 以一个随机个体起步", flush=True)
        spec = sample_spec(rng, pool_params)
        metrics, _ = evaluate_spec(panel, spec, args.min_signals)
        best = {"spec": spec,
                "score": _score(metrics.get("annualized_return", 0.0),
                                metrics.get("max_drawdown", 0.0)),
                "ann": metrics.get("annualized_return", 0.0),
                "mdd": metrics.get("max_drawdown", 0.0)}
    global_best = best
    print(f"[refine] 初始最优: 年化 {best['ann']:.1%} 回撤 {best['mdd']:.1%}", flush=True)

    restarts, since_improve, done = 0, 0, 0
    while done < args.max_iter:
        spec = mutate_spec(rng, best["spec"], pool_params)
        row, met, script_path = run_iteration(
            it, spec, panel, args, writer, log_f, history, note="refine")
        done += 1
        it += 1
        if row["total_trades"]:
            sc = _score(row["annualized_return"], row["max_drawdown"])
            if sc > global_best["score"]:
                global_best = {"spec": spec, "score": sc,
                               "ann": row["annualized_return"],
                               "mdd": row["max_drawdown"]}
            if sc > best["score"]:
                best = {"spec": spec, "score": sc,
                        "ann": row["annualized_return"],
                        "mdd": row["max_drawdown"]}
                since_improve = 0
                print(f"  ↑ 新最优: 年化 {best['ann']:.1%} 回撤 {best['mdd']:.1%}",
                      flush=True)
            else:
                since_improve += 1
        else:
            since_improve += 1
        if met:
            _print_target_met(row["iter"], row["annualized_return"],
                              row["max_drawdown"], script_path)
            return
        if since_improve >= 60:
            if restarts >= 2:
                print(f"[refine] 连续 {since_improve} 轮无改进且重启次数 (2) 已用尽, "
                      f"提前结束", flush=True)
                break
            restarts += 1
            print(f"[refine] 连续 {since_improve} 轮最优无改进, 第 {restarts} 次重启播种",
                  flush=True)
            best = reseed() or best
            since_improve = 0
    else:
        pass
    print(f"\n跑满 {done} 轮未达标。refine 全程最优: 年化 {global_best['ann']:.1%} "
          f"回撤 {global_best['mdd']:.1%}", flush=True)


def main(argv=None):
    # Windows 控制台默认 GBK, 强制 UTF-8 防中文进度行乱码
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    # 门槛可调化 (2026-08-10): _score/load_seed_specs/run_iteration 都读模块级常量,
    # 在撒种子/变异之前统一改写, 保证整轮运行口径一致
    global TARGET_ANNUAL, TARGET_MDD, BT_START, BT_END, MKT_PROB, SWITCH_PROB
    global CUR_POOL_FIRST, CUR_POOL_LAST, CUR_DATA_START
    TARGET_ANNUAL, TARGET_MDD = args.target_ann, args.target_mdd
    # v3 长周期口径覆写 (2026-08-11): 窗口/池子/读数起点跟随命令行
    if args.bt_start:
        BT_START = args.bt_start
    if args.bt_end:
        BT_END = args.bt_end
    if args.pool_first:
        CUR_POOL_FIRST = args.pool_first
    if args.pool_last:
        CUR_POOL_LAST = args.pool_last
    if args.data_start:
        CUR_DATA_START = args.data_start
    MKT_PROB = args.mkt_prob
    SWITCH_PROB = args.switch_prob
    if args.smoke:
        args.pool_size = 200
        args.max_iter = 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    enforce_offline()

    pool_params = {"amount_quantile": args.amount_quantile,
                   "min_price": 1.0, "max_stocks": args.pool_size}

    print(f"[初始化] 读取 manifest 筛股票池候选...", flush=True)
    t0 = time.time()
    candidates = build_pool_candidates(first_date=CUR_POOL_FIRST,
                                       last_date=CUR_POOL_LAST)
    print(f"[初始化] 候选 {len(candidates)} 只, 加载日线面板 (一次性读盘)...", flush=True)
    panel = load_panel(candidates, start=CUR_DATA_START)
    codes = filter_pool(panel, start=BT_START, end=BT_END, **pool_params)
    panel = shrink_panel(panel, codes)
    seed_stock_info(codes)
    print(f"[初始化] 股票池 {len(codes)} 只, 面板 {panel['close'].shape[0]} 交易日, "
          f"耗时 {time.time() - t0:.1f}s", flush=True)

    rng = np.random.default_rng(args.seed)
    # 2026-08-10 v2: --tag 分桶日志 (新一轮实验与老日志隔离); refine 播种默认跟随
    log_csv = OUT_DIR / (f"iter_log_{args.tag}.csv" if args.tag else "iter_log.csv")
    if args.refine_from is None:
        args.refine_from = str(log_csv)
    print(f"[初始化] 日志文件: {log_csv}", flush=True)
    new_log = not log_csv.exists()
    log_f = open(log_csv, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(log_f, fieldnames=LOG_FIELDS)
    if new_log:
        writer.writeheader()

    history = []  # 供结束时打印最佳 5 轮
    try:
        if args.refine:
            refine_loop(args, panel, pool_params, rng, writer, log_f, history)
        else:
            for it in range(1, args.max_iter + 1):
                spec = sample_spec(rng, pool_params)
                row, met, script_path = run_iteration(
                    it, spec, panel, args, writer, log_f, history)
                if met:
                    _print_target_met(it, row["annualized_return"],
                                      row["max_drawdown"], script_path)
                    break
            else:
                print(f"\n跑满 {args.max_iter} 轮未达标。", flush=True)
    finally:
        log_f.close()

    # 最佳 5 轮 (优先满足回撤约束的里面挑年化最高的; 都没有就纯看年化)
    ran = [r for r in history if r["total_trades"]]
    mdd_ok = [r for r in ran if r["max_drawdown"] >= TARGET_MDD]
    pool = mdd_ok if mdd_ok else ran
    top5 = sorted(pool, key=lambda r: r["annualized_return"], reverse=True)[:5]
    if top5:
        print("最佳轮次 (至多 5 轮, 按年化排序" +
              ("，均已满足回撤约束):" if mdd_ok else "，无满足回撤约束者):"), flush=True)
        for r in top5:
            gap_ann = max(0.0, TARGET_ANNUAL - r["annualized_return"])
            gap_mdd = max(0.0, TARGET_MDD - r["max_drawdown"])
            print(f"  iter {r['iter']:03d} [{r['side']}] 年化 {r['annualized_return']:.1%} "
                  f"(差 {gap_ann:.1%}) 回撤 {r['max_drawdown']:.1%} (差 {gap_mdd:.1%}) "
                  f"→ {r['script_file']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
