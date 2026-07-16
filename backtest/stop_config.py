"""
止盈止损统一加载入口 (P2 修复)

修复前问题:
  - 16 个脚本各自硬编码 STOP_CONFIG 字典
  - 与 config/default.yaml 一致/不一致混在一起, 配置真相源失效
  - 改一次止损参数要同步 17 处, 必然漂移

修复:
  - 所有 [已和 yaml 对齐的] 脚本改为 from backtest.stop_config import load_stop_config
  - 4 个 [故意与 yaml 不同的] 脚本 (ma_cross/batch_*) 保留硬编码, 因为它们是不同策略意图
  - 老脚本输出数字与改前完全一致 (yaml 里 threshold=-0.12, 与原硬编码相同)

用法:
    from backtest.stop_config import load_stop_config
    STOP_CONFIG = load_stop_config()   # 读 config/default.yaml['stop_loss']
"""
from typing import Any, Dict, Optional

from utils.logger import get_logger

# P0-2 (2026-07-15): 模块级 logger (修复审计 C2 描述失真 —
# 原代码缺 logger = get_logger(__name__), except 块靠丑陋的局部赋值)
logger = get_logger(__name__)

# 移动止损止盈默认值：与 config/default.yaml 和 commit 1900b8f 对齐。
DEFAULT_TRAILING_ACTIVATION = 0.035
DEFAULT_TRAILING_DRAWDOWN = 0.01


def load_stop_config(default_path: Optional[str] = None) -> Dict[str, Any]:
    """
    加载 config/default.yaml 里的 stop_loss 子树.

    Returns:
        dict 形如:
        {
          'priority':      'trailing_first' | 'ladder_tp_first' | 'stop_first',  # 2026-07-05: 优先级
          'cost_stop':     {'enabled': True, 'threshold': -0.12},
          'trailing_stop': {'enabled': True, 'activation': 0.035, 'drawdown': 0.01},
          'ladder_tp': {
            'enabled': True,
            'levels': [
              {'profit': 0.06, 'sell_ratio': 0.30},
              {'profit': 0.15, 'sell_ratio': 0.30},
            ],
          },
          'time_stop':      {'enabled': True, 'max_hold_days': 20},
          'cond_time_stop': {'enabled': False},
          'first_day':      {'enabled': False},
          'formula_sell':   {'enabled': False, ...},                  # P-v3.4
          'capabilities':   {'formula_exit': True, 'gap_protection': True, 'delisting': True},  # 候选 A 阶段 1
        }

    capabilities 语义 (候选 A 阶段 1): 三开关默认全开, gate run_cached 已提供的能力数据。
    开关 on + 数据 None → 能力 off (=旧行为); 开关 off → 强制 None。yaml 缺 capabilities 时
    run_cached 用 .get("capabilities", {}) 回退全 True (安全)。

    Raises:
        FileNotFoundError: yaml 不存在
        KeyError: yaml 里没有 stop_loss 节点
    """
    from utils.config_loader import ConfigLoader

    defaults = ConfigLoader.load_defaults() if default_path is None else ConfigLoader.load_yaml(default_path)
    if 'stop_loss' not in defaults:
        raise KeyError("config/default.yaml 里没有 'stop_loss' 节点 — 请检查 yaml 结构")
    return defaults['stop_loss']


def get_stop_config_summary(stop_loss_config: dict) -> str:
    """
    根据 stop_loss 配置字典生成摘要字符串。

    C2: 从 engine.py run() 里 StopManager.get_config_summary() 调用迁出，
    消除 engine.py 旧版 exit 计算逻辑的重复（compute_exit_signals / _compute_single_stock）。
    仅做配置展示，无交易逻辑。
    """
    lines = []
    cost = stop_loss_config.get("cost_stop", {})
    if cost.get("enabled", True):
        lines.append(f"成本止损: {cost.get('threshold', -0.12):.1%}（Low触发, stop_price执行）")

    ladder = stop_loss_config.get("ladder_tp", {})
    if ladder.get("enabled", True) and ladder.get("levels"):
        levels_str = " → ".join(
            f"盈利{lv['profit']:.0%}卖{lv['sell_ratio']:.0%}"
            for lv in sorted(ladder["levels"], key=lambda x: x.get("profit", 0))
        )
        lines.append(f"阶梯止盈: {levels_str}（High触发, ladder_price执行）")

    trail = stop_loss_config.get("trailing_stop", {})
    if trail.get("enabled", True):
        activation = trail.get("activation")
        if activation is None:
            activation = DEFAULT_TRAILING_ACTIVATION
        drawdown = trail.get("drawdown")
        if drawdown is None:
            drawdown = DEFAULT_TRAILING_DRAWDOWN
        lines.append(
            f"移动止盈: 盈利{activation:.1%}激活, "
            f"盘中Low触及回撤{drawdown:.1%}线即按回撤线价成交"
        )

    time_s = stop_loss_config.get("time_stop", {})
    if time_s.get("enabled", True):
        lines.append(f"时间止损: {time_s.get('max_hold_days', 20)}天（Close执行）")

    # P-v3.4: 公式卖出
    fs = stop_loss_config.get("formula_sell", {})
    if fs.get("enabled", False):
        fname = fs.get("formula_name", "") or "?(未配置公式名)"
        lines.append(
            f"公式卖出: [{fname}] 命中即卖{fs.get('sell_ratio', 1.0):.0%} "
            f"（优先级 #{fs.get('priority', 0)}，最高=0）"
        )
    return "\n".join(lines)


def load_stop_config_or_default() -> Dict[str, Any]:
    """
    加载失败时回退到代码内兜底 (-0.12/0.035/0.01/6%-15%/20天)
    用于 yaml 缺失也能跑的容错场景.
    """
    try:
        return load_stop_config()
    except Exception:
        # P0-2 (2026-07-15): 用模块级 logger (替代原 except 内局部赋值)
        logger.error("加载 stop_config 失败, 回退硬编码兜底 (请检查 config/default.yaml 格式)", exc_info=True)
        return {
            # 候选 A 阶段 1: 补 priority (原兜底漏了, 与 default.yaml 对齐)
            'priority':       'trailing_first',
            'cost_stop':     {'enabled': True, 'threshold': -0.12},
            'trailing_stop': {
                'enabled': True,
                'activation': DEFAULT_TRAILING_ACTIVATION,
                'drawdown': DEFAULT_TRAILING_DRAWDOWN,
            },
            'ladder_tp': {
                'enabled': True,
                'levels': [
                    {'profit': 0.06, 'sell_ratio': 0.30},
                    {'profit': 0.15, 'sell_ratio': 0.30},
                ],
            },
            'time_stop':      {'enabled': True, 'max_hold_days': 20},
            'cond_time_stop': {'enabled': False},
            'first_day':      {'enabled': False},
            # P-v3.4: 公式卖出兜底 (默认关, 安全优先)
            'formula_sell': {
                'enabled': False,
                'formula_name': '',
                'formula_arg': '',
                'sell_ratio': 1.0,
                'priority': 0,
            },
            # 候选 A 阶段 1: 三类能力开关 (默认全开; 数据未提供时对应能力自然 off)
            'capabilities': {
                'formula_exit': True,
                'gap_protection': True,
                'delisting': True,
            },
        }


# === 自检 ===
if __name__ == '__main__':
    cfg = load_stop_config()
    print('[OK] load_stop_config 返回值:')
    import json
    print(json.dumps(cfg, ensure_ascii=False, indent=2))