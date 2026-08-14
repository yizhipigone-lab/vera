# -*- coding: utf-8 -*-
"""定向实验: 给两个冠军底座叠加市场过滤因子, 网格扫描看能否兼顾 25% 年化 + 15% 回撤。

底座A = iter_1092 (年化27.1%/回撤-30.3%): 收益够, 要砍回撤。
底座B = iter_1140 合规区最优 (~19%/-11.6% 家族): 回撤够, 要抬收益。
每个底座 × 3 个市场因子 × 各因子参数网格, 逐一评估。

用法: python -X utf8 auto_iter/grid_mkt_filter.py
"""
import sys, os, copy, json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auto_iter.common import (enforce_offline, build_pool_candidates, load_panel,
                              filter_pool, shrink_panel, seed_stock_info)
from auto_iter import auto_strategy_loop as m

# 底座A: iter_1092 冠军 (27.1%/-30.3%)
BASE_A = {"side": "left",
          "factors": [{"name": "bias_ma", "n": 55, "x": 0.2},
                      {"name": "bias_low", "n": 26, "x": 0.0117},
                      {"name": "rsi_low", "n": 8, "th": 19.0121}],
          "stop": {"priority": "trailing_first",
                   "cost_stop": {"enabled": True, "threshold": -0.25},
                   "trailing_stop": {"enabled": False, "activation": 0.0964,
                                     "drawdown": 0.0363, "confirm": "real"},
                   "ladder_tp": {"enabled": True, "levels": [
                       {"profit": 0.0714, "sell_ratio": 0.1683},
                       {"profit": 0.1315, "sell_ratio": 0.1619},
                       {"profit": 0.3335, "sell_ratio": 0.2161}]},
                   "time_stop": {"enabled": True, "max_hold_days": 35},
                   "cond_time_stop": {"enabled": False}, "first_day": {"enabled": False},
                   "formula_sell": {"enabled": False}}}

# 市场因子参数网格
MKT_GRID = (
    [{"name": "mkt_med_ma", "n": n} for n in (20, 40, 60, 120)]
    + [{"name": "mkt_breadth", "n": n, "th": th}
       for n in (20, 60) for th in (0.3, 0.4, 0.5, 0.6)]
    + [{"name": "mkt_not_crash", "n": n, "x": x}
       for n in (5, 10, 20) for x in (0.03, 0.05, 0.08, 0.12)]
)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    enforce_offline()
    args = m.parse_args([])
    pool_params = {"amount_quantile": args.amount_quantile,
                   "min_price": 1.0, "max_stocks": args.pool_size}
    candidates = build_pool_candidates()
    panel = load_panel(candidates)
    codes = filter_pool(panel, **pool_params)
    panel = shrink_panel(panel, codes)
    seed_stock_info(codes)
    print(f"[初始化] 股票池 {len(codes)} 只, 网格 {len(MKT_GRID)} 组", flush=True)

    # 底座A 裸跑基线
    spec = copy.deepcopy(BASE_A)
    spec.update(bt_cfg=m.DEFAULT_BT_CFG, pool=dict(pool_params))
    base_m, base_n = m.evaluate_spec(panel, spec, min_signals=1)
    print(f"底座A 裸跑: 年化 {base_m['annualized_return']:+.1%} "
          f"回撤 {base_m['max_drawdown']:+.1%} 信号 {base_n}", flush=True)

    rows = []
    for mf in MKT_GRID:
        spec = copy.deepcopy(BASE_A)
        spec["factors"] = spec["factors"] + [mf]
        spec.update(bt_cfg=m.DEFAULT_BT_CFG, pool=dict(pool_params))
        metrics, n_sig = m.evaluate_spec(panel, spec, min_signals=30)
        if not metrics:
            continue
        rows.append((mf, metrics, n_sig))
        tag = "★双达标" if (metrics["annualized_return"] >= 0.25
                          and metrics["max_drawdown"] >= -0.15) else ""
        print(f"{json.dumps(mf):45s} 年化 {metrics['annualized_return']:+6.1%} "
              f"回撤 {metrics['max_drawdown']:+6.1%} 夏普 {metrics['sharpe_ratio']:5.2f} "
              f"信号 {n_sig:6d} {tag}", flush=True)

    print("\n=== 按 年化(回撤合规优先) 排序 Top5 ===")
    ok = [r for r in rows if r[1]["max_drawdown"] >= -0.15]
    for mf, mt, n in sorted(ok or rows, key=lambda r: r[1]["annualized_return"],
                            reverse=True)[:5]:
        print(f"{json.dumps(mf):45s} 年化 {mt['annualized_return']:+.1%} "
              f"回撤 {mt['max_drawdown']:+.1%} 信号 {n}")


if __name__ == "__main__":
    main()
