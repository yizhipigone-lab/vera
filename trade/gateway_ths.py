"""trade/gateway_ths.py — 同花顺 GUI 客户端网关 (easytrader 唯一收口)。

设计意图:
    全项目唯一允许 import easytrader 的文件, 且只在 ThsGuiGateway 方法内
    lazy import (铁律沿用 gateway.py: 无 GUI/无客户端的开发机必须能跑
    全量测试)。

    与 RealGateway(QMT) 的本质差异, 调用方须知:
    1. 通道原理是"模拟人点鼠标键盘"操作同花顺下单客户端 —— 慢 (秒级)、
       脆 (客户端升级改版即失效)、无回报推送。委托/成交全靠 reconciler
       轮询 query_orders/query_trades 补齐, 回调回调一路都不接。
    2. THS 客户端下单没有 remark 字段, VERA 的单号体系 (V{mmdd}-{seq})
       带不到券商侧; 券商侧合同编号下单成功后才从弹窗里拿到。拿不到时
       发本地占位号 (THS{mmdd}-{seq}) 并记 warning —— 对账只能靠
       reconciler 按 code/价格/数量/时间窗认领。
    3. armed 保险栓 (2026-09-06): 构造默认 armed=False, order() 直接
       raise —— GUI 下单不可撤回, 必须配置显式武装才放行。查询类接口
       不受 armed 限制 (只读)。
    4. 无行情腿: GUI 客户端不是行情源。subscribe_quotes 空操作,
       query_quotes 返回空 (monitor 视为陈旧 fail-closed)。行情必须
       另接数据源, 不在本网关职责内。
    5. 客户端版本适配: easytrader 的控件 ID/菜单名写死在
       easytrader/config/client.py (CommonConfig), 适配的是经典
       同花顺下单客户端 ("网上股票交易系统5.0")。同花顺智能交易等新版
       DuiLib 界面可能不兼容 —— 实盘启用前必须先只读连通性实测
       (balance/position 能取到数) 再武装。
"""

from __future__ import annotations

import time
from typing import Any

from trade.book import (
    DIRECTION_BUY,
    DIRECTION_SELL,
    OS_CANCELED,
    OS_JUNK,
    OS_PARTSUCC_CANCEL,
    OS_PART_CANCEL,
    OS_PART_SUCC,
    OS_REPORTED,
    OS_REPORTED_CANCEL,
    OS_SUCCEEDED,
    OS_UNREPORTED,
    OS_WAIT_REPORTING,
    PRICE_TYPE_LIMIT,
    PRICE_TYPE_MARKET_PEER_FIRST,
    PRICE_TYPE_SZ_5LEVEL_CANCEL,
)
from trade.gateway import BaseGateway
from utils.logger import get_logger

_logger = get_logger("trade.gateway_ths")


# 委托状态说明文字 → OS_* 常量。列表顺序即匹配优先级:
# 长串在前 ("部成待撤" 含子串 "部成", 必须先比)。
_STATUS_TEXT_MAP: tuple[tuple[str, int], ...] = (
    ("全部成交", OS_SUCCEEDED),
    ("部成待撤", OS_PARTSUCC_CANCEL),
    ("已报待撤", OS_REPORTED_CANCEL),
    ("部撤", OS_PART_CANCEL),
    ("部成", OS_PART_SUCC),
    ("已成", OS_SUCCEEDED),
    ("已撤", OS_CANCELED),
    ("废单", OS_JUNK),
    ("已报", OS_REPORTED),
    ("未报", OS_UNREPORTED),
    ("待报", OS_WAIT_REPORTING),
)


def _status_from_text(text: str) -> int:
    """THS 委托"状态说明"中文 → OS_* 常量。未识别按已报处理
    (保守: 不假装终态, 等 reconciler 下轮再看)。"""
    for key, status in _STATUS_TEXT_MAP:
        if key in text:
            return status
    return OS_REPORTED


def _to_float(x: Any) -> float:
    """表格单元格 → float。Copy 策略经剪贴板取值, 数字可能带千分位
    逗号或干脆是字符串; 解析失败给 0.0 (查询口径, 宁缺毋滥)。"""
    try:
        return float(str(x).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _to_int(x: Any) -> int:
    return int(_to_float(x))


def _code6(code: str) -> str:
    """VERA 代码 ("159949.SZ") → THS 六位代码 ("159949")。"""
    return code.split(".")[0]


def _vera_code(code6: str) -> str:
    """THS 六位代码 → VERA 带市场后缀代码。6/5/9 头沪市, 0/1/2/3 头深市,
    4/8 头北交所。无市场信息的客户端返回值全靠这个前缀规则补。"""
    if code6.startswith(("6", "5", "9")):
        return f"{code6}.SH"
    if code6.startswith(("4", "8")):
        return f"{code6}.BJ"
    return f"{code6}.SZ"


def _hms_to_ts(text: str, clock) -> float | None:
    """"09:30:01" → 当日 epoch 秒 (客户端只给时分秒)。解析失败给 None
    (与 gateway.py 同口径: 不伪装时间戳)。"""
    try:
        h, m, s = (int(p) for p in str(text).strip().split(":"))
        base = time.localtime(clock())
        return time.mktime(base[:3] + (h, m, s) + base[6:])
    except (TypeError, ValueError):
        return None


def _direction_from_text(text: str) -> int:
    return DIRECTION_BUY if "买" in str(text) else DIRECTION_SELL


class ThsGuiGateway(BaseGateway):
    """同花顺 GUI 网关。所有 easytrader import 都在方法内 lazy。

    connect() 只 attach 到【已登录、已运行】的客户端进程 —— 登录必须
    人工完成 (账号密码不进本系统, 不落盘)。disconnect() 只解除 attach,
    绝不杀客户端进程。
    """

    def __init__(self, exe_path: str = r"D:\thsstio\xiadan.exe", *,
                 armed: bool = False, title_re: str = ".*网上股票交易系统.*",
                 clock=time.time, **callbacks: Any):
        super().__init__(**callbacks)
        self._exe_path = exe_path
        self._armed = armed
        self._title_re = title_re
        self._clock = clock
        self._trader = None
        self._seq = 0

    def set_armed(self, armed: bool) -> None:
        """武装锁 (ChannelManager 调用)。order() 下单前检查 _armed,
        False 直接 raise —— GUI 下单不可撤回, 探针不过谁也别想放行。"""
        self._armed = bool(armed)

    @staticmethod
    def _easytrader():
        """lazy import 唯一入口。缺库时给一句人话。"""
        try:
            import easytrader
        except ImportError as e:
            raise RuntimeError(
                "未安装 easytrader: 同花顺 GUI 网关不可用。"
                "请 pip install easytrader, 测试请用 FakeGateway。"
            ) from e
        return easytrader

    def connect(self) -> bool:
        """attach 已登录的同花顺客户端。客户端没启动/没登录 → 抛异常,
        由调用方 (重连循环) 处置 —— GUI 通道无法代登录。"""
        easytrader = self._easytrader()
        trader = easytrader.use("ths")
        self._attach(trader)
        self._trader = trader
        _logger.info("THS GUI 网关已连接: %s", self._exe_path)
        return True

    def _attach(self, trader) -> None:
        """优先按窗口标题 attach (2026-09-06 实测: 非管理员权限读不到
        进程路径, 按路径 attach 报 ProcessNotFoundError; 按标题不需要)。
        标题匹配不到才退回 easytrader 原生按路径 attach。
        title_re 构造可配 —— 换客户端版本 (如"同花顺智能交易") 时只改配置。"""
        import pywinauto  # lazy, 与 easytrader 同生命周期
        try:
            app = pywinauto.Application().connect(
                title_re=self._title_re, timeout=5)
        except Exception:
            trader.connect(self._exe_path)
            return
        # 复刻 ClientTrader.connect 的后三步, 但 _main 锁标题窗口
        # (top_window 可能是弹窗)
        trader._app = app
        trader._close_prompt_windows()
        trader._main = app.window(title_re=self._title_re)
        trader._init_toolbar()

    def disconnect(self) -> None:
        # 只解除引用, 不关客户端 —— 客户端是用户的, 还可能有人工单
        self._trader = None

    def order(self, code: str, direction: int, price: float, qty: int,
              price_type: Any = PRICE_TYPE_LIMIT, remark: str = "") -> str:
        """下单。armed=False 直接 raise (保险栓, 见模块 docstring 3)。

        price_type 映射: 限价 → 普通买/卖; MARKET_PEER_FIRST → 市价
        "对手方最优价格"; SZ_5LEVEL_CANCEL → 市价 "最优五档即时成交剩余
        撤销" (与 RealGateway 的逃生通道语义对齐)。"""
        if not self._armed:
            raise RuntimeError(
                "THS GUI 网关未武装 (armed=False), 下单被拒。"
                "GUI 下单不可撤回, 请确认只读实测通过后在配置里显式 armed=True。")
        if self._trader is None:
            raise RuntimeError("THS GUI 网关未连接")
        code6 = _code6(code)
        t0 = self._clock()
        if price_type == PRICE_TYPE_MARKET_PEER_FIRST:
            result = self._market(code6, direction, qty, "对手方最优价格", price)
        elif price_type == PRICE_TYPE_SZ_5LEVEL_CANCEL:
            result = self._market(code6, direction, qty,
                                  "最优五档即时成交剩余撤销", price)
        elif direction == DIRECTION_BUY:
            result = self._trader.buy(code6, price=float(price), amount=int(qty))
        else:
            result = self._trader.sell(code6, price=float(price), amount=int(qty))
        entrust_no = str((result or {}).get("entrust_no") or "").strip()
        elapsed = self._clock() - t0
        if entrust_no:
            _logger.info("THS 下单回报: %s %s %s股 @%s → 合同编号 %s (%.1fs)",
                         "买" if direction == DIRECTION_BUY else "卖",
                         code, qty, price, entrust_no, elapsed)
            return entrust_no
        self._seq += 1
        local_id = time.strftime("THS%m%d-", time.localtime(self._clock())) \
            + f"{self._seq:03d}"
        _logger.warning(
            "THS 下单未取到合同编号 (%s %s %s股 @%s, 弹窗返回 %s), "
            "发本地占位号 %s —— 此单靠 reconciler 按代码/价量认领, remark 丢失",
            code, direction, qty, price, result, local_id)
        return local_id

    def _market(self, code6: str, direction: int, qty: int,
                ttype: str, limit_price: float) -> dict:
        fn = self._trader.market_buy if direction == DIRECTION_BUY \
            else self._trader.market_sell
        return fn(code6, amount=int(qty), ttype=ttype, limit_price=limit_price)

    def cancel(self, order_id: str) -> bool:
        """撤单 (按合同编号)。契约同 BaseGateway: True=已受理, 受理≠撤成,
        终态以 query_orders 轮询为准。"""
        if self._trader is None:
            raise RuntimeError("THS GUI 网关未连接")
        result = self._trader.cancel_entrust(str(order_id))
        ok = (result or {}).get("message") == "success"
        if not ok:
            _logger.warning("THS 撤单未受理: %s → %s", order_id, result)
        return ok

    def query_asset(self) -> dict:
        """资金快照。口径对齐 RealGateway (159949 事件教训): total_asset
        不信客户端汇总字段, 按 可用+冻结+市值 重算。
        THS 口径: 资金余额=可用+冻结, 可用金额=可用。"""
        b = self._trader.balance
        cash = _to_float(b.get("可用金额"))
        balance = _to_float(b.get("资金余额"))
        frozen = max(0.0, balance - cash)
        market = _to_float(b.get("股票市值"))
        return {"cash": cash, "frozen_cash": frozen,
                "market_value": market,
                "total_asset": cash + frozen + market}

    def query_positions(self) -> list[dict]:
        """持仓。avg_cost 取客户端"成本价" —— 注意与 RealGateway 的
        open_price (不摊薄成本) 口径不同: THS 成本价会被卖出摊薄,
        预埋档位价基准有漂移风险 (GUI 通道的已知口径债)。"""
        rows = self._trader.position
        out = []
        for r in rows or []:
            volume = _to_int(r.get("股票余额"))
            if volume <= 0:
                continue
            out.append({
                "code": _vera_code(str(r.get("证券代码", "")).strip()),
                "volume": volume,
                "can_use": _to_int(r.get("可用余额")),
                "avg_cost": _to_float(r.get("成本价")),
            })
        return out

    def query_orders(self) -> list[dict]:
        """当日委托。order_id=合同编号 (券商侧真相); remark 恒空
        (THS 无此字段, 见模块 docstring 2)。"""
        rows = self._trader.today_entrusts
        return [{
            "order_id": str(r.get("合同编号", "")).strip(),
            "remark": "",
            "code": _vera_code(str(r.get("证券代码", "")).strip()),
            "direction": _direction_from_text(r.get("操作", "")),
            "price": _to_float(r.get("委托价格")),
            "qty": _to_int(r.get("委托数量")),
            "filled_qty": _to_int(r.get("成交数量")),
            "status": _status_from_text(str(r.get("状态说明", ""))),
            "status_msg": str(r.get("状态说明", "")),
            "ts": _hms_to_ts(str(r.get("委托时间", "")), self._clock),
        } for r in rows or []]

    def query_trades(self) -> list[dict]:
        rows = self._trader.today_trades
        return [{
            "traded_id": str(r.get("成交编号", "")).strip(),
            "order_id": str(r.get("合同编号", "")).strip(),
            "code": _vera_code(str(r.get("证券代码", "")).strip()),
            "direction": _direction_from_text(r.get("操作", "")),
            "price": _to_float(r.get("成交均价")),
            "qty": _to_int(r.get("成交数量")),
            "amount": _to_float(r.get("成交金额")),
            "ts": _hms_to_ts(str(r.get("成交时间", "")), self._clock),
        } for r in rows or []]

    # ── 行情腿: 不提供 (模块 docstring 4) ─────────────────────

    def subscribe_quotes(self, codes: list[str]) -> bool:
        _logger.warning("THS GUI 网关无行情腿, subscribe_quotes 空操作; "
                        "行情必须另接数据源")
        return True

    def unsubscribe_all(self) -> None:
        pass

    def query_quotes(self, codes: list[str]) -> dict[str, dict]:
        """永远返回空 —— monitor 视为陈旧 fail-closed, 不会拿假行情驱动
        自动规则 (2026-07-27 ETF 误卖事件口径)。"""
        return {}
