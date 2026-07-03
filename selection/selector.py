"""选股引擎 — 调度 TDX 条件选股公式执行，输出标准化选股结果。"""

import pandas as pd
from typing import List, Optional
from datetime import datetime

from core.data_fetcher import DataFetcher
from core.formula_runner import FormulaRunner
from core.stock_filter import filter_stocks
from utils.logger import get_logger
from utils.code_normalizer import normalize_list

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
        u = self.universe_config
        utype = u.get("type", "")

        # 自定义列表
        if utype == "custom":
            stocks = u.get("stocks", [])
            return normalize_list(stocks)

        # 预定义类型映射
        list_type = UNIVERSE_TYPE_MAP.get(str(utype), utype)
        stocks = DataFetcher.get_stock_universe(list_type)

        if not stocks:
            logger.warning(f"股票池 {utype} 返回空，请检查 TDX 客户端数据")
            return []

        # 过滤 ST / 退市 / 港股（P0-3: 改用 TDX IsSTGP 真实判定，原字符串过滤对纯代码恒 True）
        if u.get("exclude_st", False):
            before = len(stocks)
            stocks, excluded = filter_stocks(stocks)
            if excluded:
                logger.info(f"ST/退市/港股过滤: {before} → {len(stocks)}（剔除 {len(excluded)} 只）")

        # 过滤次新股
        exclude_new = u.get("exclude_new_listings_days", 0)
        if exclude_new > 0:
            logger.warning(f"exclude_new_listings_days={exclude_new} "
                           "— TDX get_stock_list 暂不支持按上市天数过滤，此选项被忽略")

        logger.info(f"解析股票池: {len(stocks)} 只股票 (type={utype})")
        return normalize_list(stocks)

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


class MultiFormulaSelector:
    """多公式并行选股。"""

    def __init__(self, formulas_config: List[dict]):
        """
        Args:
            formulas_config: 多个公式配置列表
                [{formula_name, formula_arg, universe, period}, ...]
        """
        self.selectors = [StockSelector(cfg) for cfg in formulas_config]

    def run_all(
        self,
        start_time: str = "",
        end_time: str = "",
    ) -> pd.DataFrame:
        """依次执行所有公式选股，合并去重。"""
        results = []
        for i, sel in enumerate(self.selectors):
            logger.info(f"执行第 {i+1}/{len(self.selectors)} 个选股公式: {sel.formula_name}")
            df = sel.run(start_time=start_time, end_time=end_time)
            if not df.empty:
                results.append(df)

        if not results:
            return pd.DataFrame(columns=["stock_code", "select_date", "formula_name"])

        merged = pd.concat(results, ignore_index=True)
        merged = merged.drop_duplicates(subset=["stock_code", "select_date", "formula_name"])
        merged = merged.sort_values(["select_date", "stock_code"]).reset_index(drop=True)
        logger.info(f"多公式选股合并: {len(merged)} 条记录")
        return merged
