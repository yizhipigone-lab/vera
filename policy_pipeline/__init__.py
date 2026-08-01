"""policy_pipeline — 政策流水线 (政策研究平台 P1, 计划书 §6)。

DeepSeek 抽政策→行业 (NBER w33814 四步法) → AFFECTS 边入库 (kg_edges)。
松耦合: DeepSeek 挂/超时 → 返 None, 不影响选股交易。
"""
