"""信号输出层 — 将选股/回测结果推送到通达信客户端界面。"""

from typing import List, Optional
import pandas as pd

from .connector import TdxConnector
from utils.logger import get_logger

logger = get_logger(__name__)


class SignalExporter:
    """将系统结果输出到通达信 TQ 界面。"""

    @staticmethod
    def _ensure_ready():
        TdxConnector.ensure_connected()

    @classmethod
    def send_warnings(
        cls,
        stock_list: List[str],
        time_list: Optional[List[str]] = None,
        price_list: Optional[List[str]] = None,
        close_list: Optional[List[str]] = None,
        volume_list: Optional[List[str]] = None,
        bs_flag_list: Optional[List[str]] = None,
        reasons: Optional[List[str]] = None,
        count: int = 1,
    ) -> bool:
        """发送预警信号到通达信 TQ 预警界面。"""
        cls._ensure_ready()
        from tqcenter import tq

        if time_list is None:
            from datetime import datetime
            time_list = [datetime.now().strftime("%Y%m%d%H%M%S")] * count
        if bs_flag_list is None:
            bs_flag_list = ["2"] * count  # 2=未知，由TDX API默认填充

        try:
            result = tq.send_warn(
                stock_list=stock_list[:count],
                time_list=time_list,
                price_list=price_list or ["0"] * count,
                close_list=close_list or ["0"] * count,
                volum_list=volume_list or ["0"] * count,
                bs_flag_list=bs_flag_list,
                warn_type_list=["0"] * count,
                reason_list=reasons or [""] * count,
                count=count,
            )
            if isinstance(result, dict) and result.get("ErrorId") not in ("0", None):
                logger.warning(f"发送预警部分失败: {result.get('Error', '')}")
            logger.info(f"已发送 {count} 条预警到通达信")
            return True
        except Exception as e:
            logger.error(f"发送预警失败: {e}")
            return False

    @classmethod
    def send_backtest_data(
        cls,
        stock_code: str,
        time_list: List[str],
        data_list: List[List[str]],
        count: int = 1,
    ) -> bool:
        """
        发送回测数据到通达信 K 线图展示。
        data_list 每个子列表最多 16 个纯数字字符串。
        """
        cls._ensure_ready()
        from tqcenter import tq

        try:
            tq.send_bt_data(
                stock_code=stock_code,
                time_list=time_list,
                data_list=data_list,
                count=count,
            )
            logger.info(f"已发送回测数据到通达信: {stock_code}")
            return True
        except Exception as e:
            logger.error(f"发送回测数据失败: {e}")
            return False

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
        from tqcenter import tq

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

    @classmethod
    def send_file(cls, file_path: str) -> bool:
        """发送文件路径到通达信，可供客户端打开。"""
        cls._ensure_ready()
        from tqcenter import tq

        try:
            tq.send_file(file_path)
            logger.info(f"已发送文件到通达信: {file_path}")
            return True
        except Exception as e:
            logger.error(f"发送文件失败: {e}")
            return False
