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


def load_stop_config(default_path: Optional[str] = None) -> Dict[str, Any]:
    """
    加载 config/default.yaml 里的 stop_loss 子树.

    Returns:
        dict 形如:
        {
          'priority':      'trailing_first' | 'ladder_tp_first' | 'stop_first',  # 2026-07-05: 优先级
          'cost_stop':     {'enabled': True, 'threshold': -0.12},
          'trailing_stop': {'enabled': True, 'activation': 0.08, 'drawdown': 0.05},
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
    消除 stop_manager.py 的重复 exit 计算逻辑（compute_exit_signals / _compute_single_stock）。
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
        lines.append(
            f"移动止损: 盈利{trail.get('activation', 0.08):.1%}激活, "
            f"盘中Low触及回撤{trail.get('drawdown', 0.05):.1%}线即按回撤线价成交"
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
    加载失败时回退到代码内兜底 (-0.12/0.08/0.05/6%-15%/20天)
    用于 yaml 缺失也能跑的容错场景.
    """
    try:
        return load_stop_config()
    except Exception:
        return {
            # 候选 A 阶段 1: 补 priority (原兜底漏了, 与 default.yaml 对齐)
            'priority':       'trailing_first',
            'cost_stop':     {'enabled': True, 'threshold': -0.12},
            'trailing_stop': {'enabled': True, 'activation': 0.08, 'drawdown': 0.05},
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