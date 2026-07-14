"""管线编排器 — 串联选股 → 数据 → 回测 → 报告全流程。"""

import os
import pandas as pd
from pathlib import Path
from datetime import datetime
from typing import Optional

from core.connector import TdxConnector
from selection.selector import StockSelector
from selection.deduplicator import Deduplicator
from pipeline.result_writer import PipelineResult
from backtest.engine import BacktestEngine
from backtest.benchmark import BenchmarkComparator
from report.report_generator import ReportGenerator
from report.tdx_export import TdxExporter
from utils.config_loader import ConfigLoader
from utils.logger import get_logger, setup_logger

logger = get_logger(__name__)


class Pipeline:
    """
    全自动量化管线。

    用法:
        pipe = Pipeline("config/strategy_example.yaml")
        result = pipe.run()
    """

    def __init__(self, strategy_config_path: str, default_config_path: Optional[str] = None):
        self.config = ConfigLoader.load_strategy(strategy_config_path, default_config_path)
        self.strategy_name = self.config.get("strategy", {}).get("name", "VERA")

        # Logger
        log_cfg = self.config.get("logging", {})
        log_file = log_cfg.get("file", "")
        if log_file:
            log_file = str(Path(strategy_config_path).parent.parent / log_file)
        setup_logger(
            "VERA",
            level=log_cfg.get("level", "INFO"),
            log_file=log_file,
            max_mb=log_cfg.get("max_size_mb", 100),
        )

        # 输出目录
        output_base = Path(__file__).resolve().parents[1] / "output"
        self.output_dirs = {
            "selections": str(output_base / "selections"),
            "reports": str(output_base / "reports"),
            "backtest": str(output_base / "backtest_results"),
            "logs": str(output_base / "logs"),
        }
        for d in self.output_dirs.values():
            os.makedirs(d, exist_ok=True)

        # 各模块
        self.selector = None
        self.backtest_engine = None
        self.stop_config = None

    def step1_select(self) -> pd.DataFrame:
        """执行选股。"""
        sel_cfg = self.config.get("selection", {})
        time_cfg = self.config.get("time_range", {})

        if not sel_cfg:
            logger.warning("未配置选股参数，跳过选股")
            return pd.DataFrame()

        self.selector = StockSelector(sel_cfg)
        stocks = self.selector.resolve_universe()

        start = time_cfg.get("start", "")
        end = time_cfg.get("end", "")

        picks = self.selector.run(start_time=start, end_time=end, stock_list=stocks)

        # 保存原始选股结果
        if not picks.empty:
            output_path = os.path.join(
                self.output_dirs["selections"],
                f"{self.strategy_name}_raw_{datetime.now():%Y%m%d_%H%M%S}.csv",
            )
            picks.to_csv(output_path, index=False, encoding="utf-8-sig")

        return picks

    def step2_backtest(self, selections: pd.DataFrame) -> dict:
        """执行回测。"""
        bt_cfg = self.config.get("backtest", {})
        time_cfg = self.config.get("time_range", {})
        self.stop_config = self.config.get("stop_loss", {})

        # P1-7: 校验选股/回测复权口径一致（engine 硬编码 "front"）
        from core.dividend_type import assert_consistent
        sel_cfg = self.config.get("selection", {})
        sel_adj = sel_cfg.get("dividend_type", 1)
        assert_consistent(sel_adj, "front")

        self.backtest_engine = BacktestEngine(bt_cfg)

        start = time_cfg.get("start", "")
        end = time_cfg.get("end", "")

        result = self.backtest_engine.run(
            selections=selections,
            start_time=start,
            end_time=end,
            stop_config=self.stop_config,
        )

        return result

    def step3_benchmark(self, backtest_result: dict) -> dict:
        """基准对比。"""
        bench_cfg = self.config.get("benchmark", {})
        time_cfg = self.config.get("time_range", {})
        # 传入回测周期，让基准对齐
        bt_cfg = self.config.get("backtest", {})
        if "period" in bt_cfg and "period" not in bench_cfg:
            bench_cfg = {**bench_cfg, "period": bt_cfg["period"]}

        comparator = BenchmarkComparator(bench_cfg)
        equity_curve = backtest_result.get("equity_curve", pd.DataFrame())

        if equity_curve.empty:
            return {}

        return comparator.fetch_and_compare(
            equity_curve,
            start_time=time_cfg.get("start", ""),
            end_time=time_cfg.get("end", ""),
        )

    def step4_report(
        self,
        backtest_result: dict,
        benchmark_results: dict,
    ) -> dict:
        """生成报告。"""
        report_cfg = self.config.get("report", {})
        time_cfg = self.config.get("time_range", {})

        generator = ReportGenerator(report_cfg, dark_theme=True)

        date_range = f"{time_cfg.get('start', '')} ~ {time_cfg.get('end', '')}"

        outputs = generator.generate(
            backtest_result=backtest_result,
            benchmark_results=benchmark_results,
            strategy_name=self.strategy_name,
            date_range=date_range,
        )

        return outputs

    def step5_export_to_tdx(self, backtest_result: dict) -> None:
        """可选：输出到通达信。"""
        exporter = TdxExporter()
        exporter.export_full_report(backtest_result, self.strategy_name)

    def run(self, export_tdx: bool = False, progress_callback=None) -> dict:
        """
        执行完整管线。

        Args:
            export_tdx: 是否将结果推送到通达信界面
            progress_callback: 可选回调 fn(progress_pct: int, step_name: str),
                              每步完成后调用, 用于 web 进度条细化 (候选 E)。
                              失败被吞 (不影响管线)。

        Returns:
            dict with keys: selections, backtest, benchmark, reports
        """
        def _cb(pct: int, name: str):
            """内部 callback 包装: 失败被吞, 防止 callback 异常中断管线."""
            if progress_callback:
                try:
                    progress_callback(pct, name)
                except Exception:
                    pass

        logger.info("=" * 60)
        logger.info(f"VERA 量化管线启动: {self.strategy_name}")
        logger.info(f"时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
        logger.info("=" * 60)

        # Step 1: 连接 TDX
        try:
            TdxConnector.initialize()
        except Exception as e:
            logger.error(f"TDX 连接失败: {e}")
            _cb(5, "连接通达信失败")
            return {"error": str(e), "selections": None, "backtest": None}
        _cb(5, "连接通达信")
        # C1-2: 对齐 server 的 8% TDX就绪进度点
        _cb(8, "TDX就绪")

        # Step 2: 选股
        logger.info("[Step 1/5] 执行选股...")
        _cb(10, "准备选股参数")
        selections = self.step1_select()
        _cb(15, "执行选股")
        if selections.empty:
            logger.warning("选股结果为空，管线终止")
            TdxConnector.close()
            _cb(20, "选股为空")
            return {"error": "no_selections", "selections": pd.DataFrame(), "backtest": None}
        _cb(20, "保存选股结果")

        # Step 3: 回测
        logger.info("[Step 2/5] 执行回测...")
        _cb(30, "构造回测引擎")
        backtest_result = self.step2_backtest(selections)
        _cb(50, "回测完成")

        # Step 4: 基准对比
        logger.info("[Step 3/5] 基准对比...")
        _cb(65, "拉取基准数据")
        benchmark_results = self.step3_benchmark(backtest_result)
        _cb(75, "基准对比完成")

        # Step 5: 生成报告
        logger.info("[Step 4/5] 生成报告...")
        _cb(85, "生成图表")
        report_outputs = self.step4_report(backtest_result, benchmark_results)
        _cb(90, "生成报告")

        # Step 6: 导出到 TDX（可选）
        if export_tdx:
            logger.info("[Step 5/5] 导出到通达信...")
            _cb(93, "导出到通达信")
            try:
                self.step5_export_to_tdx(backtest_result)
            except Exception as e:
                logger.warning(f"导出到通达信失败: {e}")
        _cb(95, "落盘结果")

        # 清理
        TdxConnector.close()

        logger.info("=" * 60)
        logger.info("管线执行完成！")
        if "html" in report_outputs:
            logger.info(f"HTML 报告: {report_outputs['html']}")
        if "json" in report_outputs:
            logger.info(f"JSON 指标: {report_outputs['json']}")
        logger.info("=" * 60)

        _cb(100, "完成")
        # C1-2: 返回 PipelineResult dataclass，dict-like 访问兼容 main.py 老调用方
        return PipelineResult(
            selections=selections,
            backtest=backtest_result,
            benchmark=benchmark_results,
            reports=report_outputs,
        )
