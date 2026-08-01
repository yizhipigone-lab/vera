"""信号输出层 — 将选股/回测结果推送到通达信客户端界面。"""

from typing import List, Optional

import pandas as pd

from utils.logger import get_logger

from .connector import TdxConnector

logger = get_logger(__name__)


class SignalExporter:
    """将系统结果输出到通达信 TQ 界面。"""

    @staticmethod
    def _ensure_ready():
        TdxConnector.ensure_connected()

    @classmethod
    def print_to_tdx(
        cls,
        df_list: List[pd.DataFrame],
        sp_name: str = "VERA",
        xml_filename: str = "vera_report.xml",
        jsn_filenames: Optional[List[str]] = None,
        table_names: Optional[List[str]] = None,
    ) -> bool:
        """将多个 DataFrame 展示在通达信 TQ 界面。"""
        cls._ensure_ready()
        tq = TdxConnector.tq()

        if jsn_filenames is None:
            jsn_filenames = [f"vera_t{i+1}.jsn" for i in range(len(df_list))]

        try:
            tq.print_to_tdx(
                df_list=df_list,
                sp_name=sp_name,
                xml_filename=xml_filename,
                jsn_filenames=jsn_filenames,
                vertical=len(df_list),
                table_names=table_names,
            )
            logger.info(f"已输出 {len(df_list)} 个表格到通达信界面")
            return True
        except Exception as e:
            logger.error(f"输出到通达信失败: {e}")
            return False
