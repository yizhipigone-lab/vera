"""kg — 政策-行业-个股知识图谱 (政策研究平台 P0, 计划书 §3/§6)。

ChainKnowledgeGraph 数据 → SQLite kg_nodes + kg_edges。
不引 neo4j (实盘后台「如无必要勿增实体」, 几十万边 SQLite + 递归 CTE 够)。

节点: company(公司) / industry(行业) / product(产品)
边: belongs_to(公司→行业) / main_product(公司→主营产品) /
    industry_upstream(行业→上游行业) / product_upstream(产品→上游材料)

松耦合: 图谱崩/空 → query 返 None → serialize 不加 key → 选股交易零感知。
"""
