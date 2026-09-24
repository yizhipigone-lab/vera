# -*- coding: utf-8 -*-
"""定向实验2: 仓位管理对 冠军底座(iter_1092, 27.1%/-30.3%) 的收益/回撤弹性。

假设: 信号充足时仓位接近满仓, 回撤与仓位大致成正比 — 若如此, 降仓位=等比降收益,
不存在"回撤砍半收益不变"的免费午餐。用网格实测验证。

用法: python -X utf8 auto_iter/grid_position_sizing.py
"""
import sys, os, copy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auto_iter.common import (enforce_offline, build_pool_candidates, load_panel,
                              filter_pool, shrink_panel, seed_stock_info)
from auto_iter import auto_strategy_loop as m
from auto_iter.grid_mkt_filter import BASE_A  # 复用冠军底座定义

# (max_positions, max_buy_amount) 网格: 从满仓到约 1/10 仓
GRID = [(999, 20000), (50, 20000), (25, 20000), (15, 20000),
        (10, 20000), (10, 10000), (5, 20000)]


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
    print(f"[初始化] 股票池 {len(codes)} 只", flush=True)

    for max_pos, max_amt in GRID:
        spec = copy.deepcopy(BASE_A)
        bt = copy.deepcopy(m.DEFAULT_BT_CFG)
        bt["position_sizing"] = dict(bt["position_sizing"],
                                     max_positions=max_pos, max_buy_amount=float(max_amt))
        spec.update(bt_cfg=bt, pool=dict(pool_params))
        metrics, n_sig = m.evaluate_spec(panel, spec, min_signals=1)
        cap = max_pos * max_amt / 1_000_000  # 理论最大仓位占本金比例
        print(f"max_pos={max_pos:4d} 单笔上限={max_amt:6d} (约{cap:4.0%}仓) | "
              f"年化 {metrics['annualized_return']:+6.1%} 回撤 {metrics['max_drawdown']:+6.1%} "
              f"夏普 {metrics['sharpe_ratio']:5.2f} 交易 {metrics['total_trades']:6d} 笔", flush=True)


if __name__ == "__main__":
    main()
