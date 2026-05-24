"""报告生成器 — 聚合回测结果，生成标准化报告。"""

import json
import pandas as pd
import os
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Any

from .visualizer import Visualizer
from utils.logger import get_logger

logger = get_logger(__name__)


class ReportGenerator:
    """
    聚合回测结果，生成 HTML/JSON 报告。

    Parameters:
        config: 报告配置 (report section)
        dark_theme: 默认深色主题
    """

    def __init__(self, config: Optional[dict] = None, dark_theme: bool = True):
        config = config or {}
        self.output_formats = config.get("formats", ["html", "json"])
        self.plot_engine = config.get("plot_engine", "plotly")
        self.include_trade_log = config.get("include_trade_log", True)
        self.include_benchmark_chart = config.get("include_benchmark_chart", True)
        self.dpi = config.get("dpi", 300)
        self.output_dir = config.get("output_dir", "")
        if not self.output_dir:
            self.output_dir = str(Path(__file__).resolve().parents[1] / "output" / "reports")

        self.dark_theme = dark_theme
        self.visualizer = Visualizer(dark=dark_theme)

    def set_theme(self, dark: bool):
        self.dark_theme = dark
        self.visualizer.set_theme(dark)

    def generate(
        self,
        backtest_result: dict,
        benchmark_results: Optional[Dict[str, pd.DataFrame]] = None,
        strategy_name: str = "VERA",
        date_range: str = "",
    ) -> Dict[str, str]:
        """
        生成完整报告。

        Args:
            backtest_result: BacktestEngine.run() 的返回值
            benchmark_results: BenchmarkComparator 的返回值
            strategy_name: 策略名称
            date_range: 回测日期范围字符串

        Returns:
            dict: {"html": path, "json": path, "equity_png": path}
        """
        equity_curve = backtest_result.get("equity_curve", pd.DataFrame())
        trades = backtest_result.get("trades", pd.DataFrame())
        metrics = backtest_result.get("metrics", {})
        stop_summary = backtest_result.get("stop_config_summary", "")

        os.makedirs(self.output_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = f"{strategy_name}_{timestamp}"
        outputs = {}

        # 1. 生成图表
        equity_fig = self.visualizer.plot_equity_curve(
            equity_curve, benchmark_results,
            title=f"{strategy_name} 权益曲线",
        )

        monthly_fig = self.visualizer.plot_monthly_returns(equity_curve)
        trade_fig = self.visualizer.plot_trade_distribution(trades)
        exit_fig = self.visualizer.plot_exit_reasons(trades)
        hold_fig = self.visualizer.plot_hold_days(trades)

        # 2. 生成 HTML 报告
        if "html" in self.output_formats:
            html = self.visualizer.generate_html_report(
                equity_fig=equity_fig,
                monthly_fig=monthly_fig,
                trade_fig=trade_fig,
                exit_fig=exit_fig,
                hold_fig=hold_fig,
                metrics=metrics,
                config_summary=stop_summary,
                strategy_name=strategy_name,
                date_range=date_range,
            )
            html_path = os.path.join(self.output_dir, f"{base_name}.html")
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html)
            outputs["html"] = html_path
            logger.info(f"HTML 报告已生成: {html_path}")

        # 3. 生成 JSON 指标
        if "json" in self.output_formats:
            json_data = {
                "strategy_name": strategy_name,
                "run_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "date_range": date_range,
                "metrics": {k: (float(v) if isinstance(v, (int, float)) else str(v))
                           for k, v in metrics.items()},
                "stop_config": stop_summary,
                "trade_count": len(trades),
            }
            json_path = os.path.join(self.output_dir, f"{base_name}.json")
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(json_data, f, ensure_ascii=False, indent=2)
            outputs["json"] = json_path

        # 4. 生成权益曲线 PNG
        if not equity_curve.empty and self.plot_engine in ("matplotlib", "both"):
            png_path = os.path.join(self.output_dir, f"{base_name}_equity.png")
            self.visualizer.save_equity_png(equity_curve, png_path, self.dpi)
            outputs["equity_png"] = png_path

        # 5. 保存交易明细 CSV
        if not trades.empty and self.include_trade_log:
            csv_path = os.path.join(self.output_dir, f"{base_name}_trades.csv")
            trades.to_csv(csv_path, index=False, encoding="utf-8-sig")
            outputs["trades_csv"] = csv_path

        # 6. 保存权益曲线 CSV
        if not equity_curve.empty:
            equity_csv = os.path.join(self.output_dir, f"{base_name}_equity.csv")
            equity_curve.to_csv(equity_csv, index=False, encoding="utf-8-sig")
            outputs["equity_csv"] = equity_csv

        return outputs
