"""公式执行层 — 封装 TDX 原生条件选股和指标计算公式。

T-H-2 (2026-07-15): 加 connector seam (set_connector/reset_connector/_connector),
与 DataFetcher 的 C5 seam 模式一致, 支持 mock 集成测试.
"""

from typing import List, Optional

import pandas as pd

from core import progress as _progress
from core.connector import ConnectorSeam
from core.dividend_type import to_formula_int
from utils.code_normalizer import extract_codes
from utils.logger import get_logger

logger = get_logger(__name__)

# TDX 选股扫描深度: 从"当前日期"往前的 bar 数 (公式计算与命中返回都受此窗口约束)。
# 覆盖 ~12 年日线 (今天往前 3000 个交易日 ≈ 到 2014)。
_MAX_SCAN_COUNT = 3000


def _adaptive_scan_count(start_time: str, end_time: str, stock_period: str) -> int:
    """TDX 选股扫描深度 (恒返回 _MAX_SCAN_COUNT=3000)。

    2026-07-31 回退: 2026-07-23 引入的"区间跨度+预热"自适应算法有致命缺陷 ——
    它按 (end_time - start_time) 估算 count, 误以为 count 是从 end_time 往前扫。
    但实测 TDX formula_process_mul_xg 的 count 是从**当前真实日期**往前、且忽略
    end_time 参数。当回测 end_time 早于今天 (如 2020-2022 回测, 今天 2026-07-31):
    自适应算出 count=1030, 从今天往前只到 2022-08 → 2020-2021 的信号全在窗口外
    被丢弃 (实测 600800.SH 在 2020-2021 本有 20 个 QUANTQQ 信号, 却返回 0,
    导致回测前两年权益曲线为 0)。写死 3000 覆盖到 2014, 代价仅全市场公式阶段
    多约 13s (实测 0.37s→0.87s/批×50批), 相对回测总耗时为噪音。
    参数保留以兼容调用方 (run_stock_selection_with_dates)。
    若未来需 start<2014 的回测: 提上限并同步调小 BATCH_SIZE 防 "返回数据过大"。
    """
    return _MAX_SCAN_COUNT


def _empty_selection_df() -> pd.DataFrame:
    """空选股结果 DataFrame (统一列结构)。"""
    return pd.DataFrame(columns=["stock_code", "select_date", "formula_name"])


class FormulaRunner(ConnectorSeam):
    """TDX 公式执行封装。支持条件选股 (XG) 和指标计算 (ZB)。

    T-H-2 connector 缝隙五成员 2026-08-01 收编为 core.connector.ConnectorSeam。
    """

    # 2026-07-26: 上次 run_stock_selection_with_dates 的批次失败数 (L2 按日缓存
    # 区分"真空无信号" vs "失败空" — 失败区段不缓存; 每次 run 重置, 不改签名)
    last_batch_errors = 0

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
        str_codes = extract_codes(stock_list)

        if not str_codes:
            return _empty_selection_df()

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
        # 2026-07-23: 自适应 (区间交易日 + 预热缓冲), 原写死 3000 扫 ~12年全历史
        count = _adaptive_scan_count(start_time, end_time, stock_period)

        for batch_start in range(0, len(str_codes), BATCH_SIZE):
            batch = str_codes[batch_start:batch_start + BATCH_SIZE]
            batch_num = batch_start // BATCH_SIZE + 1
            logger.info(f"  批次 {batch_num}/{total_batches} ({len(batch)} stocks)")
            # 2026-07-26: 细粒度进度 (tools 直调时无人读, ~1µs)
            _progress.report("formula", batch_num / total_batches,
                             f"批次 {batch_num}/{total_batches}",
                             batch_num, total_batches)

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

            except Exception:
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
                        # 口径: Value 非0即信号 (修 ==1 过严 bug, 2026-07-19)
                        # 通达信 XG 返回公式输出值: 选股输出 1 或 30/15 等"选中值",
                        # 指标返回 None, 真·零信号输出 0。数字非0=信号, None/非数字跳过。
                        _v = entry.get("Value")
                        try:
                            _is_signal = float(_v) != 0
                        except (ValueError, TypeError):
                            _is_signal = False
                        if not _is_signal:
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

        cls.last_batch_errors = batch_errors  # 2026-07-26 (L2 用)
        if not all_records:
            if batch_errors >= total_batches:
                logger.error(f"所有 {total_batches} 批次均失败，请检查公式名称 [{formula_name}] 是否存在")
            else:
                logger.warning("选股结果解析后为空")
            return _empty_selection_df()

        df = pd.DataFrame(all_records)
        df["select_date"] = pd.to_datetime(df["select_date"])
        df = df.drop_duplicates(subset=["stock_code", "select_date"])
        df = df.sort_values(["select_date", "stock_code"]).reset_index(drop=True)
        logger.info(f"选股完成: {len(df)} 条记录, {df['stock_code'].nunique()} 只股票")
        return df
