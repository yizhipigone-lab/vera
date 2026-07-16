"""可视化引擎 — Plotly 交互图表 + Matplotlib 静态图表。

风格：量化专业风，深色/浅色双主题，A股红涨绿跌惯例。
"""

import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple
from pathlib import Path

from utils.logger import get_logger

logger = get_logger(__name__)

# === 配色方案 ===

class ThemeColor:
    """量化专业配色 — 深色主题 / 浅色主题。"""

    @staticmethod
    def get_colors(dark: bool = True) -> dict:
        if dark:
            return {
                "bg": "#0d1117",
                "plot_bg": "#161b22",
                "paper": "#0d1117",
                "grid": "rgba(255,255,255,0.08)",
                "text": "#e6edf3",
                "text_secondary": "#8b949e",
                "up": "#ef4444",        # 红涨
                "down": "#22c55e",      # 绿跌
                "equity": "#58a6ff",    # 权益曲线
                "benchmark": "#d2a8ff",  # 基准曲线
                "drawdown": "rgba(239,68,68,0.3)",
                "card_bg": "#21262d",
                "accent": "#58a6ff",
            }
        else:
            return {
                "bg": "#ffffff",
                "plot_bg": "#f6f8fa",
                "paper": "#ffffff",
                "grid": "rgba(0,0,0,0.06)",
                "text": "#24292f",
                "text_secondary": "#656d76",
                "up": "#d1242f",        # 红涨（浅色下更深一点）
                "down": "#1a7f37",      # 绿跌
                "equity": "#0550ae",
                "benchmark": "#8250df",
                "drawdown": "rgba(209,36,47,0.15)",
                "card_bg": "#f6f8fa",
                "accent": "#0969da",
            }

    @staticmethod
    def get_template(dark: bool = True) -> str:
        return "plotly_dark" if dark else "plotly_white"


class Visualizer:
    """双引擎可视化：Plotly（交互HTML）+ Matplotlib（静态PNG）。"""

    def __init__(self, dark: bool = True):
        self.dark = dark
        self.colors = ThemeColor.get_colors(dark)
        self.template = ThemeColor.get_template(dark)

    def set_theme(self, dark: bool) -> None:
        self.dark = dark
        self.colors = ThemeColor.get_colors(dark)
        self.template = ThemeColor.get_template(dark)

    def _base_layout(self, title: str, height: int = 500) -> dict:
        """通用 chart layout。"""
        return dict(
            template=self.template,
            paper_bgcolor=self.colors["paper"],
            plot_bgcolor=self.colors["plot_bg"],
            font=dict(color=self.colors["text"], family="Inter, -apple-system, sans-serif"),
            title=dict(
                text=title,
                font=dict(size=16, color=self.colors["text"]),
                x=0.02,
            ),
            height=height,
            hovermode="x unified",
            margin=dict(l=60, r=30, t=60, b=40),
            xaxis=dict(
                gridcolor=self.colors["grid"],
                zeroline=False,
                showline=True,
                linecolor=self.colors["grid"],
            ),
            yaxis=dict(
                gridcolor=self.colors["grid"],
                zeroline=False,
                showline=True,
                linecolor=self.colors["grid"],
            ),
            legend=dict(
                orientation="h",
                yanchor="top",
                y=1.12,
                xanchor="right",
                x=1,
                font=dict(size=11),
            ),
        )

    # ===== 权益曲线 =====

    def plot_equity_curve(
        self,
        equity_df: pd.DataFrame,
        benchmark_data: Optional[Dict[str, pd.DataFrame]] = None,
        title: str = "策略权益曲线",
    ) -> go.Figure:
        """
        Plotly 权益曲线图。

        上子图: 策略权益 + 基准曲线
        下子图: 回撤区域
        """
        fig = make_subplots(
            rows=2, cols=1,
            shared_xaxes=True,
            vertical_spacing=0.03,
            row_heights=[0.7, 0.3],
        )

        # 策略权益
        if "equity" in equity_df.columns and "date" in equity_df.columns:
            fig.add_trace(
                go.Scatter(
                    x=equity_df["date"],
                    y=equity_df["equity"],
                    mode="lines",
                    name="策略权益",
                    line=dict(color=self.colors["equity"], width=2),
                    hovertemplate="%{x|%Y-%m-%d}<br>权益: %{y:,.0f}<extra></extra>",
                ),
                row=1, col=1,
            )

        # 基准曲线
        if benchmark_data:
            for name, bm_df in benchmark_data.items():
                if "date" in bm_df.columns or bm_df.index.name == "date":
                    dates = bm_df.index if bm_df.index.name == "date" else bm_df["date"]
                    vals = bm_df.get("strategy_equity", bm_df.iloc[:, 0] if len(bm_df.columns) > 0 else None)
                    if vals is not None:
                        fig.add_trace(
                            go.Scatter(
                                x=dates, y=vals,
                                mode="lines",
                                name=f"基准: {name}",
                                line=dict(color=self.colors["benchmark"], width=1.5, dash="dot"),
                                hovertemplate=f"{name}: %{{y:.3f}}<extra></extra>",
                            ),
                            row=1, col=1,
                        )

        # 回撤
        if "drawdown" in equity_df.columns:
            drawdown = equity_df["drawdown"]
            fill_color = self.colors["drawdown"]
            fig.add_trace(
                go.Scatter(
                    x=equity_df["date"],
                    y=drawdown,
                    mode="none",
                    fill="tozeroy",
                    fillcolor=fill_color,
                    name="回撤",
                    hovertemplate="%{x|%Y-%m-%d}<br>回撤: %{y:.1%}<extra></extra>",
                ),
                row=2, col=1,
            )

        layout = self._base_layout(title, height=700)
        layout.update(
            yaxis=dict(title="权益", **layout["yaxis"]),
            yaxis2=dict(title="回撤", tickformat=".0%", **layout["xaxis"]),
        )
        fig.update_layout(layout)

        return fig

    # ===== KPI 指标卡片 =====

    def plot_kpi_cards(self, metrics: dict) -> str:
        """生成 KPI 指标卡片的 HTML。"""
        items = [
            ("累计收益", f"{metrics.get('cumulative_return', 0):+.2%}"),
            ("年化收益", f"{metrics.get('annualized_return', 0):+.2%}"),
            ("最大回撤", f"{metrics.get('max_drawdown', 0):+.2%}"),
            ("夏普比率", f"{metrics.get('sharpe_ratio', 0):.2f}"),
            ("胜率", f"{metrics.get('win_rate', 0):.1%}"),
            ("盈亏比", f"{metrics.get('profit_loss_ratio', 0):.2f}"),
            ("交易笔数", f"{metrics.get('total_trades', 0)}"),
            ("卡玛比率", f"{metrics.get('calmar_ratio', 0):.2f}"),
        ]

        cards = ""
        for label, value in items:
            # 数值正负着色 (红涨绿跌)
            val_str = str(value)
            color_class = ""
            if value and isinstance(value, (int, float)):
                if value > 0:
                    color_class = "kpi-positive"
                elif value < 0:
                    color_class = "kpi-negative"

            cards += f"""
            <div class="kpi-card">
                <div class="kpi-label">{label}</div>
                <div class="kpi-value {color_class}">{value}</div>
            </div>"""

        return cards

    # ===== 月度收益热力图 =====

    def plot_monthly_returns(
        self,
        equity_df: pd.DataFrame,
        title: str = "月度收益热力图",
    ) -> go.Figure:
        """Plotly 月度收益热力图。"""
        if "date" not in equity_df.columns or "equity" not in equity_df.columns:
            return go.Figure()

        df = equity_df.copy()
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")

        # 计算每日收益
        returns = df["equity"].pct_change().dropna()

        # 按月汇总
        monthly = returns.resample("ME").apply(lambda x: (1 + x).prod() - 1)

        # 构建 pivot table
        monthly_df = pd.DataFrame({
            "year": monthly.index.year,
            "month": monthly.index.month,
            "return": monthly.values,
        })
        pivot = monthly_df.pivot(
            index="year", columns="month", values="return",
        )

        month_labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        pivot.columns = month_labels[:pivot.shape[1]]

        # 自定义色阶: 绿→白→红 (A股惯例)
        colorscale = [
            [0.0, self.colors["down"]],
            [0.5, self.colors["plot_bg"]],
            [1.0, self.colors["up"]],
        ]

        fig = go.Figure(data=go.Heatmap(
            z=pivot.values,
            x=pivot.columns,
            y=pivot.index,
            colorscale=colorscale,
            zmid=0,
            text=[[f"{v:.1%}" if not np.isnan(v) else "" for v in row] for row in pivot.values],
            texttemplate="%{text}",
            textfont=dict(size=11),
            hovertemplate="%{y} %{x}<br>收益: %{z:.2%}<extra></extra>",
        ))

        fig.update_layout(self._base_layout(title, height=400))
        fig.update_layout(
            xaxis=dict(title="", side="top", **fig.layout.xaxis.to_plotly_json()),
            yaxis=dict(title="", dtick=1, **fig.layout.yaxis.to_plotly_json()),
        )

        return fig

    # ===== 交易盈亏分布 =====

    def plot_trade_distribution(
        self,
        trades: pd.DataFrame,
        title: str = "交易盈亏分布",
    ) -> go.Figure:
        """盈亏分布直方图。"""
        if trades.empty or "profit_pct" not in trades.columns:
            return go.Figure()

        pnl = trades["profit_pct"].dropna() * 100  # 转为百分比

        fig = go.Figure()

        fig.add_trace(go.Histogram(
            x=pnl,
            nbinsx=30,
            marker_color=[self.colors["up"] if v > 0 else self.colors["down"] for v in pnl],
            hovertemplate="收益率: %{x:.1f}%<br>笔数: %{y}<extra></extra>",
        ))

        # 添加均值线
        mean_val = pnl.mean()
        fig.add_vline(
            x=mean_val,
            line_dash="dash",
            line_color=self.colors["text_secondary"],
            annotation_text=f"均值: {mean_val:.1f}%",
        )

        fig.update_layout(self._base_layout(title, height=400))
        fig.update_layout(
            xaxis=dict(title="收益率 (%)", **fig.layout.xaxis.to_plotly_json()),
            yaxis=dict(title="交易笔数", **fig.layout.yaxis.to_plotly_json()),
            showlegend=False,
        )

        return fig

    # ===== 退出原因分布 =====

    def plot_exit_reasons(self, trades: pd.DataFrame) -> go.Figure:
        """退出原因饼图。"""
        if trades.empty or "exit_reason" not in trades.columns:
            return go.Figure()

        counts = trades["exit_reason"].value_counts()

        reason_labels = {
            "cost_stop": "成本止损",
            "trailing_stop": "移动止盈",
            "ladder_tp": "阶梯止盈",
            "time_stop": "时间止损",
            "signal": "信号卖出",
            "end_of_data": "期末平仓",
        }
        labels = [reason_labels.get(r, r) for r in counts.index]

        colors = [
            self.colors["down"],
            "#f0883e",
            self.colors["up"],
            "#d2a8ff",
            self.colors["accent"],
            self.colors["text_secondary"],
        ]

        fig = go.Figure(data=go.Pie(
            labels=labels,
            values=counts.values,
            marker=dict(colors=colors[:len(labels)]),
            textinfo="label+percent",
            hovertemplate="%{label}: %{value}笔 (%{percent})<extra></extra>",
        ))

        fig.update_layout(self._base_layout("退出原因分布", height=400))
        fig.update_layout(showlegend=False)

        return fig

    # ===== 持仓天数分布 =====

    def plot_hold_days(self, trades: pd.DataFrame) -> go.Figure:
        """持仓天数分布直方图。"""
        if trades.empty:
            return go.Figure()

        if "entry_date" not in trades.columns or "exit_date" not in trades.columns:
            return go.Figure()

        hold = (pd.to_datetime(trades["exit_date"]) - pd.to_datetime(trades["entry_date"])).dt.days
        hold = hold.dropna()

        if hold.empty:
            return go.Figure()

        fig = go.Figure(data=go.Histogram(
            x=hold,
            nbinsx=max(10, min(50, len(hold) // 5)),
            marker_color=self.colors["accent"],
            hovertemplate="持仓天数: %{x}天<br>笔数: %{y}<extra></extra>",
        ))

        fig.update_layout(self._base_layout("持仓天数分布", height=350))
        fig.update_layout(
            xaxis=dict(title="持仓天数", **fig.layout.xaxis.to_plotly_json()),
            yaxis=dict(title="交易笔数", **fig.layout.yaxis.to_plotly_json()),
        )

        return fig

    # ===== HTML 报告组装 =====

    def generate_html_report(
        self,
        equity_fig: go.Figure,
        monthly_fig: go.Figure,
        trade_fig: go.Figure,
        exit_fig: go.Figure,
        hold_fig: go.Figure,
        metrics: dict,
        config_summary: str,
        strategy_name: str = "VERA",
        date_range: str = "",
    ) -> str:
        """生成完整 HTML 报告。"""
        kpi_cards = self.plot_kpi_cards(metrics)
        theme_class = "dark" if self.dark else "light"
        theme_btn = "浅色模式" if self.dark else "深色模式"

        html = f"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="{theme_class}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=1280">
<title>{strategy_name} — 回测报告</title>
<style>
:root {{
    --bg: {self.colors["bg"]};
    --card-bg: {self.colors["card_bg"]};
    --text: {self.colors["text"]};
    --text-secondary: {self.colors["text_secondary"]};
    --up: {self.colors["up"]};
    --down: {self.colors["down"]};
    --accent: {self.colors["accent"]};
    --border: {self.colors["grid"]};
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    background: var(--bg);
    color: var(--text);
    font-family: Inter, -apple-system, "Microsoft YaHei", sans-serif;
    line-height: 1.6;
    padding: 0;
}}
.container {{ max-width: 1280px; margin: 0 auto; padding: 24px; }}
/* Header */
.header {{
    display: flex; justify-content: space-between; align-items: flex-start;
    padding: 32px 0; border-bottom: 1px solid var(--border); margin-bottom: 24px;
}}
.header h1 {{ font-size: 28px; font-weight: 700; letter-spacing: -0.5px; }}
.header .meta {{ color: var(--text-secondary); font-size: 14px; margin-top: 4px; }}
.theme-btn {{
    padding: 8px 16px; border: 1px solid var(--border); border-radius: 6px;
    background: var(--card-bg); color: var(--text); cursor: pointer;
    font-size: 13px; transition: background 0.2s;
}}
.theme-btn:hover {{ background: var(--border); }}
/* KPI Grid */
.kpi-grid {{
    display: grid; grid-template-columns: repeat(4, 1fr);
    gap: 12px; margin-bottom: 24px;
}}
.kpi-card {{
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 8px; padding: 16px 20px;
}}
.kpi-label {{ font-size: 12px; color: var(--text-secondary); text-transform: uppercase;
    letter-spacing: 0.5px; margin-bottom: 4px; }}
.kpi-value {{ font-size: 24px; font-weight: 600; font-variant-numeric: tabular-nums; }}
.kpi-positive {{ color: var(--up); }}
.kpi-negative {{ color: var(--down); }}
/* Chart */
.chart-section {{ margin-bottom: 24px; }}
.chart-section h2 {{
    font-size: 18px; font-weight: 600; margin-bottom: 12px;
    padding-left: 12px; border-left: 3px solid var(--accent);
}}
.chart-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
.chart-box {{ background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 8px; padding: 8px; }}
.chart-box.full {{ grid-column: 1 / -1; }}
/* Trade Table */
.trade-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
.trade-table th, .trade-table td {{
    padding: 8px 12px; text-align: right; border-bottom: 1px solid var(--border);
}}
.trade-table th {{ color: var(--text-secondary); font-weight: 500; font-size: 11px;
    text-transform: uppercase; letter-spacing: 0.3px; }}
.trade-table td:first-child, .trade-table th:first-child {{ text-align: left; }}
.trade-up {{ color: var(--up); }}
.trade-down {{ color: var(--down); }}
/* Summary */
.summary-box {{
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 8px; padding: 16px 20px; margin-bottom: 16px;
    font-size: 13px; white-space: pre-wrap; color: var(--text-secondary);
}}
/* Footer */
.footer {{ margin-top: 40px; padding: 16px 0; border-top: 1px solid var(--border);
    text-align: center; font-size: 12px; color: var(--text-secondary); }}
</style>
</head>
<body>
<div class="container">
<div class="header">
    <div>
        <h1>{strategy_name}</h1>
        <div class="meta">{date_range} | VERA Quant System</div>
    </div>
    <button class="theme-btn" onclick="toggleTheme()">{theme_btn}</button>
</div>

<div class="kpi-grid">{kpi_cards}</div>

<div class="summary-box">止损止盈配置:\n{config_summary}</div>

<div class="chart-section">
    <h2>权益曲线与基准对比</h2>
    <div class="chart-box full" id="equity_chart"></div>
</div>

<div class="chart-row">
    <div class="chart-section">
        <h2>月度收益热力图</h2>
        <div class="chart-box" id="monthly_chart"></div>
    </div>
    <div class="chart-section">
        <h2>交易盈亏分布</h2>
        <div class="chart-box" id="trade_chart"></div>
    </div>
</div>

<div class="chart-row">
    <div class="chart-section">
        <h2>退出原因分布</h2>
        <div class="chart-box" id="exit_chart"></div>
    </div>
    <div class="chart-section">
        <h2>持仓天数分布</h2>
        <div class="chart-box" id="hold_chart"></div>
    </div>
</div>
</div>

<script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
<script>
var theme = "{theme_class}";
function toggleTheme() {{
    var newTheme = theme === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", newTheme);
    location.reload();
}}
var config = {{ responsive: true, displayModeBar: true, modeBarButtonsToRemove: ["lasso2d", "select2d"] }};
Plotly.newPlot("equity_chart", {equity_fig.to_json()}.data, {equity_fig.to_json()}.layout, config);
Plotly.newPlot("monthly_chart", {monthly_fig.to_json()}.data, {monthly_fig.to_json()}.layout, config);
Plotly.newPlot("trade_chart", {trade_fig.to_json()}.data, {trade_fig.to_json()}.layout, config);
Plotly.newPlot("exit_chart", {exit_fig.to_json()}.data, {exit_fig.to_json()}.layout, config);
Plotly.newPlot("hold_chart", {hold_fig.to_json()}.data, {hold_fig.to_json()}.layout, config);
</script>
</body>
</html>"""
        return html

    # ===== Matplotlib 静态导出 =====

    def save_equity_png(self, equity_df, path: str, dpi: int = 300) -> str:
        """Matplotlib 导出权益曲线 PNG。"""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        colors = self.colors
        plt.style.use("dark_background" if self.dark else "default")

        fig, (ax1, ax2) = plt.subplots(
            2, 1, figsize=(16, 9), gridspec_kw={"height_ratios": [3, 1]},
            sharex=True,
        )

        if self.dark:
            fig.patch.set_facecolor(colors["bg"])
            ax1.set_facecolor(colors["plot_bg"])
            ax2.set_facecolor(colors["plot_bg"])

        ax1.plot(
            equity_df["date"], equity_df["equity"],
            color=colors["equity"], linewidth=1.5,
        )
        ax1.set_title("策略权益曲线", color=colors["text"], fontsize=14)

        if "drawdown" in equity_df.columns:
            ax2.fill_between(
                equity_df["date"], 0, equity_df["drawdown"],
                color=colors["down"], alpha=0.4,
            )

        ax1.tick_params(colors=colors["text_secondary"])
        ax2.tick_params(colors=colors["text_secondary"])

        for spine in ax1.spines.values():
            spine.set_color(colors["grid"])
        for spine in ax2.spines.values():
            spine.set_color(colors["grid"])

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        plt.tight_layout()
        plt.savefig(path, dpi=dpi, bbox_inches="tight", facecolor=fig.get_facecolor())
        plt.close()
        logger.info(f"权益曲线已保存: {path}")
        return path
