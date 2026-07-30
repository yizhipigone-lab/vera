"""trade/ — QMT 实盘交易包 (P1 第一阶段地基)。

设计意图:
    单进程、单事件消费者线程 (交易状态唯一写者)、SQLite 持久化、
    Fake SDK 测试替身。铁律: 任何模块不得顶层 import xtquant
    (仅 gateway.py 方法内 lazy import); 业务代码零锁。

    本 __init__ 刻意不做任何 eager import —— 包内模块由
    composition root (trade_main.py, 后续阶段) 显式组装,
    禁止模块互 import 单例。
"""
