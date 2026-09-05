"""trade/legacy_three_state.py — 旧 MA20 三态规则归档 (2026-08-20 动量改造后弃用)。

2026-08-28 治理 II P0-2 (2026-09-05 执行): 从 trade/rotation.py 整体迁出。
生产已切动量规则 (compute_momentum_signal / momentum_target_values), 本模块
仅两类调用方:
  - trade/shadow.py 影子校尺 (只记录不交易的对照策略);
  - research/*.py 历史回测脚本 (2026-08-20 前研究的复现/比对)。

纯函数、零运行时依赖 (无 import), 便于独立 import 不被 rotation 的
类体/事件链牵连。改名必改引用: 三态符号全从这里 import。

规则 (唯一真相):
  state = full_cyb(满仓创业板50) / half(半仓, 主腿+避险各半) / full_gold(满仓避险)
  = MA20 方向 (今>昨 up / 否则 down) 叠加 250 日高点回撤:
    up 且回撤 ≥ -threshold → full_cyb; up 且跌破 → half; down → full_gold。
"""
from __future__ import annotations

# 三态状态名
STATE_FULL_CYB = "full_cyb"    # 满仓创业板50ETF (deprecated)
STATE_HALF = "half"            # 半仓 (创业板 + 避险腿各半) (deprecated)
STATE_FULL_GOLD = "full_gold"  # 满仓避险腿 (deprecated)

# 三态 → (主腿比例, 避险腿总比例)。避险腿内部再按 hedge_ratio 拆两只 (见 target_values)。
STATE_RATIOS = {
    STATE_FULL_CYB: (1.0, 0.0),
    STATE_HALF: (0.5, 0.5),
    STATE_FULL_GOLD: (0.0, 1.0),
}


def compute_signal(closes: list[float], ma_window: int = 20,
                   high_window: int = 250,
                   drawdown_threshold: float = 0.20) -> dict:
    """(deprecated, 2026-08-20) 旧 MA20 三态信号纯函数。生产已切动量规则
    compute_momentum_signal, 此函数仅 research/*.py 历史回测脚本仍调用。

    返回 state (三态之一) 或 None (数据不足, fail-closed 不动作);
    其余字段为明细 (ma20 方向/250日高点/回撤率/最新收盘), 供 UI 展示。
    """
    need = max(high_window, ma_window + 1)
    if not closes or len(closes) < need:
        return {
            "state": None,
            "reason": f"数据不足: 需要 {need} 根, 实际 {len(closes)} 根",
            "ma20_today": None, "ma20_yesterday": None,
            "ma20_direction": None, "high_250": None,
            "drawdown": None, "close": None,
        }
    ma20_today = sum(closes[-ma_window:]) / ma_window
    ma20_yesterday = sum(closes[-ma_window - 1:-1]) / ma_window
    # 审计 L2: 手册只定义今>昨/今<昨, 未定义今==昨; 用严格 > , 相等归 down
    # (→ 满仓黄金, 风险回避方向, 保守 fail-safe)。浮点均值精确相等属测度零。
    direction = "up" if ma20_today > ma20_yesterday else "down"
    high_250 = max(closes[-high_window:])
    close = closes[-1]
    drawdown = (close / high_250 - 1.0) if high_250 > 0 else 0.0
    if direction == "up":
        state = STATE_FULL_CYB if drawdown >= -drawdown_threshold else STATE_HALF
    else:
        state = STATE_FULL_GOLD
    return {
        "state": state,
        "ma20_today": round(ma20_today, 4),
        "ma20_yesterday": round(ma20_yesterday, 4),
        "ma20_direction": direction,
        "high_250": round(high_250, 4),
        "drawdown": round(drawdown, 4),
        "close": round(close, 4),
    }


def _derive_state(v_cyb: float, v_hedge: float) -> str | None:
    """从主腿市值 + 避险腿总市值派生当前状态 (自愈: 换档半途失败次日自然补齐)。
    None = 主腿与避险腿都空仓 (首日未建仓)。阈值 90%/10% 容差防碎仓误判。"""
    total = v_cyb + v_hedge
    if total <= 0:
        return None
    ratio = v_cyb / total
    if ratio >= 0.9:
        return STATE_FULL_CYB
    if ratio <= 0.1:
        return STATE_FULL_GOLD
    return STATE_HALF


def target_values(cyb_etf: str, gold_etf: str, hedge_etf2: str,
                  hedge_ratio: float, target_state: str,
                  pool: float) -> list[tuple[str, float]]:
    """三态 + 避险两腿配置 → [(代码, 目标市值)] (主腿在前, 避险腿在后)。

    避险腿总权重 = STATE_RATIOS[state] 第二项; 再按 hedge_ratio 拆两只:
      黄金 = hedge_ratio × 避险总,  避险ETF2 = (1-hedge_ratio) × 避险总。
    hedge_etf2 为空时 hedge_ratio 视为 1.0 (单避险, 与旧口径逐字节一致)。
    """
    cyb_w, hedge_w = STATE_RATIOS[target_state]
    ratio = hedge_ratio if hedge_etf2 else 1.0
    hedge_pool = hedge_w * pool
    legs = [(cyb_etf, cyb_w * pool), (gold_etf, ratio * hedge_pool)]
    if hedge_etf2:
        legs.append((hedge_etf2, (1.0 - ratio) * hedge_pool))
    return legs
