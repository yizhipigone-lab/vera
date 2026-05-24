"""结果输出 — 将选股结果标准化输出为 CSV/Parquet，并附加收盘价、成交量等数据。"""

import pandas as pd
import os
from pathlib import Path
from typing import List, Optional

from core.data_fetcher import DataFetcher
from utils.logger import get_logger

logger = get_logger(__name__)


class ResultWriter:
    """
    选股结果增强输出。

    为选股结果附加当日收盘价、成交量等核心数据，
    输出标准化 CSV/Parquet 文件供回测模块直接使用。
    """

    def __init__(self, output_dir: str = ""):
        if not output_dir:
            output_dir = str(Path(__file__).resolve().parents[1] / "output" / "selections")
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)

    def enrich(
        self,
        selections: pd.DataFrame,
        price_data: Optional[dict] = None,
    ) -> pd.DataFrame:
        """
        用收盘价和成交量增强选股结果。

        Args:
            selections: 选股结果 DataFrame (stock_code, select_date)
            price_data: 可选的K线数据 dict，为 None 则自动获取

        Returns:
            增强后的 DataFrame，增加 close, volume 列
        """
        if selections.empty:
            return selections

        df = selections.copy()
        df["select_date"] = pd.to_datetime(df["select_date"])

        # 获取唯一股票列表和日期范围
        codes = df["stock_code"].unique().tolist()
        date_strs = df["select_date"].dt.strftime("%Y%m%d").unique()
        start = date_strs.min()
        end = date_strs.max()

        # 获取K线数据
        if price_data is None:
            price_data = DataFetcher.get_kline(
                codes, start_time=start, end_time=end, dividend_type="front",
            )

        if not price_data:
            logger.warning("无法获取价格数据，跳过增强")
            return df

        close_df: pd.DataFrame = price_data.get("Close", pd.DataFrame())
        volume_df: pd.DataFrame = price_data.get("Volume", pd.DataFrame())

        if close_df.empty:
            return df

        # 为每条选股记录匹配收盘价和成交量
        close_values = []
        volume_values = []

        for _, row in df.iterrows():
            code = row["stock_code"]
            dt = row["select_date"]

            close_val = None
            vol_val = None

            if code in close_df.columns:
                # 找最接近的交易日
                idx = close_df.index[close_df.index <= dt]
                if len(idx) > 0:
                    closest = idx[-1]
                    close_val = close_df.loc[closest, code]
                    if volume_df is not None and code in volume_df.columns:
                        vol_val = volume_df.loc[closest, code]

            close_values.append(close_val)
            volume_values.append(vol_val)

        df["close"] = close_values
        df["volume"] = volume_values

        missing = df["close"].isna().sum()
        if missing > 0:
            logger.warning(f"{missing} 条记录缺少收盘价")

        return df

    def save_csv(self, df: pd.DataFrame, file_name: str = "selections_enriched.csv") -> str:
        """保存增强后的选股结果为 CSV。"""
        file_path = os.path.join(self.output_dir, file_name)
        df.to_csv(file_path, index=False, encoding="utf-8-sig")
        logger.info(f"选股结果已保存: {file_path} ({len(df)} 条)")
        return file_path

    def save_parquet(self, df: pd.DataFrame, file_name: str = "selections.parquet") -> str:
        """保存选股结果为 Parquet（更高性能）。"""
        file_path = os.path.join(self.output_dir, file_name)
        df.to_parquet(file_path, index=False)
        logger.info(f"选股结果已保存: {file_path} ({len(df)} 条)")
        return file_path
