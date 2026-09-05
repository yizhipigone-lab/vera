# -*- coding: utf-8 -*-
"""QMT (miniQMT / xtquant) 实盘运行脚本: 70%稳健 + 30%进攻 左侧抄底组合。

═══════════════ 使用前必读 ═══════════════
1. 本脚本在 QMT 环境【未实测】——本机没有 QMT 客户端, 无法联调。
   xtquant 的 API 字段名(如持仓对象的均价字段)以你券商 QMT 版本附带的
   xtquant 文档为准, 首次运行务必: DRY_RUN=True + QMT 模拟账户 跑至少 2 周。
2. 运行环境: Windows + miniQMT 客户端已登录 + pip install xtquant
   (xtquant 不是 pypi 公共包, 从 QMT 客户端安装目录或券商处获取)。
3. 每日流程 (建议交易日 09:31 运行, 此时昨日K线已完整):
   昨日收盘数据算信号 → 开盘买入(T+1口径, 比回测的T收盘买入保守)
   → 检查持仓的止损/阶梯止盈/时间止损 → 挂单卖出。
   盘中不再盯市 (与回测的日频口径一致)。想盘中触发止盈止损,
   请在 QMT 客户端用内置条件单功能兜底, 或自行扩展定时循环。
4. 仓位记账: 本地 JSON 文件持久化 (position_book.json), 重启不丢。
   但【真实持仓以券商为准】, 每日启动时会用 query_stock_positions 对账,
   对不上的仓位会打印警告, 请人工核对。

策略来源: research/auto_iter/2026-08-10_自动策略迭代寻优_研究报告.md
  腿A(70%): iter_828 稳健型  年化18.8%/回撤-12.4%/胜率82%
  腿B(30%): iter_1092 进攻型 年化27.1%/回撤-30.3%/胜率36%
═══════════════════════════════════════════
"""
import os
import sys
import json
import time
import logging
from datetime import datetime

import pandas as pd

# 复用 VERA 项目的信号生成代码 (因子与回测完全同源, 防"两套信号各说各话")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from auto_iter.common import build_signals  # noqa: E402

# ── xtquant 只在 QMT 机器上存在, 本机开发时允许 import 失败 ──
try:
    from xtquant import xtdata
    from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
    from xtquant.xttype import StockAccount
    from xtquant import xtconstant
    QMT_AVAILABLE = True
except ImportError:
    QMT_AVAILABLE = False

# ═══════════════════ 配置区 (按自己情况改) ═══════════════════
DRY_RUN = True                 # True=只打印不下单 (首次必须 True!)
ACCOUNT_ID = "你的资金账号"     # QMT 资金账号, 如 "8880123456"
MINIQMT_PATH = r"D:\国金QMT交易端\userdata_mini"  # miniQMT 安装目录下的 userdata_mini
TOTAL_CAPITAL = 1_000_000.0    # 计划投入本策略的总资金 (元)
MAX_PRICE_FILTER = None        # 如 "只想买30元以下" 设 30.0, 不限设 None

# ── 下单方式 (2026-08-11 改: 废单问题修复) ──
# 原 LATEST_PRICE 市价类报单被券商柜台禁用 (废单码 63596, 市价类型禁程序化)。
# 改限价单 (FIX_PRICE), 两种定价模式:
#   "buffer" (默认, 推荐): 参考价×(1±1%), 实际仍按对手方最优价成交,
#            1% 只是成交保障帽 — 价格快动时成交率更高, 不依赖实时行情接口;
#   "book": 直接挂卖一价(买)/买一价(卖), 取自实时快照 get_full_tick,
#            报价最精确, 但快照到下单的毫秒间价格跳动可能导致挂单不成交,
#            且实时行情取不到时自动回退 buffer 模式。
# 注意运行时段: 连续竞价内 (9:30-14:57) 都有效; 想贴近收盘价就 14:50-14:55 跑,
# 不要 14:57 之后跑 (集合竞价限价单可报但不保证成交, 15:00 后全部废单)。
PRICE_MODE = "buffer"          # "buffer" | "book"
BUY_BUFFER = 0.01              # 买入溢价 1% (宁可贵一点也要成交)
SELL_BUFFER = 0.01             # 卖出折价 1%

# 股票池: None=全市场扫描(慢, 首次约1-2分钟/天); 也可以给固定股票列表
UNIVERSE = None

# 两腿参数 (与回测配方逐字一致, 勿凭感觉改 — 改了就不是验证过的策略了)
LEGS = {
    "A_稳健": {
        "weight": 0.70,
        "factors": [{"name": "ret_drop", "n": 7, "x": 0.2389},
                    {"name": "bias_low", "n": 23, "x": 0.0123},
                    {"name": "rsi_low", "n": 9, "th": 15.0}],
        "cost_stop": -0.25,                      # 亏25%无条件割
        "ladder_tp": [(0.0712, 0.2161),          # (涨幅, 卖出现持仓的比例)
                      (0.1775, 0.1923),
                      (0.3099, 0.2673)],
        "max_hold_days": 41,                     # 到期没戏就撤
        "ticket": 14_000.0,                      # 每信号买入金额(按100万本金口径, 会随总资金等比缩放)
    },
    "B_进攻": {
        "weight": 0.30,
        "factors": [{"name": "bias_ma", "n": 55, "x": 0.2},
                    {"name": "bias_low", "n": 26, "x": 0.0117},
                    {"name": "rsi_low", "n": 8, "th": 19.0121}],
        "cost_stop": -0.25,
        "ladder_tp": [(0.0714, 0.1683),
                      (0.1315, 0.1619),
                      (0.3335, 0.2161)],
        "max_hold_days": 35,
        "ticket": 6_000.0,
    },
}

LOOKBACK_DAYS = 120            # 取数窗口: 最长因子需要55日均线+暖机, 120天足够
BOOK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "position_book.json")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("qmt_combo")


# ═══════════════════ 数据层: QMT → VERA 面板格式 ═══════════════════

def get_universe() -> list:
    """全市场股票列表, 剔除 ST/退市/次新股/1元股 (对齐回测股票池口径)。"""
    if UNIVERSE is not None:
        return UNIVERSE
    stocks = xtdata.get_stock_list_in_sector("沪深A股")
    out = []
    for code in stocks:
        d = xtdata.get_instrument_detail(code) or {}
        name = d.get("InstrumentName", "")
        if "ST" in name or "退" in name:
            continue
        if code.startswith(("300", "301")):   # 创业板: 想纳入就删掉这两行
            continue                           # (回测池含创业板, 但20cm涨跌幅
        out.append(code)                       #  会让-25%止损更难执行, 保守剔除)
    return out


def fetch_panel(codes: list) -> dict:
    """从 QMT 取日线, 组装成 build_signals 需要的 panel (close/low/volume, 日期×股票)。"""
    data = xtdata.get_market_data_ex(
        field_list=[], stock_list=codes, period="1d",
        count=LOOKBACK_DAYS, dividend_type="front", fill_data=False)
    close, low, vol = {}, {}, {}
    for code, df in data.items():
        if df is None or len(df) < 60:        # 上市不足60天的次新股跳过
            continue
        idx = pd.to_datetime(df.index.astype(str))
        close[code] = pd.Series(df["close"].to_numpy(), index=idx)
        low[code] = pd.Series(df["low"].to_numpy(), index=idx)
        vol[code] = pd.Series(df["volume"].to_numpy(), index=idx)
    return {"close": pd.DataFrame(close), "low": pd.DataFrame(low),
            "volume": pd.DataFrame(vol)}


# ═══════════════════ 持仓账本 (本地持久化) ═══════════════════

def load_book() -> dict:
    if os.path.exists(BOOK_FILE):
        with open(BOOK_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_book(book: dict):
    with open(BOOK_FILE, "w", encoding="utf-8") as f:
        json.dump(book, f, ensure_ascii=False, indent=1)


# ═══════════════════ 交易层 ═══════════════════

def make_trader():
    """连接 miniQMT。session_id 用时间戳防多开冲突。"""
    trader = XtQuantTrader(MINIQMT_PATH, int(time.time()))
    trader.register_callback(XtQuantTraderCallback())
    trader.start()
    if trader.connect() != 0:
        raise RuntimeError("连接 miniQMT 失败 — 客户端登录了吗? 路径对吗?")
    acc = StockAccount(ACCOUNT_ID)
    trader.subscribe(acc)
    return trader, acc


def _limit_cap(code: str, side: str, price: float) -> float:
    """涨跌停帽: 限价不得超过当日涨停价(买)/低于跌停价(卖)。
    xtdata.get_instrument_detail 的 UpStopPrice/DownStopPrice 字段名
    各券商版本基本一致; 取不到就不盖帽 (价格笼子 1% 溢价本身够保守)。"""
    try:
        d = xtdata.get_instrument_detail(code) or {}
        if side == "buy" and d.get("UpStopPrice"):
            return min(price, float(d["UpStopPrice"]))
        if side == "sell" and d.get("DownStopPrice"):
            return max(price, float(d["DownStopPrice"]))
    except Exception:
        pass
    return price


def _book_price(code: str, side: str):
    """实时快照的对手方最优价: 买取卖一, 卖取买一。取不到返回 None (回退 buffer)。"""
    try:
        tick = xtdata.get_full_tick([code]).get(code) or {}
        arr = tick.get("askPrice" if side == "buy" else "bidPrice") or []
        px = float(arr[0]) if len(arr) else 0.0
        return px if px > 0 else None
    except Exception:
        return None


def buy(trader, acc, code: str, amount: float, ref_price: float):
    """按金额买入: 换算成整手股数, 限价单 (防券商禁用市价类型 → 63596 废单)。"""
    shares = int(amount / ref_price / 100) * 100
    if shares < 100:
        log.info("跳过 %s: 金额 %.0f 不够买 1 手 (现价 %.2f)", code, amount, ref_price)
        return
    px = None
    if PRICE_MODE == "book":
        px = _book_price(code, "buy")          # 直接卖一价
    if px is None:
        px = ref_price * (1 + BUY_BUFFER)      # buffer 模式 / book 取不到兜底
    px = round(_limit_cap(code, "buy", px), 2)
    log.info("买入 %s %d 股 (≈%.0f 元, 限价 %.2f, 模式 %s)",
             code, shares, amount, px, PRICE_MODE)
    if not DRY_RUN:
        trader.order_stock(acc, code, xtconstant.STOCK_BUY, shares,
                           xtconstant.FIX_PRICE, px, "vera_combo", "")


def sell(trader, acc, code: str, shares: int, reason: str, ref_price: float = None):
    if ref_price is None:
        ref_price = 0.0
    px = None
    if PRICE_MODE == "book":
        px = _book_price(code, "sell")         # 直接买一价
    if px is None and ref_price > 0:
        px = ref_price * (1 - SELL_BUFFER)
    px = round(_limit_cap(code, "sell", px), 2) if px else 0.0
    log.info("卖出 %s %d 股, 原因: %s, 限价 %.2f, 模式 %s",
             code, shares, reason, px, PRICE_MODE)
    if not DRY_RUN and shares > 0:
        if px > 0:
            trader.order_stock(acc, code, xtconstant.STOCK_SELL, shares,
                               xtconstant.FIX_PRICE, px, "vera_combo", reason)
        else:
            # 两种模式都拿不到价时退回市价类报单 (连续竞价时段才安全)
            trader.order_stock(acc, code, xtconstant.STOCK_SELL, shares,
                               xtconstant.LATEST_PRICE, 0, "vera_combo", reason)


def get_real_positions(trader, acc) -> dict:
    """券商真实持仓 {code: (可用股数, 成本价)}。
    ⚠️ 字段名以你 xtquant 版本为准: 常见是 avg_price, 有的是 open_price。"""
    out = {}
    for p in trader.query_stock_positions(acc) or []:
        price = getattr(p, "avg_price", None) or getattr(p, "open_price", 0.0)
        out[p.stock_code] = (p.can_use_volume, float(price))
    return out


# ═══════════════════ 出场判定 (与回测引擎同语义) ═══════════════════

def check_exits(leg_name: str, leg: dict, pos: dict, last_close: float):
    """按优先级返回 (卖出股数, 原因) 或 None。日频收盘后判定, 次日执行。
    pos = {shares, entry_price, peak, entry_date, stage}
    注意: 回测是"当天触发当天收盘价成交", 实盘 T+1 开盘成交, 会有滑点偏差。
    """
    pnl = last_close / pos["entry_price"] - 1
    # 1) 成本止损: 亏 25% 全割
    if pnl <= leg["cost_stop"]:
        return pos["shares"], f"成本止损({pnl:+.1%})"
    # 2) 阶梯止盈: 按 stage 逐档触发 (每档只卖一次)
    levels = leg["ladder_tp"]
    if pos["stage"] < len(levels):
        profit_th, ratio = levels[pos["stage"]]
        if pnl >= profit_th:
            sell_shares = int(pos["shares"] * ratio / 100) * 100
            return max(sell_shares, 100), f"阶梯止盈第{pos['stage']+1}档({pnl:+.1%})"
    # 3) 时间止损: 到期全撤
    hold_days = (datetime.now() - datetime.strptime(pos["entry_date"],
                                                    "%Y-%m-%d")).days
    if hold_days >= leg["max_hold_days"]:
        return pos["shares"], f"时间止损(持有{hold_days}天)"
    return None


# ═══════════════════ 主流程: 每个交易日跑一次 ═══════════════════

def daily_run():
    if not QMT_AVAILABLE:
        raise RuntimeError("xtquant 未安装 — 请在 QMT 机器上运行本脚本")
    trader, acc = make_trader()
    book = load_book()
    codes = get_universe()
    log.info("股票池 %d 只, 开始取数...", len(codes))
    panel = fetch_panel(codes)
    last_close = panel["close"].iloc[-1]
    log.info("数据截至 %s", str(panel["close"].index[-1].date()))

    scale = TOTAL_CAPITAL / 1_000_000.0   # 单笔金额随总资金等比缩放
    real_pos = get_real_positions(trader, acc) if not DRY_RUN else {}

    # ── 第一步: 处理持仓 (先卖后买, 释放资金) ──
    for code, pos in list(book.items()):
        if code not in last_close or pd.isna(last_close[code]):
            continue
        pos["peak"] = max(pos["peak"], float(last_close[code]))
        leg = LEGS[pos["leg"]]
        decision = check_exits(pos["leg"], leg, pos, float(last_close[code]))
        if decision:
            shares, reason = decision
            can_use = real_pos.get(code, (pos["shares"], 0))[0]
            shares = min(shares, int(can_use // 100) * 100) if not DRY_RUN else shares
            if shares >= 100:
                sell(trader, acc, code, shares, reason,
                     ref_price=float(last_close[code]))
                pos["shares"] -= shares
                if "阶梯" in reason:
                    pos["stage"] += 1
            if pos["shares"] < 100:
                del book[code]

    # ── 第二步: 两条腿分别算信号、买入 ──
    for leg_name, leg in LEGS.items():
        entries = build_signals(panel, "left", leg["factors"])
        today = entries.iloc[-1]
        picks = today[today].index.tolist()
        log.info("[%s] 今日信号 %d 个", leg_name, len(picks))
        ticket = leg["ticket"] * scale
        for code in picks:
            if code in book:                    # 已持有不重复买
                continue
            px = float(last_close[code])
            if px < 1.0:                        # 1元股红线 (回测池口径)
                continue
            if MAX_PRICE_FILTER and px > MAX_PRICE_FILTER:
                continue
            buy(trader, acc, code, ticket, px)
            book[code] = {"leg": leg_name, "shares": int(ticket / px / 100) * 100,
                          "entry_price": px, "peak": px,
                          "entry_date": str(panel["close"].index[-1].date()),
                          "stage": 0}

    save_book(book)
    log.info("完成。当前记账持仓 %d 只 (DRY_RUN=%s)", len(book), DRY_RUN)
    if not DRY_RUN:
        real = get_real_positions(trader, acc)
        diff = set(book) ^ set(real)
        if diff:
            log.warning("账本与券商持仓不一致: %s — 请人工核对!", diff)


if __name__ == "__main__":
    daily_run()
