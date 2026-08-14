# -*- coding: utf-8 -*-
"""多策略组合寻优 (v7, 2026-08-11 用户拍板走多策略方向)。

思路: 单策略穿越牛熊年化 20% 已被六代搜索证明不存在 (天花板 ~18%),
但不同策略的亏损发生在不同时段 — 把低相关的策略按权重混合,
组合层面平滑回撤 ("东边不亮西边亮")。

流程:
  1. 候选池: 从各代落盘脚本 (output/auto_iter/iter_*.py) 用 ast 提取 SPEC,
     统一在 v6 长周期口径 (2012-01~2026-06, 1626 只池) 重跑, 取权益曲线
  2. 相关性筛选: 按日收益相关系数贪心挑选互补组合 ( pairwise 最大相关 < CORR_MAX )
  3. 权重寻优: Dirichlet 随机采样 N_WEIGHTS 组权重, 组合日收益 = Σ w_i × r_i
     (等效每日再平衡), 目标: 先卡回撤 <= MDD_LIMIT, 再最大化年化
  4. 输出最优组合的完整绩效 + 逐年收益 + 对照表

口径坦白: 引擎权益曲线不做资金约束 (每信号固定 2 万), 组合 = 归一化权益的
加权混合, 是"资金按比例分配给各策略"的近似, 非精确账户级模拟。

用法: python -X utf8 auto_iter/portfolio_multi.py
"""
import ast
import copy
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
from auto_iter.common import (enforce_offline, build_pool_candidates, load_panel,
                              filter_pool, shrink_panel, seed_stock_info,
                              run_backtest, build_entries_and_bear)
from auto_iter import auto_strategy_loop as m

# ── 配置 ──
BT_START, BT_END = "2012-01-01", "2026-06-30"
DATA_START = "2011-01-01"
POOL_FIRST, POOL_LAST = "20120101", "20260630"
CORR_MAX = 0.75          # 相关性筛选阈值: 与已选集合的最大 pairwise 相关上限
MAX_LEGS = 5             # 组合最多几条腿
N_WEIGHTS = 4000         # 权重采样组数
MDD_LIMIT = -0.20        # 权重寻优的回撤约束 (用户: 回撤适当放宽)
SEED = 7

# 候选: 三代搜索的各类冠军 (文件名即 output/auto_iter/ 下的落盘脚本)
CANDIDATES = [
    "iter_3758_左侧_年化17.9%_回撤-33.4%.py",   # v6 收益王 (左侧, 距顶回撤因子)
    "iter_3736_左侧_年化17.7%_回撤-33.2%.py",   # v6 收益亚军
    "iter_3478_左侧_年化11.3%_回撤-11.8%.py",   # v6 均衡 (夏普 1.15)
    "iter_3449_左侧_年化8.8%_回撤-7.9%.py",     # v6 卡玛王 (回撤仅 -8%)
    "iter_3396_左侧_年化8.6%_回撤-7.9%.py",     # v6 卡玛亚军
    "iter_3566_左侧_年化9.6%_回撤-10.6%.py",    # v6 稳健
    "iter_2867_左侧_年化17.7%_回撤-33.7%.py",   # v4 收益王 (单因子 bias_ma)
    "iter_2667_左侧_年化11.5%_回撤-14.0%.py",   # v4 总开关稳健王
    "iter_2085_右侧_年化25.9%_回撤-17.6%.py",   # v2.1 短窗王 (右侧趋势, 分散用)
]


def load_spec_from_script(path) -> dict:
    """从落盘脚本里用 AST 提取 SPEC 字面量 (比正则稳, 支持嵌套/多行)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "SPEC":
            return ast.literal_eval(node.value)
    raise ValueError(f"{path.name} 里没找到 SPEC")


def perf(eq: pd.Series) -> dict:
    rets = eq.pct_change().dropna()
    n = len(eq)
    ann = float((eq.iloc[-1] / eq.iloc[0]) ** (252 / max(n, 1)) - 1)
    dd = eq / eq.cummax() - 1
    sharpe = float(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0.0
    return {"ann": ann, "mdd": float(dd.min()), "sharpe": sharpe,
            "calmar": ann / abs(float(dd.min())) if dd.min() < 0 else 0.0,
            "cum": float(eq.iloc[-1] / eq.iloc[0] - 1)}


def yearly(eq: pd.Series) -> dict:
    eq = eq.copy()
    eq.index = pd.to_datetime(eq.index)
    return {str(y): float(g.iloc[-1] / g.iloc[0] - 1)
            for y, g in eq.groupby(eq.index.year) if len(g) > 1}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    enforce_offline()
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "output", "auto_iter")

    # ── 长周期面板 (v6 口径) ──
    candidates_pool = build_pool_candidates(first_date=POOL_FIRST, last_date=POOL_LAST)
    panel = load_panel(candidates_pool, start=DATA_START)
    pool_params = {"amount_quantile": 0.2, "min_price": 1.0, "max_stocks": 3000}
    codes = filter_pool(panel, start=BT_START, end=BT_END, **pool_params)
    panel = shrink_panel(panel, codes)
    seed_stock_info(codes)
    print(f"[初始化] 股票池 {len(codes)} 只, 窗口 {BT_START}~{BT_END}", flush=True)

    # ── 候选逐个回测, 收集归一化日收益 ──
    legs = {}
    for fname in CANDIDATES:
        path = os.path.join(out_dir, fname)
        if not os.path.exists(path):
            print(f"跳过 {fname} (文件不存在)", flush=True)
            continue
        spec = load_spec_from_script(Path(path))
        spec["bt_cfg"] = copy.deepcopy(m.DEFAULT_BT_CFG)
        spec["pool"] = dict(pool_params)
        entries, bear = build_entries_and_bear(panel, spec)
        res = run_backtest(panel, entries, spec["stop"], spec["bt_cfg"],
                           bear_mask=bear, bt_start=BT_START, bt_end=BT_END)
        eq = res["equity_curve"].set_index("date")["equity"]
        rets = eq.pct_change()
        legs[fname.split("_年化")[0]] = rets
        p = perf(eq)
        print(f"{fname[:28]:30s} 年化 {p['ann']:+6.1%} 回撤 {p['mdd']:+6.1%} "
              f"卡玛 {p['calmar']:.2f}", flush=True)

    R = pd.DataFrame(legs).dropna()   # 行=交易日, 列=策略日收益

    # ── 相关性贪心筛选 ──
    corr = R.corr()
    solo = {c: perf((1 + R[c]).cumprod())["calmar"] for c in R.columns}
    selected = []
    for c in sorted(solo, key=solo.get, reverse=True):
        if all(abs(corr.loc[c, s]) < CORR_MAX for s in selected):
            selected.append(c)
        if len(selected) >= MAX_LEGS:
            break
    print(f"\n入选组合腿 ({len(selected)} 条, 相关上限 {CORR_MAX}):", flush=True)
    for c in selected:
        others = [s for s in selected if s != c]
        mx = max((abs(corr.loc[c, s]) for s in others), default=0.0)
        print(f"  {c:12s} 单跑卡玛 {solo[c]:.2f} 与其他腿最大相关 {mx:.2f}", flush=True)
    Rs = R[selected]

    # ── 权重寻优: 先卡回撤, 再最大化年化 ──
    rng = np.random.default_rng(SEED)
    W = rng.dirichlet(np.ones(len(selected)), size=N_WEIGHTS)
    best = None
    for w in W:
        port_ret = Rs.to_numpy() @ w
        eq = pd.Series((1 + port_ret).cumprod(), index=Rs.index)
        p = perf(eq)
        if p["mdd"] < MDD_LIMIT:
            continue
        if best is None or p["ann"] > best[1]["ann"]:
            best = (w, p, eq)
    if best is None:  # 全部超回撤线则放松, 取卡玛最高
        for w in W:
            port_ret = Rs.to_numpy() @ w
            eq = pd.Series((1 + port_ret).cumprod(), index=Rs.index)
            p = perf(eq)
            if best is None or p["calmar"] > best[1]["calmar"]:
                best = (w, p, eq)
    w, p, eq = best

    print(f"\n════════ 最优组合 (回撤约束 {MDD_LIMIT:.0%}) ════════")
    for c, wi in zip(selected, w):
        print(f"  {c:12s} 权重 {wi:.1%}")
    print(f"年化 {p['ann']:+.2%}  回撤 {p['mdd']:+.2%}  夏普 {p['sharpe']:.2f}  "
          f"卡玛 {p['calmar']:.2f}  累计 {p['cum']:+.1%}")
    print("逐年:", {k: f"{v:+.0%}" for k, v in yearly(eq).items()})


if __name__ == "__main__":
    main()
