"""tools/ — 研究/运维脚本包 (批次 6 · D3 package 化)。

生产代码通过 `from tools.xxx import ...` 正常导入 (如 selection/factor_filter.py),
不再 sys.path 注入 tools/ 目录。脚本仍可直接 `python tools/xxx.py` 运行
(各脚本顶部保留 repo-root 自举, 兼容直接执行)。
"""
