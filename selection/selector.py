"""选股引擎 — 调度 TDX 条件选股公式执行，输出标准化选股结果。"""

from datetime import datetime
from typing import List, Optional

import pandas as pd

from core.data_fetcher import DataFetcher
from core.formula_runner import FormulaRunner
from core.stock_filter import filter_stocks
from utils.code_normalizer import normalize_list
from utils.logger import get_logger

logger = get_logger(__name__)

# 股票池类型映射
UNIVERSE_TYPE_MAP = {
    "all_a": "5",
    "hs300": "23",
    "zz500": "24",
    "zz1000": "25",
    "zzA500": "28",
    "chuangyeban": "51",
    "kechuang": "52",
    "beijingsuo": "53",
    "hs_a": "50",
    "etf": "31",
}

# ETF 基金的 TDX list_type (原生分类, 天然含 51/56/58/511, 排除 501/508 LOF)
ETF_LIST_TYPE = "31"


def _merge_etf(stocks: List[str]) -> List[str]:
    """拉 ETF 池 (list_type='31') 并与现有股票池合并去重。"""
    etf_stocks = DataFetcher.get_stock_universe(ETF_LIST_TYPE)
    return list(set(stocks) | set(etf_stocks))


class StockSelector:
    """
    选股引擎。

    Parameters:
        config: 策略配置中的 selection 部分
    """

    def __init__(self, config: dict):
        self.formula_name = config["formula_name"]
        self.formula_arg = config.get("formula_arg", "")
        self.universe_config = config.get("universe", {"type": "50"})
        self.period = config.get("period", "1d")
        self.dividend_type = config.get("dividend_type", 1)

    def resolve_universe(self) -> List[str]:
        """根据 universe 配置解析股票池。"""
        from core import progress as _progress
        from selection import universe_cache as _ucache
        u = self.universe_config
        utype = u.get("type", "")
        _progress.report("universe_list", 0.0, "解析股票池...")  # 2026-07-26

        # 自定义列表 (配置即数据, 无计算成本, 不进 L1 缓存)
        if utype == "custom":
            stocks = u.get("stocks", [])
            return normalize_list(stocks)

        # 2026-07-26: L1 池缓存 (计划书 §3.1)。命中省 ~17s 拉池+ST过滤;
        # 异常回退实算, 不中断选股。
        _l1_key = None
        if _ucache.ENABLED:
            try:
                _l1_key = _ucache.build_key(u, datetime.now().strftime("%Y%m%d"))
                if not _ucache.FORCE_REFRESH:
                    _cached = _ucache.load(_ucache.default_cache_root(), _l1_key)
                    if _cached is not None:
                        # 2026-07-27 投毒事件防线: 非 custom 池 <10 只极可能是
                        # 污染/误存 (正常池数十~数千只), 仅警告不拦截 (合法小板块存在)
                        if len(_cached) < 10:
                            logger.warning(
                                "池缓存命中但仅 %d 只 (key=%s) — 疑似异常小池, "
                                "如非预期请清理 data/universe_cache 后重跑",
                                len(_cached), _l1_key[:12])
                        _progress.report("universe_list", 1.0, f"股票池 {len(_cached)} 只 (缓存)")
                        return _cached
            except Exception as e:
                logger.warning("池缓存读取异常 (回退实算): %s", e)

        # P-v3.4: ETF 开关 — 仅ETF 优先于 包含ETF
        #   list_type='31' = ETF 基金 (TDX 原生分类, 天然含 51/56/58/511, 排除 501/508 LOF)
        etf_only = bool(u.get("etf_only", False))
        include_etf = bool(u.get("include_etf", False))
        # P-v3.4: 行业板块 (可多选, 代码列表) — 与 ETF 开关叠加
        sectors = u.get("sectors", []) or []

        if etf_only:
            # 仅 ETF 池 (优先级最高, 忽略 sectors)
            stocks = DataFetcher.get_stock_universe(ETF_LIST_TYPE)
            logger.info(f"仅ETF模式: list_type=31, 拉到 {len(stocks)} 只 ETF")
        elif sectors:
            # 选了行业板块 — 拉每个板块成份股并集
            logger.info(f"已选 {len(sectors)} 个行业板块, 股票池下拉框 (type={utype}) 被忽略, 仅用板块并集")
            stocks = []
            for i, code in enumerate(sectors):
                _progress.report("universe_list", (i + 1) / len(sectors),
                                 f"板块 {i + 1}/{len(sectors)}", i + 1, len(sectors))
                sector_stocks = DataFetcher.get_sector_stocks(code)
                logger.info(f"拉取板块成份股 [{i+1}/{len(sectors)}]: {code} ({len(sector_stocks)} 只)")
                stocks.extend(sector_stocks)
            stocks = list(set(stocks))  # 并集去重
            logger.info(f"板块并集: {len(stocks)} 只 (来自 {len(sectors)} 个板块)")
            # ETF 叠加
            if include_etf:
                stocks = _merge_etf(stocks)
                logger.info(f"叠加 ETF: {len(stocks)} 只")
        else:
            # A股池 (下拉框)
            list_type = UNIVERSE_TYPE_MAP.get(str(utype), utype)
            stocks = DataFetcher.get_stock_universe(list_type)
            if include_etf:
                stocks = _merge_etf(stocks)
                logger.info(f"包含ETF模式: A股 + ETF → 合并 {len(stocks)}")

        # 2026-07-23: 北交所口径过滤 — 板块成份股可能含 .BJ (实测 881008 含 920088.BJ),
        # 仅"全部A股/北交所"口径保留, 其余 (沪深A股/沪深300/板块等) 一律剔除。
        # 对 TDX 本就干净的池 ('50' 实测 0 BJ) 是防御性 no-op。
        _BJ_OK_TYPES = {"5", "all_a", "53", "beijingsuo"}
        if str(utype) not in _BJ_OK_TYPES:
            before = len(stocks)
            stocks = [c for c in stocks if not str(c).upper().endswith(".BJ")]
            if len(stocks) < before:
                logger.info(f"剔除北交所: {before} → {len(stocks)} (universe type={utype})")

        if not stocks:
            logger.warning(f"股票池 {utype} 返回空，请检查 TDX 客户端数据")
            return []

        # 过滤 ST / 退市 / 港股（P0-3: 改用 TDX IsSTGP 真实判定，原字符串过滤对纯代码恒 True）
        # 注: ETF 的 IsSTGP=0, 不会被误删; ST 过滤保持现状
        # 2026-07-25: exclude_quit 可配置 (默认 True 维持原行为) — 样本外回测
        # 置 False 保留已退市股, 缓解幸存者偏差 (股票池快照为今日, 不含退市股
        # 会系统性高估历史收益; 引擎有退市强平 reason=11 兜底)
        if u.get("exclude_st", False):
            before = len(stocks)
            stocks, excluded = filter_stocks(
                stocks, exclude_quit=bool(u.get("exclude_quit", True)))
            if excluded:
                logger.info(f"ST/退市/港股过滤: {before} → {len(stocks)}（剔除 {len(excluded)} 只）")

        # 过滤次新股
        exclude_new = u.get("exclude_new_listings_days", 0)
        if exclude_new > 0:
            logger.warning(f"exclude_new_listings_days={exclude_new} "
                           "— TDX get_stock_list 暂不支持按上市天数过滤，此选项被忽略")

        if etf_only:
            mode = "仅ETF"
        elif sectors:
            mode = "板块" + ("+ETF" if include_etf else "")
        else:
            mode = "A股+ETF" if include_etf else "A股"
        logger.info(f"解析股票池: {len(stocks)} 只股票 (type={utype}, mode={mode})")
        _progress.report("universe_list", 1.0, f"股票池 {len(stocks)} 只")  # 2026-07-26
        result = normalize_list(stocks)
        # 2026-07-26: L1 落盘 (空池不缓存 — 多半是 TDX 数据问题, 值得每次重试)
        if _l1_key is not None and result:
            try:
                _ucache.save(_ucache.default_cache_root(), _l1_key, result)
            except Exception as e:
                logger.warning("池缓存保存失败 (不中断选股): %s", e)
        return result

    def run(
        self,
        start_time: str = "",
        end_time: str = "",
        stock_list: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        """
        执行选股。

        Args:
            start_time: 起始时间 YYYYMMDD
            end_time: 结束时间 YYYYMMDD
            stock_list: 自定义股票池，为 None 则从 universe 配置解析

        Returns:
            DataFrame with columns: stock_code, select_date, formula_name
        """
        if stock_list is None:
            stock_list = self.resolve_universe()

        if not stock_list:
            logger.warning("股票池为空，选股终止")
            return pd.DataFrame(columns=["stock_code", "select_date", "formula_name"])

        if not end_time:
            end_time = datetime.now().strftime("%Y%m%d")

        # 2026-07-26: L2 按日信号缓存 (计划书 §3.2) — 接缝在 selector 内部,
        # pipeline/tools 全部自动受益; 仅 period=1d。
        # 异常回退直跑, 不中断选股。
        from selection import signal_day_cache as _sdc
        if _sdc.ENABLED and self.period == "1d" and start_time:
            try:
                return _sdc.get_or_compute(
                    formula_name=self.formula_name,
                    formula_arg=self.formula_arg,
                    period=self.period,
                    dividend_type=self.dividend_type,
                    stock_list=stock_list,
                    start_time=start_time,
                    end_time=end_time,
                    force=_sdc.FORCE_REFRESH,
                )
            except Exception as e:
                logger.warning("L2 按日缓存异常 (回退直跑): %s", e)

        df = FormulaRunner.run_stock_selection_with_dates(
            formula_name=self.formula_name,
            formula_arg=self.formula_arg,
            stock_list=stock_list,
            start_time=start_time,
            end_time=end_time,
            stock_period=self.period,
            dividend_type=self.dividend_type,
        )
        return df

    def run_cached(
        self,
        start_time: str = "",
        end_time: str = "",
        *,
        cache_enabled: bool = True,
        l2_enabled: bool = True,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """带 L0 整段缓存的选股入口 (收编自 pipeline.step1_select 的直连代码)。

        原 pipeline 直连 selection_cache 拼 key / 注入 today_str / load / save
        的整段缓存 (L0) 收进 selector 内部, pipeline 只传 selection_cache 开关。

        语义与旧 pipeline 字节级等价:
        - L0 只服务 period≠1d (1d 由 run() 内 L2 按日信号缓存接管, 键更精确
          含 pool_hash、支持子区间命中); L2 被配置关闭时 1d 仍走 L0, 不留空档。
        - key 含 today_str (按日失效); force_refresh 跳过查找强制重跑。
        - 任何缓存异常回退直跑 + warning, 不中断选股; 空结果不缓存。
        """
        use_sel_cache = cache_enabled and (self.period != "1d" or not l2_enabled)
        key = None
        picks = None

        if use_sel_cache:
            try:
                from selection import selection_cache as sc
                key = sc.build_key(
                    formula_name=self.formula_name,
                    formula_arg=self.formula_arg,
                    universe_cfg=self.universe_config,
                    start_time=start_time, end_time=end_time,
                    period=self.period,
                    dividend_type=self.dividend_type,
                    today_str=datetime.now().strftime("%Y%m%d"),
                )
                if not force_refresh:
                    picks = sc.load(sc.default_cache_root(), key)
                else:
                    logger.info("选股缓存 force_refresh: 跳过查找, 强制重跑")
            except Exception as e:
                logger.warning("选股缓存读取异常 (回退直跑): %s", e)
                picks = None

        if picks is None:
            stocks = self.resolve_universe()
            picks = self.run(start_time=start_time, end_time=end_time,
                             stock_list=stocks)
            if use_sel_cache and key is not None and not picks.empty:
                try:
                    from selection import selection_cache as sc
                    sc.save(sc.default_cache_root(), key, picks)
                except Exception as e:
                    logger.warning("选股缓存保存失败 (不中断管线): %s", e)

        return picks
