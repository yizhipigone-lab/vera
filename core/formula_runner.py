"""公式执行层 — 封装 TDX 原生条件选股和指标计算公式。

T-H-2 (2026-07-15): 加 connector seam (set_connector/reset_connector/_connector),
与 DataFetcher 的 C5 seam 模式一致, 支持 mock 集成测试.
"""

from typing import Dict, List, Optional
import pandas as pd

from core.connector import TdxConnector
from core.dividend_type import to_formula_int
from utils.logger import get_logger
from utils.code_normalizer import normalize_list

logger = get_logger(__name__)

# T-H-2 connector seam
_connector_override = None


class FormulaRunner:
    """TDX 公式执行封装。支持条件选股 (XG) 和指标计算 (ZB)。"""

    _connector_override = None

    @classmethod
    def set_connector(cls, connector):
        """注入 mock connector (测试用)."""
        cls._connector_override = connector

    @classmethod
    def reset_connector(cls):
        """恢复默认 TdxConnector."""
        cls._connector_override = None

    @classmethod
    def _connector(cls):
        if cls._connector_override is not None:
            return cls._connector_override
        return TdxConnector

    @staticmethod
    def _ensure_ready():
        conn = FormulaRunner._connector()
        conn.ensure_connected()

    @classmethod
    def run_stock_selection_with_dates(
        cls,
        formula_name: str,
        formula_arg: str = "",
        stock_list: Optional[List[str]] = None,
        start_time: str = "",
        end_time: str = "",
        stock_period: str = "1d",
        dividend_type: int = 1,
    ) -> pd.DataFrame:
        """
        执行条件选股，返回真实日期的 DataFrame。

        使用 return_date=True 直接在 TDX 服务端获取入选日期。
        只需要 stock 代码为纯字符串（如 '600519.SH'），TDX 自动加载所需 K 线。

        Returns:
            DataFrame with columns: stock_code, select_date, formula_name
        """
        cls._ensure_ready()
        tq = cls._connector().tq()
        # 候选 D: 边界归一化, 允许 str 输入 (旧调用方传 "front" 也能正确映射到 1)
        dividend_type = to_formula_int(dividend_type)

        if stock_list is None:
            raw = tq.get_stock_list("50", list_type=1)
            stock_list = [s["Code"] if isinstance(s, dict) else str(s) for s in raw]

        # 确保所有代码都是纯字符串
        str_codes = []
        for s in stock_list:
            if isinstance(s, dict):
                str_codes.append(s.get("Code", ""))
            else:
                str_codes.append(str(s))
        str_codes = [c for c in str_codes if c]

        if not str_codes:
            return pd.DataFrame(columns=["stock_code", "select_date", "formula_name"])

        logger.info(
            f"选股 [{formula_name}] arg={formula_arg} "
            f"pool={len(str_codes)} range={start_time}~{end_time}"
        )

        # 分批执行，避免 "返回数据过大" 错误
        # A2 修复: 300 → 100, GUPIAO_012 实测 17/18 批报"返回数据过大",
        # 信号被截断导致累计收益被低估. 100 只/批牺牲时间换稳定性.
        BATCH_SIZE = 100
        all_records = []
        batch_errors = 0
        total_batches = (len(str_codes) - 1) // BATCH_SIZE + 1

        # count 决定 TDX 从 end_time 往前扫多少根 bar
        # 500 根 ≈ 2年，2022-2025 跨越 ~1000 个交易日，需足够大
        # 保守取 3000（覆盖 ~12年日线），让 TDX 扫全量历史数据
        count = 3000

        for batch_start in range(0, len(str_codes), BATCH_SIZE):
            batch = str_codes[batch_start:batch_start + BATCH_SIZE]
            batch_num = batch_start // BATCH_SIZE + 1
            logger.info(f"  批次 {batch_num}/{total_batches} ({len(batch)} stocks)")

            try:
                result = tq.formula_process_mul_xg(
                    formula_name=formula_name,
                    formula_arg=formula_arg,
                    return_count=0,
                    return_date=True,
                    stock_list=batch,
                    stock_period=stock_period,
                    start_time=start_time,
                    end_time=end_time,
                    count=count,
                    dividend_type=dividend_type,
                )

                if not result:
                    batch_errors += 1
                    continue

                error_id = result.get("ErrorId", "0")
                error_msg = result.get("Error", "")
                if error_id not in ("0", "19"):
                    batch_errors += 1
                    if batch_num == 1 and "不存在" in str(error_msg):
                        logger.error(f"选股公式 [{formula_name}] 不存在，请检查公式名称是否正确")
                        break  # 公式不存在，无需继续
                    continue

            except Exception as e:
                batch_errors += 1
                continue

            # 解析: {stock_code: {indicator_name: [{'Date': '20240603', 'Value': '1'}, ...]}}
            for stock_code, val in result.items():
                if stock_code == "ErrorId" or not val or not isinstance(val, dict):
                    continue
                for entries in val.values():
                    if not isinstance(entries, list):
                        continue
                    for entry in entries:
                        if not isinstance(entry, dict):
                            continue
                        if str(entry.get("Value", "")) != "1":
                            continue
                        date_str = str(entry.get("Date", ""))
                        if not date_str:
                            continue
                        # TDX API 返回全部 bar 的匹配，需过滤到请求的时间范围
                        if start_time and date_str < start_time:
                            continue
                        if end_time and date_str > end_time:
                            continue
                        try:
                            dt = pd.to_datetime(date_str, format="%Y%m%d")
                        except (ValueError, TypeError):
                            try:
                                dt = pd.to_datetime(date_str, format="%Y%m%d%H%M%S")
                            except (ValueError, TypeError):
                                continue
                        all_records.append({
                            "stock_code": stock_code,
                            "select_date": dt,
                            "formula_name": formula_name,
                        })
                    break

        if not all_records:
            if batch_errors >= total_batches:
                logger.error(f"所有 {total_batches} 批次均失败，请检查公式名称 [{formula_name}] 是否存在")
            else:
                logger.warning("选股结果解析后为空")
            return pd.DataFrame(columns=["stock_code", "select_date", "formula_name"])

        df = pd.DataFrame(all_records)
        df["select_date"] = pd.to_datetime(df["select_date"])
        df = df.drop_duplicates(subset=["stock_code", "select_date"])
        df = df.sort_values(["select_date", "stock_code"]).reset_index(drop=True)
        logger.info(f"选股完成: {len(df)} 条记录, {df['stock_code'].nunique()} 只股票")
        return df

    @classmethod
    def run_indicator(
        cls,
        formula_name: str,
        formula_arg: str = "",
        stock_list: Optional[List[str]] = None,
        start_time: str = "",
        end_time: str = "",
        stock_period: str = "1d",
        dividend_type: int = 1,
        return_count: int = 1,
        return_date: bool = False,
        xsflag: int = -1,
    ) -> Dict[str, List]:
        """
        批量执行 TDX 指标公式。

        Returns:
            dict: {stock_code: [[indicator_values]]}
        """
        cls._ensure_ready()
        # 候选 D: 边界归一化, 允许 str 输入
        dividend_type = to_formula_int(dividend_type)
        tq = cls._connector().tq()

        if stock_list is None:
            stock_list = tq.get_stock_list("50", list_type=1)
        else:
            stock_list = normalize_list(stock_list)

        logger.info(f"执行指标公式 [{formula_name}] 参数={formula_arg}")

        try:
            result = tq.formula_process_mul_zb(
                formula_name=formula_name,
                formula_arg=formula_arg,
                return_count=return_count,
                return_date=return_date,
                xsflag=xsflag,
                stock_list=stock_list,
                stock_period=stock_period,
                start_time=start_time,
                end_time=end_time,
                dividend_type=dividend_type,
            )

            if not result or result.get("ErrorId", "") not in ["0", "19"]:
                logger.error(f"指标公式执行失败: {result.get('Error', '未知') if result else '返回为空'}")
                return {}

            return result.get("Value", {})
        except Exception as e:
            logger.error(f"指标公式执行异常: {e}")
            return {}

    @classmethod
    def selection_to_dataframe(
        cls,
        selection_result: Dict[str, List[str]],
        formula_name: str = "",
    ) -> pd.DataFrame:
        """
        将选股结果转换为标准化 DataFrame。

        Returns:
            DataFrame with columns: stock_code, select_date, formula_name
        """
        records = []
        for stock_code, dates in selection_result.items():
            for date_str in dates:
                try:
                    dt = pd.to_datetime(date_str, format="%Y%m%d")
                except (ValueError, TypeError):
                    try:
                        dt = pd.to_datetime(date_str, format="%Y%m%d%H%M%S")
                    except (ValueError, TypeError):
                        dt = pd.to_datetime(date_str)

                records.append({
                    "stock_code": stock_code,
                    "select_date": dt,
                    "formula_name": formula_name,
                })

        if not records:
            return pd.DataFrame(columns=["stock_code", "select_date", "formula_name"])

        df = pd.DataFrame(records)
        df["select_date"] = pd.to_datetime(df["select_date"])
        df = df.drop_duplicates(subset=["stock_code", "select_date"])
        return df.sort_values(["select_date", "stock_code"]).reset_index(drop=True)
