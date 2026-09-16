"""数据获取层 — 通过 TDX TQ API 获取 K 线、财务、除权等数据。"""

from typing import List, Optional

import pandas as pd

from utils.code_normalizer import extract_codes, normalize_list
from utils.logger import get_logger

from . import progress as _progress
from .connector import ConnectorSeam
from .data_cache import DataCache
from .dividend_type import to_tdx_str
from .window import compute_window_bounds as _window_compute_bounds, merge_window_masks

# 2026-07-18: 协作式停止 (web「停止回测」按钮)
from .stop_flag import raise_if_stopped

logger = get_logger(__name__)


def _fmt_tdx_error(result) -> str:
    """把 TDX get_market_data 的错误返回渲染成可诊断文本 (2026-09-05 体检 P1)。

    此前一律 `result.get('Error', '未知错误')` —— TDX 只回 ErrorId 不带文本时
    全池日志打成一串"未知错误", 无法区分"源站故障/无该区间/需登录"。现尽量
    带出 ErrorId + 文本; 确无文本时列出返回键名供事后定位, 不再吞成未知。
    """
    if not result:
        return "空返回 (无结果对象)"
    eid = result.get("ErrorId", "?")
    msg = (result.get("Error") or result.get("ErrorMsg")
           or result.get("Message") or "").strip()
    if msg:
        return f"ErrorId={eid} {msg[:200]}"
    keys = [str(k) for k in list(result)[:8]]
    return f"ErrorId={eid} (TDX 无错误文本; 返回键={keys})"


class DataFetcher(ConnectorSeam):
    """TDX 数据获取统一门面。所有调用前自动确保连接就绪。

    C5 轻量解耦: 通过 _connector() 缝隙注入 connector, 默认仍用 TdxConnector 单例。
    测试可 set_connector(mock) 替换, 不改 27 个外部 TdxConnector 调用点。
    (2026-08-01: 缝隙五成员收编为 core.connector.ConnectorSeam mixin)
    """

    _KLINE_CACHE_DIR = None  # 测试可覆盖; None → 项目根 data/kline_cache
    # 2026-09-16 C2: KlineCache 单例池 {cache_dir: 实例} — 原每次取数新建实例,
    # 持久 sqlite 连接永不关闭, 常驻 server 下句柄持续泄漏。key 含 cache_dir:
    # _KLINE_CACHE_DIR 被测试改写 → 目录变了自动重建新实例。
    _KLINE_CACHE_POOL: dict = {}

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
            logger.error(
                "获取K线数据失败: %s (codes=%s %s %s~%s)",
                _fmt_tdx_error(result), codes[:5],
                period, start_time or "全部", end_time or "最新")
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
        from pathlib import Path

        from core.kline_cache import KlineCache

        cache_dir = (cls._KLINE_CACHE_DIR if cls._KLINE_CACHE_DIR
                     else str(Path(__file__).resolve().parent.parent / "data" / "kline_cache"))

        def _tdx_fetcher(sl, s, e, period="1d", dividend_type="front"):
            return cls._get_kline_from_tdx(sl, s, e, period=period,
                                           dividend_type=dividend_type, fill_data=False)

        def _calendar_fetcher():
            return cls.get_calendar_days("SH", "20100101", "20991231")

        cache = cls._KLINE_CACHE_POOL.get(cache_dir)
        if cache is None:
            cache = KlineCache(cache_dir, tdx_fetcher=_tdx_fetcher,
                               calendar_fetcher=_calendar_fetcher)
            cls._KLINE_CACHE_POOL[cache_dir] = cache
        if force_refresh:
            for code in normalize_list(stock_list):
                # 2026-07-18: force_invalidate = intact=False + 清 F5 冷却标记,
                # 显式强制不再被 24h 冷却吞掉
                cache.force_invalidate(code, period)
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

        【robust 版】: 异常吞掉返空 + 排序去重 —— 回测/选股窗口数学的
        唯一公开入口 (engine / signal_day_cache / window)。窗口数学依赖
        有序, 用本方法。字符串版日历 (raw, 工具/缓存用) 走 get_calendar_days;
        UI 展示用精确历在 scheduler.trading_calendar (2026-09-04 起 server 已切)。
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
    def compute_window_bounds(
        cls,
        selections: pd.DataFrame,
        window_trading_days: int,
        trading_days: Optional[List[pd.Timestamp]] = None,
        end_time: Optional[str] = None,
    ) -> tuple:
        """每只股的稀疏窗口 [窗口起, 窗口止] = [最早信号日, 最晚信号日+N 交易日]。

        2026-08-16 去上帝化: 纯数学已挪到 core.window.compute_window_bounds,
        本方法只做"拉日历 + 委托" (calendar_fetcher 注入 cls.get_trading_days)。
        行为与原内联实现一致, 完整语义见 core/window.py 的 docstring。
        """
        return _window_compute_bounds(
            selections, window_trading_days, trading_days, end_time,
            calendar_fetcher=cls.get_trading_days,
        )

    @classmethod
    def get_kline_windowed(
        cls,
        selections: pd.DataFrame,
        period: str,
        window_trading_days: int = 45,
        dividend_type: str = "front",
        fill_data: bool = False,
        *,
        use_cache: bool = False,
        end_time: Optional[str] = None,
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
            use_cache: 是否走 KlineCache 三级漏斗 (默认 False 向后兼容; True 时
                fill_data 无效, 缓存 miss-fetch 恒按 fill_data=False 拉, 见 _get_kline_via_cache)。
            end_time: 可选 ('yyyymmdd'), 窗口终点截断到请求区间终点 (2026-07-21)。

        Returns:
            (kline_dict, window_mask):
              - kline_dict: 与 get_kline 同结构 {'Open':DataFrame, ..., 'Close':DataFrame}
                行=所有窗口时间戳并集, 列=股票代码。
              - window_mask: DataFrame(同 Close 形状, bool), True=该 bar 在该股窗口内。
                窗口外为 False → 回测层据此设"不可交易", 避免窗口边界 NaN 误判退市。
        """
        if selections is None or selections.empty:
            return {}, pd.DataFrame()

        win_start, win_end = cls.compute_window_bounds(
            selections, window_trading_days, end_time=end_time)

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
            # 2026-07-18: 停止回测按钮 — 5m 窗口分批拉取是长耗时点, 逐批检查
            raise_if_stopped()
            # 2026-07-26: 细粒度进度
            _progress.report("fetch", (bi - 1) / total_buckets,
                             f"窗口批 {bi}/{total_buckets}", bi - 1, total_buckets)
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
                use_cache=use_cache,
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
            # 2026-08-04 修复: win_end 是当日子夜 (00:00) 时间戳 (交易日历/end_time
            # 截断均如此), 直接比较时间戳会把窗口最后一天的全部分钟 bar 排除在外,
            # 回测区间末日仍持仓的仓位会在末日第一根 bar 被误判退市强平 (reason=11)。
            # 终点按日期比较 (含末日全天), 与 degrade_5m 的 normalize() 语义一致。
            m = pd.DataFrame(False, index=close_b.index, columns=close_b.columns)
            idx_days = m.index.normalize()
            for c in codes:
                if c not in m.columns:
                    continue
                in_win = (m.index >= win_start[c]) & (
                    idx_days <= win_end[c].normalize())
                m.loc[in_win, c] = True
            mask_frames.append(m)

        if not mask_frames:
            logger.warning("稀疏窗口拉取结果为空")
            return {}, pd.DataFrame()
        _progress.report("fetch", 1.0, "取数完成", total_buckets, total_buckets)

        # 合并各批: 时间轴取并集, 列按股票代码。不同批可能共享时间戳
        # (如 1月信号股窗口与 2月信号股窗口在 2-3月重叠), 必须按 (行,列) 取首个非空,
        # 不能简单 drop 重复行 (会丢掉另一批的股票列)。
        kline_out: dict = {}
        for f in fields:
            if field_frames[f]:
                # 2026-08-14 内存爆炸修复: 原 pd.concat(所有批次, axis=0) 把 198 批
                # 堆成 ~1900 万行 (699 GiB) 再 groupby 去重 —— 长区间(11.5 年)下
                # 窗口=整段信号跨度, 每批都拉全量, concat 中间态先 OOM。
                # 改增量 combine_first: 结果始终停在最终尺寸(~13.5万行×4915列),
                # 语义与 groupby(level=0).first() 完全一致 (首个非 NaN 胜出)。
                merged = field_frames[f][0]
                for frame in field_frames[f][1:]:
                    merged = merged.combine_first(frame)
                kline_out[f] = merged.sort_index()

        # 合并各批窗口 mask (2026-08-16 挪到 core.window.merge_window_masks)
        window_mask = merge_window_masks(mask_frames)
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
        return extract_codes(raw)

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
        def _fetch():
            cls._ensure_ready()
            tq = cls._connector().tq()
            raw = tq.get_stock_list('11', list_type=1)
            return [{"code": s["Code"], "name": s["Name"].strip()}
                    for s in raw if isinstance(s, dict) and s.get("Code")]
        # 判过期→回源→回填 (治理III W3-get_or, TTL 语义在 DataCache)
        return cls._cache.sector_list_or(_fetch)

    @classmethod
    def get_sector_stocks(cls, sector_code: str) -> List[str]:
        """
        拉板块成份股 (支持板块代码如 '881319.SH' 或中文名如 '半导体'), 带进程级缓存.

        Returns: 纯代码字符串列表, 失败返回空列表不抛异常.
        """
        def _fetch():
            cls._ensure_ready()
            tq = cls._connector().tq()
            raw = tq.get_stock_list_in_sector(sector_code, list_type=0)
            return extract_codes(raw)
        # 判过期→回源→回填 (治理III W3-get_or)。失败**不缓存**: 异常在
        # _or 外接住返 [], 下次调用仍会重试 (旧代码同语义, 防瞬时失败
        # 毒化 24h 缓存)。
        try:
            return cls._cache.sector_stocks_or(sector_code, _fetch)
        except Exception as e:
            logger.warning(f"拉板块成份股失败 [{sector_code}]: {e}")
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
        def _fetch():
            result: dict = {}
            source = "TDX"
            try:
                cls._ensure_ready()
                tq = cls._connector().tq()
                # '5'=全部A股, '50'=沪深A股, '31'=ETF基金 (2026-08-15: 补 ETF 名称,
                # 原只拉股票列表, 159949/518880 等场内基金在持仓清单里没名字)
                for market in ('5', '50', '31'):
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
            except Exception:
                logger.warning("TDX 拉取股票简称失败, 降级腾讯", exc_info=True)
            if not result:
                # 2026-08-27 腾讯降级 (页面简称全丢事件): TDX 没开/拉空时用
                # kline_cache 清单代码全集 + 腾讯批量报价拼名称 (详见 _tencent_name_map)
                source = "腾讯"
                result = cls._tencent_name_map()
            if result:
                logger.info(f"全量简称缓存已构建({source}): {len(result)} 条")
            return result
        # 判过期→回源→回填; force_refresh 强制回源 (治理III W3-get_or)。
        # 空结果 (TDX+腾讯全挂) 不缓存 —— fetcher 返回 {} 时 set 存空,
        # 下次 has_name_map 见空不命中会重试 (与旧行为一致)。
        value = cls._cache.name_map_or(_fetch, force_refresh=refresh)
        return value

    @staticmethod
    def _manifest_codes() -> list:
        """kline_cache 清单里的去重代码全集 (腾讯降级的代码源)。

        全 A + ETF 历史上都拉过日线 (选股/轮动都走 kline_cache), 实际
        覆盖完整; 清单缺失/读取失败 → [] (降级链末端, fail-soft)。"""
        try:
            import sqlite3
            from pathlib import Path
            db = (Path(__file__).resolve().parents[1]
                  / "data" / "kline_cache" / "manifest.db")
            if not db.exists():
                return []
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    "SELECT DISTINCT stock_code FROM manifest").fetchall()
            finally:
                conn.close()
            return [r[0] for r in rows if r and r[0]]
        except Exception:
            return []

    @classmethod
    def _tencent_name_map(cls) -> dict:
        """腾讯降级 (2026-08-27): 批量走 qt.gtimg.cn 报价接口拼 {code: name}。

        腾讯报价返回 GBK 文本 v_sz000001="51~平安银行~000001~...": 第 1
        字段=名称, 第 2 字段=裸代码, v_ 前缀带市场。每批 60 只 (腾讯单
        请求上限量级), 单批失败跳过不整单失败。仅作 TDX 不可用时的兜底。"""
        import urllib.request
        codes = cls._manifest_codes()
        if not codes:
            return {}
        out: dict = {}
        for i in range(0, len(codes), 60):
            batch = codes[i:i + 60]
            q = ",".join(c.split(".")[1].lower() + c.split(".")[0]
                         for c in batch if "." in c)
            if not q:
                continue
            try:
                req = urllib.request.Request(
                    "https://qt.gtimg.cn/q=" + q,
                    headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    text = resp.read().decode("gbk", errors="ignore")
            except Exception:
                continue
            for line in text.split(";"):
                line = line.strip()
                if not line.startswith("v_") or '"' not in line:
                    continue
                try:
                    parts = line.split('"')[1].split("~")
                    if len(parts) > 2 and parts[1] and parts[2]:
                        market = line[2:4].upper()      # v_sz000001 → SZ
                        out[f"{parts[2]}.{market}"] = parts[1]
                except Exception:
                    continue
        return out

    @classmethod
    def clear_name_cache(cls):
        """清空简称缓存"""
        cls._cache.clear_name()

    @classmethod
    def get_calendar_days(
        cls,
        market: str = "SH",
        start_time: str = "",
        end_time: str = "",
    ) -> List[str]:
        """获取交易日字符串列表 (YYYYMMDD, TDX 原始顺序) —— 缓存/工具的日历源。

        【raw 版, 治理III W3-③ 由 get_trading_dates 更名】: 直接透传
        tq.get_trading_dates, 异常上抛 (不吞), 不排序去重 —— 本方法是
        KlineCache calendar_fetcher 与离线工具 (backfill/import_lc5/
        重绘检查/未来函数检查等) 的契约: 要真失败就大声失败, 不静默空表。

        与 get_trading_days (robust, Timestamp, 回测窗口数学) 刻意**不同名**:
        名字点明"字符串日历", 防误选。展示用精确历已迁
        scheduler.trading_calendar (2026-09-04), 本方法仅供工具/缓存。
        """
        cls._ensure_ready()
        tq = cls._connector().tq()
        dates = tq.get_trading_dates(
            market=market, start_time=start_time, end_time=end_time, count=-1,
        )
        return list(dates) if dates else []
