"""policy_pipeline.sources — P1b 政策源子模块 (自动抓取 → sanitize → 入库)。

松耦合/fail-open: 网络失败、解析失败、库缺失一律记 warning 返空, 绝不抛出,
不影响选股交易主链路。

子模块:
- gov_cn: 中国政府网最新政策抓取 (stdlib only, 零新依赖)
- news_search: 新闻搜索多源兜底 (akshare 可选, 缺失则降级为空)
"""
from __future__ import annotations
