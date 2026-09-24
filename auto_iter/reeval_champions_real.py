# -*- coding: utf-8 -*-
"""一次性复评: 两个年化>=25%的冠军策略, 移动止盈切到条件单语义 (confirm="real") 前后对比。

用法: python -X utf8 auto_iter/reeval_champions_real.py
口径与主循环完全一致 (同股票池/费用/滑点), 仅 trailing_stop.confirm 不同。
"""
import sys, os, copy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auto_iter.common import (enforce_offline, build_pool_candidates, load_panel,
                              filter_pool, shrink_panel, seed_stock_info)
from auto_iter import auto_strategy_loop as m

# 两个冠军 (出自 iter_log.csv, 纯随机批次)
CHAMPIONS = [
    {"tag": "冠军28.2%", "side": "right",
     "factors": [{"name": "new_high", "n": 61},
                 {"name": "ma_bull", "fast": 10, "mid": 20, "slow": 60}],
     "stop": {"priority": "trailing_first",
              "cost_stop": {"enabled": True, "threshold": -0.102},
              "trailing_stop": {"enabled": True, "activation": 0.034, "drawdown": 0.014},
              "ladder_tp": {"enabled": True, "levels": [{"profit": 0.162, "sell_ratio": 0.25}]},
              "time_stop": {"enabled": True, "max_hold_days": 16},
              "cond_time_stop": {"enabled": False}, "first_day": {"enabled": False},
              "formula_sell": {"enabled": False}}},
    {"tag": "亚军26.1%", "side": "right",
     "factors": [{"name": "new_high", "n": 103},
                 {"name": "vol_surge", "n": 6, "r": 1.86}],
     "stop": {"priority": "stop_first",
              "cost_stop": {"enabled": True, "threshold": -0.146},
              "trailing_stop": {"enabled": True, "activation": 0.048, "drawdown": 0.012},
              "ladder_tp": {"enabled": True, "levels": [{"profit": 0.187, "sell_ratio": 0.29}]},
              "time_stop": {"enabled": True, "max_hold_days": 12},
              "cond_time_stop": {"enabled": False}, "first_day": {"enabled": False},
              "formula_sell": {"enabled": False}}},
]


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

    for champ in CHAMPIONS:
        for confirm in ("intraday", "real"):
            spec = {"side": champ["side"], "factors": champ["factors"],
                    "stop": copy.deepcopy(champ["stop"]),
                    "bt_cfg": m.DEFAULT_BT_CFG, "pool": dict(pool_params)}
            spec["stop"]["trailing_stop"]["confirm"] = confirm
            metrics, n_signals = m.evaluate_spec(panel, spec, min_signals=1)
            if not metrics:
                print(f"{champ['tag']} confirm={confirm}: 信号不足 {n_signals}")
                continue
            print(f"{champ['tag']} confirm={confirm:9s} | "
                  f"年化 {metrics['annualized_return']:+.1%} "
                  f"回撤 {metrics['max_drawdown']:+.1%} "
                  f"夏普 {metrics['sharpe_ratio']:.2f} "
                  f"胜率 {metrics['win_rate']:.1%} "
                  f"PF {metrics['profit_factor']:.2f} "
                  f"交易 {metrics['total_trades']} 笔 信号 {n_signals}", flush=True)


if __name__ == "__main__":
    main()
