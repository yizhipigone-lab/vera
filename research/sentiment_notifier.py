"""research/sentiment_notifier.py — 舆情/情绪 飞书推送器 (独立, 物理隔离)。

审计 HIGH-2 决策: 本模块绝不 import trade/ 包, 与交易模块物理零耦合
(守业务铁律 1: 情绪只报告显示, 绝不影响交易)。架构参考 trade/notifier.py
(daemon worker + 有界队列 + 生产侧微秒入队 + 读飞书业务码), 但用纯 stdlib
重写, 不共享代码。飞书卡片格式参考 _build_fill_card 的 lark_md 结构 (只抄格式)。

webhook 走环境变量 FEISHU_WEBHOOK_URL (与交易通知器共用同一 webhook, 用户
裁决: 半密钥不入 yaml)。URL 缺失 → no-op (启动告警一次)。

fail-soft: 任何 POST/解析异常只记日志, 永不上抛 (飞书宕机 ≠ 该停监控)。
"""
from __future__ import annotations

import os
import queue
import threading
import time
from typing import Callable

from utils.feishu_webhook import post_webhook
from utils.logger import get_logger

logger = get_logger("research.sentiment_notifier")

_WEBHOOK_ENV = "FEISHU_WEBHOOK_URL"
_POST_TIMEOUT_SEC = 5.0
_QUEUE_MAXSIZE = 1000


def polarity_emoji(polarity: float) -> str:
    """polarity → 中文情绪标签。"""
    if polarity >= 0.6:
        return "利好"
    if polarity >= 0.2:
        return "偏多"
    if polarity <= -0.6:
        return "利空"
    if polarity <= -0.2:
        return "偏空"
    return "中性"


class SentimentNotifier:
    """舆情飞书推送器: 有界队列 + 单 worker 线程, 对调度线程零阻塞。

    生产侧 (notify_alert/notify_summary) 只 put_nowait 裸 dict;
    消费侧重活 (拼卡片 / POST) 全在 worker 线程。
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
        self._warned_no_url = False

    # ── 生产侧 (调度线程调, 必须飞快) ─────────────────────────────

    def notify_alert(self, payload: dict, title: str = "舆情异动",
                     color: str = "red") -> None:
        """异动告警。payload: {code, name, polarity, strength, confidence,
        evidence_quote, hit_pool, ts}。只入队。"""
        if not self._enabled_getter():
            return
        if not self._webhook_getter():
            self._warn_no_url_once()
            return
        try:
            self._queue.put_nowait(
                {"kind": "alert", "data": payload, "title": title, "color": color})
        except queue.Full:
            logger.warning("舆情推送队列满 (%d), 丢弃一条告警", self._queue.maxsize)

    def notify_summary(self, payload: dict, title: str = "舆情日报") -> None:
        """盘后日报。payload: {date, top_movers:[...], sector_heat:[...]}。只入队。"""
        if not self._enabled_getter():
            return
        if not self._webhook_getter():
            self._warn_no_url_once()
            return
        try:
            self._queue.put_nowait({"kind": "summary", "data": payload, "title": title})
        except queue.Full:
            logger.warning("舆情推送队列满 (%d), 丢弃日报", self._queue.maxsize)

    # ── 生命周期 ───────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if self._enabled_getter() and not self._webhook_getter():
            self._warn_no_url_once()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="sentiment-notifier", daemon=True)
        self._thread.start()

    def stop(self, drain_timeout_sec: float = 2.0) -> None:
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=drain_timeout_sec + _POST_TIMEOUT_SEC)
            self._thread = None

    # ── worker (独占慢活, 异常永不上抛) ─────────────────────────────

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
                logger.exception("舆情推送处理异常 (worker 继续)")

    def _handle(self, job: dict) -> None:
        webhook = self._webhook_getter()
        if not webhook:
            return
        kind = job.get("kind")
        data = job.get("data") or {}
        if kind == "alert":
            card = self._build_alert_card(data, job.get("title", "舆情异动"),
                                          job.get("color", "red"))
        elif kind == "summary":
            card = self._build_summary_card(data, job.get("title", "舆情日报"))
        else:
            return
        self._post(webhook, card)

    def _post(self, webhook: str, body: dict) -> None:
        """POST 卡片。已下沉 utils.feishu_webhook.post_webhook（读业务 code 判真送达）。"""
        post_webhook(webhook, body, context="舆情")

    def _warn_no_url_once(self) -> None:
        if self._warned_no_url:
            return
        self._warned_no_url = True
        logger.warning("舆情推送已启用但未配置环境变量 %s, 推送为 no-op", _WEBHOOK_ENV)

    # ── 卡片构建 ───────────────────────────────────────────────────

    def _build_alert_card(self, d: dict, title: str, color: str) -> dict:
        """异动告警卡: 标的 + 情绪分 + 依据 + 命中池。header 红/绿/橙。"""
        code = d.get("code", "")
        name = d.get("name", "")
        polarity = float(d.get("polarity", 0.0) or 0.0)
        strength = int(d.get("strength", 0) or 0)
        confidence = float(d.get("confidence", 0.0) or 0.0)
        evidence = d.get("evidence_quote", "")
        hit_pool = d.get("hit_pool") or []
        ts = d.get("ts")
        time_str = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
                    if ts else "")

        head = f"**{name} {code}**" if name else f"**{code}**"
        emoji = polarity_emoji(polarity)
        lines = [head, f"情绪 {emoji} {polarity:+.2f}",
                 f"强度 {strength} · 把握 {confidence:.0%}"]
        if evidence:
            lines.append(f"依据: {evidence}")
        if hit_pool:
            tags = []
            for h in hit_pool:
                if isinstance(h, dict):
                    v = h.get("value")
                    if v:
                        tags.append(str(v))
                elif h:
                    tags.append(str(h))
            if tags:
                lines.append("命中: " + ", ".join(tags))
        if time_str:
            lines.append(f"_时间 {time_str}_")

        return {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": color,
                },
                "elements": [
                    {"tag": "div",
                     "text": {"tag": "lark_md", "content": "\n".join(lines)}}],
            },
        }

    def _build_summary_card(self, d: dict, title: str) -> dict:
        """盘后舆情日报卡: 日期 + Top 异动 + 板块热度。缺字段 fail-soft。"""
        date_str = d.get("date", "")
        top_movers = d.get("top_movers") or []
        sector_heat = d.get("sector_heat") or []
        ts = d.get("ts")
        if not date_str and ts:
            date_str = time.strftime("%Y-%m-%d", time.localtime(ts))

        elements: list = []
        if top_movers:
            mlines = ["**Top 异动**"]
            for m in top_movers[:10]:
                code = m.get("code", "")
                name = m.get("name", "")
                pol = float(m.get("polarity", 0.0) or 0.0)
                head = f"{name} {code}" if name else code
                mlines.append(f"{head} {polarity_emoji(pol)} {pol:+.2f}")
            elements.append({"tag": "div", "text": {"tag": "lark_md",
                                                    "content": "\n".join(mlines)}})
        if sector_heat:
            elements.append({"tag": "hr"})
            slines = ["**板块温度**"]
            for s in sector_heat[:8]:
                name = s.get("name", "")
                score = float(s.get("score", 0.0) or 0.0)
                slines.append(f"{name} {score:+.2f}")
            elements.append({"tag": "div", "text": {"tag": "lark_md",
                                                    "content": "\n".join(slines)}})
        if not elements:
            elements = [{"tag": "div", "text": {"tag": "lark_md",
                                                "content": "今日无显著异动"}}]

        return {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text",
                                     "content": f"{title} · {date_str}"},
                           "template": "blue"},
                "elements": elements,
            },
        }


# ── 默认 getter (读环境变量, 与交易通知器共用 webhook) ─────────────

def _env_enabled() -> bool:
    """启用开关: webhook 配了就算启用 (简化; 也可接 config.feishu.enabled)。"""
    return bool(os.environ.get(_WEBHOOK_ENV))


def _env_webhook() -> str | None:
    return os.environ.get(_WEBHOOK_ENV) or None


_default: SentimentNotifier | None = None


def get_default_notifier() -> SentimentNotifier:
    """全局单例 (scheduler 进程用)。懒加载。"""
    global _default
    if _default is None:
        _default = SentimentNotifier(_env_enabled, _env_webhook)
    return _default
