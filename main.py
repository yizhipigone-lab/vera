"""VERA — 通达信 VectorBT 一体化选股回测系统。

用法:
    python main.py
    python main.py --config path/to/strategy.yaml
    python main.py --config strategy.yaml --tdx
"""

import sys
import argparse
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from pipeline import Pipeline
from utils.logger import setup_logger, get_logger

logger = setup_logger("VERA-CLI", level="INFO")


def main():
    parser = argparse.ArgumentParser(
        description="VERA — 通达信 VectorBT 量化回测系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", "-c", type=str, default="",
                        help="策略配置文件路径（YAML）")
    parser.add_argument("--tdx", action="store_true",
                        help="将回测结果导出到通达信客户端界面")
    parser.add_argument("--defaults", type=str, default="",
                        help="默认配置文件路径")
    args = parser.parse_args()

    if args.config:
        config_path = Path(args.config)
    else:
        config_path = _PROJECT_ROOT / "config" / "strategy_example.yaml"

    if not config_path.exists():
        logger.error(f"配置文件不存在: {config_path}")
        sys.exit(1)

    default_path = str(_PROJECT_ROOT / "config" / "default.yaml")
    if args.defaults:
        default_path = args.defaults

    pipe = Pipeline(str(config_path), default_path)
    result = pipe.run(export_tdx=args.tdx)

    if "error" in result:
        logger.error(f"管线执行失败: {result['error']}")
        sys.exit(1)

    metrics = result.get("backtest", {}).get("metrics", {})
    if metrics:
        logger.info("=" * 50)
        logger.info("策略回测完成 — 核心指标:")
        logger.info(f"  累计收益率:   {metrics.get('cumulative_return', 0):>+.2%}")
        logger.info(f"  年化收益率:   {metrics.get('annualized_return', 0):>+.2%}")
        logger.info(f"  最大回撤:     {metrics.get('max_drawdown', 0):>+.2%}")
        logger.info(f"  夏普比率:     {metrics.get('sharpe_ratio', 0):>.2f}")
        logger.info(f"  胜率:         {metrics.get('win_rate', 0):>.1%}")
        logger.info(f"  交易笔数:     {metrics.get('total_trades', 0)}")
        logger.info("=" * 50)

        reports = result.get("reports", {})
        if "html" in reports:
            logger.info(f"查看完整报告: {reports['html']}")


if __name__ == "__main__":
    main()
