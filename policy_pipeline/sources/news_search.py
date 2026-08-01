"""policy_pipeline.sources.news_search — 新闻搜索多源兜底 (P1b)。

多 fetcher 按序尝试, 任一成功即返回; 全失败记 warning 返 [] (fail-open, 绝不抛出)。
akshare 为可选依赖: 缺失时对应 fetcher 记 warning 跳过, 模块本身照常可用。

数据源 (akshare 1.18.64 实测):
- stock_news_main_cx: 财新要闻 (tag/summary/url), 无参数, 主源
- news_cctv: 央视新闻联播文字稿 (date/title/content), 按日期, 备源

抓回文本是不可信外部内容, 统一过 gov_cn.sanitize_untrusted 加边界标记 (防注入)。

公开接口:
- fetch_news(keyword="", limit=10) → [{title, url, date, text}]
  (text 已 sanitize; keyword 非空时按标题/摘要包含过滤)
"""
from __future__ import annotations

import datetime
import importlib.util
import re

from policy_pipeline.sources.gov_cn import sanitize_untrusted
from utils.logger import get_logger

logger = get_logger(__name__)

_HAS_AKSHARE = importlib.util.find_spec("akshare") is not None
if not _HAS_AKSHARE:
    logger.warning("未安装 akshare, 新闻搜索 fetcher 全部降级为空 (fail-open)")


def _import_akshare():
    """延迟 import akshare (加载较慢); 未装返 None."""
    if not _HAS_AKSHARE:
        return None
    try:
        import akshare as ak
        return ak
    except Exception as e:
        logger.warning(f"import akshare 失败(fail-open): {e}")
        return None


def _date_from_url(url: str) -> str:
    """从 URL 里抠 YYYY-MM-DD (如 database.caixin.com/2026-07-28/...)."""
    m = re.search(r"20\d{2}-\d{2}-\d{2}", url or "")
    return m.group(0) if m else ""


def _match_keyword(title: str, text: str, keyword: str) -> bool:
    """keyword 为空不过滤; 否则标题/摘要任一包含即命中."""
    if not keyword:
        return True
    return keyword in title or keyword in text


def _fetch_caixin_main(keyword: str, limit: int) -> list[dict]:
    """财新要闻 (akshare stock_news_main_cx). 失败抛异常由上层兜底."""
    ak = _import_akshare()
    if ak is None:
        return []
    df = ak.stock_news_main_cx()
    items = []
    for _, row in df.iterrows():
        summary = str(row.get("summary", "") or "").strip()
        tag = str(row.get("tag", "") or "").strip()
        url = str(row.get("url", "") or "").strip()
        if not summary:
            continue
        title = f"[{tag}] {summary[:60]}" if tag else summary[:60]
        if not _match_keyword(title, summary, keyword):
            continue
        items.append({
            "title": title,
            "url": url,
            "date": _date_from_url(url),
            "text": sanitize_untrusted(summary),
        })
        if len(items) >= limit:
            break
    return items


def _fetch_cctv_news(keyword: str, limit: int) -> list[dict]:
    """央视新闻联播文字稿 (akshare news_cctv, 取当日). 失败抛异常由上层兜底."""
    ak = _import_akshare()
    if ak is None:
        return []
    today = datetime.date.today().strftime("%Y%m%d")
    df = ak.news_cctv(date=today)
    items = []
    for _, row in df.iterrows():
        title = str(row.get("title", "") or "").strip()
        content = str(row.get("content", "") or "").strip()
        if not title and not content:
            continue
        if not _match_keyword(title, content, keyword):
            continue
        items.append({
            "title": title or content[:60],
            "url": "",
            "date": str(row.get("date", today) or ""),
            "text": sanitize_untrusted(content or title),
        })
        if len(items) >= limit:
            break
    return items


# 多源按序尝试: 任一成功即返回 (fetcher 返空视为失败, 继续下一个)
_FETCHERS = [
    ("caixin_main", _fetch_caixin_main),
    ("cctv_news", _fetch_cctv_news),
]


def fetch_news(keyword: str = "", limit: int = 10) -> list[dict]:
    """多源兜底抓新闻 → [{title, url, date, text}] (text 已 sanitize).

    任一 fetcher 成功 (返非空) 即返回; 全失败/全空记 warning 返 [] (fail-open).
    """
    for name, fn in _FETCHERS:
        try:
            items = fn(keyword, limit)
            if items:
                return items[:limit]
            logger.warning(f"新闻源 {name} 返空, 尝试下一个")
        except Exception as e:
            logger.warning(f"新闻源 {name} 失败(fail-open): {e}")
    logger.warning(f"所有新闻源均失败/为空, 返 [] (keyword={keyword!r})")
    return []
