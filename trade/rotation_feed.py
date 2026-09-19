"""trade/rotation_feed.py — 轮动指数日线取数降级链 (2026-09-19 批次 4.5 自 rotation.py 端出)。

职责单一: 给一个指数代码, 按 **QMT(主) → TDX → 腾讯** 三级降级取日线收盘价;
每级判空 (根数 ≥ min_bars) 才算成功, 全挂返回 `([], "none")` 交给调用方
fail-closed (数据不足不动作)。

端出动机(架构审查 P1-9): `trade/rotation.py` 89KB 七合一, 其中取数降级链是
**与特性状态无关**的自包含块 (只依赖 gateway + 日志), 单独成模块后 rotation
的调仓主逻辑更易读, 取数链也能独立测。

降级顺序的历史 (2026-08-17, 别随手改): 东财源因限连已剔除; 腾讯是最后兜底。
"""
from __future__ import annotations

from utils.logger import get_logger

_logger = get_logger("trade.rotation_feed")


class IndexFeed:
    """指数日线取数门面。唯一公开方法 closes(); 两个备源是私有实现细节。"""

    def __init__(self, gateway):
        self._gateway = gateway

    def closes(self, code: str, count: int, min_bars: int) -> tuple[list[float], str]:
        """取指数日线收盘价, 三级降级: QMT(主) → TDX → 腾讯。

        每级判空 (根数 ≥ min_bars) 才算成功, 否则降级下一级; 全挂返回
        ([], "none") → compute_momentum_signal 判数据不足 → fail-closed 不动作。
        返回 (closes, 来源名) —— 来源名进决策台账, 让"这次信号用的哪路数据"
        可查 (QMT 陈旧时带"(陈旧)"后缀, 2026-09-16 审计修复①)。
        """
        # 1) QMT 主源
        try:
            closes = self._gateway.query_daily_closes(code, count=count)
            if closes and len(closes) >= min_bars:
                # 2026-09-16 审计修复①: 补下载后仍陈旧时网关会落 history_stale
                # 标记 —— 这里把"陈旧"写进来源名, 让交易页看得见 (不再静默)。
                stale = getattr(self._gateway, "history_stale", {}).get(code)
                if stale:
                    _logger.warning("轮动信号 %s 用陈旧 QMT 日线 (%s)", code, stale)
                return [float(c) for c in closes], ("QMT(陈旧)" if stale else "QMT")
            _logger.warning("轮动信号 QMT 取数不足(%s 根), 降级 TDX", len(closes or []))
        except Exception as e:
            _logger.warning("轮动信号 QMT 取数失败, 降级 TDX: %s", e)
        # 2) TDX (core/data_fetcher)
        try:
            closes = self._tdx_closes(code, count)
            if closes and len(closes) >= min_bars:
                return closes, "TDX"
            _logger.warning("轮动信号 TDX 取数不足(%s 根), 降级腾讯", len(closes or []))
        except Exception as e:
            _logger.warning("轮动信号 TDX 取数失败, 降级腾讯: %s", e)
        # 3) 腾讯 (akshare)
        try:
            closes = self._tencent_closes(code, count)
            if closes and len(closes) >= min_bars:
                return closes, "腾讯"
            _logger.warning("轮动信号腾讯取数不足(%s 根)", len(closes or []))
        except Exception as e:
            _logger.warning("轮动信号腾讯取数失败: %s", e)
        return [], "none"

    @staticmethod
    def _tdx_closes(code: str, count: int) -> list[float]:
        """TDX (core/data_fetcher) 取指数日线收盘价, 返回最近 count 根。"""
        import datetime as _dt
        from core.data_fetcher import DataFetcher
        # count 根交易日 ≈ 1.5×count 自然日 (含周末/节假日), 再留 60 天余量
        end = _dt.date.today().strftime("%Y%m%d")
        start = (_dt.date.today() - _dt.timedelta(days=count * 2 + 60)).strftime("%Y%m%d")
        kl = DataFetcher.get_kline([code], start, end, period="1d",
                                   dividend_type="front", use_cache=True)
        s = (kl or {}).get("Close")
        if s is None or code not in s.columns:
            return []
        s = s[code].dropna()
        if s.empty:
            return []
        return [float(x) for x in s.tolist()[-count:]]

    @staticmethod
    def _tencent_closes(code: str, count: int) -> list[float]:
        """腾讯 (akshare stock_zh_index_daily_tx) 取指数日线收盘价, 最近 count 根。"""
        import akshare as ak
        num, ex = code.split(".")
        sym = f"{ex.lower()}{num}"   # 399673.SZ → sz399673
        df = ak.stock_zh_index_daily_tx(symbol=sym)
        if df is None or df.empty or "close" not in df.columns:
            return []
        return [float(x) for x in df["close"].tolist()[-count:]]
