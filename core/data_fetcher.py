"""数据获取层 — 通过 TDX TQ API 获取 K 线、财务、除权等数据。"""

import pandas as pd
from typing import List, Optional

from .connector import TdxConnector
from .data_cache import DataCache
from .dividend_type import to_tdx_str
from utils.logger import get_logger
from utils.code_normalizer import normalize_list

logger = get_logger(__name__)


class DataFetcher:
    """TDX 数据获取统一门面。所有调用前自动确保连接就绪。

    C5 轻量解耦: 通过 _connector() 缝隙注入 connector, 默认仍用 TdxConnector 单例。
    测试可 set_connector(mock) 替换, 不改 27 个外部 TdxConnector 调用点。
    """

    # C5: connector 注入缝隙（默认 None → 用 TdxConnector 单例）
    _connector_override = None
    _KLINE_CACHE_DIR = None  # 测试可覆盖; None → 项目根 data/kline_cache

    # 基准指数代码（P1-6: 补沪深300/中证500）
    INDEX_CODES = {
        "shanghai": "999999.SH",       # 上证指数
        "hs300": "000300.SH",          # 沪深300
        "zz500": "000905.SH",          # 中证500
        "chuangyeban": "399006.SZ",    # 创业板指
        "kechuang50": "000688.SH",     # 科创50
        "zhongzhengA500": "000510.SH", # 中证A500（代码待 TDX 核实）
    }

    @classmethod
    def _connector(cls):
        """返回当前生效的 connector（默认 TdxConnector 单例, 可被 set_connector 覆盖）。"""
        return cls._connector_override if cls._connector_override is not None else TdxConnector

    @classmethod
    def set_connector(cls, connector) -> None:
        """注入 connector（测试用, 传 mock 替换 TDX 连接）。"""
        cls._connector_override = connector

    @classmethod
    def reset_connector(cls) -> None:
        """恢复默认 TdxConnector 单例。"""
        cls._connector_override = None

    @classmethod
    def _ensure_ready(cls):
        cls._connector().ensure_connected()

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
        *,
        use_cache: bool = False,
        force_refresh: bool = False,
    ) -> dict:
        """
        获取 K 线数据。

        Returns:
            dict: {'Open': DataFrame, 'High': DataFrame, 'Low': DataFrame,
                   'Close': DataFrame, 'Volume': DataFrame, 'Amount': DataFrame}
            每个 DataFrame 的行索引为 DatetimeIndex，列为股票代码。

        use_cache=True (opt-in, Phase 1 默认 False): 走本地 KlineCache, miss-fetch 增量补 TDX,
        含 1d gap 检测+告警 (治 002008 类数据缺口)。force_refresh 强制全量重拉。
        详见 docs/plan/2026-07-17_本地K线parquet缓存_计划书.md。
        """
        if use_cache:
            return cls._get_kline_via_cache(
                stock_list, start_time, end_time, period, dividend_type,
                force_refresh=force_refresh,
            )
        return cls._get_kline_from_tdx(
            stock_list, start_time, end_time, period, dividend_type,
            count=count, fill_data=fill_data, field_list=field_list,
        )

    @classmethod
    def _get_kline_from_tdx(
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
        """TDX 直拉 (原 get_kline 实现, 缓存关闭或 miss-fetch 时用)。"""
        cls._ensure_ready()
        # 候选 D: 边界归一化, 允许 int 输入 (旧调用方传 int=1 也能正确映射到 "front")
        dividend_type = to_tdx_str(dividend_type)
        tq = cls._connector().tq()

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
    def _get_kline_via_cache(
        cls,
        stock_list: List[str],
        start_time: str,
        end_time: str,
        period: str,
        dividend_type: str,
        force_refresh: bool = False,
    ) -> dict:
        """走本地 KlineCache (Phase 1)。miss-fetch 回退 _get_kline_from_tdx(fill_data=False)。"""
        from core.kline_cache import KlineCache
        from pathlib import Path

        cache_dir = (cls._KLINE_CACHE_DIR if cls._KLINE_CACHE_DIR
                     else str(Path(__file__).resolve().parent.parent / "data" / "kline_cache"))

        def _tdx_fetcher(sl, s, e, period="1d", dividend_type="front"):
            return cls._get_kline_from_tdx(sl, s, e, period=period,
                                           dividend_type=dividend_type, fill_data=False)

        def _calendar_fetcher():
            return cls.get_trading_dates("SH", "20100101", "20991231")

        cache = KlineCache(cache_dir, tdx_fetcher=_tdx_fetcher,
                           calendar_fetcher=_calendar_fetcher)
        if force_refresh:
            for code in normalize_list(stock_list):
                cache._manifest_set_intact(code, period, False)
        return cache.get(stock_list, start_time, end_time,
                         period=period, dividend_type=dividend_type)

    @classmethod
    def get_trading_days(
        cls,
        start_time: str,
        end_time: str,
        market: str = "SH",
    ) -> List[pd.Timestamp]:
        """获取 [start_time, end_time] 内的有序交易日列表 (Timestamp, 已排序去重)。

        用于稀疏窗口拉取 (get_kline_windowed) 按交易日推进窗口, 避免自然日误差
        (周末/节假日)。底层调 tq.get_trading_dates, 失败时返回空列表。
        """
        cls._ensure_ready()
        tq = cls._connector().tq()
        try:
            raw = tq.get_trading_dates(market, start_time, end_time)
        except Exception as e:
            logger.warning(f"获取交易日历失败: {e}")
            return []
        days = []
        for d in raw or []:
            try:
                days.append(pd.to_datetime(str(d)[:8], format="%Y%m%d"))
            except (ValueError, TypeError):
                continue
        return sorted(set(days))

    @classmethod
    def get_kline_windowed(
        cls,
        selections: pd.DataFrame,
        period: str,
        window_trading_days: int = 45,
        dividend_type: str = "front",
        fill_data: bool = False,
    ) -> tuple:
        """稀疏窗口拉取: 只拉每只股票信号日往后 window_trading_days 交易日的 K 线。

        用于 5m/分钟级回测: 全区间全股池数据量爆炸 (4889 只 × 4.5 年 5m ≈ 15 亿点),
        但持仓期由止盈止损决定 (≤30 天), 每只股只需信号日附近的短窗口。

        Args:
            selections: 选股结果, 需含 stock_code + select_date 列。
            period: K 线周期 (如 "5m")。
            window_trading_days: 每只股信号日往后拉多少交易日 (默认 45, 覆盖 30 天持仓+15 缓冲)。
            dividend_type: 复权口径 (默认 "front", 与 engine 对齐)。
            fill_data: 是否让 TDX 前向填充 (默认 False, 保留停牌 NaN)。

        Returns:
            (kline_dict, window_mask):
              - kline_dict: 与 get_kline 同结构 {'Open':DataFrame, ..., 'Close':DataFrame}
                行=所有窗口时间戳并集, 列=股票代码。
              - window_mask: DataFrame(同 Close 形状, bool), True=该 bar 在该股窗口内。
                窗口外为 False → 回测层据此设"不可交易", 避免窗口边界 NaN 误判退市。
        """
        if selections is None or selections.empty:
            return {}, pd.DataFrame()

        sel = selections.copy()
        sel["select_date"] = pd.to_datetime(sel["select_date"])
        sel["stock_code"] = sel["stock_code"].apply(
            lambda c: normalize_list([c])[0] if normalize_list([c]) else c
        )

        # 每只股的窗口起点 = 最早信号日; 窗口需覆盖到 最晚信号日 + N 交易日
        first_sig = sel.groupby("stock_code")["select_date"].min()
        last_sig = sel.groupby("stock_code")["select_date"].max()

        global_start = first_sig.min()
        global_end = last_sig.max()

        # 拉全区间交易日历 (往后多留 window+10 天缓冲, 保证末批窗口能推满)
        cal_end = (global_end + pd.Timedelta(days=int(window_trading_days * 1.7) + 20))
        trading_days = cls.get_trading_days(
            global_start.strftime("%Y%m%d"), cal_end.strftime("%Y%m%d")
        )
        if not trading_days:
            logger.warning("交易日历为空, 稀疏窗口退化为按自然日估算窗口")
            trading_days = None

        def _window_end(sig_date: pd.Timestamp) -> pd.Timestamp:
            """信号日往后 window_trading_days 个交易日的日期。"""
            if trading_days:
                idx = 0
                lo, hi = 0, len(trading_days) - 1
                while lo <= hi:
                    mid = (lo + hi) // 2
                    if trading_days[mid] < sig_date:
                        lo = mid + 1
                    else:
                        hi = mid - 1
                idx = lo  # 第一个 >= sig_date 的交易日
                target = min(idx + window_trading_days, len(trading_days) - 1)
                return trading_days[target]
            # 无交易日历兜底: 自然日估算 (交易日≈自然日×5/7, 反推)
            return sig_date + pd.Timedelta(days=int(window_trading_days * 1.5) + 5)

        # 每只股的 [窗口起, 窗口止]
        win_start = {c: first_sig[c] for c in first_sig.index}
        win_end = {c: _window_end(last_sig[c]) for c in last_sig.index}

        # 按 (窗口起月份) 分桶批量拉取, 减少 tq 往返
        sel_codes = list(win_start.keys())
        buckets: dict = {}
        for c in sel_codes:
            key = win_start[c].strftime("%Y%m")
            buckets.setdefault(key, []).append(c)

        fields = ["Open", "High", "Low", "Close", "Volume", "Amount"]
        field_frames: dict = {f: [] for f in fields}
        mask_frames = []

        total_buckets = len(buckets)
        for bi, (mkey, codes) in enumerate(sorted(buckets.items()), 1):
            b_start = min(win_start[c] for c in codes)
            b_end = max(win_end[c] for c in codes)
            logger.info(
                f"  窗口批次 {bi}/{total_buckets} [{mkey}] "
                f"{len(codes)} 只 {b_start.date()}~{b_end.date()}"
            )
            data = cls.get_kline(
                codes,
                start_time=b_start.strftime("%Y%m%d"),
                end_time=b_end.strftime("%Y%m%d"),
                period=period,
                dividend_type=dividend_type,
                fill_data=fill_data,
            )
            if not data or "Close" not in data:
                continue

            close_b = data["Close"]
            if not isinstance(close_b.index, pd.DatetimeIndex):
                close_b.index = pd.to_datetime(close_b.index)

            for f in fields:
                if f in data and not data[f].empty:
                    field_frames[f].append(data[f])

            # 构建本批 window_mask: 每只股只在自己 [win_start, win_end] 内为 True
            m = pd.DataFrame(False, index=close_b.index, columns=close_b.columns)
            for c in codes:
                if c not in m.columns:
                    continue
                in_win = (m.index >= win_start[c]) & (m.index <= win_end[c])
                m.loc[in_win, c] = True
            mask_frames.append(m)

        if not mask_frames:
            logger.warning("稀疏窗口拉取结果为空")
            return {}, pd.DataFrame()

        # 合并各批: 时间轴取并集, 列按股票代码。不同批可能共享时间戳
        # (如 1月信号股窗口与 2月信号股窗口在 2-3月重叠), 必须按 (行,列) 取首个非空,
        # 不能简单 drop 重复行 (会丢掉另一批的股票列)。
        kline_out: dict = {}
        for f in fields:
            if field_frames[f]:
                merged = pd.concat(field_frames[f], axis=0)
                # groupby(level=0).first() 逐列取首个非 NaN, 正确合并跨批重叠时间戳
                merged = merged.groupby(level=0).first().sort_index()
                kline_out[f] = merged

        window_mask = pd.concat(mask_frames, axis=0)
        # 同 (行,列) 跨批取 OR (任一批标记窗口内即为窗口内)
        window_mask = window_mask.groupby(level=0).max().sort_index().fillna(False)
        # 对齐到 Close 的行列 (兜底: 缺失填 False)
        if "Close" in kline_out:
            window_mask = window_mask.reindex(
                index=kline_out["Close"].index,
                columns=kline_out["Close"].columns,
                fill_value=False,
            ).fillna(False).astype(bool)

        logger.info(
            f"稀疏窗口拉取完成: {len(kline_out.get('Close', pd.DataFrame()).columns)} 只股, "
            f"{len(kline_out.get('Close', pd.DataFrame()))} 个 bar (窗口={window_trading_days}交易日)"
        )
        return kline_out, window_mask

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
        *,
        use_cache: bool = False,
    ) -> pd.DataFrame:
        """获取收盘价 DataFrame（回测核心输入）。period 可指定 1d/1w/5m 等。
        use_cache=True: 走本地 KlineCache (miss-fetch 增量补 TDX)。"""
        data = cls.get_kline(
            stock_list, start_time, end_time,
            dividend_type=dividend_type, period=period,
            use_cache=use_cache,
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
        tq = cls._connector().tq()
        raw = tq.get_stock_list(str(list_type), list_type=1)
        codes = []
        for s in raw:
            if isinstance(s, dict):
                codes.append(s.get("Code", ""))
            elif isinstance(s, str):
                codes.append(s)
        return [c for c in codes if c]

    # P-v3.4: 行业板块支持 — 板块列表 + 成份股, 均带进程级缓存
    # C6: 三类缓存抽到 DataCache, DataFetcher 委托
    _cache = DataCache()

    @classmethod
    def get_sector_list(cls) -> List[dict]:
        """
        获取 128 个细分行业板块 (list_type=11), 带进程级缓存.

        Returns:
            [{"code": "881319.SH", "name": "半导体"}, ...]
        """
        if cls._cache.has_sector_list():
            return cls._cache.get_sector_list()
        cls._ensure_ready()
        tq = cls._connector().tq()
        raw = tq.get_stock_list('11', list_type=1)
        cls._cache.set_sector_list([
            {"code": s["Code"], "name": s["Name"].strip()}
            for s in raw if isinstance(s, dict) and s.get("Code")
        ])
        return cls._cache.get_sector_list()

    @classmethod
    def get_sector_stocks(cls, sector_code: str) -> List[str]:
        """
        拉板块成份股 (支持板块代码如 '881319.SH' 或中文名如 '半导体'), 带进程级缓存.

        Returns: 纯代码字符串列表, 失败返回空列表不抛异常.
        """
        if cls._cache.has_sector_stocks(sector_code):
            return cls._cache.get_sector_stocks(sector_code)
        cls._ensure_ready()
        tq = cls._connector().tq()
        try:
            raw = tq.get_stock_list_in_sector(sector_code, list_type=0)
            stocks = []
            for s in raw:
                if isinstance(s, str):
                    stocks.append(s)
                elif isinstance(s, dict):
                    stocks.append(s.get("Code", ""))
            stocks = [s for s in stocks if s]
            cls._cache.set_sector_stocks(sector_code, stocks)
            return stocks
        except Exception as e:
            from utils.logger import get_logger
            get_logger(__name__).warning(f"拉板块成份股失败 [{sector_code}]: {e}")
            return []

    @classmethod
    def clear_sector_cache(cls):
        """清空板块缓存 (板块成份股更新时手动调)."""
        cls._cache.clear_sector()

    # === 全量股票代码→简称 进程级缓存 (修复交易表显示问题) ===

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
        if cls._cache.has_name_map() and not refresh:
            return cls._cache.get_name_map()
        cls._ensure_ready()
        tq = cls._connector().tq()
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
        cls._cache.set_name_map(result)
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
        cls._cache.clear_name()

    @classmethod
    def get_trading_dates(
        cls,
        market: str = "SH",
        start_time: str = "",
        end_time: str = "",
    ) -> List[str]:
        """获取交易日列表。"""
        cls._ensure_ready()
        tq = cls._connector().tq()
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
        tq = cls._connector().tq()
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
        tq = cls._connector().tq()
        code = normalize_list([stock_code])[0]
        return tq.get_divid_factors(stock_code=code, start_time=start_time, end_time=end_time)
