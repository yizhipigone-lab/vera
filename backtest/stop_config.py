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