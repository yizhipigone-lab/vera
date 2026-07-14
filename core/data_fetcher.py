"""数据获取层 — 通过 TDX TQ API 获取 K 线、财务、除权等数据。"""

import pandas as pd
from typing import List, Optional

from .connector import TdxConnector
from .dividend_type import to_tdx_str
from utils.logger import get_logger
from utils.code_normalizer import normalize_list

logger = get_logger(__name__)


class DataFetcher:
    """TDX 数据获取统一门面。所有调用前自动确保连接就绪。"""

    # 基准指数代码（P1-6: 补沪深300/中证500）
    INDEX_CODES = {
        "shanghai": "999999.SH",       # 上证指数
        "hs300": "000300.SH",          # 沪深300
        "zz500": "000905.SH",          # 中证500
        "chuangyeban": "399006.SZ",    # 创业板指
        "kechuang50": "000688.SH",     # 科创50
        "zhongzhengA500": "000510.SH", # 中证A500（代码待 TDX 核实）
    }

    @staticmethod
    def _ensure_ready():
        TdxConnector.ensure_connected()

    @classmethod
    def get_kline(
        cls,
        stock_list: List[str],
        start_time: str = "",
        end_time: str = "",
        period: str = "1d",
        dividend_type: str = "front",
        count: int = -1,
        fill_data: bool = True,
        field_list: Optional[List[str]] = None,
    ) -> dict:
        """
        获取 K 线数据。

        Returns:
            dict: {'Open': DataFrame, 'High': DataFrame, 'Low': DataFrame,
                   'Close': DataFrame, 'Volume': DataFrame, 'Amount': DataFrame}
            每个 DataFrame 的行索引为 DatetimeIndex，列为股票代码。
        """
        cls._ensure_ready()
        # 候选 D: 边界归一化, 允许 int 输入 (旧调用方传 int=1 也能正确映射到 "front")
        dividend_type = to_tdx_str(dividend_type)
        tq = TdxConnector.tq()

        codes = normalize_list(stock_list)
        if not codes:
            logger.warning(f"无有效股票代码: {stock_list[:5]}...")
            return {}

        logger.info(f"获取 {len(codes)} 只股票 {period} K线数据...")
        result = tq.get_market_data(
            field_list=field_list or [],
            stock_list=codes,
            start_time=start_time,
            end_time=end_time,
            count=count,
            dividend_type=dividend_type,
            period=period,
            fill_data=fill_data,
        )

        if not result or ("ErrorId" in result and result.get("ErrorId") != "0"):
            logger.error(f"获取K线数据失败: {result.get('Error', '未知错误')}")
            return {}

        logger.info(f"获取到 {len(result)} 个字段的数据")
        return result

    @classmethod
    def get_kline_single(
        cls,
        stock_code: str,
        start_time: str = "",
        end_time: str = "",
        period: str = "1d",
        dividend_type: str = "front",
        count: int = -1,
    ) -> pd.DataFrame:
        """
        获取单只股票 K 线，返回整合的 DataFrame。
        Columns: open, high, low, close, volume, amount
        """
        data = cls.get_kline(
            [stock_code],
            start_time=start_time,
            end_time=end_time,
            period=period,
            dividend_type=dividend_type,
            count=count,
        )
        if not data:
            return pd.DataFrame()

        code = normalize_list([stock_code])[0]
        df = pd.DataFrame(index=data.get("Close", pd.DataFrame()).index)

        field_map = {
            "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Volume": "volume", "Amount": "amount",
        }
        for src, dst in field_map.items():
            if src in data and code in data[src].columns:
                df[dst] = data[src][code]

        df.index.name = "date"
        return df

    @classmethod
    def get_kline_as_wide(
        cls,
        stock_list: List[str],
        start_time: str = "",
        end_time: str = "",
        period: str = "1d",
        dividend_type: str = "front",
    ) -> pd.DataFrame:
        """
        获取 K 线数据并重整为 VectorBT 兼容的 wide 格式。

        Returns:
            DataFrame，列为 MultiIndex: (price_field, stock_code)，行为 DatetimeIndex
        """
        data = cls.get_kline(
            stock_list, start_time, end_time, period=period,
            dividend_type=dividend_type,
        )
        if not data:
            return pd.DataFrame()

        codes = normalize_list(stock_list)
        fields = ["Open", "High", "Low", "Close", "Volume"]
        frames = {}
        for f in fields:
            if f in data:
                df = data[f].copy()
                df.columns = [(f.lower(), c) for c in df.columns]
                frames[f] = df

        if not frames:
            return pd.DataFrame()

        result = pd.concat(frames.values(), axis=1)
        result.columns = pd.MultiIndex.from_tuples(result.columns)
        return result.sort_index()

    @classmethod
    def get_close_price(
        cls,
        stock_list: List[str],
        start_time: str = "",
        end_time: str = "",
        dividend_type: str = "front",
        period: str = "1d",
    ) -> pd.DataFrame:
        """获取收盘价 DataFrame（回测核心输入）。period 可指定 1d/1w/5m 等。"""
        data = cls.get_kline(
            stock_list, start_time, end_time,
            dividend_type=dividend_type, period=period,
        )
        if "Close" not in data:
            return pd.DataFrame()
        return data["Close"]

    @classmethod
    def get_index_data(
        cls,
        index_name: str,
        start_time: str = "",
        end_time: str = "",
        dividend_type: str = "none",
        period: str = "1d",
    ) -> pd.DataFrame:
        """获取指数 K 线数据。"""
        code = cls.INDEX_CODES.get(index_name, index_name)
        return cls.get_kline_single(
            code, start_time, end_time, dividend_type=dividend_type, period=period,
        )

    @classmethod
    def get_stock_universe(cls, list_type: str = "50") -> List[str]:
        """
        获取股票池（返回纯代码字符串列表）。

        list_type 常用值:
        '5'=全部A股, '50'=沪深A股, '23'=沪深300, '24'=中证500,
        '25'=中证1000, '28'=中证A500, '51'=创业板, '52'=科创板, '53'=北交所
        """
        cls._ensure_ready()
        tq = TdxConnector.tq()
        raw = tq.get_stock_list(str(list_type), list_type=1)
        codes = []
        for s in raw:
            if isinstance(s, dict):
                codes.append(s.get("Code", ""))
            elif isinstance(s, str):
                codes.append(s)
        return [c for c in codes if c]

    # P-v3.4: 行业板块支持 — 板块列表 + 成份股, 均带进程级缓存
    _SECTOR_CACHE: List[dict] = []                      # 128 个板块列表 [{code, name}]
    _SECTOR_STOCKS_CACHE: dict = {}                     # {sector_code: [成份股代码]}

    @classmethod
    def get_sector_list(cls) -> List[dict]:
        """
        获取 128 个细分行业板块 (list_type=11), 带进程级缓存.

        Returns:
            [{"code": "881319.SH", "name": "半导体"}, ...]
        """
        if cls._SECTOR_CACHE:
            return cls._SECTOR_CACHE
        cls._ensure_ready()
        tq = TdxConnector.tq()
        raw = tq.get_stock_list('11', list_type=1)
        cls._SECTOR_CACHE = [
            {"code": s["Code"], "name": s["Name"].strip()}
            for s in raw if isinstance(s, dict) and s.get("Code")
        ]
        return cls._SECTOR_CACHE

    @classmethod
    def get_sector_stocks(cls, sector_code: str) -> List[str]:
        """
        拉板块成份股 (支持板块代码如 '881319.SH' 或中文名如 '半导体'), 带进程级缓存.

        Returns: 纯代码字符串列表, 失败返回空列表不抛异常.
        """
        if sector_code in cls._SECTOR_STOCKS_CACHE:
            return cls._SECTOR_STOCKS_CACHE[sector_code]
        cls._ensure_ready()
        tq = TdxConnector.tq()
        try:
            raw = tq.get_stock_list_in_sector(sector_code, list_type=0)
            stocks = []
            for s in raw:
                if isinstance(s, str):
                    stocks.append(s)
                elif isinstance(s, dict):
                    stocks.append(s.get("Code", ""))
            stocks = [s for s in stocks if s]
            cls._SECTOR_STOCKS_CACHE[sector_code] = stocks
            return stocks
        except Exception as e:
            from utils.logger import get_logger
            get_logger(__name__).warning(f"拉板块成份股失败 [{sector_code}]: {e}")
            return []

    @classmethod
    def clear_sector_cache(cls):
        """清空板块缓存 (板块成份股更新时手动调)."""
        cls._SECTOR_CACHE.clear()
        cls._SECTOR_STOCKS_CACHE.clear()

    # === 全量股票代码→简称 进程级缓存 (修复交易表显示问题) ===
    _NAME_MAP_CACHE: dict = {}        # {code: name}

    @staticmethod
    def _fix_tq_name(s: str) -> str:
        """TDX TQ Name 字段错位修复: TQ 把 utf-8 字节重新打包到 Unicode 私有区 codepoint,
        把每个字符 UTF-8 编码后拼字节再解码."""
        try:
            bs = b''
            for c in s:
                bs += c.encode('utf-8')
            return bs.decode('utf-8')
        except Exception:
            return s

    @classmethod
    def get_name_map(cls, refresh: bool = False) -> dict:
        """获取 {stock_code: name} 全量映射, 进程级缓存.

        Args:
            refresh: True 强制重拉 (股票简称变更后手动调)
        Returns:
            {'601872.SH': '招商轮船', ...} 共约 5200 条
        """
        if cls._NAME_MAP_CACHE and not refresh:
            return cls._NAME_MAP_CACHE
        cls._ensure_ready()
        tq = TdxConnector.tq()
        result: dict = {}
        # list_type='50' = 沪深A股, list_type=1 = 每只用 dict 返回 (含 Name 字段)
        for market in ('5', '50'):
            try:
                raw = tq.get_stock_list(market, list_type=1)
            except Exception:
                continue
            for s in raw:
                if not isinstance(s, dict):
                    continue
                code = str(s.get("Code", "")).strip()
                name_raw = str(s.get("Name", "")).strip()
                if not code or not name_raw:
                    continue
                result[code] = cls._fix_tq_name(name_raw)
        cls._NAME_MAP_CACHE = result
        logger.info(f"全量简称缓存已构建: {len(result)} 条")
        return result

    @classmethod
    def get_stock_name(cls, code: str, fallback: str = "") -> str:
        """查单只股票简称; 命中返回真实名, 未命中返回 fallback (默认空字符串)."""
        m = cls.get_name_map()
        return m.get(code, fallback)

    @classmethod
    def clear_name_cache(cls):
        """清空简称缓存"""
        cls._NAME_MAP_CACHE.clear()

    @classmethod
    def get_trading_dates(
        cls,
        market: str = "SH",
        start_time: str = "",
        end_time: str = "",
    ) -> List[str]:
        """获取交易日列表。"""
        cls._ensure_ready()
        tq = TdxConnector.tq()
        dates = tq.get_trading_dates(
            market=market, start_time=start_time, end_time=end_time, count=-1,
        )
        return list(dates) if dates else []

    @classmethod
    def get_financial(
        cls,
        stock_list: List[str],
        field_list: List[str] = None,
        start_time: str = "",
        end_time: str = "",
        report_type: str = "announce_time",
    ) -> dict:
        """获取专业财务数据。"""
        cls._ensure_ready()
        tq = TdxConnector.tq()
        codes = normalize_list(stock_list)
        return tq.get_financial_data(
            stock_list=codes,
            field_list=field_list or [],
            start_time=start_time,
            end_time=end_time,
            report_type=report_type,
        )

    @classmethod
    def get_divid_factors(
        cls,
        stock_code: str,
        start_time: str = "",
        end_time: str = "",
    ) -> pd.DataFrame:
        """获取除权除息数据。"""
        cls._ensure_ready()
        tq = TdxConnector.tq()
        code = normalize_list([stock_code])[0]
        return tq.get_divid_factors(stock_code=code, start_time=start_time, end_time=end_time)
