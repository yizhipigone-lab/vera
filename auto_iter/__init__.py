"""auto_iter — 自动策略迭代回测循环 (2026-08-10)。

全程离线: 信号由本地 parquet 自行计算, 回测走 BacktestEngine.run_cached
(预取矩阵入口, 不触发 DataFetcher/TDX)。详见 auto_strategy_loop.py 头注释。
"""
