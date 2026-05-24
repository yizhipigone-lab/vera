"""去重引擎 — 基于 (stock_code, select_date) 主键去重，支持增量追加。"""

import pandas as pd
import os
from pathlib import Path
from typing import Optional

from utils.logger import get_logger

logger = get_logger(__name__)


class Deduplicator:
    """
    选股结果去重器。

    按 (stock_code, select_date) 作为主键去重。
    支持将新结果与已有结果合并，确保不重复。
    """

    def __init__(self, storage_dir: str = ""):
        if not storage_dir:
            storage_dir = str(Path(__file__).resolve().parents[1] / "output" / "selections")
        self.storage_dir = storage_dir
        os.makedirs(self.storage_dir, exist_ok=True)

    def deduplicate(self, new_results: pd.DataFrame) -> pd.DataFrame:
        """
        去除 new_results 内部的重复项。

        Returns:
            去重后的 DataFrame
        """
        if new_results.empty:
            return new_results

        before = len(new_results)
        subset = ["stock_code", "select_date"]

        # 确保 select_date 是 datetime 类型
        if "select_date" in new_results.columns:
            new_results = new_results.copy()
            new_results["select_date"] = pd.to_datetime(new_results["select_date"])

        result = new_results.drop_duplicates(subset=subset, keep="first")
        after = len(result)

        if before != after:
            logger.info(f"去重: {before} → {after} 条 (去除 {before - after} 条重复)")
        return result.reset_index(drop=True)

    def merge_with_existing(
        self,
        new_results: pd.DataFrame,
        file_name: str = "latest_selections.csv",
    ) -> pd.DataFrame:
        """
        与已有选股结果合并去重。

        Args:
            new_results: 新的选股结果
            file_name: 已有结果的文件名

        Returns:
            合并去重后的完整 DataFrame
        """
        file_path = os.path.join(self.storage_dir, file_name)

        existing = pd.DataFrame()
        if os.path.exists(file_path):
            existing = pd.read_csv(file_path, parse_dates=["select_date"])
            logger.info(f"加载已有选股结果: {len(existing)} 条")

        if new_results.empty:
            return existing

        combined = pd.concat([existing, new_results], ignore_index=True)
        result = self.deduplicate(combined)

        if not result.equals(existing):
            self.save(result, file_name)
            logger.info(f"合并后选股结果: {len(result)} 条 (新增 {len(result) - len(existing)} 条)")

        return result

    def save(self, df: pd.DataFrame, file_name: str) -> str:
        """保存选股结果到 CSV。"""
        file_path = os.path.join(self.storage_dir, file_name)
        df.to_csv(file_path, index=False, encoding="utf-8-sig")
        return file_path

    def load(self, file_name: str) -> pd.DataFrame:
        """加载选股结果。"""
        file_path = os.path.join(self.storage_dir, file_name)
        if not os.path.exists(file_path):
            return pd.DataFrame()
        return pd.read_csv(file_path, parse_dates=["select_date"])
