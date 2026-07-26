"""policy_kb - 政策匹配知识库(A 层:行业优先级标签,计划书 2026-07-26)。

模块:
- tongdaxin_priority.json: 128 通达信行业 → 十五五优先级(P1/P2/P3/AVOID)
- build_sector_index: 票 → 行业反向索引(遍历 128 行业构建,带缓存)
- policy_tagger: 给票列表打优先级标签(查 JSON + 反向索引)

松耦合:本模块失败不影响选股/回测/交易(只报告,铁律)。
"""
