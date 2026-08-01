"""trade/signals.py — 尾盘选股桥 (2026-07-27 尾盘自动买入 MVP)。

设计意图 (深模块: 藏住 TDX 复杂性):
    把 "TDX 路径注入 → TdxConnector 生命周期 → StockSelector →
    DataFrame → 当日信号 list[dict]" 这一串封成一个纯函数。
    调用方必须是工作线程 —— TDX 批量选股阻塞可达 60s,
    消费者线程 (交易状态唯一写者) 禁入, 卡死它 = 全系统停摆。
    只回股票代码与日期, 不回行情 (行情走 monitor 的订阅/轮询通道)。
"""

from __future__ import annotations

import sys
import time

from utils.logger import get_logger

_logger = get_logger("trade.signals")

# TDX 插件路径 (单一真相: core/tdx_path.py, 支持 TDX_HOME 环境变量)
from core.tdx_path import tdx_plugins_user

_TDX_PATH = tdx_plugins_user()


def run_tail_selection(formula_name: str, formula_arg: str,
                       universe_cfg: dict) -> list[dict]:
    """跑 TDX 公式选股, 只取**当日**信号 [{code, select_date}, ...]。

    Args:
        formula_name: 通达信公式名 (默认 QUANTQQ)
        formula_arg: 公式参数 (多数公式留空)
        universe_cfg: 股票池配置, 与回测 selection.universe 同形
            (type "50" 等, 参照 config/strategy_QUANTQQ.yaml)
    """
    if _TDX_PATH not in sys.path:
        sys.path.insert(0, _TDX_PATH)
    from core.connector import TdxConnector          # 阻塞初始化
    from selection.selector import StockSelector

    today = time.strftime("%Y%m%d")
    cfg = {
        "formula_name": formula_name,
        "formula_arg": formula_arg,
        "universe": dict(universe_cfg),
        "period": "1d",
        "dividend_type": 1,
    }
    # 生命周期对齐 pipeline/pipeline.py: initialize → 用 → close
    TdxConnector.initialize()
    try:
        df = StockSelector(cfg).run(start_time=today, end_time=today)
    finally:
        TdxConnector.close()

    if df is None or df.empty:
        _logger.info("尾盘选股无信号: %s", formula_name)
        return []
    out = []
    for _, row in df.iterrows():
        # select_date 口径归一化 (TDX 返回可能是 20260727 / 2026-07-27 /
        # 时间戳), 只留数字取前 8 位比对 —— 只要当日信号
        d = "".join(ch for ch in str(row["select_date"]) if ch.isdigit())[:8]
        if d == today:
            out.append({"code": str(row["stock_code"]), "select_date": d})
    _logger.info("尾盘选股: %s 命中 %d 只 (当日)", formula_name, len(out))
    return out
