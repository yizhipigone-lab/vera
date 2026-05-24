"""TDX 输出 — 将回测结果发送到通达信客户端展示。"""

import pandas as pd
from typing import Dict, Optional

from core.signal_exporter import SignalExporter
from utils.logger import get_logger

logger = get_logger(__name__)


class TdxExporter:
    """将回测结果推送到通达信 TQ 界面。"""

    def export_full_report(
        self,
        backtest_result: dict,
        strategy_name: str = "VERA",
    ) -> bool:
        """
        将回测报告的全部数据发送到通达信。

        Args:
            backtest_result: BacktestEngine.run() 的返回值
            strategy_name: 策略名称
        """
        metrics = backtest_result.get("metrics", {})
        trades = backtest_result.get("trades", pd.DataFrame())
        equity_curve = backtest_result.get("equity_curve", pd.DataFrame())
        stop_summary = backtest_result.get("stop_config_summary", "")

        # 准备多个 DataFrame
        df_list = []

        # Table 1: 回测指标
        if metrics:
            metrics_df = pd.DataFrame([
                {"指标": "累计收益率", "数值": f"{metrics.get('cumulative_return', 0):.2%}"},
                {"指标": "年化收益率", "数值": f"{metrics.get('annualized_return', 0):.2%}"},
                {"指标": "最大回撤", "数值": f"{metrics.get('max_drawdown', 0):.2%}"},
                {"指标": "夏普比率", "数值": f"{metrics.get('sharpe_ratio', 0):.2f}"},
                {"指标": "卡玛比率", "数值": f"{metrics.get('calmar_ratio', 0):.2f}"},
                {"指标": "胜率", "数值": f"{metrics.get('win_rate', 0):.1%}"},
                {"指标": "盈亏比", "数值": f"{metrics.get('profit_loss_ratio', 0):.2f}"},
                {"指标": "总交易笔数", "数值": str(metrics.get('total_trades', 0))},
                {"指标": "平均持仓天数", "数值": f"{metrics.get('avg_hold_days', 0):.0f}天"},
            ])
            df_list.append(metrics_df)

        # Table 2: 交易明细 (最近50笔)
        if not trades.empty and len(trades) > 0:
            trade_preview = trades.tail(50).copy()
            cols = ["stock_code", "entry_date", "exit_date", "exit_reason", "profit_pct"]
            available = [c for c in cols if c in trade_preview.columns]
            if available:
                trade_preview = trade_preview[available]
                df_list.append(trade_preview)

        # Table 3: 权益曲线（最近数据）
        if not equity_curve.empty:
            eq_preview = equity_curve.tail(100).copy()
            if "date" in eq_preview.columns and "equity" in eq_preview.columns:
                df_list.append(eq_preview[["date", "equity", "drawdown"]])

        if not df_list:
            logger.warning("无数据可导出到通达信")
            return False

        table_names = ["回测指标", "交易明细", "权益曲线"]
        table_names = table_names[:len(df_list)]

        success = SignalExporter.print_to_tdx(
            df_list=df_list,
            sp_name=strategy_name,
            xml_filename=f"{strategy_name}_report.xml",
            table_names=table_names,
        )

        return success

    def export_trades_as_warnings(
        self,
        trades: pd.DataFrame,
        max_count: int = 50,
    ) -> bool:
        """
        将交易记录作为预警信号发送到通达信。

        Args:
            trades: 交易记录 DataFrame
            max_count: 最多发送条数
        """
        if trades.empty:
            return False

        recent = trades.tail(max_count)
        stock_list = recent["stock_code"].tolist()[:max_count]
        time_list = recent["exit_date"].apply(
            lambda d: pd.Timestamp(d).strftime("%Y%m%d%H%M%S")
        ).tolist()[:max_count]

        reasons = []
        if "exit_reason" in recent.columns:
            reason_map = {
                "cost_stop": "成本止损",
                "trailing_stop": "移动止损",
                "ladder_tp": "阶梯止盈",
                "time_stop": "时间止损",
                "signal": "信号卖出",
                "end_of_data": "期末平仓",
            }
            reasons = [reason_map.get(r, r) for r in recent["exit_reason"].tolist()]

        # 盈利=买入信号(0), 亏损=卖出信号(1)
        bs_flags = []
        if "profit_pct" in recent.columns:
            bs_flags = ["0" if p > 0 else "1" for p in recent["profit_pct"]]

        return SignalExporter.send_warnings(
            stock_list=stock_list,
            time_list=time_list,
            reasons=reasons,
            bs_flag_list=bs_flags,
            count=min(max_count, len(stock_list)),
        )
