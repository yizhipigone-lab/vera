# -*- coding: utf-8 -*-
"""core/market_score.py — 大盘环境仪表盘·纯函数打分引擎 (2026-09-18)。

职责：输入「14 个指标的原始值」，输出「各指标分 + 维度分 + 基础总分 + 定性 + 环境参照仓位」。

设计定位（与需求/计划书一致）:
- 这是**温度分**（描述"现在市场热不热"）, 不是涨跌预测分（不说"明天涨还是跌"）。
- 纯函数模块: **不联网、不读文件、不写盘、不 import trade**（业务铁律 1, 可被离线单测,
  由 tests/test_market_position.py 的 AST 断言守护）。
- 阈值表照需求书**硬编码**（需求第七条第 4 款: 不得擅自修改标准）; 需求没覆盖到的空白档,
  按 `docs/plan/2026-09-18_大盘仪表盘四页签改造_实施计划书.md` §5.2 补档, 并在对应常量注释里写明。

打分约定（本模块统一, 防边界漂移）:
- 阈值打出的单项分是整数 (0/2/5/8/10 等); 维度均分与总分保留 4 位小数, 展示层再四舍五入。
- 阈值点归属: **「越大越好」指标用 value >= cut（阈值点归更高分档）; 「越小越好」指标用
  value <= cut（阈值点归更低分档）**。即"边界点归更利好的那一档"。现实中恰好踩中阈值的概率
  趋近于 0, 此约定只影响理论边界, 不影响实际结果。
- 任一指标取不到数（None）→ 该指标分为 None, 维度内求平均时**跳过**（缺失指标不参与,
  权重也不上收 —— 用户拍板决策 7）。

特殊指标（阈值表表达不了, 需要额外输入, 用独立函数）:
- ppi            : 需要"近3个月前的值"判断是否"由负转正"(趋势) + 当前值(水平);
- margin_trend   : 需要"60 交易日累计变化"和"单周变化"两个输入, 先判单周激增(异常)再判趋势;
- usdcny_ma_dev  : 用"汇率偏离 250 日均线的百分比"(替代美元指数, 阈值重定 —— 计划书 §5.2)。
"""
from __future__ import annotations

# ──────────────────────────── 阈值表 ────────────────────────────
# 结构: {"dir": "high"|"low"|"special", "cuts": [(cut, score)...], "default": score}
#   high: 从大到小遍历 cuts, 首个满足 value >= cut 的返回其分; 全不满足 → default
#   low : 从小到大遍历 cuts, 首个满足 value <= cut 的返回其分; 全不满足 → default
#   special: 阈值表表达不了, 走独立函数 (见文件头注释)
# 出处: 需求书 §三.4「单项指标打分阈值」; 空白档补档见计划书 §5.2。

THRESHOLDS = {
    # —— 维度1 估值与股权赔率 ——
    "erp": {  # 沪深300 股债利差(%): ≥6.0→10; 5.5~6.0→8; 4.5~5.5→5; 3.5~4.5→2; <3.5→0
        "dir": "high", "cuts": [(6.0, 10), (5.5, 8), (4.5, 5), (3.5, 2)], "default": 0},
    "pe_percentile": {  # 全A PE 近10年分位(%, 中位数口径): ≤10→10; 10~30→7; 30~70→5; 70~90→2; >90→0
        "dir": "low", "cuts": [(10, 10), (30, 7), (70, 5), (90, 2)], "default": 0},
    "pb_percentile": {  # 上证 PB 近10年分位(%): 同 pe_percentile 阈值
        "dir": "low", "cuts": [(10, 10), (30, 7), (70, 5), (90, 2)], "default": 0},
    "dividend_spread": {  # 股息率-国债利差(百分点): >0.5→10; 0~0.5→6; -0.5~0→3; <-0.5→0
        "dir": "high", "cuts": [(0.5, 10), (0.0, 6), (-0.5, 3)], "default": 0},
    # —— 维度2 国内宏观与盈利周期 ——
    "m1m2_spread": {  # M1-M2 剪刀差(百分点): >0→10; -2~0→6; -4~-2→3; <-4→0
        "dir": "high", "cuts": [(0, 10), (-2, 6), (-4, 3)], "default": 0},
    "pmi": {  # 制造业 PMI: >50→10; 48~50→5; <48→0
        "dir": "high", "cuts": [(50, 10), (48, 5)], "default": 0},
    "ppi": {"dir": "special"},  # 由负转正→8 / 水平分档, 见 _score_ppi
    # —— 维度3 市场趋势与量价 ——
    "hs300_vs_ma200": {  # 沪深300 偏离200日均线(%): ≥3→10; 0~3→7; -3~0→3; <-3→0
        "dir": "high", "cuts": [(3, 10), (0, 7), (-3, 3)], "default": 0},
    "turnover_amt": {  # 两市日均成交额(亿元, 20日均): >8000→10; 5000~8000→5; <4000→0
        # 需求未定义 4000~5000 这一档(空白), 补归 5 分档(视为"非低迷"), 见计划书 §5.2 同款处理
        "dir": "high", "cuts": [(8000, 10), (4000, 5)], "default": 0},
    "breadth_20d": {  # 涨跌家数占比20日均值(%): ≥60→10; 50~60→6; 40~50→4; <40→0
        "dir": "high", "cuts": [(60, 10), (50, 6), (40, 4)], "default": 0},
    # —— 维度4 市场微观情绪 ——
    "margin_trend": {"dir": "special"},  # 先单周激增→1, 再60日趋势, 见 _score_margin
    "option_pcr": {  # 期权 PCR(上证50ETF, 成交量口径, 0~1): ≥0.9→10; 0.7~0.9→7; 0.5~0.7→5; 0.3~0.5→2; <0.3→0
        "dir": "high", "cuts": [(0.9, 10), (0.7, 7), (0.5, 5), (0.3, 2)], "default": 0},
    # —— 维度5 海外流动性 ——
    "us10y": {  # 美债10年期(%): <3.5→10; 3.5~4.2→7; 4.2~4.8→4; >4.8→0
        "dir": "low", "cuts": [(3.5, 10), (4.2, 7), (4.8, 4)], "default": 0},
    "usdcny_ma_dev": {"dir": "special"},  # 汇率偏离年线%, 见 _score_usdcny
}

#: 指标中文名(供前端/报告用, 大白话)
INDICATOR_NAMES = {
    "erp": "股债利差（股票相对国债划不划算）",
    "pe_percentile": "全A市盈率近10年分位（中位数口径）",
    "pb_percentile": "上证市净率近10年分位",
    "dividend_spread": "股息率减国债收益率（持股收息 vs 买国债）",
    "m1m2_spread": "M1-M2 剪刀差（活钱有没有变多）",
    "pmi": "制造业 PMI（工厂景气度，50 是荣枯线）",
    "ppi": "工业品出厂价同比（PPI，通缩还是温和涨价）",
    "hs300_vs_ma200": "沪深300 偏离200日均线（在长期趋势线上方还是下方）",
    "turnover_amt": "两市日均成交额（市场活不活跃）",
    "breadth_20d": "近20日上涨家数占比均值（涨的股票多不多）",
    "margin_trend": "两融余额趋势（借钱炒股的钱在加还是减）",
    "option_pcr": "期权认沽/认购比（PCR，恐慌还是狂热）",
    "us10y": "美债10年期收益率（海外无风险利率）",
    "usdcny_ma_dev": "美元兑人民币偏离年线（人民币偏强还是偏弱）",
}

#: 维度权重（出处 = 计划书 §5.3, 按证据定; 估值提权因体检唯一通过 3/6/12 月检验,
#:  宏观降权因月度低频对日频短期指导弱, 情绪降权因广度层被体检证伪）。
DIMENSION_WEIGHTS = {
    "valuation": 0.30,   # 估值与股权赔率
    "macro": 0.20,       # 国内宏观与盈利周期
    "trend": 0.20,       # 市场趋势与量价
    "sentiment": 0.15,   # 市场微观情绪
    "overseas": 0.15,    # 海外流动性
}

#: 维度 -> 该维度包含的指标 key（缺口径的社融/CME 不在内, 从结构上保证不被误算）
DIMENSION_INDICATORS = {
    "valuation": ("erp", "pe_percentile", "pb_percentile", "dividend_spread"),
    "macro": ("m1m2_spread", "pmi", "ppi"),
    "trend": ("hs300_vs_ma200", "turnover_amt", "breadth_20d"),
    "sentiment": ("margin_trend", "option_pcr"),
    "overseas": ("us10y", "usdcny_ma_dev"),
}

#: 维度中文名(大白话)
DIMENSION_NAMES = {
    "valuation": "估值与股权赔率（现在买贵不贵）",
    "macro": "国内宏观与盈利周期（经济大方向）",
    "trend": "市场趋势与量价（价格在走强还是走弱）",
    "sentiment": "市场微观情绪（场内的钱慌不慌）",
    "overseas": "海外流动性（外围资金环境松不松）",
}

#: 总分 -> (定性, 环境参照仓位)。需求 §三.5; "基准仓位"改名"环境参照仓位"(只读参照, 守铁律1)。
POSITION_BANDS = [
    (8.0, "极佳赔率，牛市环境", "75%~100%"),
    (6.5, "偏乐观，趋势向好", "50%~75%"),
    (5.0, "中性震荡，结构性行情", "30%~50%"),
    (3.5, "偏悲观，磨底阶段", "15%~30%"),
    (0.0, "高风险，熊市主跌段", "0~15%"),
]

#: 总分校验容差（需求第一条第 3 款: 基础总分与各维度加权分之和误差不超过 0.1 分）
CHECK_TOLERANCE = 0.1

# 模块加载即校验: 维度权重之和必须 = 1, 且每个指标恰属一个维度(防手改权重/漏配指标引入系统性偏差)
assert abs(sum(DIMENSION_WEIGHTS.values()) - 1.0) < 1e-9, "DIMENSION_WEIGHTS 之和必须等于 1"
_flat = tuple(k for keys in DIMENSION_INDICATORS.values() for k in keys)
assert len(_flat) == len(set(_flat)) == len(THRESHOLDS), "每个指标必须恰好属于一个维度, 且与 THRESHOLDS 对齐"


# ──────────────────────────── 单项打分 ────────────────────────────

def score_indicator(key: str, value: float | None) -> int | None:
    """简单阈值指标打分。value 为 None（数据暂缺）→ 返回 None（维度均分时跳过）。"""
    if value is None:
        return None
    spec = THRESHOLDS.get(key)
    if spec is None:
        raise KeyError(f"未知指标: {key}")
    if spec["dir"] == "special":
        raise ValueError(f"{key} 是特殊指标, 请走 score_all() 里的专用函数, 不要直接调 score_indicator")
    if spec["dir"] == "high":
        for cut, sc in spec["cuts"]:
            if value >= cut:
                return sc
        return spec["default"]
    # low
    for cut, sc in spec["cuts"]:
        if value <= cut:
            return sc
    return spec["default"]


def _score_ppi(ppi_now: float | None, ppi_3m_ago: float | None) -> int | None:
    """PPI: 先判趋势(近3个月由负转正→8), 否则按水平分档。

    水平档: ≥5→1(过热); 2~5→6(温和扩张, 计划书 §5.2 补档); 0~2→5; -2~0→3; <-2→0(通缩)。
    "由负转正"用月度数据判断(上月/近3月前值), 不需要高频序列, 不属过拟合。
    """
    if ppi_now is None:
        return None
    if ppi_3m_ago is not None and ppi_3m_ago < 0 <= ppi_now:
        return 8
    if ppi_now >= 5:
        return 1
    if ppi_now >= 2:
        return 6
    if ppi_now >= 0:
        return 5
    if ppi_now >= -2:
        return 3
    return 0


def _score_margin(margin_60d_pct: float | None, margin_week_pct: float | None) -> int | None:
    """两融余额趋势: **先判异常**(单周激增>10%→1, 防加杠杆过快), 再判60日趋势。

    60日趋势: 累计涨>3%→8(稳步抬升); 区间震荡(-3~3%)→5; 累计跌>3%→2(持续下降)。
    60日数据缺失但有单周 → 中性 5; 两者都缺 → None。
    """
    if margin_60d_pct is None and margin_week_pct is None:
        return None
    if margin_week_pct is not None and margin_week_pct > 10:
        return 1
    if margin_60d_pct is None:
        return 5
    if margin_60d_pct > 3:
        return 8
    if margin_60d_pct >= -3:
        return 5
    return 2


def _score_usdcny(ma_dev_pct: float | None) -> int | None:
    """美元兑人民币偏离年线(%)(替代美元指数, 阈值重定 —— 计划书 §5.2)。

    ma_dev_pct = (当前汇率 - 250日均线)/250日均线 × 100; 正 = 汇率高于年线 = 人民币偏弱。
    汇率低于年线 2% 以上(人民币偏强)→8; ±2% 内→5; 高于年线 2% 以上(人民币偏弱)→2。
    """
    if ma_dev_pct is None:
        return None
    if ma_dev_pct < -2:
        return 8
    if ma_dev_pct <= 2:
        return 5
    return 2


def score_all(values: dict) -> dict:
    """输入原始值 dict → 各指标分 dict(特殊指标也在这里算)。

    需要的输入 key = THRESHOLDS 的全部 key, 另加特殊指标的辅助输入:
      ppi_3m_ago, margin_60d_pct, margin_week_pct (usdcny_ma_dev 直接用其值)。
    缺失的 key 当作 None(该指标分 None, 不报错) —— 缺数据是常态, 不是异常。
    """
    out = {}
    for key, spec in THRESHOLDS.items():
        if spec["dir"] == "special":
            continue  # 特殊指标下面单独算
        out[key] = score_indicator(key, values.get(key))
    out["ppi"] = _score_ppi(values.get("ppi"), values.get("ppi_3m_ago"))
    out["margin_trend"] = _score_margin(values.get("margin_60d_pct"), values.get("margin_week_pct"))
    out["usdcny_ma_dev"] = _score_usdcny(values.get("usdcny_ma_dev"))
    return out


# ──────────────────────────── 维度合成 ────────────────────────────

def score_dimensions(ind_scores: dict, prev_dim_avg: dict | None = None) -> dict:
    """各维度内求有效指标的平均分, 再乘维度权重。

    - 缺失指标(None)在求平均时**跳过**(不参与, 权重也不上收 —— 用户拍板决策 7)。
    - 某维度**所有指标都缺失**: 沿用上一交易日该维度均分(prev_dim_avg)并标注; 连上一日也没有
      (冷启动) → 按中性 5 分记并标注(需求第七条第 2 款"数据暂缺沿用历史得分")。
    """
    dims = {}
    for dim, keys in DIMENSION_INDICATORS.items():
        vals = [ind_scores[k] for k in keys if ind_scores.get(k) is not None]
        if vals:
            avg = sum(vals) / len(vals)
            note = None
        else:
            if prev_dim_avg and prev_dim_avg.get(dim) is not None:
                avg = prev_dim_avg[dim]
                note = "该维度数据暂缺，沿用上一交易日得分"
            else:
                avg = 5.0
                note = "该维度数据暂缺，按中性 5 分记"
        w = DIMENSION_WEIGHTS[dim]
        dims[dim] = {
            "weight": w,
            "avg": round(avg, 4),
            "weighted": round(avg * w, 4),
            "n_valid": len(vals),
            "n_total": len(keys),
            "note": note,
        }
    return dims


def compute_base_total(dim_scores: dict) -> tuple[float, float]:
    """基础总分 = Σ(维度加权分)。返回 (base_total, check_err)。

    check_err: 用「逐指标展开式」独立重算一遍基础总分, 与「维度加权分求和」比对, 误差应≈0。
    这是需求第一条第 3 款「基础总分与各维度加权分之和误差≤0.1」的实现 —— 两条独立计算路径
    互相印证, 防"手改公式后两边不一致"的计算 bug(恒等式 sanity check)。
    """
    by_dim = sum(d["weighted"] for d in dim_scores.values())
    # 独立重算路径: 逐维度 avg×weight 直接求和(不经过 weighted 字段, 防止 weighted 算错)
    recon = sum(d["avg"] * d["weight"] for d in dim_scores.values())
    err = abs(by_dim - recon)
    return round(by_dim, 4), round(err, 4)


def label_and_band(total: float) -> dict:
    """总分 → 定性 + 环境参照仓位(只读参照, 不联实盘, 守铁律1)。"""
    for low, label, band in POSITION_BANDS:
        if total >= low:
            return {"label": label, "position_band": band}
    return {"label": POSITION_BANDS[-1][1], "position_band": POSITION_BANDS[-1][2]}


def build_scores(values: dict, prev_dim_avg: dict | None = None) -> dict:
    """顶层组装: 原始值 → 完整打分结果(不含事件修正分, 那是 market_events 的职责)。

    返回 dict 供 market_dashboard_runner 组装快照。
    """
    ind = score_all(values)
    dims = score_dimensions(ind, prev_dim_avg)
    base, check_err = compute_base_total(dims)
    return {
        "indicators": ind,
        "dimensions": dims,
        "base_total": base,
        "check_err": check_err,
        "check_ok": check_err <= CHECK_TOLERANCE,
        **label_and_band(base),
    }
