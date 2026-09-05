"""trade/shadow.py — MA20 三态影子策略 (2026-08-23, P0 影子校尺)。

设计意图:
    2026-08-23《MA20三态vs动量轮动多维研判》结论: 动量为骨、三态为皮。
    旧三态规则 (已迁 trade/legacy_three_state.py, 2026-09-05 治理 III P0-2)
    改作 **影子策略**: 每个交易日与实跑动量并行算出三态状态, 追加 JSONL 落盘,
    **只记录不交易**; tools/shadow_compare.py 按季度对比滚动 90 日收益,
    连续两季显著跑赢 → 提示人工复审换规则。规则之争用数据自动裁决。

    铁律: 全函数 fail-soft —— 任何异常只记日志, 绝不影响交易链路;
    三态判断**复用** legacy_three_state.compute_signal (该模块零依赖,
    shadow→legacy 单向, rotation↔shadow 循环已随之破除), 不写第二份规则。

公开接口 (≤8):
    - compute_shadow_state(closes) -> dict        纯函数: 收盘序列 → 三态状态 dict
    - append_shadow_log(path, date_str, state)    JSONL 追加 (按日去重, 同日保留首条)
    - run_shadow(closes, date_str, path) -> dict|None   fail-soft 组合入口
"""

from __future__ import annotations

import json
import os

from trade.legacy_three_state import compute_signal  # 旧三态单一规则源 (零依赖, 无环)
from utils.logger import get_logger

_logger = get_logger("trade.shadow")

SHADOW_LOG_PATH = os.path.join("data", "shadow_rotation.jsonl")


def compute_shadow_state(closes: list[float]) -> dict:
    """收盘序列 → MA20 三态状态 dict (复用 legacy_three_state.compute_signal, 单一规则源)。

    返回 compute_signal 原样 dict: {"state": "full_cyb"|"half"|"full_gold"|None, ...}
    state=None 表示数据不足 (fail-closed 语义由调用方处理: 不落盘)。
    """
    return compute_signal(list(closes or []))


def append_shadow_log(path: str, date_str: str, state: dict) -> bool:
    """追加一条影子状态到 JSONL, 按日去重 (同日重跑保留首条, 不覆盖)。

    返回 True=已写入, False=同日已存在跳过。文件很小 (~250 行/年), 全量读判重。
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    if json.loads(line).get("date") == date_str:
                        return False
                except Exception:
                    continue
    rec = {"date": date_str, "state": state.get("state"),
           "ma20_direction": state.get("ma20_direction"),
           "drawdown": state.get("drawdown"),
           "close": state.get("close")}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return True


def run_shadow(closes: list[float], date_str: str,
               path: str = SHADOW_LOG_PATH) -> dict | None:
    """fail-soft 组合入口: 算三态 → 落盘。任何异常只记日志返 None。

    数据不足 (state=None) → 不落盘返 None (次日数据够了自然补上, 无空洞语义)。
    """
    try:
        st = compute_shadow_state(closes)
        if not st or st.get("state") is None:
            _logger.info("影子三态数据不足 (%d 根), 本日不落盘", len(closes or []))
            return None
        append_shadow_log(path, date_str, st)
        return st
    except Exception as e:
        _logger.warning("影子三态落盘失败 (不影响交易): %s", e)
        return None
