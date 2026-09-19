"""VeraCore 回测引擎 — 纯Python，内置OHLC止盈止损判断。

核心循环: `backtest/loop/BacktestLoop` (候选 A 阶段 2, 2026-07-14)。
run()/run_cached() 经共享段 `_resolve_stop_and_build_loop` 调用
`build_backtest_loop` + `loop.run()` (2026-08-01 批次 3b C2: 双入口重复段合并,
原 `_simulate_core_v3` 测试兼容壳同日退役, 测试改直调 build_backtest_loop,
等价展开见 tests/loop_direct.py)。
设计说明: `docs/architecture/loop.md`。
"""

from typing import Optional

import numpy as np
import pandas as pd

from backtest._constants import (
    BARS_PER_DAY,
    PERIODS_PER_YEAR,
    STD_5M_BAR_TIMES,
    detect_limit_up,
)
from backtest.degrade_5m import (
    apply_5m_degradation,
    recompute_last_tradable_idx,
    scan_degraded_positions,
    synthesize_5m_grid,
)
from backtest.loop import build_backtest_loop
from backtest.metrics import MetricsCalculator
from backtest.result import BacktestResult
from backtest.stop_config import (
    DEFAULT_PRIORITY,
    DEFAULT_TRAILING_ACTIVATION,
    DEFAULT_TRAILING_DRAWDOWN,
    VALID_PRIORITIES,
)
from core.data_fetcher import DataFetcher
from core.stock_filter import get_cached_info
from utils.logger import get_logger

logger = get_logger(__name__)

ENGINE_VERSION = "v3.7-entry-t1-open-20260820"

# 2026-09-16 P2: 硬止损阈值缺省值单一真相源 (原 -0.12 在 build 调用与
# degrade 报告两处各自硬编码)。与 config/default.yaml cost_stop.threshold 对齐。
_DEFAULT_COST_STOP_THRESHOLD = -0.12

# ═══════════════════════════════════════════════════════════════
# VeraCore 设计要点 — 核心循环实现已迁至 backtest/loop/ (候选 A 阶段 2, 2026-07-14)
# 默认优先级 (priority=stop_first, 历史): 成本止损 > 阶梯止盈 > 移动止损/止盈 > 时间止损
# priority=ladder_tp_first 模式: 阶梯止盈 > 成本止损 > 移动 > 时间
#   优先级配置见 config/default.yaml['stop_loss']['priority']; 策略实现见 backtest/loop/strategies/
#
# 浮点阈值比较设计决策: 本项目所有止损/止盈阈值比较（如 lo_pp <= cost_stop_threshold）
# 有意不使用 epsilon 容差。原因: 用户配置 threshold=-0.12 时，算出来 -0.119999 就该触发
# （浮点误差方向不定, 加了 epsilon 反而可能漏触发）。唯一例外: tests/ 合成数据中允许
# 用 pytest.approx 做精确断言。代码内 profit_pct > 0 的平盘判定场景也无 epsilon — 浮点
# 尾巴 0.00000001 不该算盈利, 与 <= 阈值触发是同一边界逻辑。审计 F-H6 (2026-07-15) 确认
# 这是有意的设计选择, 不是遗漏。
#   formula_sell (reason=12) 始终最高优先级, 不受 priority 开关影响
# 执行价格 (权威实现见 backtest/loop/strategies/, 此处仅注记):
#   成本止损 → stop_price (ep*(1+threshold))
#   阶梯止盈 → ladder_price (ep*(1+profit))
#   移动止损/止盈 → 取决于 confirm 模式 (trailing.py): intraday=Low触线按线价;
#     low/close=日频确认按收盘; simple=5M碰线按bar收盘 / 1D收盘判定按收盘 (无阶梯休息)
#   其他     → Close
# ═══════════════════════════════════════════════════════════════




def _build_tradable_from_raw(close_raw, close):
    """从原始未 ffill 价自建 tradable_np + last_tradable_idx (退市检测用).

    候选 A 阶段 1 (审计 H1/H4/M1 修复): 抽成 helper 让 run_cached / run 共用, 消除 drift。
    - close_raw: DataFrame 或 2D array, 原始价(含停牌 NaN)。array 时用 close 的 index/columns 对齐。
    - close: 对齐基准 (已 ffill 的 DataFrame)。
    返回 (tradable_np, last_tradable_idx); close_raw=None 时返回 (None, None)。
    全-False 时 warning (标签/形状不匹配的常见症状)。
    """
    if close_raw is None:
        return None, None
    if isinstance(close_raw, pd.DataFrame):
        raw_df = close_raw
    else:
        # numpy array / list: 用 close 的 index/columns 赋标签, 防 pd.DataFrame 默认整数列名 → reindex 全 NaN
        raw_df = pd.DataFrame(close_raw, index=close.index, columns=close.columns)
    raw_aligned = raw_df.reindex(index=close.index, columns=close.columns)
    tradable_np = raw_aligned.notna().values.astype(np.bool_)
    last_tradable_idx = recompute_last_tradable_idx(tradable_np)
    if tradable_np.sum() == 0:
        logger.warning(
            "_build_tradable_from_raw: tradable_np 全 False — close_raw 标签/形状可能与 close 不匹配, "
            "退市检测将无效果 (检查 close_raw 的 index/columns 是否与 close 对齐)"
        )
    return tradable_np, last_tradable_idx


def _compute_atr_matrix(high_np, low_np, close_np, period: int = 14):
    """计算 ATR 矩阵 (n_dates, n_stocks) float64。

    TR = max(H-L, |H-prevClose|, |L-prevClose|); ATR = TR 的 period 周期简单均值
    (min_periods=1, 首根 bar 用 H-L)。首 period-1 根为部分均值。
    high_np/low_np/close_np 形状一致 (n,k)。返回 float64 矩阵, 缺值 NaN。

    2026-07-18: 逐列 pandas 循环 → 全矩阵向量化 (278ms→18ms, 15x)。
    np.fmax 忽略 NaN 与 pandas .max(skipna) 语义一致; rolling 仍是同一 pandas
    引擎(2D 一次算), A/B 实测数值含 NaN 位置完全一致。
    """
    prev_c = np.empty_like(close_np)
    prev_c[0] = np.nan
    prev_c[1:] = close_np[:-1]
    tr = np.fmax(np.fmax(np.abs(high_np - low_np), np.abs(high_np - prev_c)),
                 np.abs(low_np - prev_c))
    return pd.DataFrame(tr).rolling(period, min_periods=1).mean().values


# ═══════════════════════════════════════════════════════════════
# BacktestEngine — Python 包装层
# ═══════════════════════════════════════════════════════════════

class BacktestEngine:

    # P2-6 (2026-07-15): 数据字典移至 backtest/_constants.py, 类属性保留为向后兼容入口
    # (benchmark.py 现在直接 from ._constants import, 无需拖入本引擎模块)
    BARS_PER_DAY = BARS_PER_DAY
    PERIODS_PER_YEAR = PERIODS_PER_YEAR

    def __init__(self, config: Optional[dict] = None):
        config = config or {}
        self.initial_capital = float(config.get("initial_capital", 1000000.0))
        self.commission = float(config.get("commission", 0.0003))
        self.slippage = float(config.get("slippage", 0.001))
        self.stamp_tax = float(config.get("stamp_tax", 0.0005))  # A股卖出单边
        # P0-5: 默认 True — 含印花税+滑点的真实成本
        # 2026-07-18 审计修正注释: realistic_costs=False 只免滑点+印花税, 佣金照收
        # (eff_commission 两分支均 = self.commission), 并非"零成本基线"
        self.realistic_costs = bool(config.get("enable_realistic_costs", True))
        self.period = config.get("period", "1d")
        self.bars_per_day = self.BARS_PER_DAY.get(self.period, 1)
        ps = config.get("position_sizing", {})
        self.min_buy_amount = float(ps.get("min_buy_amount", 2000.0))
        self.max_buy_amount = float(ps.get("max_buy_amount", 20000.0))
        self.lot_size = int(ps.get("lot_size", 100))
        self.min_lots = int(ps.get("min_lots", 1))
        # 2026-07-09: 单票仓位占比上限 (<1.0 启用, 基于上一bar总权益; 默认1.0=不约束, 老脚本零变化)
        self.max_position_pct = float(ps.get("max_position_pct", 1.0))
        # 2026-08-28: 流动性约束 — 单笔买入 ≤ 当日成交额 × max_turnover_pct
        # (<1.0 启用, 如 0.01 = 单笔 ≤ 当日成交额 1%; 默认 1.0=不约束, 零行为变化)。
        # 启用时矩阵缓存自动绕过 (prep 需含 turnover_day 字段, 旧缓存无此字段会
        # 静默失效约束 → 必须重新取数, 见 run() 的 use_mc 条件)。
        self.max_turnover_pct = float(ps.get("max_turnover_pct", 1.0))
        # 2026-07-17: 本地 K 线 parquet 缓存开关 (Phase 1, 默认 True 启用; 配置 use_kline_cache:false 回退 TDX 直拉)
        self.use_kline_cache = bool(config.get("use_kline_cache", True))
        # 2026-07-18: 5m 数据层降级 (计划书 2026-07-18, 默认关 G4)。缺 5m 的股-天
        # 用 1d OHLC 填充保住信号; 仅 period="5m" + run() 路径生效
        # (2026-07-26 守卫改 ==48: 1m 强制不降级, 见 _prepare_run_matrices)。
        self.degrade_5m = bool(config.get("degrade_5m", False))
        # 2026-07-18: 矩阵级缓存 (backtest/matrix_cache.py, 默认关 — 测试隔离;
        # pipeline server 路径 setdefault 开)。止盈止损参数不影响准备段产物,
        # 命中时改参数重跑只剩核心循环。degrade_5m=on 时自动跳过。
        self.matrix_cache = bool(config.get("matrix_cache", False))
        self.matrix_cache_dir = config.get("matrix_cache_dir")  # None → data/matrix_cache
        # 2026-07-23: 卖出冷却 (交易日), 全清仓后 N 个交易日内禁止同票重新买入。
        # 默认 0=关闭 (零行为变化); run 时 × bpday 转 bar 数传给 loop。
        self.sell_cooldown_days = int(config.get("sell_cooldown_days", 0))
        # 2026-08-08: 总仓位上限 (持仓市值/总权益 >= 此值停开新仓; 1.0=不约束, 默认零变化)
        self.max_total_exposure = float(config.get("max_total_exposure", 1.0))
        # 2026-08-08: 全局连亏冷却 — 连亏 n 笔停开新仓 days 交易日 (n<=0=关闭)
        _ls = config.get("loss_streak_halt", {}) or {}
        self.loss_streak_halt_n = int(_ls.get("n", 0))
        self.loss_streak_halt_days = int(_ls.get("days", 0))
        # 2026-08-20: 买入价口径 — close_t=信号日收盘价(默认, 零行为变化);
        # open_t1=次日开盘价买入 (T+1 一字涨停拒买, T 日涨停过滤关闭)。
        # 计划书: docs/plan/2026-08-20_回测次日开盘买入模式_计划书.md
        self.entry_price_mode = str(config.get("entry_price_mode", "close_t"))
        if self.entry_price_mode not in ("close_t", "open_t1"):
            raise ValueError(
                f"entry_price_mode 非法: {self.entry_price_mode!r} "
                f"(合法: close_t/open_t1)")
        # 2026-09-19 架构修订批次 3.1: 涨停过滤显式开关 (默认开, 零行为变化)。
        # 此前要关只能 monkeypatch _filter_limit_up (tools/attr_gp1014.py 的
        # no_limit_filter 变体就是这么干的) —— 显式配置取代运行时打补丁。
        # open_t1 口径下本开关不适用 (T 日涨停过滤被 T+1 一字板判定替代)。
        self.filter_limit_up = bool(config.get("filter_limit_up", True))
        # 2026-08-20 审计 HIGH: open_t1 仅支持日频语义 period — "次日开盘价"在
        # 1w (周线) 下会被静默解释成"下周开盘价" (BARS_PER_DAY[1w]=1, T+1=下一根周 bar),
        # 语义偷换且无任何告警。构造期 fail-fast, 不许静默跑错口径。
        if (self.entry_price_mode == "open_t1"
                and self.period not in ("1d", "5m", "1m")):
            raise ValueError(
                f"entry_price_mode=open_t1 暂不支持 period={self.period!r} "
                f"(次日开盘价仅日频语义; 支持: 1d/5m/1m)")

        # C1 修复: 实际生效的费率 (兼容层)
        # 关闭时用 0 覆盖, 确保绝对不破坏老脚本行为
        if not self.realistic_costs:
            self.eff_commission = self.commission
            self.eff_slippage = 0.0
            self.eff_stamp_tax = 0.0
        else:
            self.eff_commission = self.commission
            self.eff_slippage = self.slippage
            self.eff_stamp_tax = self.stamp_tax

    def _resolve_window_td(self, stop) -> Optional[int]:
        """分钟级稀疏窗口长度 (交易日); 1d/1w 路径返回 None。

        窗口尾部安全校验: 窗口必须 > max_hold_days, 否则时间止损来不及触发,
        持仓被窗口边界当退市强平(reason=11)。见 loop.py:107。
        """
        if self.bars_per_day <= 1:
            return None
        win_td = int(getattr(self, "_window_trading_days", 45))
        _mhd = int(stop.get("time_stop", {}).get("max_hold_days", 20))
        if win_td <= _mhd + 5:
            win_td = _mhd + 15
            logger.warning(
                "分钟级稀疏窗口: max_hold_days=%d 接近窗口, 自动加长窗口到 %d 交易日防退市误杀",
                _mhd, win_td,
            )
        if not stop.get("time_stop", {}).get("enabled", True):
            logger.warning(
                "分钟级稀疏窗口模式建议开启时间止损(time_stop.enabled); "
                "否则窗口尾部未平持仓会被当退市强平(reason=11)"
            )
        return win_td

    def prepare_matrices(self, selections, start_time, end_time, win_td):
        """准备段公开接缝 (2026-09-19 架构修订批次 3.1)。

        run() 的准备段对外出口: 取数 → 非标准 bar 过滤 → (可选) 降级 →
        entries → 列对齐 → ffill → tradable。tools/ 的 4 个 sweep 脚本曾把
        这段配方各复刻一份 (engine 一改即静默漂移, 2026-09-19 架构审查 P1-7),
        一律改调本方法。

        返回 dict (run()/matrix_cache 的内部契约: close/entries/high/low/open/
        tradable/last_tradable_idx/idx/cols/degraded_np/degrade_res/turnover_day);
        取数为空返回 None。**注意**: 不含涨停过滤 —— 那是买入口径的事
        (_apply_entry_price_mode, 与 entry_price_mode 绑定), 需要预过滤的
        调用方在拿到 entries 后自行调 _filter_limit_up (sweep 脚本即如此)。
        """
        return self._prepare_run_matrices(selections, start_time, end_time, win_td)

    def _prepare_run_matrices(self, selections, start_time, end_time, win_td):
        """run() 的准备段 (2026-07-18 抽出, 供矩阵级缓存复用)。

        取数 → 非标准bar过滤 → degrade → entries → 列对齐 → ffill → tradable。
        产物只依赖 选股结果/区间/period/窗口/复权/数据, 与止盈止损参数无关。
        取数为空返回 None (调用方转 _empty_result)。
        2026-09-19: 公开出口 = prepare_matrices (本方法保持私有实现)。
        """
        window_mask = None
        if self.bars_per_day > 1:
            # 2026-07-21: end_time 透传 — 窗口终点截断到请求区间终点,
            # 执行窗口=请求区间 (不再延长 +win_td 尾巴); 期末持仓按市值计价。
            kline, window_mask = DataFetcher.get_kline_windowed(
                selections, period=self.period,
                window_trading_days=win_td, dividend_type="front", fill_data=False,
                use_cache=self.use_kline_cache, end_time=end_time,
            )
        else:
            codes = selections["stock_code"].unique().tolist()
            kline = DataFetcher.get_kline(codes, start_time, end_time, dividend_type="front", period=self.period, fill_data=False, use_cache=self.use_kline_cache)
        if not kline or "Close" not in kline:
            return None

        close = self._ensure_index(kline["Close"])
        high_df_raw = kline.get("High")
        low_df_raw = kline.get("Low")
        open_df_raw = kline.get("Open")  # P1-1/P1-2: Open 列
        high_df = self._ensure_index(high_df_raw) if high_df_raw is not None else None
        low_df = self._ensure_index(low_df_raw) if low_df_raw is not None else None
        open_df = self._ensure_index(open_df_raw) if open_df_raw is not None else None

        # 2026-07-18: 分钟级非标准时刻 bar 过滤 (48/240 根/天不变量, 001399/300227
        # 实盘事件)。盘中临停股 13:00 复牌竞价 bar 会给并集网格注入 +1 行,
        # loop 的 T+1 i//bpday 日界随之错位, 次日早盘卖出被锁到 14:55 (止损延迟 +5%)。
        if self.bars_per_day in (48, 240):
            from backtest._constants import STD_BAR_TIMES
            close, high_df, low_df, open_df = self._drop_nonstandard_intraday_bars(
                close, high_df, low_df, open_df, STD_BAR_TIMES[self.period])

        # 2026-07-18: 5m 数据层降级 (opt-in, 计划书 2026-07-18)。必须在
        # _build_entry_signals 之前 (信号日插行才有行可放信号) 且在 high/low
        # ffill 之前 (否则前一日数据先 ffill 进缺口, 审计 MEDIUM-2)。
        # 2026-07-26: 守卫改为 ==48 (审计 HIGH-1) — 原 >1 会被 1m (bpday=240) 踩中:
        # 48 槽位 reindex 每天丢 192 根 + reshape %48, loop 按 240 解释, 静默全错。
        degraded_df = None
        degrade_res = None
        if self.degrade_5m and self.bars_per_day == 48:
            close, high_df, low_df, open_df, degraded_df, degrade_res = \
                self._apply_5m_degradation(
                    close, high_df, low_df, open_df, selections, win_td,
                    start_time, end_time)
        elif self.degrade_5m and self.bars_per_day == 240:
            logger.warning(
                "degrade_5m 对 1m 不适用 (数据深度仅 2026-01-26 起, 降级无意义), "
                "本次回测不做数据层降级")

        entries = self._build_entry_signals(selections, close)
        # 统一列对齐：close ∩ entries ∩ high ∩ low（open 不参与交集，缺失则回退 close）
        cols = sorted(close.columns.intersection(entries.columns))
        if high_df is not None:
            cols = sorted(set(cols) & set(high_df.columns))
        if low_df is not None:
            cols = sorted(set(cols) & set(low_df.columns))

        # P1-3: 保留原始价（含停牌 NaN）用于退市检测；close 用 ffill 做 mark-to-market
        close_raw = close.reindex(index=close.index, columns=cols)
        close = close_raw.ffill()
        entries = entries.reindex(index=close.index, columns=cols, fill_value=False)
        idx = close.index
        high_np = high_df.reindex(index=idx, columns=cols).ffill().values.astype(np.float64) if high_df is not None else None
        low_np = low_df.reindex(index=idx, columns=cols).ffill().values.astype(np.float64) if low_df is not None else None
        # P1-1/P1-2: Open 不做 ffill — 停牌日 open=NaN 应保留，让 T+1 买入自然跳过
        open_np = open_df.reindex(index=idx, columns=cols).values.astype(np.float64) if open_df is not None else None

        # degrade_5m: degraded_np 对齐最终 (idx, cols)
        degraded_np = None
        if degraded_df is not None:
            degraded_np = degraded_df.reindex(
                index=idx, columns=cols, fill_value=False).values.astype(bool)

        # 2026-08-28: 流动性约束 — 当日成交额矩阵 (元, 天×股)。
        # 供 entry 约束 单笔 ≤ 当日成交额×max_turnover_pct。成交额 = Σ(volume×close)
        # 按日聚合 (不用 kline 的 Amount 字段 — 5m/1d Amount 单位是「万元」,
        # 直接当元用会缩小 10000 倍, 2026-08-28 踩坑实录)。停牌日 volume=NaN/0
        # → 乘积 0 → 当日成交额 0 → entry 拒买 (保守, 不会虚假放大买入)。
        turnover_day_np = None
        if self.max_turnover_pct < 1.0:
            vol_df = kline.get("Volume")
            if vol_df is not None:
                vols = vol_df.reindex(index=idx, columns=cols)
                amt = vols * close  # close 已 ffill; 停牌日 volume NaN → NaN
                day_sum = amt.groupby(amt.index.date).sum()
                turnover_day_np = day_sum.values.astype(np.float64)
                _nz = np.count_nonzero(~np.isnan(turnover_day_np) & (turnover_day_np > 0))
                logger.info(
                    "流动性约束: 成交额矩阵 %s 天×%s 股, 非零格 %d (%.2f%%)",
                    turnover_day_np.shape[0], turnover_day_np.shape[1],
                    _nz, 100.0 * _nz / max(1, turnover_day_np.size))
            else:
                logger.warning(
                    "max_turnover_pct<1.0 但 K 线数据无 Volume 字段, 流动性约束跳过")

        # 候选 A 审计 M1 修复: 改用公用 _build_tradable_from_raw helper
        # 消除 run 与 run_cached 的 drift (close_raw 是 line 671 重索引后的 DataFrame, helper 对 DataFrame 等价)
        tradable_np, last_tradable_idx = _build_tradable_from_raw(close_raw, close)

        # 2026-07-16: 稀疏窗口模式 — 窗口外强制不可交易, 避免窗口边界 NaN 被
        #   _build_tradable_from_raw 判为退市。用户决策"窗口内才可交易"。
        if window_mask is not None and not window_mask.empty:
            wm = window_mask.reindex(index=idx, columns=cols, fill_value=False).values.astype(bool)
            tradable_np = tradable_np & wm
            # last_tradable_idx 重算为窗口内最后一个可交易 bar (每列)
            last_tradable_idx = recompute_last_tradable_idx(tradable_np)

        # degrade_5m (审计 CRITICAL-2): 降级 bar 恢复可交易 + 重算 last_tradable_idx。
        # 必须在 window_mask 合并之后 — 缺口日在 mask 里没行会被 &= 杀掉,
        # 放前面 OR 了也白搭 (计划书 §4.3 集成点笔误修正)。
        if degraded_np is not None:
            tradable_np = tradable_np | degraded_np
            last_tradable_idx = recompute_last_tradable_idx(tradable_np)

        return {
            "close": close, "entries": entries,
            "high": high_np, "low": low_np, "open": open_np,
            "tradable": tradable_np, "last_tradable_idx": last_tradable_idx,
            "idx": idx, "cols": cols,
            "degraded_np": degraded_np, "degrade_res": degrade_res,
            "turnover_day": turnover_day_np,
        }

    def _resolve_stop_and_build_loop(self, stop, close, entry_np,
                                     high_np, low_np, open_np,
                                     tradable_np, last_tradable_idx,
                                     ladder_profits, ladder_ratios, n_ladder,
                                     formula_exit_np, formula_exit_ratio,
                                     formula_exit_lag_bars=1,
                                     degraded_np=None,
                                     buy_price_np=None,
                                     turnover_day_np=None):
        """run()/run_cached() 共享段 (2026-08-01 批次 3b C2 合并)。

        priority 校验 → trailing 缺省 → 时间参数 ×bpday 缩放 → ATR →
        build_backtest_loop → loop.run。两入口曾各自维护这段 ~60 行且发生过
        漂移 (计划书批次 3 C2), 抽出单一实现; 行为与合并前逐字节一致,
        由快照基线 tests/test_snapshot_parity.py 锁定。

        注意: 必须经模块级 `build_backtest_loop` 名字调用 (tests 会
        monkeypatch engine_module.build_backtest_loop 捕获参数)。

        返回 (equity_arr, raw_trades, resolved); resolved 携带调用方后续需要的
        解析值 (目前仅 run() 的 degrade 报告用 trailing 缺省后值)。
        """
        # 2026-09-16 B1: 流动性约束静默失效 → 有声。约束已配置但无换手数据时
        # (run_cached 调用方未提供 turnover_day_np / run() 取数缺 Volume),
        # loop 层实际不执行约束, 必须显式告警而非静默跑完全程。
        if float(self.max_turnover_pct) < 1.0 and turnover_day_np is None:
            logger.warning(
                "流动性约束已配置 (max_turnover_pct=%s) 但无换手数据 "
                "(turnover_day_np=None), 约束不生效", self.max_turnover_pct)
        # 2026-08-16 药2 (回测提速): 价格矩阵降 float32 — 价格只需 ~7 位有效数字,
        # float64 是浪费 (内存减半 + CPU 缓存友好); 资金/权益账 (cash/equity/
        # trade buffer) 仍 float64 保精度。两入口 (run/run_cached) 都经此收口,
        # 统一转 float32 防 drift。见 docs/plan/2026-08-16_float32价格矩阵_回测提速_方案书.md
        close_np = np.asarray(close.values, dtype=np.float32)
        if high_np is not None:
            high_np = np.asarray(high_np, dtype=np.float32)
        if low_np is not None:
            low_np = np.asarray(low_np, dtype=np.float32)
        if open_np is not None:
            open_np = np.asarray(open_np, dtype=np.float32)
        # 2026-08-20: open_t1 买入价矩阵 (与价格矩阵同一 float32 口径)
        if buy_price_np is not None:
            buy_price_np = np.asarray(buy_price_np, dtype=np.float32)
        cost = stop.get("cost_stop", {})
        trail = stop.get("trailing_stop", {})
        # 移动止损止盈缺字段/None 语义: 回退命名常量 (两入口同一兜底, 防漂移)
        trailing_activation = trail.get("activation")
        if trailing_activation is None:
            trailing_activation = DEFAULT_TRAILING_ACTIVATION
        trailing_drawdown = trail.get("drawdown")
        if trailing_drawdown is None:
            trailing_drawdown = DEFAULT_TRAILING_DRAWDOWN
        ladder = stop.get("ladder_tp", {})
        time_s = stop.get("time_stop", {})
        cond_t = stop.get("cond_time_stop", {})
        first_day = stop.get("first_day", {})
        # 优先级 (ladder_tp_first / trailing_first), 非法值回退默认
        priority = str(stop.get("priority", DEFAULT_PRIORITY))
        if priority not in VALID_PRIORITIES:
            logger.warning(
                "stop_config.priority=%r 非法, 回退 %s (合法: %s)",
                priority, DEFAULT_PRIORITY, sorted(VALID_PRIORITIES),
            )
            priority = DEFAULT_PRIORITY
        ladder_tp_first = (priority == "ladder_tp_first")
        trailing_first = (priority == "trailing_first")

        bpday = self.bars_per_day
        mhd_scaled = int(time_s.get("max_hold_days", 20)) * bpday
        ctd_scaled = int(cond_t.get("days", 7)) * bpday

        # ATR 波动率止损: stop_config["atr_stop"], 内部从 high/low/close 预算
        atr_cfg = stop.get("atr_stop", {})
        atr_enabled = bool(atr_cfg.get("enabled", False))
        atr_matrix = None
        atr_multiplier = float(atr_cfg.get("multiplier", 3.0))
        if atr_enabled:
            if high_np is not None and low_np is not None:
                atr_matrix = _compute_atr_matrix(
                    high_np, low_np, close_np,
                    period=int(atr_cfg.get("period", 14)))
            else:
                logger.warning("atr_stop.enabled=true 但 high_np/low_np 缺失, ATR 强制禁用")
                atr_enabled = False

        loop = build_backtest_loop(
            float(self.initial_capital), float(self.eff_commission),
            float(self.min_buy_amount), float(self.max_buy_amount),
            int(self.lot_size), int(self.min_lots),
            cost.get("enabled", True), float(cost.get("threshold", _DEFAULT_COST_STOP_THRESHOLD)),
            trail.get("enabled", True), float(trailing_activation),
            float(trailing_drawdown),
            ladder.get("enabled", True), ladder_profits, ladder_ratios, n_ladder,
            time_s.get("enabled", True), mhd_scaled,
            cond_t.get("enabled", False), ctd_scaled, float(cond_t.get("profit", 0.01)),
            first_day_enabled=first_day.get("enabled", False),
            first_day_target=float(first_day.get("target", 0.03)),
            bpday=bpday, slippage=float(self.eff_slippage), stamp_tax=float(self.eff_stamp_tax),
            max_position_pct=float(self.max_position_pct),
            ladder_tp_first=ladder_tp_first, trailing_first=trailing_first,
            formula_exit_np=formula_exit_np, formula_exit_ratio=formula_exit_ratio,
            formula_exit_lag_bars=formula_exit_lag_bars,
            atr_enabled=atr_enabled, atr_matrix=atr_matrix, atr_multiplier=atr_multiplier,
            trailing_gap_protection=bool(trail.get("gap_protection", False)),
            trailing_confirm=str(trail.get("confirm", "intraday")),
            sell_cooldown_bars=self.sell_cooldown_days * bpday,
            max_total_exposure=float(self.max_total_exposure),
            loss_streak_halt_n=self.loss_streak_halt_n,
            loss_streak_halt_bars=self.loss_streak_halt_days * bpday,
            buy_price_np=buy_price_np,
            max_turnover_pct=float(self.max_turnover_pct),
        )
        self._last_loop = loop
        equity_arr, raw_trades = loop.run(
            close_np, entry_np,
            high_np=high_np, low_np=low_np, open_np=open_np,
            tradable_np=tradable_np, last_tradable_idx=last_tradable_idx,
            formula_exit_np=formula_exit_np,
            degraded_np=degraded_np,
            turnover_day_np=turnover_day_np,
        )
        resolved = {
            "trailing_activation": float(trailing_activation),
            "trailing_drawdown": float(trailing_drawdown),
            # run() 的 ENGINE_DEBUG 日志从 resolved 读缩放值, 与本处同一计算源
            "mhd_scaled": mhd_scaled,
        }
        return equity_arr, raw_trades, resolved

    def _apply_entry_price_mode(self, entries, close, high_np, low_np, open_np,
                                tradable_np):
        """买入价口径应用 (run()/run_cached() 共用, 2026-08-20, 防双入口漂移)。

        close_t (默认): T 日收盘涨停过滤 (_filter_limit_up), 返回 (entries, None, None)。
        open_t1: 信号平移到 T+1 首个可交易 bar (一字涨停拒买, 详见
        backtest/entry_next_open.py), 返回 (平移后 entries, 买入价矩阵=open, 统计)。
        open_t1 需要 OHLC 齐全, 缺失 fail-fast (不许静默退化成错误口径)。
        """
        if self.entry_price_mode != "open_t1":
            # 2026-09-19: filter_limit_up=False 时恒等放行 (显式配置,
            # 取代 tools/attr_gp1014.py 的 monkeypatch 变体)
            if not self.filter_limit_up:
                return entries, None, None
            return self._filter_limit_up(entries, close), None, None
        if open_np is None or high_np is None or low_np is None:
            raise ValueError(
                "entry_price_mode=open_t1 需要 OHLC 数据 (open/high/low 缺失)")
        from backtest.entry_next_open import shift_entries_to_next_open
        idx, cols = close.index, close.columns
        t1 = shift_entries_to_next_open(
            entries,
            pd.DataFrame(open_np, index=idx, columns=cols),
            pd.DataFrame(high_np, index=idx, columns=cols),
            pd.DataFrame(low_np, index=idx, columns=cols),
            close,
            limit_ratio_vec=self._limit_ratio_vector(entries.columns),
            tradable_np=tradable_np)
        info = {"mode": "open_t1", "n_signals": t1.n_signals,
                "n_shifted": t1.n_shifted,
                "n_oneline_limit_up": t1.n_oneline_limit_up,
                "n_no_t1_bar": t1.n_no_t1_bar,
                "n_no_tradable_bar": t1.n_no_tradable_bar}
        logger.info(
            "open_t1: 信号 %d → 平移 %d, 一字涨停拒买 %d, 无T+1丢弃 %d, 全天停牌丢弃 %d",
            t1.n_signals, t1.n_shifted, t1.n_oneline_limit_up,
            t1.n_no_t1_bar, t1.n_no_tradable_bar)
        return t1.entries, np.asarray(open_np), info

    def _validate_caliber(self, caliber):
        """选股口径校验 (2026-09-19 架构修订批次 3.2 —— 自 pipeline 下沉)。

        原本只在 Pipeline.step2_backtest 里校验, **直调 engine.run() 全部绕过**
        (本类 run() 的旧 docstring 自己承认)。下沉后: 传了 caliber 就校验
        (复权不一致直接抛 ValueError; period 不一致告警), 没传则打 WARNING
        明示"本次未校验" —— 不留静默旁路 (静默旁路才是真问题)。

        caliber: {"dividend_type": int|str, "period": str}; None = 未声明。
        """
        if not caliber:
            logger.warning(
                "caliber_unverified: 本次 run() 未声明选股口径 (selection_caliber), "
                "跳过复权一致性校验 —— Pipeline 路径会自动带该声明; 直调本方法"
                "请显式传 {\"dividend_type\":…, \"period\":…}, 否则选股与回测"
                "复权口径不一致时不会被发现 (engine 硬编码 front)")
            return
        from core.dividend_type import assert_consistent
        assert_consistent(caliber.get("dividend_type", 1), "front")
        sel_period = caliber.get("period", "1d")
        if sel_period != self.period:
            # P1-8 (2026-07-17, 002008 bug): 1d 选股 + 5m 回测是合法组合, 仅告警
            logger.warning(
                "period_mismatch: 选股 period=%s 与 回测 period=%s 不一致, "
                "若回测 period 数据有缺口, 选股信号会被丢弃 (不顺延)。"
                "1d 选股 + 5m 回测为合法组合, 数据完整时可忽略; "
                "若非有意, 请统一 period 或补全回测 period 的盘后数据。",
                sel_period, self.period,
            )

    def run(self, selections, start_time="", end_time="", stop_config=None,
            selection_caliber=None):
        """执行回测。dividend_type 硬编码 "front"（前复权）。

        selection_caliber: 选股口径声明 {"dividend_type":…, "period":…}。
        Pipeline 路径自动传 (校验在此处统一执行, 2026-09-19 批次 3.2 自 pipeline
        下沉); 直调不传则打 WARNING 明示"未校验", 不再静默绕过。
        """
        if selections is None or selections.empty:
            # 口径校验先于空信号早退 —— 空 selections 也代表一次"用某口径跑的请求",
            # 复权不一致该抛还是要抛 (与 pipeline 下沉前行为一致)
            self._validate_caliber(selection_caliber)
            return self._empty_result()
        self._validate_caliber(selection_caliber)

        # 2026-07-26: 1m 数据深度硬限制 (探针实测 TDX 1m 仅 2026-01-26 起,
        # 更早区间无数据 → 截断不静默); win_td 过大时告警 (取数跨度守卫在
        # kline_cache._fetch_and_store 按 ≤80 交易日分段兜底)。
        if self.period == "1m":
            if start_time and start_time < "20260126":
                logger.warning(
                    "1m 数据深度仅 2026-01-26 起, 回测起点 %s 自动截断为 20260126",
                    start_time)
                start_time = "20260126"

        stop = stop_config or {}
        # 2026-08-01 批次 3b C2: priority 校验/trailing 缺省/时间缩放/ATR/build+run
        # 已并入 _resolve_stop_and_build_loop (run/run_cached 共享, 防漂移);
        # 此处只保留本入口后续仍直接引用的子配置 (degrade 报告/日志/ladder 数组)。
        cost = stop.get("cost_stop", {})
        trail = stop.get("trailing_stop", {})
        ladder = stop.get("ladder_tp", {})
        time_s = stop.get("time_stop", {})
        cond_t = stop.get("cond_time_stop", {})

        codes = selections["stock_code"].unique().tolist()

        # 始终获取完整OHLC数据（不再仅首日规则）
        # P0-1: fill_data=False — 让停牌以 NaN 显式暴露，避免 TDX 源头前向填充掩盖前视偏差
        # 2026-07-16: 5m/分钟级走稀疏窗口拉取, 只拉每只股信号日往后 45 交易日,
        #   避免 4889 只×4.5年 5m 全量(~15亿点)拉取卡死。日线路径一字不变(向后兼容)。
        # 2026-07-18: 准备段抽为 _prepare_run_matrices + 矩阵级缓存接缝。
        #   止盈止损参数不进 key (win_td 由 max_hold_days 推出, 已在 key 里),
        #   命中时改参数重跑只剩核心循环; degrade_5m=on 跳过缓存
        #   (degrade_res 含非序列化对象, 见 backtest/matrix_cache.py docstring)。
        win_td = self._resolve_window_td(stop)
        use_mc = (self.matrix_cache
                  and not (self.degrade_5m and self.bars_per_day == 48)
                  # 2026-09-16 B2: 约束开启即不用矩阵缓存 — _ARRAY_FIELDS 不含
                  # turnover_day, 缓存命中时约束静默丢失 (原条件只对 5m 生效,
                  # 1d/1m 漏网)
                  and not (self.max_turnover_pct < 1.0))
        from core import progress as _progress
        _progress.report("fetch", 0.0, "准备取数...")  # 2026-07-26
        prep = None
        if use_mc:
            from backtest import matrix_cache as _mc
            mc_root = self.matrix_cache_dir or _mc.default_cache_root()
            mc_key = _mc.build_key(selections, start_time, end_time, self.period,
                                   win_td, self.use_kline_cache, ENGINE_VERSION)
            prep = _mc.load(mc_root, mc_key, ENGINE_VERSION)
        if prep is None:
            prep = self._prepare_run_matrices(selections, start_time, end_time, win_td)
            if prep is None:
                return self._empty_result()
            if use_mc:
                # 2026-07-26: 1m 矩阵 GB 级, LRU 收紧到 2 份 (5m/1d 默认 3)
                _mc.save(mc_root, mc_key, ENGINE_VERSION, prep,
                         keep=2 if self.period == "1m" else 3)
        else:
            _progress.report("fetch", 1.0, "矩阵缓存命中")  # 2026-07-26
        _progress.report("matrix", 1.0, "矩阵就绪")  # 2026-07-26
        close = prep["close"]
        entries = prep["entries"]
        high_np = prep["high"]
        low_np = prep["low"]
        open_np = prep["open"]
        tradable_np = prep["tradable"]
        last_tradable_idx = prep["last_tradable_idx"]
        cols = prep["cols"]
        degraded_np = prep.get("degraded_np")
        degrade_res = prep.get("degrade_res")

        # 准备阶梯止盈数组
        levels = ladder.get("levels", [])
        lv = sorted(levels, key=lambda x: x.get("profit", 0))
        ladder_profits = np.array([lv[i]["profit"] for i in range(len(lv))], dtype=np.float64)
        ladder_ratios = np.array([lv[i]["sell_ratio"] for i in range(len(lv))], dtype=np.float64)

        # P-v3.4: 公式卖出 (formula_sell) — 一次性预计算信号矩阵
        formula_exit_np = None
        formula_exit_ratio = 1.0
        formula_sell_failed = None  # 2026-09-16 B4: 构造失败标记 (进 result)
        fs_cfg = stop.get("formula_sell", {})
        if fs_cfg.get("enabled", False):
            formula_name = str(fs_cfg.get("formula_name", "")).strip()
            formula_arg = str(fs_cfg.get("formula_arg", ""))
            formula_exit_ratio = float(fs_cfg.get("sell_ratio", 1.0))
            if formula_name and start_time and end_time:
                try:
                    from backtest.formula_exit import (
                        FormulaExitResult,
                        build_formula_exit_matrix,
                        cache_key,
                        load_cached_formula_exit,
                        save_cached_formula_exit,
                    )
                    from core.formula_runner import FormulaRunner

                    key = cache_key(
                        formula_name, formula_arg,
                        tuple(cols), start_time, end_time, period=self.period,
                    )
                    cached = load_cached_formula_exit(key)
                    if cached is not None:
                        formula_exit_np = cached.matrix
                        logger.info(
                            "formula_sell: 命中缓存 [%s] (信号=%d, shape=%s)",
                            formula_name, int(formula_exit_np.sum()), formula_exit_np.shape,
                        )
                    else:
                        logger.info("formula_sell: 调 TDX 取信号 [%s]...", formula_name)
                        sig_df = FormulaRunner.run_stock_selection_with_dates(
                            formula_name=formula_name,
                            formula_arg=formula_arg,
                            stock_list=list(cols),
                            start_time=start_time,
                            end_time=end_time,
                            stock_period=self.period,
                        )
                        formula_exit_np = build_formula_exit_matrix(
                            sig_df, close.index, close.columns,
                        )
                        save_cached_formula_exit(
                            key,
                            FormulaExitResult(
                                matrix=formula_exit_np,
                                meta={
                                    "formula_name": formula_name,
                                    "formula_arg": formula_arg,
                                    "fetched_at": pd.Timestamp.now().isoformat(),
                                    "total_signals": int(formula_exit_np.sum()),
                                },
                            ),
                        )
                        logger.info(
                            "formula_sell: 写入缓存 [%s] (信号=%d)",
                            formula_name, int(formula_exit_np.sum()),
                        )
                except Exception as e:
                    logger.error("formula_sell 构造失败, 回退禁用: %s", e)
                    formula_exit_np = None
                    # 2026-09-16 B4: 容错保留 (不抛, 怕破坏批量流程), 但结果带
                    # 标记 — 否则报告读者不知道公式卖出根本没生效
                    formula_sell_failed = (
                        f"formula_sell 构造失败已回退禁用: {formula_name}: {e}")
            elif not formula_name:
                logger.warning("formula_sell: enabled=true 但 formula_name 为空, 跳过")
            else:
                logger.warning("formula_sell: 缺 start_time/end_time, 跳过")

        cond_profit_pct = cond_t.get("profit", 0.01)
        logger.info("VeraCore %s: 资金=%s 每笔%s~%s元 %s股/手 时间=%s天 条件=%s天/%.1f%% %s stocks",
                     ENGINE_VERSION,
                     f"{self.initial_capital:,.0f}", f"{self.min_buy_amount:,.0f}",
                     f"{self.max_buy_amount:,.0f}", self.lot_size,
                     time_s.get("max_hold_days", "?"), cond_t.get("days", "?"), cond_profit_pct * 100,
                     len(codes))

        bpday = self.bars_per_day
        t0 = pd.Timestamp.now()
        # 2026-08-20: 买入价口径 (close_t=T日收盘+涨停过滤; open_t1=T+1开盘+一字板拒买)
        entries, buy_price_np, entry_t1_info = self._apply_entry_price_mode(
            entries, close, high_np, low_np, open_np, tradable_np)
        _progress.report("loop", 0.0, "核心回测...")  # 2026-07-26
        # 2026-08-01 批次 3b C2: 共享段 (priority/trailing 缺省/缩放/ATR/build+run)
        equity_arr, raw_trades, resolved = self._resolve_stop_and_build_loop(
            stop, close, entries.values,
            high_np, low_np, open_np,
            tradable_np, last_tradable_idx,
            ladder_profits, ladder_ratios, len(lv),
            formula_exit_np, formula_exit_ratio,
            degraded_np=degraded_np,
            buy_price_np=buy_price_np,
            turnover_day_np=prep.get("turnover_day"),
        )
        # ENGINE_DEBUG 日志的缩放值仅作展示, 从 resolved 读 (2026-08-01 批次 3b C2;
        # 权威计算在 _resolve_stop_and_build_loop, 两处不得各自演化)。
        logger.info("ENGINE_DEBUG max_hold_days=%d(scaled=%d) time_enabled=%s period=%s bpday=%d",
                     int(time_s.get("max_hold_days", 20)),
                     resolved["mhd_scaled"],
                     time_s.get("enabled", True), self.period, bpday)
        _progress.report("loop", 1.0, "回测完成")  # 2026-07-26
        # 2026-07-21: 期末未平仓持仓快照
        final_positions = list(getattr(self._last_loop, "final_positions", None) or [])
        elapsed = (pd.Timestamp.now() - t0).total_seconds()
        # DEBUG: check raw bar differences from Numba output directly
        raw_holds = [int(row[2]) - int(row[1]) for row in raw_trades]
        logger.info("VeraCore: %s笔交易 %.2fs RAW_MAX_HOLD=%d", len(raw_trades), elapsed, max(raw_holds) if raw_holds else 0)

        # C2: 共享后处理（与 run_cached 同一入口, 防 drift）
        equity_curve, trades_df, metrics = self._post_process(equity_arr, raw_trades, close, bpday)
        self._log_results(metrics)

        # degrade_5m: 降级交易事后扫描 (审计 HIGH-2 — degraded_np 不进 BacktestLoop,
        # 报告走 _post_process 后扫描 raw_trades 的 (ci, entry_idx, exit_idx))。
        # 部分出场 (阶梯分批卖) 按 (ci, entry_idx) 聚合成持仓再判定。
        degradation = None
        if degraded_np is not None and degrade_res is not None:
            positions = scan_degraded_positions(raw_trades, degraded_np)
            n_deg = sum(1 for p in positions if p["is_degraded"])
            degradation = {
                "enabled": True,
                "degraded_trades": n_deg,
                "total_trades": len(positions),
                "total_trade_rows": int(len(raw_trades)),
                "degraded_pct": (n_deg / len(positions)) if positions else 0.0,
                "n_stock_days": degrade_res.n_stock_days,
                "degraded_days": degrade_res.degraded_days,
                "rejected_limit_up": degrade_res.rejected_limit_up,
                "adjust_mismatches": degrade_res.adjust_mismatches,
            }
            logger.info(
                "degrade_5m: 降级持仓 %d/%d (%.1f%%), 降级股-天 %d, 涨停拒绝 %d",
                n_deg, len(positions), degradation["degraded_pct"] * 100,
                degrade_res.n_stock_days, degrade_res.rejected_limit_up,
            )
            # Phase B: 降级影响报告 (模糊日两档 + close 价策略偏差 + 夏普/回撤范围)
            if high_np is not None and low_np is not None:
                from backtest.degrade_report import compute_impact_report
                degradation.update(compute_impact_report(
                    raw_trades, degraded_np, high_np, low_np, bpday,
                    cost_enabled=cost.get("enabled", True),
                    cost_threshold=float(cost.get("threshold", _DEFAULT_COST_STOP_THRESHOLD)),
                    trailing_enabled=trail.get("enabled", True),
                    # 2026-08-01 批次 3b C2: 缺省后值取自共享段 resolved (口径一致)
                    trailing_activation=resolved["trailing_activation"],
                    trailing_drawdown=resolved["trailing_drawdown"],
                    ladder_enabled=ladder.get("enabled", True),
                    ladder_profits=tuple(float(p) for p in ladder_profits),
                    initial_capital=float(self.initial_capital),
                    equity_arr=equity_arr,
                    periods_per_year=self.PERIODS_PER_YEAR.get(
                        self.period, self.bars_per_day * 252),
                ))

        # C2: stop_config_summary 改从函数生成（不再调用 StopManager，避免 compute_exit_signals 重复计算）
        from backtest.stop_config import get_stop_config_summary

        # 2026-07-21 用户决策: 请求区间终点仍持仓的, 不平仓不强平, 按市值统计
        # (equity 已由 EquityTracker.finalize 计入), 此处导出明细供报告展示。
        open_positions = []
        if final_positions and len(close.index) and len(close.columns):
            dates = close.index
            last_close = close.values[-1]  # 已 ffill, 与 equity 市值计价同源
            for pos in final_positions:
                ci = int(pos.code)
                if not (0 <= ci < len(cols)):
                    continue
                last_px = float(last_close[ci])
                if np.isnan(last_px) or last_px <= 0:
                    continue
                entry_px = float(pos.entry_px)
                shares = float(pos.shares)
                mv = round(shares * last_px, 2)
                ei = int(pos.entry_idx)
                open_positions.append({
                    "stock_code": cols[ci],
                    "entry_date": str(dates[ei]) if 0 <= ei < len(dates) else "",
                    "entry_price": round(entry_px, 4),
                    "shares": int(shares),
                    "last_price": round(last_px, 4),
                    "market_value": mv,
                    "unrealized_pnl": round(shares * (last_px - entry_px), 2),
                    "unrealized_pct": round(last_px / entry_px - 1, 4) if entry_px > 0 else 0.0,
                })
            if open_positions:
                mv_sum = sum(p["market_value"] for p in open_positions)
                logger.info("期末未平仓 %d 笔, 市值合计 %.0f (按市值计入权益)",
                            len(open_positions), mv_sum)

        bt_kwargs = dict(
            equity_curve=equity_curve, trades=trades_df, metrics=metrics,
            stop_config_summary=get_stop_config_summary(stop),
            selections=selections, stock_count=len(cols),
        )
        # 2026-07-26: K线数据指纹 (可复现性戳, ~0.07s)
        from backtest.matrix_cache import data_fingerprint as _data_fp
        bt_kwargs["data_fingerprint"] = _data_fp()
        if degradation is not None:
            bt_kwargs["degradation"] = degradation
        if entry_t1_info is not None:
            bt_kwargs["entry_mode_info"] = entry_t1_info
        if formula_sell_failed is not None:
            bt_kwargs["formula_sell_failed"] = formula_sell_failed
        if open_positions:
            bt_kwargs["open_positions"] = open_positions
        return BacktestResult(**bt_kwargs)

    def run_cached(self, prepared, stop_config,
                   ladder_profits, ladder_ratios, n_ladder, *,
                   filter_limit_up=True,
                   formula_exit_np=None, formula_exit_ratio=None, formula_exit_lag_bars=1,
                   close_raw=None,
                   return_raw=False):
        """用预取数据运行回测，跳过K线获取（P2-1 前门收敛: prepared 打包 7 矩阵）。

        close/entries/high_np/low_np/open_np/tradable_np/last_tradable_idx 收进
        PreparedMatrix (消除位置顺序陷阱 + 配对不变量构造期 fail-fast); 删除了
        死参数 selections。

        - filter_limit_up: 默认 True; 收编脚本传 False 复现旧直调核心循环口径。
        - formula_exit_np/close_raw: 可选能力数据, None=off。
        - close_raw: 显式原始未 ffill 价, 提供时自建 tradable_np。
        - return_raw: True 时 result dict 加 raw_equity/raw_trades。

        ⚠️ degrade_5m (5m 数据层降级) 仅 run() 路径支持, run_cached 不做降级。
        """
        stop = stop_config or {}
        close = prepared.close
        entries = prepared.entries
        high_np = prepared.high_np
        low_np = prepared.low_np
        open_np = prepared.open_np
        tradable_np = prepared.tradable_np
        last_tradable_idx = prepared.last_tradable_idx

        bpday = self.bars_per_day

        # 候选 A 阶段 1: capabilities 三开关 (默认全开), gate 已提供的能力数据。
        # 语义: 开关 on + 数据 None → 能力 off (=旧行为); 开关 off → 强制 None。
        caps = stop.get("capabilities", {})
        cap_formula = caps.get("formula_exit", True)
        cap_gap = caps.get("gap_protection", True)
        cap_delist = caps.get("delisting", True)
        if not cap_formula:
            formula_exit_np = None
        if not cap_gap:
            open_np = None
        # 退市: 仅当显式传 close_raw 时自建 tradable_np (不从 close 自动建, 防 ffill 调用方误触发)
        if cap_delist and tradable_np is None and close_raw is not None:
            tradable_np, last_tradable_idx = _build_tradable_from_raw(close_raw, close)
        if not cap_delist:
            tradable_np = None
            last_tradable_idx = None
        # (M2 配对 warning 已删: PreparedMatrix.__post_init__ 在构造期强制成对,
        #  此处的 tradable_np/last_tradable_idx 恒成对或同 None, warning 永不触发)
        # formula_exit_ratio: keyword 优先, None 回退 config.formula_sell.sell_ratio
        if formula_exit_ratio is None:
            formula_exit_ratio = float(stop.get("formula_sell", {}).get("sell_ratio", 1.0))
        # ladder 隐含约定: ladder_profits 应升序 (调用方责任); 不升序 warning 不重排
        if n_ladder > 1 and not bool(np.all(np.diff(ladder_profits[:n_ladder]) >= 0)):
            logger.warning("ladder_profits 非升序, 阶梯触发可能不符预期 (调用方应预排序)")

        # 2026-08-20: 买入价口径 (engine config entry_price_mode)。
        # open_t1 时 T 日涨停过滤由 T+1 一字板判定替代 (filter_limit_up 开关不适用);
        # close_t 维持 filter_limit_up 开关语义 (收编脚本传 False 复现旧口径)。
        buy_price_np = None
        entry_t1_info = None
        if self.entry_price_mode == "open_t1":
            entries, buy_price_np, entry_t1_info = self._apply_entry_price_mode(
                entries, close, high_np, low_np, open_np, tradable_np)
        elif filter_limit_up:
            entries = self._filter_limit_up(entries, close)
        # 2026-08-01 批次 3b C2: 共享段 (priority/trailing 缺省/缩放/ATR/build+run)
        equity_arr, raw_trades, _ = self._resolve_stop_and_build_loop(
            stop, close, entries.values,
            high_np, low_np, open_np,
            tradable_np, last_tradable_idx,
            ladder_profits, ladder_ratios, n_ladder,
            formula_exit_np, formula_exit_ratio,
            formula_exit_lag_bars=formula_exit_lag_bars,
            buy_price_np=buy_price_np,
            turnover_day_np=prepared.turnover_day_np,  # 2026-09-16 B1: 原 getattr 恒 None
        )

        # C2: 共享后处理（与 run 同一入口, 防 drift）
        equity_curve, trades_df, metrics = self._post_process(equity_arr, raw_trades, close, bpday)
        bt_kwargs = dict(
            metrics=metrics,
            trades=trades_df,
            cumulative_return=metrics.get("cumulative_return", 0),
            equity_curve=equity_curve,
        )
        if return_raw:
            bt_kwargs["raw_equity"] = equity_arr
            bt_kwargs["raw_trades"] = raw_trades
        if entry_t1_info is not None:
            bt_kwargs["entry_mode_info"] = entry_t1_info
        return BacktestResult(**bt_kwargs)

    def _post_process(self, equity_arr, raw_trades, close, bpday):
        """C2: run() 与 run_cached() 的共享后处理。

        equity_arr + raw_trades → equity_curve(DataFrame) + trades_df + metrics。
        抽出此方法消除两入口的 equity_curve/_build_trades/metrics 重复
        （修一个 equity_curve bug 不再需要在两处各改一次）。
        """
        dates = close.index
        equity_curve = pd.DataFrame({"date": dates, "equity": equity_arr})
        equity_curve.set_index("date", inplace=True)
        peak = equity_curve["equity"].expanding().max()
        equity_curve["drawdown"] = (equity_curve["equity"] - peak) / peak
        equity_curve.reset_index(inplace=True)

        trades_df = self._build_trades(raw_trades, close.columns, dates, bpday)
        if not trades_df.empty:
            trades_df["entry_date"] = pd.to_datetime(trades_df["entry_date"])
            trades_df["exit_date"] = pd.to_datetime(trades_df["exit_date"])

        metrics = MetricsCalculator.compute_all(
            equity_curve, trades_df, self.initial_capital,
            periods_per_year=self.PERIODS_PER_YEAR.get(self.period, self.bars_per_day * 252))
        return equity_curve, trades_df, metrics

    def _build_trades(self, raw, columns, dates, bpday=1):
        """raw_trades 数组 → 交易明细 DataFrame。

        2026-07-17 Phase 2: 逐行 dict 构造 → 按列构建 (76ms→~20ms)。
        舍入保留 Python round 列表推导 — 探针实测 np.round 对 x.xx5 型值
        ~4% 不一致 (二进制 half-even vs 十进制正确舍入), 不可向量化。
        数据流顺序与旧版一致: 先 round4 得 ep/xp → 再乘 sh → 再 round2。
        """
        if len(raw) == 0: return pd.DataFrame()
        # 2026-07-18 审计修正: 4.0 是 trailing 亏损出(移动止损), 8.0 才是盈利出(移动止盈)
        reason_map = {1.0: "换股卖出", 3.0: "成本止损",
                      4.0: "移动止损", 8.0: "移动止盈",
                      5.0: "阶梯止盈",
                      6.0: "时间止损", 9.0: "时间止盈",
                      7.0: "cond_time_stop",
                      10.0: "首日未达标",
                      11.0: "退市",
                      12.0: "formula_sell",
                      13.0: "ATR止损"}
        inv_col = {i: c for i, c in enumerate(columns)}
        n_dates = len(dates)
        ci_arr = raw[:, 0].astype(np.int64)
        ei_arr = raw[:, 1].astype(np.int64)
        xi_arr = raw[:, 2].astype(np.int64)
        ep = [round(float(v), 4) for v in raw[:, 3]]
        xp = [round(float(v), 4) for v in raw[:, 4]]
        sh = [int(v) for v in raw[:, 5]]
        if bpday > 1:
            hold = np.maximum(1, (xi_arr - ei_arr) // bpday)
        else:
            hold = xi_arr - ei_arr
        return pd.DataFrame({
            "stock_code": [inv_col.get(ci, str(ci)) for ci in ci_arr],
            "entry_date": [dates[i] if 0 <= i < n_dates else dates[0] for i in ei_arr],
            "exit_date": [dates[i] if 0 <= i < n_dates else dates[-1] for i in xi_arr],
            "entry_price": ep,
            "exit_price": xp,
            "shares": sh,
            "entry_amount": [round(e * s, 2) for e, s in zip(ep, sh)],
            "exit_amount": [round(x * s, 2) for x, s in zip(xp, sh)],
            "pnl": [round(float(v), 2) for v in raw[:, 6]],
            "return": [round(float(v), 4) for v in raw[:, 7]],
            "profit_pct": [round(float(v), 4) for v in raw[:, 7]],
            # 2026-09-16 P2: 未知原因码不再误标"换股卖出", 显式暴露异常码值
            "exit_reason": [reason_map.get(v, f"未知原因({v})") for v in raw[:, 8]],
            "hold_days": list(hold),
        })

    def _ensure_index(self, df):
        if not isinstance(df.index, pd.DatetimeIndex): df.index = pd.to_datetime(df.index)
        return df.sort_index()

    @staticmethod
    def _drop_nonstandard_intraday_bars(close, high_df, low_df, open_df, bar_times):
        """丢弃时刻不在标准槽位 (bar_times) 的 bar (2026-07-26 由 5m 版泛化)。

        盘中临停股复牌竞价 bar 等非标准时刻会让并集网格某天 bar 数 ≠ 预期,
        破坏 loop 的 T+1 i//bpday 日界。被丢 bar 多为临停复牌竞价打印,
        该股的当日数据会缺一根 (NaN, 按停牌语义处理), 日志明示。
        """
        times = close.index.strftime("%H:%M")
        keep = pd.Index(times).isin(bar_times)
        n_drop = int((~keep).sum())
        if not n_drop:
            return close, high_df, low_df, open_df
        odd_days = sorted({d.strftime("%Y-%m-%d") for d in close.index[~keep]})
        logger.warning(
            "分钟级非标准时刻 bar 过滤: 丢弃 %d 根 (时刻 %s, 涉及 %d 天 %s), "
            "保持 %d 根/天不变量 (临停复牌竞价 bar 所致)",
            n_drop, sorted(set(times[~keep])), len(odd_days), odd_days[:5],
            len(bar_times))
        close = close.loc[keep]
        high_df = high_df.loc[keep] if high_df is not None else None
        low_df = low_df.loc[keep] if low_df is not None else None
        open_df = open_df.loc[keep] if open_df is not None else None
        return close, high_df, low_df, open_df

    @staticmethod
    def _drop_nonstandard_5m_bars(close, high_df, low_df, open_df):
        """5m 版 (STD_5M_BAR_TIMES, 2026-07-18)。

        盘中临停股 13:00 复牌竞价 bar 等非标准时刻会让并集网格某天 ≠48 根,
        破坏 loop 的 T+1 i//bpday 日界。2026-07-26 起为
        _drop_nonstandard_intraday_bars 的 5m 委托 alias
        (外部调用方: tools/gs_5m_sweep.py, tools/quantqq_5m_sweep.py)。
        """
        return BacktestEngine._drop_nonstandard_intraday_bars(
            close, high_df, low_df, open_df, STD_5M_BAR_TIMES)

    def _apply_5m_degradation(self, close, high_df, low_df, open_df, selections, win_td,
                              start_time="", end_time=""):
        """5m 数据层降级编排 (计划书 2026-07-18 §4.1/4.2): 缺 5m 的股-天用 1d OHLC 填充。

        步骤: 交易日历 → synthesize_5m_grid 合成完整网格 (每天恰好 48 根,
        全市场缺口日也有行, 审计 CRITICAL-1) → 全部矩阵 reindex → 取 1d
        (KlineCache) → apply_5m_degradation 填充 + degraded_np。
        窗口边界复用 DataFetcher.compute_window_bounds (与稀疏窗口拉取同一套,
        防 drift)。任何一步失败 → 告警并返回未降级原矩阵 (回退现状丢信号路径)。

        2026-07-21: 网格起止改为请求区间 [start_time, end_time] (原取
        close.index 首末, 导致 5m 数据深度 (2024-06-27) 之前的时段永远进不了
        网格, 信号照丢)。起点之前的无 5m 时段由 1d 填充覆盖, 权益从请求起点开始。

        返回 (close, high, low, open, degraded_df, DegradeResult);
        失败/跳过返回原矩阵 + (None, None)。

        ⚠️ 性能注意 (审计 MEDIUM-2): 网格 = [请求起点, 请求终点] 内每个日历
        交易日 × 48 根 × 全部股票列。信号稀疏分布在大区间时, reindex 会制造大量
        全 NaN 日, loop 主循环按膨胀后的行数跑 (4.5 年 ≈ 5.3 万行/股)。
        002008 类短区间场景无感; 长区间 5m 回测开启降级时会感知内存/耗时增长。
        """
        orig = (close, high_df, low_df, open_df)
        try:
            # 2026-07-21: 网格起止 = 请求区间 (缺省回退 close.index 首末, 兼容旧调用)
            grid_start = str(start_time) if start_time else close.index.min().strftime("%Y%m%d")
            grid_end = str(end_time) if end_time else close.index.max().strftime("%Y%m%d")
            days = DataFetcher.get_trading_days(grid_start, grid_end)
            if not days:
                logger.warning("degrade_5m: 交易日历为空, 跳过降级 (回退丢信号路径)")
                return (*orig, None, None)
            grid = synthesize_5m_grid(days)
            off_grid = close.index.difference(grid)
            if len(off_grid):
                logger.warning(
                    "degrade_5m: %d 个 5m bar 时间戳不在标准网格 (首处 %s), 将被丢弃",
                    len(off_grid), off_grid[0])
            close_g = close.reindex(grid)
            high_g = high_df.reindex(grid) if high_df is not None else None
            low_g = low_df.reindex(grid) if low_df is not None else None
            open_g = open_df.reindex(grid) if open_df is not None else None

            codes = list(close_g.columns)
            # 1d 往前多取 10 天: 涨停判定需要前一交易日 1d 收盘
            start_1d = (close_g.index.min() - pd.Timedelta(days=10)).strftime("%Y%m%d")
            end_1d = close_g.index.max().strftime("%Y%m%d")
            k1d = DataFetcher.get_kline(
                codes, start_1d, end_1d, period="1d",
                dividend_type="front", fill_data=False, use_cache=self.use_kline_cache)
            if not k1d or "Close" not in k1d:
                logger.warning("degrade_5m: 1d 数据为空, 跳过降级 (回退丢信号路径)")
                return (*orig, None, None)

            def _f(name):
                df = k1d.get(name)
                return self._ensure_index(df) if df is not None else None

            c1 = _f("Close")
            # 2026-07-21 审计修复: 请求终点晚于数据末端时 (如缓存只到 7-17,
            # end=7-31), 网格尾部全 NaN 日会让持仓被 i > last_tradable_idx
            # 误判退市强平 (reason=11)。网格裁到最后一个有数据 (5m 或 1d)
            # 的交易日 — 那之后没有行情可计价, 持仓保持 open 到数据末端。
            data_days = close_g.index[close_g.notna().any(axis=1)]
            if c1 is not None and not c1.empty:
                data_days = data_days.union(c1.index[c1.notna().any(axis=1)])
            if len(data_days):
                horizon = data_days.max().normalize()
                tail = grid.normalize() > horizon
                if tail.any():
                    logger.warning(
                        "degrade_5m: 请求终点晚于数据末端, 裁掉 %d 根无数据 bar "
                        "(最后数据日 %s) — 防全 NaN 日误判退市强平",
                        int(tail.sum()), horizon.date())
                    grid = grid[~tail]
                    close_g = close_g.reindex(grid)
                    high_g = high_g.reindex(grid) if high_g is not None else None
                    low_g = low_g.reindex(grid) if low_g is not None else None
                    open_g = open_g.reindex(grid) if open_g is not None else None

            win_start, win_end = DataFetcher.compute_window_bounds(
                selections, win_td, end_time=end_time or None)
            bounds = {c: (win_start[c], win_end[c]) for c in codes if c in win_start}
            # 2026-08-20: open_t1 口径下 T 日不成交, 降级的 1d 涨停拒单无意义 → 关闭
            ratio_vec = (None if self.entry_price_mode == "open_t1"
                         else self._limit_ratio_vector(close_g.columns))
            res = apply_5m_degradation(
                close_g, high_g, low_g, open_g,
                c1, _f("High"), _f("Low"), _f("Open"),
                window_bounds=bounds, limit_ratio_vec=ratio_vec)
            degraded_df = pd.DataFrame(
                res.degraded_np, index=res.close.index, columns=res.close.columns)
            return res.close, res.high, res.low, res.open, degraded_df, res
        except Exception:
            logger.exception("degrade_5m: 降级失败, 回退现状 (丢信号路径)")
            return (*orig, None, None)

    def _build_entry_signals(self, selections, prices):
        entries = pd.DataFrame(False, index=prices.index, columns=prices.columns)
        dropped_gap = 0  # 信号日整天缺失(数据缺口)被丢弃的计数
        for _, row in selections.iterrows():
            code = row["stock_code"]; dt = pd.to_datetime(row["select_date"])
            if code not in entries.columns: continue
            if dt in entries.index: entries.loc[dt, code] = True
            else:
                # 信号日 00:00 不在 index (5m: bar 在 9:35-15:00, 00:00 恒不在) → 找首个 >= 信号日的 bar
                m = entries.index >= dt
                if m.any():
                    first_bar = entries.index[m][0]
                    # 数据缺口判定: 首个可用 bar 已跨到信号日之后某天 → 信号日整天缺失
                    # (002008 bug, 2026-07-17: 5m 6.23-6.29 缺口曾把 6.23 信号顺延到 6.30,
                    #  用未来价格成交旧信号, 违反"信号日 T 收盘价买入"铁律 + 隐性前视)
                    if first_bar.normalize() != dt.normalize():
                        logger.warning(
                            "entry_signal_drop: %s 信号日 %s 在价格 index 缺失 "
                            "(首个可用 bar=%s), 丢弃该信号 (不顺延, 避免用未来价格成交)",
                            code, dt.strftime("%Y-%m-%d"), first_bar.strftime("%Y-%m-%d %H:%M"),
                        )
                        dropped_gap += 1
                        continue
                    # 同日顺延 (5m intended): 取信号日最后一根 bar (1d 不变, 5m→15:00)
                    day_mask = entries.index.normalize() == first_bar.normalize()
                    if day_mask.any():
                        entries.loc[entries.index[day_mask][-1], code] = True
                    else:
                        entries.loc[first_bar, code] = True
                else:
                    # 信号日晚于价格 index 最后一根 bar (价格数据未覆盖到信号日) → 丢弃并告警
                    logger.warning(
                        "entry_signal_drop: %s 信号日 %s 晚于价格数据最后一根 bar, 丢弃该信号",
                        code, dt.strftime("%Y-%m-%d"),
                    )
                    dropped_gap += 1
        if dropped_gap > 0:
            logger.warning(
                "entry_signal_drop: 共 %d 个信号因信号日数据缺失被丢弃 "
                "(多为 5m K线未下载完整, 请补 TDX 盘后数据后重跑)",
                dropped_gap,
            )
        return entries

    _LIMIT_RATIO_CACHE_MAX = 32  # 2026-07-18 审计修复: 缓存上限, FIFO 淘汰防无界增长

    def _limit_ratio_vector(self, columns):
        """每列涨停幅度向量 (0.10 主板 / 0.20 创业板+科创板 / 0.30 北交所 / 0.05 ST)。

        2026-07-17 Phase 1: 按列集合缓存, 批量跑 N 个公式时 ST 信息只查一次。
        2026-08-01 P1: 涨停幅度规则统一到 core/limit_ratio.py (全项目唯一真相源),
        消除与 trade/executor.py 的 ST 标记来源漂移。
        """
        from core.limit_ratio import limit_ratio
        key = tuple(str(c) for c in columns)
        cache = getattr(self, "_limit_ratio_cache", None)
        if cache is None:
            cache = self._limit_ratio_cache = {}
        vec = cache.get(key)
        if vec is None:
            vec = np.full(len(columns), 0.10)
            for j, col in enumerate(columns):
                col_str = str(col)
                info = get_cached_info(col_str)
                is_st = str(info.get('IsSTGP', '0')) == '1'
                vec[j] = limit_ratio(col_str, is_st)
            if len(cache) >= self._LIMIT_RATIO_CACHE_MAX:
                cache.pop(next(iter(cache)))
            cache[key] = vec
        return vec

    def _filter_limit_up(self, entries, close):
        """过滤涨停板买入信号: A股涨停日无法买入。将涨停日的entry设为False。

        2026-07-17 Phase 1: 全矩阵向量化 (旧逐列循环 590ms→3ms, A/B 验证 equals)。
        逐元素运算顺序与旧逻辑完全一致 (prev*(1+ratio) 再 *0.997, 无浮点重结合);
        首行 prev=NaN → 比较恒 False → 首行不过滤 (与 shift(1) 行为一致)。
        """
        if not isinstance(entries, pd.DataFrame):
            return entries
        if len(entries) == 0:
            return entries.copy()  # 2026-07-18: 空帧守卫 (旧 shift(1) 天然安全, 向量化版需显式)
        close_aligned = close[entries.columns]  # 列缺失时与旧版一样 KeyError
        ratio_vec = self._limit_ratio_vector(entries.columns)
        cv = close_aligned.values
        if self.bars_per_day > 1 and isinstance(close_aligned.index, pd.DatetimeIndex):
            # 2026-07-18 修 5m 涨停过滤失效(MEMORY 老账): 分钟级必须相对
            # 前一**交易日**收盘判定, 而非前一根 5m bar (5m 单 bar 涨 10% 几乎
            # 不发生, 旧口径等于不过滤)。首日 prev=NaN → 不滤 (与 1d 首行一致)。
            day_idx = close_aligned.index.normalize()
            daily_close = close_aligned.groupby(day_idx).last()
            prev = daily_close.shift(1).reindex(day_idx).values
        else:
            prev = np.empty_like(cv)
            prev[0] = np.nan
            prev[1:] = cv[:-1]
        # 接近涨停价(0.3%容差)则取消买入信号
        # 2026-09-16 B3: 公式下沉 detect_limit_up (浮点顺序 prev*(1+ratio) 再 *0.997 不变)
        limit_up = detect_limit_up(cv, prev, ratio_vec)
        vals = entries.values.copy()
        vals[limit_up] = False
        result = pd.DataFrame(vals, index=entries.index, columns=entries.columns)
        filtered = (entries.sum().sum() - result.sum().sum())
        if filtered > 0:
            logger.info(f"涨停过滤: 移除 {int(filtered)} 个涨停买入信号")
        return result

    def _log_results(self, m):
        logger.info("-" * 40)
        logger.info("累计:%+.2f%% 年化:%+.2f%% 回撤:%+.2f%% 夏普:%.2f",
                     m.get('cumulative_return',0)*100, m.get('annualized_return',0)*100,
                     m.get('max_drawdown',0)*100, m.get('sharpe_ratio',0))
        logger.info("胜率:%.1f%% 交易:%s", m.get('win_rate',0)*100, m.get('total_trades',0))
        logger.info("-" * 40)

    def _empty_result(self):
        return BacktestResult(
            equity_curve=pd.DataFrame(columns=["date", "equity", "drawdown"]),
            trades=pd.DataFrame(), metrics={}, stop_config_summary="",
            selections=pd.DataFrame(), stock_count=0)
