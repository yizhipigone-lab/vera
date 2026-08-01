"""policy_pipeline.sources.gov_cn — 中国政府网最新政策抓取 (P1b)。

数据源 (2026-07 实测可用):
- 列表: https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json
  (zuixin 列表页由 JS 读该 JSON 渲染, 直接取 JSON 最稳; 失败回退解析 HTML 列表页)
- 正文: 政策详情页 <div class="pages_content">...</div> 内为正文

【注入防护 (重要)】
爬回的政策文本是不可信外部内容, 可能藏「忽略之前指令」之类的 prompt 注入。
本模块所有送往下游 LLM (extract_policy) 的文本必须先过 sanitize_untrusted():
1) 截掉控制字符, 防终端/日志污染;
2) 剥除文本内伪造的边界标记, 防「越狱出数据区」;
3) 用 <untrusted_policy_text>...</untrusted_policy_text> 显式包裹,
   让下游 prompt 能把它当纯数据而非指令对待。
sanitize 只是纵深防御的一层, 下游 prompt 仍应明确声明边界内是数据。

松耦合/fail-open: 网络失败、解析失败一律记 warning 返空, 绝不抛出。

公开接口:
- fetch_latest_policies(limit=10) → [{title, url, date, text}] (text 列表阶段为空)
- fetch_policy_text(url) → 正文纯文本 (截断 ~4000 字)
- sanitize_untrusted(text) → 加边界标记的净文本
- run(limit=10, ingest=True, db_path=None) → 成功入库条数

CLI:
    python -m policy_pipeline.sources.gov_cn --limit 3          # 只抓不入库, 打印摘要
    python -m policy_pipeline.sources.gov_cn --limit 3 --ingest # 抓取并入库
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.parse
import urllib.request

from utils.logger import get_logger

logger = get_logger(__name__)

_LIST_JSON_URL = "https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json"
_LIST_HTML_URL = "https://www.gov.cn/zhengce/zuixin/"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VERA-policy-bot/1.0"
_TIMEOUT = 15
_MAX_TEXT_CHARS = 4000

# 不可信文本边界标记 (给下游 LLM 的防注入措施, 见模块 docstring)
UNTRUSTED_OPEN = "<untrusted_policy_text>"
UNTRUSTED_CLOSE = "</untrusted_policy_text>"


def _http_get(url: str) -> str:
    """GET url → 文本. 失败抛异常, 由上层 fail-open 兜底."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        raw = resp.read()
    # gov.cn 均为 utf-8; 容错用 errors="ignore"
    return raw.decode("utf-8", errors="ignore")


def parse_list_json(payload: str) -> list[dict]:
    """解析 ZUIXINZHENGCE.json → [{title, url, date, text=""}]. 解析失败返 []."""
    try:
        data = json.loads(payload)
        items = []
        for it in data if isinstance(data, list) else []:
            title = (it.get("TITLE") or "").strip()
            url = (it.get("URL") or "").strip()
            if not title or not url:
                continue
            items.append({
                "title": title,
                "url": url,
                "date": (it.get("DOCRELPUBTIME") or "").strip(),
                "text": "",
            })
        return items
    except Exception as e:
        logger.warning(f"政策列表 JSON 解析失败(fail-open): {e}")
        return []


def parse_list_html(html: str, base_url: str = _LIST_HTML_URL) -> list[dict]:
    """解析 HTML 列表页 → [{title, url, date, text=""}] (JSON 失效时的回退).

    匹配 <li>...<a href="...htm">标题</a>...YYYY-MM-DD...</li> 结构. 解析失败返 [].
    """
    try:
        items = []
        # 每个 <li> 块内找链接 + 日期
        for li in re.findall(r"<li[^>]*>(.*?)</li>", html, re.S | re.I):
            m = re.search(r'<a[^>]*href="([^"]+\.htm)"[^>]*>(.*?)</a>', li, re.S | re.I)
            if not m:
                continue
            url = urllib.parse.urljoin(base_url, m.group(1))
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if not title:
                continue
            dm = re.search(r"(20\d{2}-\d{1,2}-\d{1,2})", li)
            items.append({"title": title, "url": url,
                          "date": dm.group(1) if dm else "", "text": ""})
        return items
    except Exception as e:
        logger.warning(f"政策列表 HTML 解析失败(fail-open): {e}")
        return []


def fetch_latest_policies(limit: int = 10) -> list[dict]:
    """抓最新政策列表 → [{title, url, date, text}] (text 留空, 正文按需再抓).

    JSON 优先, 失败回退 HTML 列表页; 全失败记 warning 返 [] (fail-open).
    """
    try:
        items = parse_list_json(_http_get(_LIST_JSON_URL))
        if items:
            return items[:limit]
        logger.warning("政策列表 JSON 为空, 回退 HTML 列表页")
    except Exception as e:
        logger.warning(f"抓政策列表 JSON 失败, 回退 HTML: {e}")
    try:
        return parse_list_html(_http_get(_LIST_HTML_URL))[:limit]
    except Exception as e:
        logger.warning(f"抓政策列表 HTML 也失败(fail-open 返空): {e}")
        return []


def extract_article_text(html: str) -> str:
    """从政策详情页 HTML 提取正文纯文本 (去标签, 规整空白). 失败返 ""."""
    try:
        m = re.search(r'<div class="pages_content"[^>]*>(.*?)<div class="editor"', html, re.S | re.I)
        if not m:
            # 兜底: pages_content 到下一个同级大块
            m = re.search(r'<div class="pages_content"[^>]*>(.*?)</div>\s*<', html, re.S | re.I)
        if not m:
            return ""
        text = re.sub(r"<script.*?</script>", "", m.group(1), flags=re.S | re.I)
        text = re.sub(r"<style.*?</style>", "", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", "\n", text)
        text = re.sub("[ \t\xa0\u2002\u2003\u200b]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text)
        return text.strip()
    except Exception as e:
        logger.warning(f"正文提取失败(fail-open): {e}")
        return ""


def fetch_policy_text(url: str) -> str:
    """抓政策正文纯文本, 截断到 ~4000 字. 网络/解析失败记 warning 返 ""."""
    try:
        text = extract_article_text(_http_get(url))
        if not text:
            logger.warning(f"正文为空(可能页面改版): {url}")
        return text[:_MAX_TEXT_CHARS]
    except Exception as e:
        logger.warning(f"抓政策正文失败(fail-open): {url}: {e}")
        return ""


def sanitize_untrusted(text: str, max_chars: int = _MAX_TEXT_CHARS) -> str:
    """不可信外部文本 → 送 LLM 前的净化 (防注入, 见模块 docstring).

    - 截掉控制字符 (保留 \\n \\t)
    - 剥除文本内伪造的边界标记 (防越狱出数据区)
    - 截断 max_chars
    - 用 <untrusted_policy_text>...</untrusted_policy_text> 包裹
    空输入返 ""。
    """
    if not text:
        return ""
    cleaned = "".join(
        ch for ch in text
        if ch in "\n\t" or (ord(ch) >= 32 and not 0x7F <= ord(ch) <= 0x9F)
    )
    cleaned = cleaned.replace(UNTRUSTED_OPEN, "").replace(UNTRUSTED_CLOSE, "")
    cleaned = cleaned.strip()[:max_chars]
    if not cleaned:
        return ""
    return f"{UNTRUSTED_OPEN}\n{cleaned}\n{UNTRUSTED_CLOSE}"


def run(limit: int = 10, ingest: bool = True, db_path=None) -> int:
    """抓 → sanitize → 逐条 ingest_policy_text 入库, 返成功条数.

    单条失败 (抓正文失败/抽取失败/入库失败) 不中断后续条 (松耦合).
    ingest=False 时只抓取+净化, 不入库 (返回成功抓到正文的条数).
    """
    items = fetch_latest_policies(limit=limit)
    if not items:
        logger.warning("政策列表为空, run 结束")
        return 0
    ingest_fn = None
    if ingest:
        try:
            # 延迟 import: ingest 链路重 (kg/llm), 保持本模块轻量松耦合
            from policy_pipeline.ingest import ingest_policy_text as ingest_fn
        except Exception as e:
            logger.warning(f"导入 ingest_policy_text 失败, 降级为只抓不入库: {e}")
            ingest = False
    ok = 0
    for it in items:
        try:
            text = fetch_policy_text(it["url"])
            if not text:
                continue
            sanitized = sanitize_untrusted(text)
            if not sanitized:
                continue
            if ingest:
                if ingest_fn(it["title"], sanitized, db_path=db_path):
                    ok += 1
            else:
                it["text"] = sanitized
                ok += 1
        except Exception as e:
            logger.warning(f"单条政策处理失败(不中断后续): {it.get('title', '')[:40]}: {e}")
    return ok


def main(argv=None) -> int:
    """CLI 入口: 默认只抓不入库打印摘要; --ingest 才入库."""
    parser = argparse.ArgumentParser(description="中国政府网最新政策抓取 (P1b)")
    parser.add_argument("--limit", type=int, default=10, help="抓取条数 (默认 10)")
    parser.add_argument("--ingest", action="store_true", help="抓取后入库 (默认只抓不入库)")
    parser.add_argument("--db-path", default=None, help="kg 图谱 db 路径 (默认 kg/graph.db)")
    args = parser.parse_args(argv)

    if args.ingest:
        n = run(limit=args.limit, ingest=True, db_path=args.db_path)
        print(f"入库成功 {n} 条 (limit={args.limit})")
        return 0
    items = fetch_latest_policies(limit=args.limit)
    for i, it in enumerate(items, 1):
        print(f"[{i}] {it['date']} {it['title']}")
        print(f"    {it['url']}")
    print(f"共 {len(items)} 条 (仅抓取未入库; 加 --ingest 入库)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
