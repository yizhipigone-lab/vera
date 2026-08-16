"""trade/notifier.py — 飞书 webhook 通知 (成交 + 盘后日报)。

设计意图:
    飞书推送是阻塞网络调用, 绝不能跑在消费者线程 (唯一写者, 铁律 3)
    或回调线程 (铁律 2)。本模块自带一个 daemon worker 线程 + 有界队列:
    notify_fill / notify_daily 只往队里塞一个裸 dict 就返回 (微秒级,
    消费者线程零阻塞), worker 线程独占所有慢活 (查股票名 / 拼卡片 / POST)。

    webhook URL 走环境变量 FEISHU_WEBHOOK_URL (用户裁决: 半密钥不入 yaml);
    enabled 开关在 config.feishu.enabled, 设置面板可热关。URL 缺失或
    enabled=False → 通知器为 no-op (启动告警一次), 交易照常。

    fail-soft: 任何 POST/解析异常只记日志, 永不上抛, 永不影响交易
    (飞书宕机 ≠ 该停交易)。如无必要勿增实体: 用 stdlib urllib, 不引依赖。
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable

from utils.feishu_webhook import post_webhook
from utils.logger import get_logger

_logger = get_logger("trade.notifier")

_WEBHOOK_ENV = "FEISHU_WEBHOOK_URL"
_POST_TIMEOUT_SEC = 5.0
_QUEUE_MAXSIZE = 1000

DIRECTION_BUY = 23   # 与 trade.book 同值 (不 import book, 避免环)


class FeishuNotifier:
    """飞书通知器: 有界队列 + 单 worker 线程, 对交易线程零阻塞。

    生产侧 (notify_fill/notify_daily) 只 put_nowait 一个裸 dict;
    消费侧重活 (查股票名 / 拼卡片 / POST / 超时重试) 全在 worker 线程。
    """

    def __init__(self, enabled_getter: Callable[[], bool],
                 webhook_getter: Callable[[], str | None],
                 clock: Callable[[], float] = time.time):
        self._enabled_getter = enabled_getter
        self._webhook_getter = webhook_getter
        self._clock = clock
        self._queue: queue.Queue[dict | None] = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._name_map: dict | None = None   # 惰性, worker 线程首次查名时加载
        self._warned_no_url = False

    # ── 生产侧 (消费者线程调, 必须飞快) ──────────────────────────

    def notify_fill(self, payload: dict) -> None:
        """成交通知。payload 含全部字段, 这里只入队。队列满丢弃+告警。"""
        if not self._enabled_getter():
            return
        if not self._webhook_getter():
            self._warn_no_url_once()
            return
        try:
            self._queue.put_nowait({"kind": "fill", "data": payload})
        except queue.Full:
            _logger.warning("飞书通知队列满 (%d), 丢弃一笔成交通知",
                            self._queue.maxsize)

    def notify_daily(self, payload: dict, level: str = "full",
                     ai_review: bool = False) -> None:
        """盘后日报。同 notify_fill, 只入队。level: full=全明细/summary=简报
        (2026-08-07), 透传给 worker 的 _build_daily_card; ai_review (2026-08-15)
        追加「AI 复盘」段 (LLM 在 worker 线程跑, 失败返 None 跳过)。"""
        if not self._enabled_getter():
            return
        if not self._webhook_getter():
            self._warn_no_url_once()
            return
        try:
            self._queue.put_nowait(
                {"kind": "daily", "data": payload, "level": level,
                 "ai_review": ai_review})
        except queue.Full:
            _logger.warning("飞书通知队列满 (%d), 丢弃日报", self._queue.maxsize)

    # ── 生命周期 ─────────────────────────────────────────────────

    def start(self) -> None:
        """启动 worker 线程。重复调用 no-op (防起第二个)。
        M-功2 (2026-07-31 审计): 启动即查 URL —— 别让用户配了 enabled=true
        却忘了设环境变量, 直到当天首笔成交才发现飞书其实没通。"""
        if self._thread is not None and self._thread.is_alive():
            return
        if self._enabled_getter() and not self._webhook_getter():
            self._warn_no_url_once()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="feishu-notifier", daemon=True)
        self._thread.start()

    def stop(self, drain_timeout_sec: float = 2.0) -> None:
        """优雅退出: 哨兵 None 唤醒 worker, join 等其发完最后一批。"""
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=drain_timeout_sec + _POST_TIMEOUT_SEC)
            self._thread = None

    # ── worker (独占慢活, 异常永不上抛) ──────────────────────────

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                return
            try:
                self._handle(job)
            except Exception:
                _logger.exception("飞书通知处理异常 (worker 继续)")

    def _handle(self, job: dict) -> None:
        webhook = self._webhook_getter()
        if not webhook:
            return
        kind = job.get("kind")
        data = job.get("data") or {}
        if kind == "fill":
            card = self._build_fill_card(data)
        elif kind == "daily":
            card = self._build_daily_card(data, level=job.get("level", "full"),
                                          ai_review=job.get("ai_review", False))
        else:
            return
        self._post(webhook, card)

    def _post(self, webhook: str, body: dict) -> None:
        """POST 卡片。已下沉 utils.feishu_webhook.post_webhook（读业务 code 判真送达）。"""
        post_webhook(webhook, body, context="交易")

    # ── 股票名 (worker 线程惰性加载, 不阻塞交易线程) ────────────

    def _name_of(self, code: str) -> str:
        if self._name_map is None:
            try:
                from core.data_fetcher import DataFetcher
                self._name_map = DataFetcher.get_name_map() or {}
            except Exception:
                self._name_map = {}
        return self._name_map.get(code, "")

    def _warn_no_url_once(self) -> None:
        if self._warned_no_url:
            return
        self._warned_no_url = True
        _logger.warning(
            "飞书通知已启用但未配置环境变量 %s, 通知为 no-op (交易不受影响)",
            _WEBHOOK_ENV)

    # ── 卡片构建 ────────────────────────────────────────────────

    def _build_fill_card(self, d: dict) -> dict:
        """成交通知卡: 买入绿/卖出橙, 主体 lark_md。"""
        code = d.get("code", "")
        name = d.get("name") or self._name_of(code)
        is_buy = d.get("direction") == DIRECTION_BUY
        price = float(d.get("price", 0.0) or 0.0)
        qty = int(d.get("qty", 0) or 0)
        amount = d.get("amount")
        if amount is None:
            amount = round(price * qty, 2)
        ts = d.get("ts")
        time_str = (time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(ts)) if ts else "")
        action = "买入" if is_buy else "卖出"
        label = d.get("label") or action
        title_stock = f"{name} {code}" if name else code

        lines = [f"**{title_stock}**",
                 f"{action} · {label}",
                 f"单价 {price:.2f} × {qty} 股 = **{float(amount):,.2f}**"]
        # 卖出额外信息: 盈亏% / 档位比例 / 剩余股数市值 (基础版)
        if not is_buy:
            extra = []
            pnl_pct = d.get("pnl_pct")
            pnl_amount = d.get("pnl_amount")
            if pnl_pct is not None or pnl_amount is not None:
                parts = []
                if pnl_amount is not None:
                    sign = "+" if pnl_amount >= 0 else ""
                    parts.append(f"{sign}{float(pnl_amount):,.2f}")
                if pnl_pct is not None:
                    parts.append(f"{float(pnl_pct):+.2f}%")
                extra.append("盈亏 " + "（".join(parts) + "）" if len(parts) == 2 else
                             ("盈亏 " + parts[0]))
            tier = d.get("tier")
            sell_ratio = d.get("sell_ratio")
            if tier is not None and sell_ratio is not None:
                extra.append(f"档位{int(tier) + 1} 卖{float(sell_ratio):.0%}")
            remaining_vol = d.get("remaining_vol")
            if remaining_vol is not None:
                rv = d.get("remaining_value")
                rv_str = f"{float(rv):,.2f}" if rv is not None else "—"
                extra.append(f"剩余 {int(remaining_vol)} 股 / 市值 {rv_str}")
            if extra:
                lines.append(" · ".join(extra))
        if time_str:
            lines.append(f"_时间 {time_str}_")

        return {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text",
                              "content": f"{action}提醒 · {title_stock}"},
                    "template": "green" if is_buy else "orange"},
                "elements": [
                    {"tag": "div",
                     "text": {"tag": "lark_md",
                              "content": "\n".join(lines)}}],
            },
        }

    def _build_daily_card(self, d: dict, level: str = "full",
                          ai_review: bool = False) -> dict:
        """盘后日报卡 (2026-08-07 全明细): 多 section, header 盈亏红绿。
        level: full=全明细 / summary=只资产+交易摘要 (不出变动/明细)。
        ai_review (2026-08-15): 追加「AI 复盘」段 (LLM, worker 线程, 失败跳过)。
        缺字段 fail-soft (该 section 省略, 不抛)。"""
        total_asset = float(d.get("total_asset", 0.0) or 0.0)
        cash = float(d.get("cash", 0.0) or 0.0)
        market_value = d.get("market_value")
        if market_value is None:
            market_value = total_asset - cash
        day_pnl = d.get("day_pnl")
        day_pnl_pct = d.get("day_pnl_pct")
        pos_count = d.get("position_count")
        floating_pnl = d.get("floating_pnl")
        ts = d.get("ts")
        date_str = time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else ""

        # ── 资产 section ──
        if day_pnl is None:
            pnl_line = "—"
        else:
            sign = "+" if day_pnl >= 0 else ""
            pnl_line = f"{sign}{float(day_pnl):,.2f}"
            if day_pnl_pct is not None:
                pnl_line += f" ({sign}{float(day_pnl_pct):.2f}%)"
        asset_lines = [f"总资产 **{total_asset:,.2f}**",
                       f"市值 {float(market_value):,.2f} · 现金 {cash:,.2f}",
                       f"当日盈亏 **{pnl_line}**"]
        if pos_count is not None:
            asset_lines.append(f"持仓 {int(pos_count)} 只")
        if floating_pnl is not None:
            fp = float(floating_pnl)
            asset_lines.append(f"浮盈 {('+' if fp >= 0 else '')}{fp:,.2f}")
        template = "green" if (day_pnl is not None and day_pnl >= 0) else "red"
        elements: list = [
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": "\n".join(asset_lines)}}]

        # ── 交易摘要 section (有买卖笔数才出) ──
        buy_count = d.get("buy_count")
        sell_count = d.get("sell_count")
        if buy_count is not None or sell_count is not None:
            tlines = [f"买 {int(buy_count or 0)} · 卖 {int(sell_count or 0)}"]
            turnover = d.get("turnover")
            if turnover is not None:
                tlines.append(f"成交额 {float(turnover):,.2f}")
            realized = d.get("realized_pnl")
            if realized is not None:
                r = float(realized)
                win_rate = d.get("win_rate")
                wr = f" 胜率 {float(win_rate) * 100:.0f}%" if win_rate is not None else ""
                tlines.append(f"已实现盈亏 {('+' if r >= 0 else '')}{r:,.2f}{wr}")
            elements.append({"tag": "hr"})
            elements.append({"tag": "div", "text": {"tag": "lark_md",
                                                    "content": " · ".join(tlines)}})

        # ── 仓位变动 section (summary 档省略; 有变化才出) ──
        if level != "summary":
            changes = d.get("position_changes") or {}
            clines = []
            for key, label in (("new", "新进"), ("closed", "清仓"),
                               ("added", "加仓"), ("reduced", "减仓")):
                items = changes.get(key) or []
                if items:
                    codes = ", ".join(
                        f"{it.get('code')}({('+' if it.get('delta', 0) >= 0 else '')}"
                        f"{int(it.get('delta', 0))})" for it in items)
                    clines.append(f"{label}: {codes}")
            if clines:
                elements.append({"tag": "hr"})
                elements.append({"tag": "div", "text": {"tag": "lark_md",
                                                        "content": "**仓位变动**\n" + "\n".join(clines)}})

        # ── 交易明细 section (summary 档省略; 2026-08-07 双列重排) ──
        # 优先 trade_details (买卖混排双列, 含简称/盈亏着色); fallback sell_details
        # (旧 payload, 单列文本, 向后兼容)。
        if level != "summary":
            details = d.get("trade_details")
            if details:
                elements.append({"tag": "hr"})
                elements.append({"tag": "div", "text": {"tag": "lark_md",
                                                        "content": "**交易明细**"}})
                elements.extend(self._build_trade_blocks(details))
                folded = d.get("trade_details_folded")
                if folded:
                    fc = int(folded.get("count", 0))
                    ssp = float(folded.get("sum_sell_pnl", 0.0) or 0.0)
                    sign = "+" if ssp >= 0 else ""
                    elements.append({"tag": "div", "text": {"tag": "lark_md",
                        "content": f"另 {fc} 笔（卖出合计 **{sign}{ssp:,.2f}**）"}})
            else:
                sells = d.get("sell_details") or []
                if sells:
                    slines = ["**卖出明细**"]
                    for s in sells:
                        pa = float(s.get("pnl_amount", 0.0) or 0.0)
                        pp = s.get("pnl_pct")
                        pp_str = (f" ({('+' if float(pp) >= 0 else '')}{float(pp):.2f}%)"
                                  if pp else "")
                        reason = s.get("reason") or ""
                        rstr = f" · {reason}" if reason else ""
                        slines.append(
                            f"{s.get('code')} {('+' if pa >= 0 else '')}{pa:,.2f}{pp_str}{rstr}")
                    folded = d.get("sell_details_folded")
                    if folded:
                        fc, fs = int(folded.get("count", 0)), float(folded.get("sum_pnl_amount", 0.0) or 0.0)
                        slines.append(f"另 {fc} 笔合计 {('+' if fs >= 0 else '')}{fs:,.2f}")
                    elements.append({"tag": "hr"})
                    elements.append({"tag": "div", "text": {"tag": "lark_md",
                                                            "content": "\n".join(slines)}})

        # ── AI 复盘 section (2026-08-15, 第一层 LLM 加持) ──
        # 开关 ai_review (默认关, 避免测试打真 LLM + 用户可控成本); 只在 worker
        # 线程跑 (LLM 阻塞 ≤45s, 绝不碰消费者线程); 失败返 None 跳过, 日报照常。
        if ai_review:
            try:
                from trade.llm_review import build_daily_summary
                summary = build_daily_summary(d)
                if summary:
                    elements.append({"tag": "hr"})
                    elements.append({"tag": "div", "text": {"tag": "lark_md",
                                                            "content": "**AI 复盘**\n" + summary}})
            except Exception:
                _logger.debug("AI 复盘生成失败 (跳过, 日报照常)")

        return {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text",
                                     "content": f"收盘日报 · {date_str}"},
                           "template": template},
                "elements": elements,
            },
        }

    def _build_trade_blocks(self, details: list) -> list:
        """交易明细单列每条一块 (2026-08-08): 标题行 + 明细文本。
        卖出: 数量×单价=金额 / 盈亏(±比例, 盈绿亏红) / 原因 / 卖出比例 / 剩余股数·市值;
        买入: 数量×单价=金额 / 原因 / 剩余股数·市值 (无盈亏无比例, 买入不显示比例)。
        缺字段 fail-soft (该行省略)。卖出比例=本次卖出÷累计买入 (用户口径); 简称走
        _name_of (worker 线程惰性查, 查不到回退代码)。"""
        blocks: list = []
        for s in details:
            code = s.get("code", "")
            name = self._name_of(code)
            is_buy = s.get("direction") == DIRECTION_BUY
            head = f"**{name} {code}**" if name else f"**{code}**"
            price = float(s.get("price", 0.0) or 0.0)
            qty = int(s.get("qty", 0) or 0)
            amount = float(s.get("amount", 0.0) or 0.0)
            lines = [f"{head} · {'买入' if is_buy else '卖出'}",
                     f"{qty}股 × {price:.2f} = **{amount:,.2f}**"]
            if not is_buy:
                pa = float(s.get("pnl_amount", 0.0) or 0.0)
                color = "green" if pa >= 0 else "red"
                sign = "+" if pa >= 0 else ""
                pp = s.get("pnl_pct")
                pp_str = f" ({sign}{float(pp):.2f}%)" if pp is not None else ""
                lines.append(
                    f"盈亏 <font color=\"{color}\">**{sign}{pa:,.2f}**</font>{pp_str}")
            reason = (s.get("reason") or "").strip()
            if reason:
                lines.append(f"原因 {reason}")
            if not is_buy:
                sr = s.get("sell_ratio")
                if sr is not None:
                    lines.append(f"卖出比例 {float(sr) * 100:.0f}%")
            rem = s.get("remaining_vol")
            if rem is not None:
                rv = s.get("remaining_value")
                rv_str = f" · 市值 {float(rv):,.2f}" if rv is not None else ""
                lines.append(f"剩余 {int(rem)}股{rv_str}")
            blocks.append({"tag": "div", "text": {"tag": "lark_md",
                                                  "content": "\n".join(lines)}})
        return blocks
