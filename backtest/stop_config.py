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
        }

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
        }


# === 自检 ===
if __name__ == '__main__':
    cfg = load_stop_config()
    print('[OK] load_stop_config 返回值:')
    import json
    print(json.dumps(cfg, ensure_ascii=False, indent=2))