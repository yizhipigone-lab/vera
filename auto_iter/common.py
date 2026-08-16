"""auto_iter 共用层 — 离线守卫 / 股票池 / 数据面板 / 因子库 / 回测封装。

被两部分复用, 保证"循环里跑的那一轮"和"落盘的独立脚本"是同一份逻辑:
  1. auto_iter/auto_strategy_loop.py  (迭代主循环)
  2. output/auto_iter/iter_*.py       (每轮生成的独立复现脚本)

关键设计 (2026-08-10):
  - 回测走 BacktestEngine.run_cached (backtest/engine.py:679): 调用方自备
    close/entries/high/low/open 矩阵, 完全绕开 DataFetcher → KlineCache → TDX
    取数链路。KlineCache 有 miss-fetch / 缺口补拉 / 复权探针三条联网兜底
    (core/kline_cache.py), 不够用"池子覆盖全"来防, 必须根本不走它。
  - enforce_offline() 把 TdxConnector.tq 替换成抛错桩 — 任何残留的联网
    尝试都会立刻炸出来, 而不是静默连网。
  - seed_stock_info(): 引擎 _filter_limit_up 需要每只股票是否 ST 来定涨停幅度
    (backtest/engine.py:_limit_ratio_vector → core.stock_filter.get_cached_info
    → TDX get_stock_info)。预填进程级缓存 _INFO_CACHE 后不再触网。
    池内股票已按"从未跌破 1 元"剔除了面退/ST 高危股, 统一按非 ST (IsSTGP='0')
    处理; 涨停幅度本身按代码前缀 (30/68→20%, 8/4→30%) 由 core/limit_ratio.py 判定。
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
KLINE_DIR = REPO_ROOT / "data" / "kline_cache" / "1d"
MANIFEST_DB = REPO_ROOT / "data" / "kline_cache" / "manifest.db"

# 信号计算起点: 因子最长回看 ~120 交易日 (如 new_high(120) / 半年均线),
# 2019-01-01 首个信号日前需约 6 个月缓冲, 从 2017-07-01 读数
DATA_START = "2017-07-01"
BT_START = "2019-01-01"
BT_END = "2026-07-31"

# 股票池硬条件: 本地缓存须完整覆盖信号缓冲期 + 回测期
POOL_FIRST_DATE = "20180101"
POOL_LAST_DATE = "20260731"

# 回测费用/仓位口径 — 与 config/cli_2019_stopfirst.yaml 一致
DEFAULT_BT_CFG = {
    "initial_capital": 1000000.0,
    "commission": 0.0003,   # 佣金万三
    "slippage": 0.001,      # 滑点千一
    "stamp_tax": 0.0005,    # 印花税 (卖出单边)
    "period": "1d",
    "position_sizing": {
        "max_positions": 999,
        "min_buy_amount": 2000.0,
        "max_buy_amount": 20000.0,
        "lot_size": 100,
        "min_lots": 1,
    },
}

PANEL_FIELDS = ("open", "high", "low", "close", "volume", "amount")


# ═══════════════════════════════════════════════════════════════
# 离线守卫
# ═══════════════════════════════════════════════════════════════

def enforce_offline():
    """硬断网: 任何 TDX 连接尝试立即抛 RuntimeError。

    run_cached 路径本不该触网, 这道守卫是把"离线"从承诺变成可验证的事实 —
    若有隐藏联网点, 第一轮冒烟就会带着堆栈炸出来。
    """
    from core.connector import TdxConnector

    def _blocked(cls, *a, **k):
        raise RuntimeError("auto_iter 离线模式: 禁止任何 TDX 连接")

    TdxConnector.tq = classmethod(_blocked)
    if hasattr(TdxConnector, "ensure_connected"):
        TdxConnector.ensure_connected = classmethod(_blocked)


def seed_stock_info(codes):
    """预填股票信息进程缓存, 让涨停过滤的 ST 判定不触网 (见模块头注释)。"""
    import core.stock_filter as sf

    for c in codes:
        sf._INFO_CACHE.setdefault(c, {"IsSTGP": "0"})


# ═══════════════════════════════════════════════════════════════
# 股票池
# ═══════════════════════════════════════════════════════════════

def build_pool_candidates(manifest_db: Path = MANIFEST_DB,
                          first_date: str = None, last_date: str = None) -> list:
    """从 kline_cache manifest 筛出本地缓存覆盖 [first_date, last_date] 的股票。

    日期缺省用模块常量 [2018-01-01, 2026-07-31]; 长周期实验 (v3) 可显式传入
    更早/更晚的覆盖期。只信 manifest 的首末日期 (实测 5698 → 3235 只)。
    intact 标记不看 — 停牌缺日会被记成 intact=0, 但对我们无影响: 停牌日
    本来就无信号、不可交易。注意: 末日期未到的退市股被排除, 池子有幸存者
    偏差, 迭代搜参可接受, 最终策略定型需另行评估。
    """
    first_date = first_date or POOL_FIRST_DATE
    last_date = last_date or POOL_LAST_DATE
    conn = sqlite3.connect(str(manifest_db))
    try:
        rows = conn.execute(
            "SELECT stock_code FROM manifest WHERE period='1d' "
            "AND first_date <= ? AND last_date >= ?",
            (first_date, last_date),
        ).fetchall()
    finally:
        conn.close()
    return sorted(r[0] for r in rows)


def load_panel(codes, start: str = DATA_START, workers: int = 8) -> dict:
    """一次性把股票池日线读入内存, 返回 {field: 宽表 DataFrame(行=date, 列=code)}。

    之后每轮迭代的信号计算都基于这份内存面板, 不重复读盘。
    3235 只 × ~2150 天 × 6 字段 float64 ≈ 330MB, 可接受。
    """
    def _read(code):
        df = pd.read_parquet(KLINE_DIR / f"{code}.parquet")
        df = df[df["date"] >= start]
        return code, df.set_index("date")

    stocks = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for code, df in ex.map(_read, codes):
            if not df.empty:
                stocks[code] = df
    panel = {}
    for field in PANEL_FIELDS:
        wide = pd.DataFrame({c: df[field] for c, df in stocks.items()})
        panel[field] = wide.sort_index().astype(np.float64)
    return panel


def filter_pool(panel, amount_quantile: float = 0.2, min_price: float = 1.0,
                max_stocks: int = 3000, start: str = None, end: str = None) -> list:
    """流动性/价格过滤, 返回排序后的股票代码列表 (确定性, 复现脚本依赖)。

    - 剔除统计窗口内收盘价曾低于 min_price 的 (面值退市红线 1 元, 跌破过的
      基本是 ST/退市高危股 — 离线拿不到股票名称, 这是最可靠的 ST 代理)
    - 剔除日均成交额最低的 amount_quantile 分位 (僵尸股)
    - 按日均成交额降序截断到 max_stocks
    start/end 缺省用模块常量 BT_START/BT_END; 长周期实验显式传入。
    """
    start = start or BT_START
    end = end or BT_END
    amt = panel["amount"].loc[start:end].mean(skipna=True)
    min_close = panel["close"].loc[start:end].min(skipna=True)
    ok = (min_close >= min_price) & (amt >= amt.quantile(amount_quantile))
    codes = amt[ok].sort_values(ascending=False).index.tolist()[:max_stocks]
    return sorted(codes)


def shrink_panel(panel, codes) -> dict:
    """面板裁到指定股票列 (释放内存 + 复现脚本与主循环口径一致)。"""
    return {k: v[codes] for k, v in panel.items()}


# ═══════════════════════════════════════════════════════════════
# 因子库 (全向量化, 输入输出均为 行=date×列=code 宽表)
# ═══════════════════════════════════════════════════════════════

def _ma(close, n):
    return close.rolling(n, min_periods=n).mean()


def _rsi(close, n):
    """简单均值版 RSI (非 Wilder 平滑): 100 - 100/(1 + 平均涨/平均跌)。"""
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(n, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).rolling(n, min_periods=n).mean()
    return 100 - 100 / (1 + gain / loss)


# ── 左侧 (均值回归/逆势) 因子 ──

def f_ret_drop(panel, n, x):
    """N 日跌幅 <= -x (超跌)"""
    c = panel["close"]
    return c / c.shift(n) - 1 <= -x


def f_bias_low(panel, n, x):
    """收盘价距 N 日最低价偏离 <= x (贴着底部)"""
    return panel["close"] / panel["low"].rolling(n, min_periods=n).min() - 1 <= x


def f_rsi_low(panel, n, th):
    """RSI(N) <= th (超卖)"""
    return _rsi(panel["close"], n) <= th


def f_vol_shrink(panel, n, r):
    """缩量: 当日量 / N 日均量 <= r"""
    v = panel["volume"]
    return v / v.rolling(n, min_periods=n).mean() <= r


def f_bias_ma(panel, n, x):
    """负乖离: 收盘价低于 N 日均线 x 以上"""
    c = panel["close"]
    return c / _ma(c, n) - 1 <= -x


# ── 右侧 (趋势突破) 因子 ──

def f_new_high(panel, n):
    """收盘价创 N 日新高 (与前 N 日最高比, 不含当日)"""
    c = panel["close"]
    return c >= c.shift(1).rolling(n, min_periods=n).max()


def f_vol_surge(panel, n, r):
    """放量: 当日量 >= 前 N 日均量 × r"""
    v = panel["volume"]
    return v >= v.shift(1).rolling(n, min_periods=n).mean() * r


def f_ma_bull(panel, fast, mid, slow):
    """均线多头排列: MA(fast) > MA(mid) > MA(slow)"""
    c = panel["close"]
    return (_ma(c, fast) > _ma(c, mid)) & (_ma(c, mid) > _ma(c, slow))


def f_break_ma(panel, n):
    """收盘价上穿 N 日均线"""
    c = panel["close"]
    m = _ma(c, n)
    return (c > m) & (c.shift(1) <= m.shift(1))


# ── 市场状态 (大盘环境过滤) 因子 ──
# 2026-08-10 新增维度: 左侧抄底的最大回撤来源是大盘崩跌时接飞刀, 而个股因子
# 看不到大盘。以下因子从 panel 全池 close 直接算市场宽度指标 — 结果是"按日"
# 的标量序列 (当天全市场统一晴/雨), 广播成 (日期×股票) 布尔矩阵与个股因子 AND。
# 约束: 每轮最多叠加 1 个市场因子 (采样/结构变异双侧保证), 防多个市场条件
# AND 把信号掐没。

def _broadcast_days(day_mask: pd.Series, like: pd.DataFrame) -> pd.DataFrame:
    """把按日布尔序列广播成 (日期×股票) 矩阵 (市场状态因子专用)。"""
    m = day_mask.fillna(False).to_numpy(dtype=bool)
    return pd.DataFrame(np.tile(m[:, None], (1, like.shape[1])),
                        index=like.index, columns=like.columns)


def f_mkt_med_ma(panel, n):
    """大盘中位线站岗: 全池每日收盘价中位数 > 其 n 日均线才放行 (中位线在均线上=天气晴)"""
    c = panel["close"]
    med = c.median(axis=1)  # 按日横截面中位数 (skipna: 停牌股自然剔除)
    return _broadcast_days(med > med.rolling(n, min_periods=n).mean(), c)


def f_mkt_breadth(panel, n, th):
    """市场宽度: 当日收盘价 > n 日均线的股票占比 > th 才放行 (多数股票站稳)"""
    c = panel["close"]
    above = (c > _ma(c, n)).sum(axis=1)
    valid = c.notna().sum(axis=1)
    return _broadcast_days(above / valid > th, c)


def f_mkt_not_crash(panel, n, x):
    """排除塌方段: 全池中位数过去 n 日跌幅不超过 x 才放行"""
    c = panel["close"]
    med = c.median(axis=1)
    return _broadcast_days(med / med.shift(n) - 1 >= -x, c)


# ── 波动率/振幅类 (2026-08-11 因子库大扩编) ──

def _tr(panel):
    """真实波幅 TR = max(高-低, |高-昨收|, |低-昨收|), 宽表逐元素"""
    h, l = panel["high"], panel["low"]
    prev = panel["close"].shift(1)
    return np.fmax(np.fmax(h - l, (h - prev).abs()), (l - prev).abs())


def f_atr_low(panel, n, th):
    """ATR(n)/close <= th (低波动, 暴风雨前的平静)"""
    atr = _tr(panel).rolling(n, min_periods=n).mean()
    return atr / panel["close"] <= th


def f_atr_high(panel, n, th):
    """ATR(n)/close >= th (高波动)"""
    atr = _tr(panel).rolling(n, min_periods=n).mean()
    return atr / panel["close"] >= th


def f_range_pos(panel, th):
    """收盘在当日振幅中的位置 (close-low)/(high-low) >= th (收在日内高位=强势);
    high==low (一字板/停牌) 置 False"""
    rng = panel["high"] - panel["low"]
    pos = (panel["close"] - panel["low"]) / rng.replace(0, np.nan)
    return (pos >= th).fillna(False)


# ── K线形态类 ──

def f_lower_shadow(panel, th):
    """下影线占比 (min(open,close)-low)/(high-low) >= th (长下影=盘中被捞起);
    分母为 0 置 False"""
    rng = panel["high"] - panel["low"]
    shadow = (np.minimum(panel["open"], panel["close"]) - panel["low"]) / rng.replace(0, np.nan)
    return (shadow >= th).fillna(False)


def f_bull_body(panel, th):
    """阳线实体占比 (close-open)/(high-low) >= th 且 close>open; 分母为 0 置 False"""
    rng = panel["high"] - panel["low"]
    body = (panel["close"] - panel["open"]) / rng.replace(0, np.nan)
    return ((body >= th) & (panel["close"] > panel["open"])).fillna(False)


def f_consec_down(panel, n):
    """连续 n 日收阴 (每日 close < 昨收, 含当日共 n 天)"""
    down = panel["close"] < panel["close"].shift(1)
    return down.rolling(n, min_periods=n).sum() >= n


# ── 位置/动量类 ──

def _price_pos(panel, n):
    """价格位置分位 (close - LLV(low,n)) / (HHV(high,n)-LLV(low,n)), 分母 0 → NaN"""
    llv = panel["low"].rolling(n, min_periods=n).min()
    hhv = panel["high"].rolling(n, min_periods=n).max()
    return (panel["close"] - llv) / (hhv - llv).replace(0, np.nan)


def f_price_pos_low(panel, n, th):
    """处在 n 日区间底部 th 以下 (分母为 0 置 False)"""
    return (_price_pos(panel, n) <= th).fillna(False)


def f_price_pos_high(panel, n, th):
    """处在 n 日区间顶部 th 以上 (分母为 0 置 False)"""
    return (_price_pos(panel, n) >= th).fillna(False)


def f_roc_up(panel, n, x):
    """n 日涨幅 >= x (正动量)"""
    c = panel["close"]
    return c / c.shift(n) - 1 >= x


def f_drawdown_from_high(panel, n, x):
    """距 n 日最高点回撤 >= x (close/HHV(high,n)-1 <= -x)"""
    hhv = panel["high"].rolling(n, min_periods=n).max()
    return panel["close"] / hhv - 1 <= -x


# ── 量价类 ──

def f_amt_surge(panel, n, r):
    """当日成交额 >= 前 n 日均额 × r (资金涌入, 比 volume 更贴近真实资金)"""
    a = panel["amount"]
    return a >= a.shift(1).rolling(n, min_periods=n).mean() * r


def f_price_up_vol_down(panel, n):
    """价涨量缩背离: close > n 日前 且 volume < n 日前 (谨慎上涨)"""
    return (panel["close"] > panel["close"].shift(n)) & \
           (panel["volume"] < panel["volume"].shift(n))


# ── 相对强度类 (横截面) ──

def f_rel_strength(panel, n, th):
    """相对强度: 个股 n 日涨幅 - 当日全池 n 日涨幅中位数 >= th (跑赢大盘);
    逐股计算再减按日中位数, 不是广播单日标量"""
    ret = panel["close"] / panel["close"].shift(n) - 1
    med = ret.median(axis=1)  # 按日横截面中位数 (skipna)
    return ret.sub(med, axis=0) >= th


FACTOR_FUNCS = {
    "ret_drop": f_ret_drop,
    "bias_low": f_bias_low,
    "rsi_low": f_rsi_low,
    "vol_shrink": f_vol_shrink,
    "bias_ma": f_bias_ma,
    "new_high": f_new_high,
    "vol_surge": f_vol_surge,
    "ma_bull": f_ma_bull,
    "break_ma": f_break_ma,
    "mkt_med_ma": f_mkt_med_ma,
    "mkt_breadth": f_mkt_breadth,
    "mkt_not_crash": f_mkt_not_crash,
    # 2026-08-11 因子库大扩编 (13 个)
    "atr_low": f_atr_low,
    "atr_high": f_atr_high,
    "range_pos": f_range_pos,
    "lower_shadow": f_lower_shadow,
    "bull_body": f_bull_body,
    "consec_down": f_consec_down,
    "price_pos_low": f_price_pos_low,
    "price_pos_high": f_price_pos_high,
    "roc_up": f_roc_up,
    "drawdown_from_high": f_drawdown_from_high,
    "amt_surge": f_amt_surge,
    "price_up_vol_down": f_price_up_vol_down,
    "rel_strength": f_rel_strength,
}

LEFT_FACTORS = ("ret_drop", "bias_low", "rsi_low", "vol_shrink", "bias_ma",
                # 2026-08-11 扩编: 逆势近亲 (低波动/长下影/连阴/区间底部/高位回撤/量价背离)
                "atr_low", "lower_shadow", "consec_down", "price_pos_low",
                "drawdown_from_high", "price_up_vol_down")
RIGHT_FACTORS = ("new_high", "vol_surge", "ma_bull", "break_ma",
                 # 2026-08-11 扩编: 顺势近亲 (高波动/强势收盘/阳线实体/区间顶部/动量/资金涌入/相对强度)
                 "atr_high", "range_pos", "bull_body", "price_pos_high",
                 "roc_up", "amt_surge", "rel_strength")
# 市场状态因子家族: 左右两侧通用 (个股条件 AND 市场条件), 每轮最多 1 个
MARKET_FACTORS = ("mkt_med_ma", "mkt_breadth", "mkt_not_crash")


def build_signals(panel, side: str, factors: list) -> pd.DataFrame:
    """因子列表 AND 组合成入场信号矩阵 (bool 宽表)。

    基础约束: 当日有收盘价且成交量 > 0 (排除停牌日)。
    side 仅作语义标记 ("left"/"right"), 组合逻辑两侧一致;
    市场状态因子 (mkt_*) 与个股因子同样是 AND 进同一张掩码。
    """
    mask = panel["close"].notna() & (panel["volume"] > 0)
    for fac in factors:
        fac = dict(fac)
        name = fac.pop("name")
        mask &= FACTOR_FUNCS[name](panel, **fac)
    return mask.fillna(False).astype(bool)


def build_entries_and_bear(panel, spec: dict):
    """算入场信号 + 熊市强制清仓掩码 (牛熊总开关 market_switch, 2026-08-11)。

    spec 含 market_switch (形如 {"name": "mkt_med_ma", "n": 200}) 时,
    该市场因子为 False 的日子 = 熊市日, 双侧同时生效:
      ① entries &= 牛市掩码 (熊市停开新仓)
      ② bear_mask = ~牛市掩码 → 传 run_backtest 走引擎 formula_exit
         绝对优先强制卖出通道 — 与信号层 AND 过滤 (v2/v3, 已证帮倒忙) 的
         本质区别: 老仓位在熊市日也被清仓, 不再留在场内被反复止损放血。
    不含 market_switch 时返回 (entries, None)。

    主循环 (evaluate_spec) 与落盘复现脚本共用本函数, 保证逐位一致。
    """
    entries = build_signals(panel, spec["side"], spec["factors"])
    sw = spec.get("market_switch")
    if not sw:
        return entries, None
    sw = dict(sw)
    name = sw.pop("name")
    bull = FACTOR_FUNCS[name](panel, **sw)  # mkt_* 因子返回广播后的 bool 宽表
    return entries & bull, ~bull


# ═══════════════════════════════════════════════════════════════
# 回测封装 (run_cached 预取矩阵入口, 全程不触网)
# ═══════════════════════════════════════════════════════════════

def run_backtest(panel, entries_full, stop_cfg, bt_cfg=None,
                 bt_start: str = BT_START, bt_end: str = BT_END,
                 bear_mask=None):
    """喂 BacktestEngine.run_cached, 返回 BacktestResult (dict-like, ["metrics"] 取指标)。

    矩阵口径与 engine.run() 内部准备段一致 (backtest/engine.py:_prepare_run_matrices):
    - close 窗口内 ffill (市值计价), close_raw 保留停牌 NaN → 退市/停牌检测
    - high/low ffill; open 不 ffill (停牌日 NaN, T+1 买入自然跳过)
    - entries 为 行=窗口日期×列=股票 的 bool 表, 信号日 T 收盘买入 (业务铁律)
    - bear_mask (可选, 同形状 bool 表): True=该日强制清仓, 走 formula_exit
      绝对优先通道 (reason=12, 先于一切止盈止损, 全仓卖)。lag_bars=0 即
      熊市信号日当天收盘价成交 — 与平台入场铁律"信号用 T 收盘算、T 收盘
      成交"同一口径, 无前视; ratio=1.0 全清。语义实现见
      backtest/loop/absolute.py (触发 signal[i-lag,ci], 执行价=当根 Close)。
    """
    from backtest.engine import BacktestEngine
    from backtest.prepared import PreparedMatrix

    eng = BacktestEngine(bt_cfg or DEFAULT_BT_CFG)
    close_raw = panel["close"].loc[bt_start:bt_end]
    close_ff = close_raw.ffill()
    entries = entries_full.reindex(
        index=close_ff.index, columns=close_ff.columns, fill_value=False).astype(bool)
    high_np = panel["high"].loc[bt_start:bt_end].ffill().to_numpy(np.float64)
    low_np = panel["low"].loc[bt_start:bt_end].ffill().to_numpy(np.float64)
    open_np = panel["open"].loc[bt_start:bt_end].to_numpy(np.float64)

    # 熊市强制清仓矩阵 (对齐方式与 entries/close 完全一致)
    formula_exit_np = None
    if bear_mask is not None:
        formula_exit_np = bear_mask.reindex(
            index=close_ff.index, columns=close_ff.columns,
            fill_value=False).to_numpy(dtype=bool)

    # 阶梯止盈数组 (升序, 引擎对非升序只告警不重排)
    levels = sorted(stop_cfg.get("ladder_tp", {}).get("levels", []),
                    key=lambda x: x.get("profit", 0))
    ladder_profits = np.array([lv["profit"] for lv in levels], dtype=np.float64)
    ladder_ratios = np.array([lv["sell_ratio"] for lv in levels], dtype=np.float64)

    prepared = PreparedMatrix(
        close=close_ff, entries=entries, high_np=high_np, low_np=low_np,
        open_np=open_np)
    return eng.run_cached(
        prepared, stop_cfg, ladder_profits, ladder_ratios, len(levels),
        close_raw=close_raw,
        formula_exit_np=formula_exit_np,
        formula_exit_ratio=1.0,      # 全仓清
        formula_exit_lag_bars=0,     # 熊市信号日当天收盘成交 (见 docstring)
    )
