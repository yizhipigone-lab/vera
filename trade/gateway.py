"""trade/gateway.py — xtquant 唯一收口 + Fake SDK 测试替身。

设计意图:
    全项目唯一允许 import xtquant 的文件, 且只在 RealGateway 方法内
    lazy import (铁律: 无 QMT 环境的开发机必须能跑全量测试)。
    FakeGateway 与真网关同接口同常量, 订单行为可脚本化 ——
    它只存在于 tests/ 的接线里, 生产代码零分支 (不是 dry-run)。
    回调通过构造函数注入的 callable 转发, 网关不知道 EventEngine
    的存在 —— 接线是 composition root 的事。
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Callable

from trade.book import (
    DIRECTION_BUY,
    OS_CANCELED,
    OS_JUNK,
    OS_PART_CANCEL,
    OS_PART_SUCC,
    OS_REPORTED,
    OS_SUCCEEDED,
    PRICE_TYPE_LIMIT,
    TERMINAL_STATUSES,
)
from utils.logger import get_logger

_logger = get_logger("trade.gateway")

# ── 日线"新鲜度"判定 (2026-09-16) ─────────────────────────────────────
# 背景: 本地日线原先只在"取空"时才 download_history_data —— 本机实测 9/16
# 盘后 513100 末根仍停在 9/14, 属"有数据但陈旧", 于是静默污染两处:
#   ① 轮动动量: 序列尾部缺 9/15, 实时价追加后参照点整体前移 ~1 个交易日;
#   ② 停机日补算: 缺 9/15 收盘价 → fail-closed 拒写 9/11、9/14 两格。
# 修法: 把"何时该补下载"收成**一处**判定 (空 或 末根 < 应有一根), 两个
# 取数口 (query_daily_closes / query_daily_closes_range) 共用 —— 同语义
# 手写两份必然漂移 (沉淀经验#1)。
_CLOSE_READY_HM = 15 * 60 + 5        # 盘后"当日线应已落地"时点 (与丢盘中 bar 同源)
_REFRESH_MIN_INTERVAL_SEC = 600.0    # 同代码补下载**成功**后的防抖间隔 (数据当天可能晚到)
_REFRESH_FAIL_RETRY_SEC = 60.0       # 补下载**失败**后的重试间隔 (一次失败不该压住一整天)
_HOLIDAY_LOOKBACK_DAYS = 15          # 连休最长回溯 (春节)
_CAL_NOT_COVERED_WARNED: set[str] = set()   # 日历不可信告警去重 (按日期, 防取数次次刷屏)


def _last_index_day(df) -> str:
    """日线 DataFrame 末行日期 (YYYYMMDD); 取不到返回 ''。"""
    try:
        return "".join(ch for ch in str(df.index[-1]) if ch.isdigit())[:8]
    except Exception:                # noqa: BLE001
        return ""


def _warn_calendar_not_covered(today: dt.date) -> None:
    """日历不可信时按日期告警一次 (每天一条, 不随取数次数刷屏)。"""
    key = today.isoformat()
    if key in _CAL_NOT_COVERED_WARNED:
        return
    _CAL_NOT_COVERED_WARNED.add(key)
    _logger.warning(
        "交易日历不可信 (%s 超出精确历与内置假日表的覆盖区间, 两者目前都只到 2026-12-31): "
        "盘后不再要求当日日线, 只要求上一交易日 —— 等 2027 年放假安排公告后补表/升级库即恢复", key)


def _behind_expected(seen_days: list[str], expected: str) -> tuple[bool, str]:
    """(末根是否落后于应有日线, 末根)。expected 为空(日历判不出) → 不落后(不猜)。"""
    days = [d for d in (seen_days or []) if d]
    last = max(days) if days else ""
    if not expected:
        return False, last
    return (not last) or last < expected, last


def _expected_last_bar_day(today: dt.date, now_hm: int) -> str:
    """当前时点本地日线"应该"已有的最新一根交易日 (YYYYMMDD); 判不出 ''。

    判据随**日历可信度**分两档 (2026-09-16 审计修复③):
      日历可信 (精确历可用, 或内置假日表覆盖该日):
        >= 15:05 且今天是交易日 → 今天 (盘后当日线应已落地);
        否则 → 今天之前最近的交易日 (盘中当日 bar 未收盘、非交易日都不要求)。
      日历**不可信** (精确历缺失且是表外年份, 如 2027 年):
        只要求"今天之前最近的交易日" —— 表外日期按"周一~周五"粗判, 落在工作日里
        的法定假日会被误判成交易日, 若仍要求当日就会永远等一根不存在的数据
        (每 600s 空补一次, 且永远校验不过)。
    日历不可用/异常 → '' (判不出就不猜, 调用方不补下载)。
    """
    try:
        from utils.trading_calendar import calendar_covers, is_trading_day
        trusted = calendar_covers(today)
        if not trusted:
            _warn_calendar_not_covered(today)
        if trusted and is_trading_day(today) and now_hm >= _CLOSE_READY_HM:
            return today.strftime("%Y%m%d")
        d = today - dt.timedelta(days=1)
        for _ in range(_HOLIDAY_LOOKBACK_DAYS):
            if is_trading_day(d):
                return d.strftime("%Y%m%d")
            d -= dt.timedelta(days=1)
    except Exception:                # noqa: BLE001
        pass
    return ""


class BaseGateway(ABC):
    """网关抽象。回调 callable 全部构造函数注入, 可为 None (不接该路)。"""

    def __init__(
        self,
        on_order: Callable[[dict], None] | None = None,
        on_trade: Callable[[dict], None] | None = None,
        on_quote: Callable[[str, dict], None] | None = None,
        on_disconnected: Callable[[str], None] | None = None,
        on_order_error: Callable[[dict], None] | None = None,
        on_cancel_error: Callable[[dict], None] | None = None,
    ) -> None:
        self._on_order = on_order
        self._on_trade = on_trade
        self._on_quote = on_quote
        self._on_disconnected = on_disconnected
        # 2026-08-07: 下单失败回报 (拒单原因原文) —— order_stock 只回本地
        # 序号, 未达柜台的单唯一可见渠道 (0807 事件: 10 笔限价单查无此单)
        self._on_order_error = on_order_error
        # 撤单失败回报 (XtCancelError: order_id/error_id/error_msg)
        self._on_cancel_error = on_cancel_error
        # 2026-09-16 审计修复(收尾): 最近一次 query_daily_closes 拿到的末根日线日期
        # (代码 → YYYYMMDD)。弱市择时闸门据此判断"今天这根是否已在序列里", 避免
        # 盘后把当日收盘价与实时价重复计入均线。只读观测, 不参与网关自身判定。
        self.daily_last_bar: dict[str, str] = {}

    def last_daily_bar_day(self, code: str) -> str:
        """最近一次 query_daily_closes 拿到的末根日线日期 (YYYYMMDD); 未知返回 ''。

        2026-09-16 审计修复收尾: `trade/auto_buy` 的弱市择时闸门要判断"当日这根
        是否已在序列里" —— 未知 ('') 时按"不在"处理 (保持原行为: 追加实时价)。
        """
        return self.daily_last_bar.get(code, "")

    def set_armed(self, armed: bool) -> None:
        """通道级武装门 (默认空实现)。实盘下单前必须武装的通道
        (如 THS GUI 网关) 覆写为写 armed 锁; 其余通道无此概念,
        下单安全由风控闸门兜底。"""
        pass

    @abstractmethod
    def connect(self) -> bool: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def order(self, code: str, direction: int, price: float, qty: int,
              price_type: Any = PRICE_TYPE_LIMIT, remark: str = "") -> str: ...

    @abstractmethod
    def cancel(self, order_id: str) -> bool:
        """撤单。审计M8修复(契约写死): 返回 True = 券商【受理】撤单,
        受理 ≠ 撤成 —— 终态以委托状态回报/查询为准 (真机实测 cancel
        返回 True 后状态可十余秒不变)。调用方要确认撤成, 必须轮询
        query_orders 等终态, 不得以返回值当撤成。"""
        ...

    @abstractmethod
    def query_asset(self) -> dict: ...

    @abstractmethod
    def query_positions(self) -> list[dict]: ...

    @abstractmethod
    def query_orders(self) -> list[dict]: ...

    @abstractmethod
    def query_trades(self) -> list[dict]: ...

    @abstractmethod
    def subscribe_quotes(self, codes: list[str]) -> bool: ...

    @abstractmethod
    def unsubscribe_all(self) -> None: ...

    @abstractmethod
    def query_quotes(self, codes: list[str]) -> dict[str, dict]:
        """主动查询行情快照 —— 订阅不健康时的轮询兜底 (计划书 §5.2)。"""
        ...

    def query_daily_highs(self, code: str, start: str, end: str = "") -> list[float]:
        """日线最高价序列 (不复权, YYYYMMDD 区间)。默认空 —— 仿真网关无历史源,
        调用方按"取不到就回退当日口径"处理 (2026-08-06, 移动止盈历史峰值用)。"""
        return []

    def query_daily_closes(self, code: str, count: int = 500) -> list[float]:
        """最近 count 根日线收盘价序列 (不复权, 升序)。默认空 —— 仿真网关无
        历史源, ETF 轮动信号 (MA20/250日高点) 的数据源 (2026-08-14)。
        用 count 取数而非日期区间 (对齐 qmt/qmt_portfolio_runner 的已验证口径)。"""
        return []


# 审计 T2 (2026-09-16): 常驻共享线程池 —— 原实现每次调用新建
# ThreadPoolExecutor 且 shutdown(wait=False), QMT 真卡死时每调用泄漏
# 一个线程 (断线风暴期可堆积数百)。共享小池控制线程总量。
_CALL_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="qmt-call")


def _call_with_timeout(fn: Callable, timeout_sec: float, *args: Any, **kwargs: Any) -> Any:
    """同步调用包超时。QMT 同步接口偶发卡死, 不能让唯一写者线程陪葬。

    注意: 超时后底层线程可能仍在跑, 但结果已被放弃 —— 这符合
    "宁可告警重试, 不可无限等待"的处置方向。
    """
    fut = _CALL_POOL.submit(fn, *args, **kwargs)
    return fut.result(timeout=timeout_sec)


def _build_trader_callback(gw, base):
    """构造 XtQuantTraderCallback 子类实例 (base 由 lazy import 传入;
    测试传 object 即可, 不依赖 xtquant 环境)。

    2026-08-07 重大修复: 回调名必须用 xtquant 官方名 —— 原实现写成
    on_order_status/on_deal_status (XtQuantTraderCallback 查无此方法,
    dir() 实证), 上线以来委托/成交回报从未到达, 全部状态靠对账轮询补。
    正确名: on_stock_order / on_stock_trade; 并新增 on_order_error
    (下单未达柜台的拒单原因, 0807 事件 10 笔限价单死因的唯一可见渠道)。
    回调线程只许转发给注入的 callable, 禁止调任何 xtquant 同步接口
    —— 官方死锁坑。
    """

    class _Cb(base):
        def on_stock_order(self, order):  # noqa: N802
            if gw._on_order:
                gw._on_order(gw._order_to_dict(order))

        def on_stock_trade(self, trade):  # noqa: N802
            if gw._on_trade:
                gw._on_trade(gw._trade_to_dict(trade))

        def on_order_error(self, order_error):  # noqa: N802
            if gw._on_order_error:
                gw._on_order_error({
                    "order_id": str(getattr(order_error, "order_id", "")),
                    "error_id": getattr(order_error, "error_id", 0),
                    "error_msg": getattr(order_error, "error_msg", ""),
                })

        def on_cancel_error(self, cancel_error):  # noqa: N802
            if gw._on_cancel_error:
                gw._on_cancel_error({
                    "order_id": str(getattr(cancel_error, "order_id", "")),
                    "error_id": getattr(cancel_error, "error_id", 0),
                    "error_msg": getattr(cancel_error, "error_msg", ""),
                })

        def on_disconnected(self):  # noqa: N802
            if gw._on_disconnected:
                gw._on_disconnected("xtquant 回调通知断线")

    return _Cb()


# 2026-08-07 订阅治理: 官方建议单股订阅 ≤50 ("订阅数较多建议直接用全推"),
# 0807 实盘 54 持仓 + 18 信号票 = 72 超建议, 当日出现订阅心跳超时降级。
_WHOLE_QUOTE_THRESHOLD = 50
_WHOLE_QUOTE_MARKETS = ["SH", "SZ"]


class RealGateway(BaseGateway):
    """真网关。所有 xtquant import 都在方法内 lazy。

    未安装 xtquant → 连接时 raise RuntimeError (清晰报错,
    不是 ImportError 栈糊脸)。P0 spike 待验证项 (remark 回传、
    on_disconnected 触发条件) 未验证前, 回调封装以官方文档为准。
    """

    def __init__(self, account_id: str, mini_qmt_path: str = "",
                 session_id: int = 0, timeout_sec: float = 5.0, **callbacks: Any):
        super().__init__(**callbacks)
        self._account_id = account_id
        self._path = mini_qmt_path
        self._session_id = session_id or int(time.time()) % 100000
        self._timeout = timeout_sec
        self._trader = None
        self._account = None
        # 2026-07-31 断线死循环修复: 连续连接失败计数 (见 _note_connect_failure)
        self._connect_failures = 0
        # 2026-09-16 日线补下载防抖: 代码 → 上次补下载时刻 (见 _should_download_history)
        self._refresh_ts: dict[str, float] = {}
        # 2026-09-16 审计修复②: 上次补下载失败的代码 —— 改按短间隔重试,
        # 一次失败不再压住一整天 (见 _download_history)
        self._refresh_failed: set[str] = set()
        # 2026-09-16 审计修复①: 只读观测 —— 补下载后仍陈旧的代码 → 人话说明
        # (页面/测试可读; 不参与任何判定, 清空时机见 _note_history_result)
        self.history_stale: dict[str, str] = {}
        # 审计H5修复: 行情订阅状态 —— code → 订阅序号 (unsubscribe 用),
        # run 线程全进程只启一次 (库级全局事件循环)
        self._quote_seqs: dict[str, int] = {}
        self._quote_lock = threading.Lock()
        self._xtdata_run_started = False
        # 2026-08-07 订阅治理: _watch = 关注的代码集合 (全推模式过滤用);
        # _whole_seq = 全推订阅号, None=逐票模式
        self._watch: set = set()
        self._whole_seq = None

    @staticmethod
    def _xt():
        """lazy import 唯一入口。缺库时给一句人话。"""
        try:
            from xtquant import xtconstant  # noqa: F401
            from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
            from xtquant.xttype import StockAccount
        except ImportError as e:
            raise RuntimeError(
                "未安装 xtquant: 真网关不可用。"
                "请在 QMT(miniQMT) 环境运行, 测试请用 FakeGateway。"
            ) from e
        return XtQuantTrader, XtQuantTraderCallback, StockAccount

    def connect(self) -> bool:
        """连接。审计M9修复(计划书 §5.5 "成功才关旧连接"):
        新建实例 → connect 成功 → 才 stop 旧实例并替换。
        失败路径 stop 新实例并置空 —— 不留已 start 未连上的悬活 trader
        (重连循环每轮新建, 旧实例及回调悬活可能多路推回调)。
        例外 (2026-07-31): 连续失败 ≥3 次主动释放旧实例并换 session_id,
        见 _note_connect_failure —— 旧会话半死占着 session 时,
        "成功才关旧"会把重连锁死成永久 -1。"""
        XtQuantTrader, XtQuantTraderCallback, StockAccount = self._xt()
        trader = XtQuantTrader(self._path, self._session_id,
                               _build_trader_callback(self, XtQuantTraderCallback))
        trader.start()
        try:
            rc = _call_with_timeout(trader.connect, self._timeout)
        except Exception:
            trader.stop()   # 失败: 新实例收尸, 旧实例不动 (旧连接还在用)
            self._note_connect_failure()
            raise
        if rc != 0:
            trader.stop()
            self._note_connect_failure()
            raise RuntimeError(f"QMT 连接失败, 返回码 {rc}")
        self._connect_failures = 0
        # 2026-08-07 订阅治理: 全推订阅随旧连接失效 —— 置 None, 重连后的
        # subscribe_quotes (_try_reconnect) 会重建; 逐票序号由重订阅覆盖
        with self._quote_lock:
            self._whole_seq = None
        # 新连接成功 —— 此刻才关旧连接 (§5.5), 断线期旧连接是最后的信息源
        old = self._trader
        self._trader = trader
        if old is not None:
            try:
                old.stop()
            except Exception:
                _logger.warning("旧 trader 实例 stop 异常 (已替换)", exc_info=True)
        self._account = StockAccount(self._account_id)
        self._trader.subscribe(self._account)
        return True

    def disconnect(self) -> None:
        if self._trader is not None:
            self._trader.stop()
            self._trader = None

    # 2026-07-31 断线死循环实测 (14:14 起 46 次 reconnect_failed, rc=-1):
    # 断线后旧会话半死挂在终端侧, 同 session_id 的新会话被拒; 而
    # "成功才关旧"意味着旧实例永远等不到释放 → 永不自愈。连续失败达
    # 阈值即主动释放旧 trader 并更换 session_id —— 心跳已死数十分钟的
    # 旧连接不可能是"最后的信息源", 此时释放它的收益 >> 残留风险。
    _CONNECT_FAILURE_RELEASE_THRESHOLD = 3

    def _note_connect_failure(self) -> None:
        self._connect_failures += 1
        if (self._connect_failures < self._CONNECT_FAILURE_RELEASE_THRESHOLD
                or self._trader is None):
            return
        old, self._trader = self._trader, None
        try:
            old.stop()
        except Exception:
            _logger.warning("释放旧 trader 异常 (继续重连)", exc_info=True)
        old_sid = self._session_id
        self._session_id = int(time.time()) % 100000
        if self._session_id == old_sid:   # 同秒撞号兜底, 保证必换
            self._session_id = (old_sid + 1) % 100000
        _logger.warning("连续 %d 次连接失败, 已释放旧会话, session_id %d → %d",
                        self._connect_failures, old_sid, self._session_id)

    def order(self, code: str, direction: int, price: float, qty: int,
              price_type: Any = PRICE_TYPE_LIMIT, remark: str = "") -> str:
        from xtquant import xtconstant  # lazy
        if price_type == "MARKET_PEER_FIRST":
            pt = xtconstant.MARKET_PEER_PRICE_FIRST
        elif price_type == "SZ_5LEVEL_CANCEL":
            pt = xtconstant.MARKET_SZ_CONVERT_5_CANCEL
        else:
            pt = int(price_type)
        seq = _call_with_timeout(
            self._trader.order_stock, self._timeout,
            self._account, code, direction, int(qty), pt, float(price),
            "VERA", remark,
        )
        return str(seq)

    def cancel(self, order_id: str) -> bool:
        rc = _call_with_timeout(
            self._trader.cancel_order_stock, self._timeout,
            self._account, int(order_id),
        )
        return rc == 0

    def query_asset(self) -> dict:
        a = _call_with_timeout(
            self._trader.query_stock_asset, self._timeout, self._account)
        # 2026-08-18 (159949 事件): QMT 的 total_asset 字段对"昨日尾盘新买入"的
        # 持仓有 T+1 结算延迟 —— market_value(市值)已含该持仓, 但 total_asset
        # 字段漏算 (08-17 尾盘买 159949 后, 08-18 盘前 total_asset 少 46 万,
        # 当日盈亏误显 -42%)。现金账户总资产 = 可用 + 冻结 + 市值, 按此重算,
        # 不信任 QMT 的 total_asset 字段 (FakeGateway 同口径)。
        return {"cash": a.cash, "frozen_cash": a.frozen_cash,
                "market_value": a.market_value,
                "total_asset": a.cash + a.frozen_cash + a.market_value}

    def query_positions(self) -> list[dict]:
        ps = _call_with_timeout(
            self._trader.query_stock_positions, self._timeout, self._account)
        # avg_cost 取 open_price 不取 avg_price (审计V1裁决, 2026-07-26
        # 真机实测: 159226.SZ/159290.SZ 两持仓无卖出摊薄时两字段一致):
        # open_price 是"不摊薄的真实买入成本", 正是预埋档位价 (成本×比例)
        # 要的基准; 部分卖出后 avg_price 被摊薄下压, 档位价会跟着跑偏。
        return [{"code": p.stock_code, "volume": p.volume,
                 "can_use": p.can_use_volume, "avg_cost": p.open_price}
                for p in ps]

    def query_orders(self) -> list[dict]:
        os_ = _call_with_timeout(
            self._trader.query_stock_orders, self._timeout, self._account)
        return [self._order_to_dict(o) for o in os_]

    def query_trades(self) -> list[dict]:
        ts = _call_with_timeout(
            self._trader.query_stock_trades, self._timeout, self._account)
        return [self._trade_to_dict(t) for t in ts]

    # ── 行情: 订阅为主 + 轮询兜底 ───────────────────────────────

    def _xtdata(self):
        """xtdata lazy import 收口。单独成方法 = 测试接缝:
        单测 monkeypatch 实例属性注入 fake xtdata, 验证的是"我们的
        转换与接线逻辑", 不是 xtdata 本身 (审计H5修复)。"""
        from xtquant import xtdata  # lazy
        return xtdata

    @staticmethod
    def _closes_by_date(df) -> dict[str, float]:
        """日线 DataFrame → {'YYYY-MM-DD': close} (索引取日期, 过滤 NaN/非正)。"""
        out: dict[str, float] = {}
        try:
            for idx, close in zip(df.index, df["close"].tolist()):
                digits = "".join(ch for ch in str(idx) if ch.isdigit())[:8]
                if len(digits) != 8 or not (close == close and close > 0):
                    continue
                out[f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"] = float(close)
        except Exception as e:                      # noqa: BLE001
            _logger.warning("日线转日期字典失败: %s", e)
            return {}
        return out

    def _expected_bar_day(self, now_ts: float, cap_day: str = "") -> str:
        """当前"应有一根"的日线 (YYYYMMDD); 历史区间查询按右端封顶。

        cap_day 非空 = 本次查询右端: 应有一根不超过它 —— 历史区间查询 (end 在
        过去) 不该被判成陈旧。判定口 (补不补) 与复检口 (补完够不够新) 共用,
        避免同一条封顶规则手写两份 (2026-09-16 审计修复①)。
        """
        now = dt.datetime.fromtimestamp(now_ts)
        expected = _expected_last_bar_day(now.date(), now.hour * 60 + now.minute)
        if cap_day and expected and cap_day < expected:
            expected = cap_day
        return expected

    def _should_download_history(self, code: str, seen_days: list[str],
                                 now_ts: float,
                                 cap_day: str = "") -> tuple[bool, str]:
        """本地日线是否需要补下载 → (是否下载, 原因人话)。两个取数口共用。

        判据: 空 或 末根 < "应有一根" (`_expected_last_bar_day`, 盘后要求当日,
        盘中/非交易日只要求上一交易日; 日历不可信时只要求上一交易日)。
        cap_day 非空 = 本次查询右端, 应有一根不超过它 —— 历史区间查询 (end 在
        过去) 不该被判成陈旧。
        防抖: 同代码 _REFRESH_MIN_INTERVAL_SEC 内不重复下载 (数据当天可能晚到,
        失败一次别把每个调用都拖成 20s 阻塞)。2026-09-16 审计修复②: 上一次
        补下载**失败过**的代码改按 _REFRESH_FAIL_RETRY_SEC 重试 (失败标记由
        `_download_history` 落), 避免"一次失败 = 当天不再补"。
        日历判不出 → 不下载 (不猜)。
        """
        expected = self._expected_bar_day(now_ts, cap_day=cap_day)
        _, last = _behind_expected(seen_days, expected)
        if not expected:
            return False, "日历判不出应有日线, 不补下载"
        if last and last >= expected:
            return False, f"末根 {last} 已达应有 {expected}"
        interval = (_REFRESH_FAIL_RETRY_SEC if code in self._refresh_failed
                    else _REFRESH_MIN_INTERVAL_SEC)
        if now_ts - self._refresh_ts.get(code, 0.0) < interval:
            return False, (f"末根 {last or '空'} 落后于应有 {expected}, "
                           f"但 {int(interval)}s 内已补过")
        self._refresh_ts[code] = now_ts
        return True, f"末根 {last or '空'} 落后于应有 {expected}, 补下载历史"

    def _download_history(self, xtdata: Any, code: str, start: str, end: str) -> bool:
        """补下载一次历史日线; 成功 (未抛异常) 返回 True。

        防抖戳由判定口 `_should_download_history` 在"决定要下载"那一刻盖上
        (那是为了限住阻塞时间, 不是"成功"的证明), 本方法只负责**记录成败**:
        失败 → `_refresh_failed` → 下一次判定改用短重试间隔 (审计修复②)。
        两处取数口共用本方法 —— 同一条规则不写两份 (沉淀经验#1)。
        """
        try:
            _call_with_timeout(xtdata.download_history_data,
                               self._timeout * 4, code, "1d", start, end)
        except Exception as e:              # noqa: BLE001
            self._refresh_failed.add(code)
            _logger.warning("download_history_data(%s) 失败 (%.0fs 后允许重试): %s",
                            code, _REFRESH_FAIL_RETRY_SEC, e)
            return False
        self._refresh_failed.discard(code)
        return True

    def _note_history_result(self, code: str, seen_days: list[str],
                             expected: str, where: str) -> bool:
        """补下载后复检: 仍落后于应有日线 → 记 WARN + 落 `history_stale` 标记。

        2026-09-16 审计修复①: 原实现重取后只判"空不空", 陈旧序列原样返回且不
        给调用方任何信号 → "有数据但陈旧"静默流到下游 (动量只数根数不看日期)。
        @returns True = 仍陈旧 (由调用方决定 fail-closed 还是带标记返回)。
        """
        stale, last = _behind_expected(seen_days, expected)
        if not stale:
            self.history_stale.pop(code, None)
            return False
        note = f"末根 {last or '空'} < 应有 {expected}"
        self.history_stale[code] = note
        _logger.warning("%s(%s) 补下载后仍陈旧 (%s) —— 数据源当天可能还没落地",
                        where, code, note)
        return True

    def query_daily_closes_range(self, code: str, start: str = "",
                                 end: str = "") -> dict[str, float]:
        """不复权日线 close, 按日期取: {'YYYY-MM-DD': 收盘价} (2026-09-10)。

        停机日资产补算用 (trade/asset_gapfill): 补过去某一天的市值必须
        "哪一天取哪一天"的价, 而 query_daily_closes 只返回"最近 N 根"的无日期
        序列, 无法对齐。口径与它一致 (get_market_data_ex, 'none' 不复权,
        与实盘成交价/成本同维度); 本地数据**空或陈旧**时 download_history_data
        后重取一次 (2026-09-16: 原只判空 → 缺 9/15 收盘价让补算 fail-closed 拒写)。
        start/end 为 'YYYYMMDD' (空 = 不限)。失败/无数据返回 {} (调用方 fail-closed)。
        **补下载后仍陈旧**: 仍返回已有数据 + 记 WARN + 落 `history_stale` 标记
        (2026-09-16 审计修复①, 原为静默返回)。这里**不**因陈旧而拒收: 本口是按
        日期问价的, 要的就是"历史某天"的价 (缺哪天由调用方 fail-closed 处理),
        若因"最新一根不够新"整段拒收, 反而会把 2026-09-16 那次修复要救的
        9/15 收盘价一起丢掉。
        """
        xtdata = self._xtdata()
        raw = _call_with_timeout(
            xtdata.get_market_data_ex, self._timeout,
            [], [code], "1d", start, end, -1, "none", False)
        df = (raw or {}).get(code)
        seen = [_last_index_day(df)] if df is not None and not df.empty else []
        now_ts = time.time()
        cap_day = str(end or "")
        need, why = self._should_download_history(
            code, seen, now_ts, cap_day=cap_day)
        expected = self._expected_bar_day(now_ts, cap_day=cap_day)
        if need:
            _logger.warning("query_daily_closes_range(%s) %s", code, why)
            self._download_history(xtdata, code, start, end)
            raw = _call_with_timeout(
                xtdata.get_market_data_ex, self._timeout,
                [], [code], "1d", start, end, -1, "none", False)
            df = (raw or {}).get(code)
        if df is None or df.empty:
            _logger.warning("query_daily_closes_range(%s) 无数据 (补下载后仍空)", code)
            return {}
        self._note_history_result(code, [_last_index_day(df)], expected,
                                  "query_daily_closes_range")
        return self._closes_by_date(df)

    @staticmethod
    def _tick_to_quote(t: dict) -> dict:
        """tick 原始 dict → 标准 quote dict。订阅回调与 query_quotes
        (轮询兜底) 共用同一份字段映射 —— 两条腿一个口径。
        ts 取 tick 内时间戳 (真机 introspect: time/timetag, 毫秒),
        缺失给 **None 不再兜底 time.time()** (2026-07-27 ETF 误卖事件
        裁决③: 无戳 tick 打上本地时刻会冒充新鲜行情, 半夜驱动自动
        规则挂出真实卖单; None 由 monitor 视为陈旧 fail-closed)。"""
        ts_ms = t.get("time") or t.get("timetag") or 0
        return {
            "last": t.get("lastPrice", 0.0),
            "bid1": (t.get("bidPrice") or [0.0])[0],
            "ask1": (t.get("askPrice") or [0.0])[0],
            "high": t.get("high", 0.0),
            "low": t.get("low", 0.0),
            "prev_close": t.get("lastClose", 0.0),
            "ts": (ts_ms / 1000.0) if ts_ms else None,
        }

    def subscribe_quotes(self, codes: list[str]) -> bool:
        """审计H5修复: 订阅腿接线 —— 每票 subscribe_quote 挂回调,
        回调把最新一条 tick 转标准 quote 转发 self._on_quote。
        回调线程只做转换+转发 (铁律 2: 不调 xtquant 同步接口, 不写 DB)。

        2026-08-07 订阅治理: 关注集合 >50 (官方建议上限) 时切全推
        subscribe_whole_quote(SH/SZ) —— 一次性订阅全市场, 回调按 _watch
        过滤, 只转发关注票 (0807 实盘 72 个逐票订阅超官方建议, 当日
        心跳超时降级); 切换时退订全部逐票序号, 防双通道重复推送。"""
        xtdata = self._xtdata()
        with self._quote_lock:
            self._watch.update(codes)
            over = len(self._watch) > _WHOLE_QUOTE_THRESHOLD
            whole_active = self._whole_seq is not None
        if over:
            if not whole_active:
                seq = xtdata.subscribe_whole_quote(
                    list(_WHOLE_QUOTE_MARKETS), callback=self._on_tick)
                with self._quote_lock:
                    self._whole_seq = seq
                    seqs = list(self._quote_seqs.values())
                    self._quote_seqs.clear()
                for s in seqs:
                    xtdata.unsubscribe_quote(s)
            self._ensure_xtdata_run()
            return True
        for code in codes:
            seq = xtdata.subscribe_quote(
                code, period="tick", count=0, callback=self._on_tick)
            with self._quote_lock:
                # 重订阅 (断线重连后) 直接覆盖旧 seq; 旧订阅由
                # unsubscribe_all 或库自身连接重建清理
                self._quote_seqs[code] = seq
        self._ensure_xtdata_run()
        return True

    def _on_tick(self, datas: dict) -> None:
        """xtdata 推送回调。两种形状归一: 逐票订阅 {code: [tick, ...]},
        全推 {code: tick} (单条 dict); 只取最新一条 (监控只要最新价,
        历史 tick 没有重放价值)。全推模式按 _watch 过滤 —— 全市场
        几千只里只转发关注票, 不进事件队列。"""
        for code, ticks in (datas or {}).items():
            if isinstance(ticks, dict):
                ticks = [ticks]
            if not ticks:
                continue
            with self._quote_lock:
                if self._whole_seq is not None and code not in self._watch:
                    continue
            if self._on_quote:
                self._on_quote(code, self._tick_to_quote(ticks[-1]))

    def _ensure_xtdata_run(self) -> None:
        """xtdata.run() 启动推送事件循环 (阻塞调用) —— 必须跑在 daemon
        线程。幂等: 全进程只启一次。disconnect 不停它: run 循环是库级
        全局资源, 停它会误伤同进程其他 xtdata 使用者, 且重连后还要用。"""
        if self._xtdata_run_started:
            return
        self._xtdata_run_started = True
        threading.Thread(target=self._xtdata().run,
                         name="xtdata-push-loop", daemon=True).start()

    def unsubscribe_all(self) -> None:
        """逐票退订 (保存的订阅序号) + 全推退订 (若在全推模式)。"""
        xtdata = self._xtdata()
        with self._quote_lock:
            seqs = list(self._quote_seqs.values())
            self._quote_seqs.clear()
            whole = self._whole_seq
            self._whole_seq = None
        for seq in seqs:
            xtdata.unsubscribe_quote(seq)
        if whole is not None:
            xtdata.unsubscribe_quote(whole)

    def query_quotes(self, codes: list[str]) -> dict[str, dict]:
        """轮询兜底: get_full_tick 与订阅推送同一字段口径 (同一转换函数)。"""
        xtdata = self._xtdata()
        raw = _call_with_timeout(xtdata.get_full_tick, self._timeout, codes)
        return {code: self._tick_to_quote(t) for code, t in (raw or {}).items()}

    def query_daily_highs(self, code: str, start: str, end: str = "") -> list[float]:
        """不复权日线 high 序列 (2026-08-06, 移动止盈持仓期峰值用)。

        不复权口径与实盘成交价/成本价同维度 —— 前复权历史价在除权日
        之后与真实成本不可比。"""
        xtdata = self._xtdata()
        raw = _call_with_timeout(
            xtdata.get_market_data_ex, self._timeout,
            ["high"], [code], "1d", start, end, -1, "none", False)
        df = (raw or {}).get(code)
        if df is None or df.empty:
            return []
        # 2026-08-06 审计 P2: 过滤 NaN/非正数 —— NaN 是 truthy,
        # 流入 monitor 会让当日移动止盈静默失效 (last <= NaN 恒 False)
        return [float(x) for x in df["high"].tolist() if x == x and x > 0]

    def query_daily_closes(self, code: str, count: int = 500) -> list[float]:
        """最近 count 根不复权日线 close 序列 (2026-08-14, ETF 轮动信号用)。

        取数口径对齐 qmt/qmt_portfolio_runner.py 的 PROVEN 用法:
        field_list=[] (返回全字段再取 close 列) + count 取最近 N 根。
        本地数据**空或陈旧**时先 download_history_data 再重取 (幂等, 已下载则
        秒回) —— 陈旧判据见 _should_download_history (2026-09-16 修: 原只判空,
        本机实测 9/16 盘后末根仍停 9/14, 动量参照点整体前移 ~1 个交易日)。
        无未来函数: 剔除「当日未收盘」bar (盘中取数可能含今日盘中 bar)。
        返回按交易日升序; 过滤 NaN/非正数; 仍空记 WARN 便于排查。
        **补下载后仍陈旧**: 本口**仍返回该序列** (不改实盘选腿行为), 但记 WARN 并
        落 `history_stale[代码]` 标记供页面/调用方观察 (2026-09-16 审计修复①:
        原实现静默返回陈旧序列)。"""
        xtdata = self._xtdata()
        raw = _call_with_timeout(
            xtdata.get_market_data_ex, self._timeout,
            [], [code], "1d", "", "", count, "none", False)
        df = (raw or {}).get(code)
        seen = [_last_index_day(df)] if df is not None and not df.empty else []
        now_ts = time.time()
        need, why = self._should_download_history(code, seen, now_ts)
        expected = self._expected_bar_day(now_ts)
        if need:
            _logger.warning("query_daily_closes(%s) %s", code, why)
            days_back = int(count * 1.6) + 30   # count 根交易日 ≈ 1.6× 自然日 + 余量
            # 注 (审计 L7): 下载窗口用真实 time.time(); 生产真网关时钟即真实
            # 时间, 与信号日期同源无分叉; 仅测试注入时钟偏移时才有理论差异。
            start = time.strftime("%Y%m%d", time.localtime(now_ts - days_back * 86400))
            self._download_history(xtdata, code, start, "")
            raw = _call_with_timeout(
                xtdata.get_market_data_ex, self._timeout,
                [], [code], "1d", "", "", count, "none", False)
            df = (raw or {}).get(code)
        if df is None or df.empty:
            _logger.warning("query_daily_closes(%s) 无数据 (补下载后仍空: 指数代码可能无数据)", code)
            self.daily_last_bar.pop(code, None)
            return []
        self._note_history_result(code, [_last_index_day(df)], expected,
                                  "query_daily_closes")
        closes = [float(x) for x in df["close"].tolist() if x == x and x > 0]
        # 无未来函数(审计 CRITICAL#1): 最后一根是"今天"且当前<15:05(未收盘) →
        # 丢弃当日盘中 bar, 信号只用已收盘完整日线 (时点常量与新鲜度判定同源)
        last_day = _last_index_day(df)
        now = time.localtime()
        if (last_day and last_day == time.strftime("%Y%m%d", now)
                and now.tm_hour * 60 + now.tm_min < _CLOSE_READY_HM):
            closes = closes[:-1]     # 日期解析失败 (last_day='') 不裁 (保守)
            last_day = _last_index_day(df.iloc[:-1]) if len(df) > 1 else ""
        # 2026-09-16 审计修复收尾: 记**实际返回序列**的末根日期 (盘中当日 bar 已被
        # 上面裁掉 → 这里自然不是今天), 供弱市择时闸门判"当日是否已在序列里"
        self.daily_last_bar[code] = last_day
        return closes

    @staticmethod
    def _order_to_dict(o: Any) -> dict:
        return {
            "order_id": str(o.order_id), "remark": o.order_remark,
            "code": o.stock_code, "direction": o.order_type,
            "price": o.price, "qty": o.order_volume,
            "filled_qty": o.traded_volume, "status": o.order_status,
            # 2026-08-07: 委托状态描述 (废单原因, 0807 事件) —— 查询/回报
            # 双通道都带, 不依赖 on_order_error 也能看到拒单原因
            "status_msg": getattr(o, "status_msg", "") or "",
            # 2026-08-04: 委托下单时间 (epoch 秒), 供 reconciler 拦截
            # QMT 跨日查询返回的历史委托; 字段缺失/为 0 给 None (不伪装)
            "ts": getattr(o, "order_time", 0) or None,
        }

    @staticmethod
    def _trade_to_dict(t: Any) -> dict:
        return {
            "traded_id": str(t.traded_id), "order_id": str(t.order_id),
            "code": t.stock_code, "direction": t.order_type,
            "price": t.traded_price, "qty": t.traded_volume,
            # 2026-08-06: traded_time 缺失/为 0 给 None (与 _order_to_dict
            # 同口径) —— _on_trade 跨日 guard 把 0 当 1970 年会误拦合法成交
            "amount": t.traded_amount, "ts": t.traded_time or None,
        }


class FakeGateway(BaseGateway):
    """内存假网关: 同接口同常量, 行为可脚本化 (simulate_* 钩子)。

    不模拟券商全部怪癖, 只模拟测试需要的: ack/成交/拒单/断线/
    资金冻结/延迟撤单。成交会真实推进内存持仓与资金, 使查询往返自洽。
    delayed_cancel=True 时模拟真机"受理≠撤成"语义 (审计M8修复):
    cancel 只受理, 终态由 simulate_cancel_ack 推进。
    """

    def __init__(self, cash: float = 1_000_000.0,
                 positions: dict[str, dict] | None = None,
                 delayed_cancel: bool = False,
                 daily_closes: dict[str, list[float]] | None = None,
                 clock=time.time,
                 **callbacks: Any):
        super().__init__(**callbacks)
        self._cash = cash
        self._frozen = 0.0      # 审计L7修复: 下单即冻结, 成交/撤单/废单释放
        self._delayed_cancel = delayed_cancel
        # 2026-08-14: 注入时钟 —— 成交/委托的 ts 用它 (与 TradeApp 注入时钟
        # 同源), 修掉周末跑测试时真实 time.time() 与注入时钟跨日的"跨日拦截"误拦
        self._clock = clock
        # 可注入期初持仓, 方便卖出路径测试: {code: {volume, can_use, avg_cost}}
        self._positions: dict[str, dict] = {
            code: dict(p) for code, p in (positions or {}).items()
        }
        # 可注入日线收盘价序列 (ETF 轮动信号测试用, 2026-08-14):
        # {code: [close1, close2, ...]} 按交易日升序
        self._daily_closes: dict[str, list[float]] = {
            code: list(v) for code, v in (daily_closes or {}).items()
        }
        self._orders: dict[str, dict] = {}
        self._trades: dict[str, dict] = {}
        self._seq = 0
        self._trade_seq = 0
        self._connected = False
        self._quotes: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ── BaseGateway 接口 ────────────────────────────────────────

    def connect(self) -> bool:
        self._connected = True
        return True

    def disconnect(self) -> None:
        self._connected = False

    def order(self, code: str, direction: int, price: float, qty: int,
              price_type: Any = PRICE_TYPE_LIMIT, remark: str = "") -> str:
        if not self._connected:
            raise RuntimeError("FakeGateway 未连接")
        with self._lock:
            self._seq += 1
            order_id = f"FAKE{self._seq:06d}"
            rec = {
                "order_id": order_id, "remark": remark, "code": code,
                "direction": direction, "price": float(price), "qty": int(qty),
                "filled_qty": 0, "status": OS_REPORTED, "ts": self._clock(),
                "price_type": price_type,
            }
            self._orders[order_id] = rec
            # 审计L7修复: 买单下单即冻结 (真实券商口径: 可用转冻结,
            # 总额不变), 成交转实扣 / 撤单废单释放回可用
            if direction == DIRECTION_BUY:
                amount = float(price) * int(qty)
                self._cash -= amount
                self._frozen += amount
        self._fire_order(rec)
        return order_id

    def cancel(self, order_id: str) -> bool:
        """受理撤单 (语义对齐 BaseGateway 契约: 受理≠撤成)。
        默认模式立即推进终态 (兼容既有测试); delayed_cancel 模式
        只受理, 终态由 simulate_cancel_ack 推进 (审计M8修复)。"""
        with self._lock:
            rec = self._orders.get(order_id)
            if rec is None or rec["status"] in TERMINAL_STATUSES:
                return False
            if self._delayed_cancel:
                self._orders[order_id] = dict(rec, _cancel_pending=True)
                return True
            rec = dict(rec, status=OS_CANCELED)
            self._orders[order_id] = rec
            self._release_frozen(rec)
        self._fire_order(rec)
        return True

    def simulate_cancel_ack(self, order_id: str) -> None:
        """delayed_cancel 模式下推进撤单终态 (真机 ack 延迟的剧本)。"""
        with self._lock:
            rec = dict(self._orders[order_id], status=OS_CANCELED)
            rec.pop("_cancel_pending", None)
            self._orders[order_id] = rec
            self._release_frozen(rec)
        self._fire_order(rec)

    def simulate_partial_cancel(self, order_id: str, filled: int = 0) -> None:
        """模拟部撤 (状态 53, OS_PART_CANCEL): 先成交 filled 份, 剩余全部撤销。

        2026-09-17 买侧在途台账核销测试接缝 (原先无钩子, 部撤路径测不到):
        真机「部分成交后撤单」的终态 = 53 且 traded_volume = 已成交量;
        只改订单状态与冻结资金, 不写 _trades (与 simulate_fill 一致 ——
        成交由 simulate_fill 记账, 这里只推进状态)。
        filled 会被夹在 [已有的已成交量, qty] 内。"""
        with self._lock:
            rec = self._orders[order_id]
            if rec["status"] in TERMINAL_STATUSES:
                raise ValueError(
                    f"终态单不可部撤: {order_id} status={rec['status']}")
            filled = max(int(rec.get("filled_qty") or 0),
                         min(int(filled), int(rec["qty"])))
            rec = dict(rec, filled_qty=filled, status=OS_PART_CANCEL)
            self._orders[order_id] = rec
            self._release_frozen(rec)     # 未成交部分释放冻结 (同撤单口径)
        self._fire_order(rec)

    def query_asset(self) -> dict:
        with self._lock:
            market = sum(p["volume"] * p["avg_cost"] for p in self._positions.values())
            return {"cash": self._cash, "frozen_cash": self._frozen,
                    "market_value": market, "total_asset": self._cash + self._frozen + market}

    def query_positions(self) -> list[dict]:
        with self._lock:
            return [dict(p, code=code) for code, p in self._positions.items()
                    if p["volume"] > 0]

    def query_orders(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._orders.values()]

    def query_trades(self) -> list[dict]:
        with self._lock:
            return [dict(t) for t in self._trades.values()]

    def subscribe_quotes(self, codes: list[str]) -> bool:
        return True

    def unsubscribe_all(self) -> None:
        pass

    def query_quotes(self, codes: list[str]) -> dict[str, dict]:
        """轮询兜底: 返回 push_quote 注入的最新快照 (未推过的票缺席)。"""
        return {code: dict(self._quotes[code]) for code in codes
                if code in self._quotes}

    def query_daily_closes(self, code: str, count: int = 500) -> list[float]:
        """返回注入的日线收盘价序列 (ETF 轮动信号测试接缝, 2026-08-14)。
        count 不做裁剪 —— 测试注入的是全序列, 信号计算自己裁窗口。"""
        with self._lock:
            return list(self._daily_closes.get(code, []))

    def query_daily_closes_range(self, code: str, start: str = "",
                                 end: str = "") -> dict[str, float]:
        """Fake 无历史日历数据 → 空字典 (补算侧 fail-closed: 拒写而不是瞎猜)。
        需要补算路径的测试直接注入 close_at 回调, 不走网关。"""
        return {}

    # ── 测试钩子 (脚本化订单行为) ───────────────────────────────

    def simulate_order_ack(self, order_id: str) -> None:
        """重放一次已报 ack (测幂等/乱序)。"""
        with self._lock:
            rec = dict(self._orders[order_id])
        self._fire_order(rec)

    def simulate_fill(self, order_id: str, price: float | None = None,
                      qty: int | None = None) -> dict:
        """模拟 (部分) 成交: 推进订单、持仓、资金, 发委托+成交双回报。
        审计L7修复: 终态单 (已撤/废/已成) 拒绝成交, raise ValueError
        —— 真机不可能成交已终态的单, Fake 放行是在帮上层掩盖状态机洞。"""
        with self._lock:
            rec = self._orders[order_id]
            if rec["status"] in TERMINAL_STATUSES:
                raise ValueError(
                    f"终态单不可成交: {order_id} status={rec['status']}")
            fill_price = float(price if price is not None else rec["price"])
            fill_qty = int(qty if qty is not None else rec["qty"] - rec["filled_qty"])
            rec = dict(rec)
            rec["filled_qty"] += fill_qty
            rec["status"] = (OS_SUCCEEDED if rec["filled_qty"] >= rec["qty"]
                             else OS_PART_SUCC)
            self._orders[order_id] = rec

            self._trade_seq += 1
            trade = {
                "traded_id": f"FAKET{self._trade_seq:06d}",
                "order_id": order_id, "code": rec["code"],
                "direction": rec["direction"], "price": fill_price,
                "qty": fill_qty, "amount": fill_price * fill_qty,
                "ts": self._clock(),
            }
            self._trades[trade["traded_id"]] = trade

            pos = self._positions.setdefault(
                rec["code"], {"volume": 0, "can_use": 0, "avg_cost": 0.0})
            if rec["direction"] == DIRECTION_BUY:
                total = pos["avg_cost"] * pos["volume"] + trade["amount"]
                pos["volume"] += fill_qty
                pos["avg_cost"] = total / pos["volume"]
                # T+1: 当日买入不进 can_use, 与真实券商口径一致。
                # 资金在下单时已 可用→冻结, 成交 = 冻结转持仓市值;
                # 成交价与委托价的差额找零/补扣 (滑点简化处理)
                self._frozen = max(0.0, self._frozen - rec["price"] * fill_qty)
                self._cash += (rec["price"] - fill_price) * fill_qty
            else:
                pos["volume"] = max(0, pos["volume"] - fill_qty)
                pos["can_use"] = max(0, pos["can_use"] - fill_qty)
                self._cash += trade["amount"]

        self._fire_order(rec)
        if self._on_trade:
            self._on_trade(dict(trade))
        return dict(trade)

    def _release_frozen(self, rec: dict) -> None:
        """撤单/废单释放未成交部分的冻结资金回可用 (调用方须持锁)。"""
        if rec["direction"] == DIRECTION_BUY:
            unfilled = rec["qty"] - rec["filled_qty"]
            amount = rec["price"] * unfilled
            self._frozen = max(0.0, self._frozen - amount)
            self._cash += amount

    def simulate_reject(self, order_id: str) -> None:
        """模拟废单 (废单释放冻结, 同撤单口径)。"""
        with self._lock:
            rec = dict(self._orders[order_id], status=OS_JUNK)
            self._orders[order_id] = rec
            self._release_frozen(rec)
        self._fire_order(rec)

    def simulate_disconnect(self, reason: str = "模拟断线") -> None:
        """触发断线回调 (断线守护测试入口)。"""
        self._connected = False
        if self._on_disconnected:
            self._on_disconnected(reason)

    def simulate_external_trade(self, code: str, direction: int,
                                price: float, qty: int) -> dict:
        """注入"用户在券商客户端手工下的单" (2026-07-27 裁决④测试接缝):
        券商端持仓与成交记录都变, 但不触发任何回调 —— 手工单没有
        回报流进本系统, 本地 book/store 不知情, 靠对账认领发现。"""
        with self._lock:
            self._trade_seq += 1
            trade = {
                "traded_id": f"FAKET{self._trade_seq:06d}",
                "order_id": f"EXT{self._trade_seq:06d}",
                "code": code, "direction": direction, "price": float(price),
                "qty": int(qty), "amount": float(price) * int(qty),
                "ts": self._clock(),
            }
            self._trades[trade["traded_id"]] = trade
            pos = self._positions.setdefault(
                code, {"volume": 0, "can_use": 0, "avg_cost": 0.0})
            if direction == DIRECTION_BUY:
                total = pos["avg_cost"] * pos["volume"] + trade["amount"]
                pos["volume"] += qty
                pos["avg_cost"] = total / pos["volume"]
                pos["can_use"] += qty     # 手工买的 T+1 前可用? 从简:
                # 测试只关心对账认领地, can_use 口径不参与断言
                self._cash -= trade["amount"]
            else:
                pos["volume"] = max(0, pos["volume"] - qty)
                pos["can_use"] = max(0, pos["can_use"] - qty)
                self._cash += trade["amount"]
        return dict(trade)

    def push_quote(self, code: str, quote: dict) -> None:
        """推一条行情 (监控腿测试入口)。未订阅也照推 —— 订阅过滤是
        monitor 的职责, 不在网关层假装智能。快照存底供轮询兜底查询。"""
        self._quotes[code] = dict(quote)
        if self._on_quote:
            self._on_quote(code, quote)

    def _fire_order(self, rec: dict) -> None:
        if self._on_order:
            self._on_order(dict(rec))
