"""concept_kb — B 层概念抽取 (政策研究平台 P1.5, 计划书 §3/§6)。

通达信 TDGN 概念板块 (题材: 5G/AI/华为/锂电...) → {票: [概念]} 反向索引。
实时 (xtquant 每次拿最新) + TTL 日级缓存 (保持更新 + 不重复拉)。
失败兜底返 None (松耦合, serialize 不加 key, 选股交易零感知)。

数据源: xtdata.get_sector_list() 筛 TDGN + get_stock_list_in_sector(concept)。
TTL: concept_kb/concept_cache.json, 默认 1 天失效 (复用 selection_cache 按日哲学)。
强制刷新: python -m concept_kb.concept_tagger --refresh
"""
