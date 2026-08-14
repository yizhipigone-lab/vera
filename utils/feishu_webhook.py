"""utils/feishu_webhook.py — 飞书 webhook 发卡片共享轮子（深模块，纯 stdlib）。

动机（2026-08-14 深模块浅模块审查 推荐①a）：`trade/notifier.py` 与
`research/sentiment_notifier.py` 各自手写一份 `_post`（POST + 读业务 code 判真送达），
逐字重复。抽出后「修一次管两处」（局部性）。

守隔离铁律 1：本模块是中立层，只 import stdlib，绝不 import trade/ 或 research/。
两个 notifier 仍不互相依赖；共享的是「发 webhook」这个纯基础设施，不含业务语义。

编码 2026-08-07 的坑（此处是唯一真相源）：飞书对卡片 JSON 校验失败时 HTTP 仍 200，
body 返回 {"code":11246,...}；只看 HTTP 200 会假成功（用户「没看到推送」的元凶）。
必须读业务码，code != 0 视为拒收。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from utils.logger import get_logger

logger = get_logger("utils.feishu_webhook")

_POST_TIMEOUT_SEC = 5.0


def post_webhook(webhook: str, body: dict, context: str = "",
                 timeout: float = _POST_TIMEOUT_SEC) -> bool:
    """POST 飞书互动卡片，读业务 code 判真送达。返 True=送达, False=失败/拒收。

    - context: 日志前缀标签（如 "交易" / "舆情"），区分调用方；缺省中性。
    - fail-soft: 任何异常只记 warning 返 False，永不上抛（飞书宕机 ≠ 该停调用方）。
    """
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=data, headers={"Content-Type": "application/json"})
    tag = f"（{context}）" if context else ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        logger.warning("飞书 webhook 投递失败%s: %s", tag, e)
        return False
    try:
        r = json.loads(raw.decode("utf-8"))
    except Exception:
        return False
    if r.get("code") not in (0, None):
        logger.warning("飞书拒收卡片 code=%s%s: %s",
                       r.get("code"), tag, str(r.get("msg") or "")[:200])
        return False
    return True
