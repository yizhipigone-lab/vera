"""brain/search_web.py — VERA 大脑联网搜索（ddgs，免费，backend 优先 bing）。

用法（CLI，LLM 通过 Bash 调用）：
  python -m brain.search_web search "查询词" [--max 8] [--backend bing]
  python -m brain.search_web selftest    # 环境自检：依次试各 backend

技术选型：
- ddgs：duckduckgo-search 的官方继任包
- backend 策略：baidu → bing → duckduckgo（大陆可达性排序）；结果无摘要的
  backend（百度 HTML 只能稳定抽标题）视为无效，自动回退到带摘要的 backend
- 安全：结果经 policy_pipeline.sources.gov_cn.sanitize_untrusted() 清洗
- 松耦合：任何失败返 [] 不抛异常
"""
from __future__ import annotations

import argparse
import sys
import time as time_mod

from utils.logger import get_logger
from utils.sysutil import ensure_utf8_stdout

logger = get_logger(__name__)

# ── 常量 ────────────────────────────────────────────────────
MAX_RESULTS = 8
RATE_LIMIT_SEC = 2.0  # 两次调用最小间隔
BACKENDS = ("baidu", "bing", "duckduckgo")  # 大陆优先百度，次选 bing
_last_call: float = 0.0


def _rate_limit():
    """确保两次调用间隔 ≥ RATE_LIMIT_SEC。"""
    global _last_call
    now = time_mod.time()
    wait = _last_call + RATE_LIMIT_SEC - now
    if wait > 0:
        time_mod.sleep(wait)
    _last_call = time_mod.time()


def search_web(query: str, max_results: int = MAX_RESULTS,
               backend: str = "auto") -> list[dict]:
    """联网文本搜索。返 [{"title","url","snippet","source"}]。
    snippet 已过 sanitize_untrusted() 清洗。任何异常 → 返 [] 不抛。"""
    _rate_limit()

    backends_to_try = list(BACKENDS) if backend == "auto" else [backend]
    if backend != "auto" and backend not in BACKENDS:
        backends_to_try = list(BACKENDS)

    for be in backends_to_try:
        try:
            results = _search_with_backend(query, max_results, be)
            # 有结果但 snippet 全空（如百度只抽到标题）→ 视为无效，继续回退：
            # LLM 拿不到摘要就无法基于内容作答，标题-only 是最弱数据源
            if results and any(r.get("snippet") for r in results):
                return results
            if results:
                logger.warning(f"search_web backend={be} 结果无摘要，回退下一 backend")
        except Exception as e:
            logger.warning(f"search_web backend={be} 失败: {e}")
            continue

    return []


def _search_baidu(query: str, max_results: int) -> list[dict]:
    """百度搜索（requests 直连，大陆最优）。异常上抛。"""
    import re as _re

    import requests
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    resp = requests.get("https://www.baidu.com/s",
                        params={"wd": query}, headers=headers, timeout=20)
    resp.raise_for_status()
    results: list[dict] = []
    text = resp.text
    # 从 HTML 中提取搜索结果：标题在 <h3> 内的 <a>，URL 在 href
    for m in _re.finditer(
            r'<h3[^>]*>\s*<a[^>]*href="(https?://[^"]+)"[^>]*>'
            r'(.*?)</a>', text, _re.DOTALL):
        href, title = m.group(1), _re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if not title:
            continue
        results.append({"title": title, "url": href, "snippet": "", "source": "baidu"})
        if len(results) >= max_results:
            break
    return results


def _search_with_backend(query: str, max_results: int, backend: str) -> list[dict]:
    """用指定 backend 搜索。异常上抛给调用方处理。"""
    from policy_pipeline.sources.gov_cn import sanitize_untrusted

    # 百度走独立实现（requests 直连，大陆最优）
    if backend == "baidu":
        raw = _search_baidu(query, max_results)
        return [{"title": r["title"], "url": r["url"],
                 "snippet": sanitize_untrusted(r["snippet"])[:300],
                 "source": "baidu"} for r in raw]

    from ddgs import DDGS
    ddgs = DDGS()
    # ddgs 各版本签名有漂移，try 两种调用方式
    try:
        raw = list(ddgs.text(query, max_results=max_results, backend=backend))
    except TypeError:
        raw = list(ddgs.text(query, max_results=max_results))

    results: list[dict] = []
    for r in raw:
        title = str(r.get("title") or "")
        url = str(r.get("href") or r.get("url", ""))
        body = str(r.get("body") or r.get("snippet", ""))
        if not title and not body:
            continue
        results.append({
            "title": title,
            "url": url,
            "snippet": sanitize_untrusted(body)[:300],
            "source": backend,
        })
    return results


def format_results(results: list[dict]) -> str:
    """渲染为 LLM 可读的纯文本。"""
    if not results:
        return ""
    lines: list[str] = []
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r['title']} ({r['source']})")
        if r.get("url"):
            lines.append(f"    {r['url']}")
        if r.get("snippet"):
            lines.append(f"    {r['snippet'][:200]}")
    return "\n".join(lines)


def selftest() -> dict:
    """依次试各 backend，返回可用性报告。"""
    result: dict = {}
    for be in BACKENDS:
        try:
            res = search_web("test", max_results=1, backend=be)
            result[be] = "OK" if res else "empty"
        except Exception as e:
            result[be] = f"FAIL: {e}"
    return result


# ── CLI ────────────────────────────────────────────────────
def _cmd_search(args):
    results = search_web(args.query, max_results=args.max,
                         backend=args.backend)
    ensure_utf8_stdout()
    if not results:
        print("（联网搜索不可用：所有 backend 均无结果或失败，请基于训练知识回答并标注截止日期）")
        sys.exit(3)
    print(format_results(results))


def _cmd_selftest(args):
    report = selftest()
    ensure_utf8_stdout()
    for be, status in report.items():
        print(f"  {be}: {status}")
    all_ok = all(v == "OK" for v in report.values())
    if not all_ok:
        print("\n（部分 backend 不可用，自动回退到可用 backend）")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m brain.search_web")
    sub = ap.add_subparsers(dest="cmd")

    p_search = sub.add_parser("search", help="联网搜索")
    p_search.add_argument("query", help="搜索查询词")
    p_search.add_argument("--max", type=int, default=MAX_RESULTS)
    p_search.add_argument("--backend", default="auto",
                          choices=["auto", "baidu", "bing", "duckduckgo"])

    sub.add_parser("selftest", help="环境自检")

    args = ap.parse_args(argv)
    if args.cmd == "search":
        _cmd_search(args)
    elif args.cmd == "selftest":
        _cmd_selftest(args)
    else:
        ap.print_help()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
