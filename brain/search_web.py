"""brain/search_web.py — VERA 大脑联网搜索 + 网页正文抓取（ddgs，免费）。

用法（CLI，LLM 通过 Bash 调用）：
  python -m brain.search_web search "查询词" [--max 8] [--backend bing] [--days 30]
  python -m brain.search_web fetch <url> [--max-chars 4000]   # 抓正文, 信息量远大于摘要
  python -m brain.search_web selftest    # 环境自检：依次试各 backend

技术选型：
- ddgs：duckduckgo-search 的官方继任包（实测 9.14.4：bing 稳；
  brave/duckduckgo/mojeek 对中文查询常 "No results found"，yahoo news 超时）
- backend 策略：baidu → bing → duckduckgo（大陆可达性排序）；结果无摘要的
  backend（百度 HTML 只能稳定抽标题）视为无效，自动回退到带摘要的 backend
- --days：映射 ddgs timelimit（d/w/m/y），锁时效防"旧闻当新闻"（年份混淆事故）
  注意：bing 对 timelimit 的遵守不严格（2026-08-12 实测 m 档仍出 3 个月前旧文），
  日期必须再用 fetch 读原文核实 —— prompt 里已写明这条纪律
- fetch：requests + 正则抽正文（去脚本/样式/导航碎块），无 readability 依赖
- 安全：结果经 policy_pipeline.sources.gov_cn.sanitize_untrusted() 清洗
- 松耦合：任何失败返 [] / 空串 不抛异常
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
FETCH_MAX_CHARS = 4000
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/120.0.0.0 Safari/537.36")
_last_call: float = 0.0

# 全部后端失败汇总告警节流（2026-08-17）：盘中每 10 分钟一 tick × 3 关键词，
# 全挂时每个 keyword 都会走到 return []，不节流会刷屏。30 分钟内只报一次。
_ALL_FAIL_LOG_INTERVAL = 30 * 60  # 30 分钟
_last_all_fail_log = 0.0

# 搜索结果短缓存（2026-08-13 提速三轮 ③）：同一 query 30 分钟内命中直接返，
# 省掉 bing 那 ~11s（快路径实测最大单点）。只缓存带摘要的有效结果，
# 空结果/标题-only 不缓存（防把瞬时失败或最弱数据源缓存住）。
# 内存态、短 TTL，server 重启即失效——与 market_snapshot 2h 快照缓存同一哲学，
# 缓存的是"搜索原始数据"不是"结论"，与"不做 LLM 回答缓存"不冲突。
_SEARCH_CACHE_TTL = 30 * 60  # 30 分钟
_search_cache: dict[tuple, tuple[float, list[dict]]] = {}


def _rate_limit():
    """确保两次调用间隔 ≥ RATE_LIMIT_SEC。"""
    global _last_call
    now = time_mod.time()
    wait = _last_call + RATE_LIMIT_SEC - now
    if wait > 0:
        time_mod.sleep(wait)
    _last_call = time_mod.time()


def _prune_search_cache(now: float) -> None:
    """清掉过期搜索缓存条目（防内存无限涨）。"""
    for k in [k for k, (ts, _) in _search_cache.items()
              if now - ts >= _SEARCH_CACHE_TTL]:
        del _search_cache[k]


def _log_all_backends_failed(query: str) -> None:
    """联网搜索所有 backend 均无可用结果时，节流打印一次汇总告警。

    单个 backend 的失败/无摘要已在循环里打过 warning；此处是"本轮整体失效"的
    收尾信号，让连续多轮搜不到时能被一眼看到（30 分钟内只报一次防刷屏）。
    """
    global _last_all_fail_log
    now = time_mod.time()
    if now - _last_all_fail_log >= _ALL_FAIL_LOG_INTERVAL:
        _last_all_fail_log = now
        logger.warning(
            "search_web 全部 backend 均无可用结果 (样例 query=%r): 本轮联网搜索失效",
            query)


def clear_search_cache() -> None:
    """清空搜索结果缓存（测试隔离用；生产不需要）。"""
    _search_cache.clear()


def _timelimit(days: int | None) -> str | None:
    """天数 → ddgs timelimit 档（d/w/m/y）。"""
    if days is None:
        return None
    if days <= 1:
        return "d"
    if days <= 7:
        return "w"
    if days <= 31:
        return "m"
    return "y"


def search_web(query: str, max_results: int = MAX_RESULTS,
               backend: str = "auto", days: int | None = None) -> list[dict]:
    """联网文本搜索。返 [{"title","url","snippet","source"}]。
    snippet 已过 sanitize_untrusted() 清洗。任何异常 → 返 [] 不抛。
    days: 时效过滤（近 N 天），映射 ddgs timelimit；bing 遵守不严，日期需 fetch 复核。

    短缓存：同一 (query,max_results,backend,days) 30 分钟内命中直接返，
    跳过限流 + 网络（bing 那 ~11s）。只缓存带摘要的有效结果。
    """
    key = (query, max_results, backend, days)
    now = time_mod.time()
    hit = _search_cache.get(key)
    if hit is not None and now - hit[0] < _SEARCH_CACHE_TTL:
        return hit[1]
    _prune_search_cache(now)

    _rate_limit()

    backends_to_try = list(BACKENDS) if backend == "auto" else [backend]
    if backend != "auto" and backend not in BACKENDS:
        backends_to_try = list(BACKENDS)

    for be in backends_to_try:
        try:
            results = _search_with_backend(query, max_results, be,
                                           timelimit=_timelimit(days))
            # 有结果但 snippet 全空（如百度只抽到标题）→ 视为无效，继续回退：
            # LLM 拿不到摘要就无法基于内容作答，标题-only 是最弱数据源
            if results and any(r.get("snippet") for r in results):
                _search_cache[key] = (now, results)
                return results
            if results:
                logger.warning(f"search_web backend={be} 结果无摘要，回退下一 backend")
        except Exception as e:
            logger.warning(f"search_web backend={be} 失败: {e}")
            continue

    # 走到这说明所有 backend 都没给出带摘要的可用结果 → 本轮联网搜索失效。
    # 汇总告警已节流 (见 _log_all_backends_failed), 防盘中 3 关键词 × 每 tick 刷屏。
    _log_all_backends_failed(query)
    return []


def fetch_url(url: str, max_chars: int = FETCH_MAX_CHARS) -> str:
    """抓网页正文（去脚本/样式/标签，按行块滤掉导航碎块）。

    信息量远大于搜索摘要 —— 核实日期、读公告细节、拿完整数字全靠它。
    任何异常返空串（松耦合）。安全：sanitize_untrusted 清洗。"""
    import re as _re

    import requests

    from policy_pipeline.sources.gov_cn import sanitize_untrusted
    try:
        headers = {"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml",
                   "Accept-Language": "zh-CN,zh;q=0.9"}
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        t = resp.text
        t = _re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", t, flags=_re.S | _re.I)
        t = _re.sub(r"<!--.*?-->", " ", t, flags=_re.S)
        t = _re.sub(r"<[^>]+>", "\n", t)
        t = _re.sub(r"&[a-zA-Z#0-9]+;", " ", t)
        # 行块 < 40 字的多是导航/按钮/尾巴, 滤掉
        blocks = [b.strip() for b in t.split("\n") if len(b.strip()) >= 40]
        return sanitize_untrusted("\n".join(blocks))[:max_chars]
    except Exception as e:
        logger.warning(f"fetch_url 失败 ({url}): {e}")
        return ""


def _search_baidu(query: str, max_results: int) -> list[dict]:
    """百度搜索（requests 直连，大陆最优）。异常上抛。"""
    import re as _re

    import requests
    headers = {
        "User-Agent": _UA,
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


def _search_with_backend(query: str, max_results: int, backend: str,
                         timelimit: str | None = None) -> list[dict]:
    """用指定 backend 搜索。异常上抛给调用方处理。"""
    from policy_pipeline.sources.gov_cn import sanitize_untrusted

    # 百度走独立实现（requests 直连，大陆最优；不支持 timelimit）
    if backend == "baidu":
        raw = _search_baidu(query, max_results)
        return [{"title": r["title"], "url": r["url"],
                 "snippet": sanitize_untrusted(r["snippet"])[:300],
                 "source": "baidu"} for r in raw]

    from ddgs import DDGS
    ddgs = DDGS()
    # ddgs 各版本签名有漂移，逐档降级调用（全参 → 去 timelimit → 裸调用）
    kw: dict = {"max_results": max_results, "backend": backend}
    if timelimit:
        kw["timelimit"] = timelimit
    try:
        raw = list(ddgs.text(query, **kw))
    except TypeError:
        kw.pop("timelimit", None)
        try:
            raw = list(ddgs.text(query, **kw))
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
                         backend=args.backend, days=args.days)
    ensure_utf8_stdout()
    if not results:
        print("（联网搜索不可用：所有 backend 均无结果或失败，请基于训练知识回答并标注截止日期）")
        sys.exit(3)
    print(format_results(results))


def _cmd_fetch(args):
    text = fetch_url(args.url, max_chars=args.max_chars)
    ensure_utf8_stdout()
    if not text:
        print(f"（正文抓取失败：{args.url}，可能是反爬/超时/需登录）")
        sys.exit(3)
    print(text)


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
    p_search.add_argument("--days", type=int, default=None,
                          help="时效过滤：近 N 天（如 7=近一周, 30=近一月）。"
                               "bing 遵守不严，日期务必 fetch 原文复核")

    p_fetch = sub.add_parser("fetch", help="抓网页正文（信息量远大于摘要）")
    p_fetch.add_argument("url", help="目标网页 URL")
    p_fetch.add_argument("--max-chars", type=int, default=FETCH_MAX_CHARS)

    sub.add_parser("selftest", help="环境自检")

    args = ap.parse_args(argv)
    if args.cmd == "search":
        _cmd_search(args)
    elif args.cmd == "fetch":
        _cmd_fetch(args)
    elif args.cmd == "selftest":
        _cmd_selftest(args)
    else:
        ap.print_help()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
