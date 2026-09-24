# -*- coding: utf-8 -*-
"""crawl_gupang: 股旁网『选股公式』栏目 + gaibianxuangu 增量抓取(v1, 只读网络)。

产出 md 归档(与 gongshi 语料同款: 标题 / 来源URL / 归档时间 / ```源码```),
写 data/formula_farm/intake/<feed>/<article_id>_<标题>.md;
state 记 last_seen id, 重跑跳过已抓。

用法:
    python -X utf8 tools/formula_farm/crawl_gupang.py --dry-run          # 干跑, 不写盘
    python -X utf8 tools/formula_farm/crawl_gupang.py --max-new 5        # 真跑(增量写 state)
    python -X utf8 tools/formula_farm/crawl_gupang.py --feed-url https://www.gupang.com/gaibianxuangu/
"""
import argparse
import html as _html
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept-Language": "zh-CN,zh;q=0.9"}
SITE = "https://www.gupang.com"
# 已知栏目(启动时还会从首页导航补充候选, 双保险)
KNOWN_FEEDS = [
    {"key": "gaibianxuangu", "url": SITE + "/gaibianxuangu/", "label": "改编选股",
     "filter_select_only": True},   # 只要选股类; 主图墙文一律不抓详情
    {"key": "xuangu", "url": SITE + "/tongdaxin/", "label": "通达信公式(含选股)",
     "filter_select_only": True},   # 客户端过滤: 只要标题含"选股指标公式"且非主图
]
DATA_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                         "data", "formula_farm")
STATE_PATH = os.path.join(DATA_ROOT, "state.json")
SLEEP = 0.5  # 限速秒/页(礼貌爬)


# ---------------- helpers(纯函数, 单测覆盖) ----------------

def is_code_line(line):
    """TDX 源码行判据: 含 ASCII ':'/'='/(' 或纯算子表达式行。中文说明句不混入。"""
    s = line.strip()
    if not s or len(s) > 300:
        return False
    # 纯算子表达式行, 如 C>O; / C/REF(C,1)>=1.099;
    if re.match(r"^[A-Za-z0-9_ .<>=+*/%\-()]+;$", s):
        return True
    return (":" in s) or ("=" in s and not s.endswith("。")) or ("(" in s)


def parse_list_links(fragment_html, base_url):
    """从列表页/片段里抽文章链接(按出现顺序), 返回 [{url,id,title}]。"""
    arts = []
    for m in re.finditer(r'<a[^>]+href="(?P<u>https://www\.gupang\.com/(?P<ym>\d{6})/(?P<id>\d+)\.html)"[^>]*>(?P<t>[\s\S]{0,200}?)</a>',
                         fragment_html):
        t = _html.unescape(re.sub(r"<[^>]+>", "", m.group("t"))).strip()
        arts.append({"url": m.group("u"), "ym": m.group("ym"),
                     "id": int(m.group("id")), "title": t})
    # 去重保序
    seen, out = set(), []
    for a in arts:
        if a["id"] not in seen:
            seen.add(a["id"])
            out.append(a)
    return out


def _visible_lines(page_html):
    h = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", "", page_html)
    h = re.sub(r"<br[^>]*>|</p>|</div>|</h[1-6]>|</li>|</tr>|</td>", "\n", h)
    h = re.sub(r"<[^>]+>", "", h)
    h = _html.unescape(h)
    return [l.strip() for l in h.splitlines() if l.strip()]


def extract_article(url, page_html):
    """详情页 -> {id,ym,url,title,date,code,raw,wall}。纯函数, 单测覆盖。"""
    rec = {"url": url, "raw": ""}
    m = re.search(r"/(\d{6})/(\d+)\.html", url)
    rec["ym"], rec["id"] = (m.group(1), int(m.group(2))) if m else ("", 0)
    t = re.search(r"<title>(.*?)</title>", page_html, re.S)
    rec["title"] = (t.group(1).strip() if t else "")
    rec["title"] = rec["title"].replace("-通达信公式-股旁网", "").strip()
    dm = re.search(r"编辑[：:]\s*股旁网\s*[,，]\s*(\d{4}-\d{2}-\d{2})", page_html) \
        or re.search(r"(\d{4}-\d{2}-\d{2})", page_html)
    rec["date"] = dm.group(1) if dm else ""
    lines = _visible_lines(page_html)
    # 源码区: 编辑行 → 相关文章 之间
    start = end = None
    for i, l in enumerate(lines):
        if start is None and re.search(r"编辑[：:]\s*股旁网", l):
            start = i + 1
        elif start is not None and "相关文章" in l:
            end = i
            break
    region = lines[start:end] if start is not None else []
    rec["raw"] = "\n".join(region)
    # 验证码墙检测用整页可见文本(部分页面无"编辑"行时区域为空也能拦住)
    wall = bool(re.search(r"验证码|此处内容已被隐藏", "\n".join(lines), re.I))
    rec["wall"] = wall
    code = [l for l in region if is_code_line(l)]
    rec["code"] = "\n".join(code).strip()
    return rec


def fetch(url, timeout=25, tries=2):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            time.sleep(SLEEP)
    raise RuntimeError("fetch %s: %r" % (url, last))


# ---------------- 流程 ----------------

def discover_feeds():
    """首页导航里找栏目候选(找不到就用已知, 并打印候选供人工确认)。"""
    try:
        home = fetch(SITE)
    except Exception:
        return KNOWN_FEEDS, []
    cand = []
    for m in re.finditer(r'<a[^>]+href="(?P<u>https://www\.gupang\.com/[a-z_]+/)"[^>]*>(?P<t>[^<]{0,20})</a>', home):
        cand.append({"u": m.group("u"), "t": m.group("t").strip()})
    feeds = []
    for kf in KNOWN_FEEDS:
        hit = next((c for c in cand if c["t"] in (kf["label"][:4],) or kf["url"].rstrip("/").endswith(c["u"].rstrip("/").split("/")[-1])), None)
        feeds.append(kf)
    return feeds, cand


def _feed_articles(feed, pages=2):
    """拉列表页到文章清单(新→旧)。真分页: {base}{N}/ (2026-09-06 实测, ?page=N 是假翻页)。"""
    arts, base = [], feed["url"]
    for page_no in range(1, pages + 1):
        url = base if page_no == 1 else "%s%d/" % (base, page_no)
        try:
            h = fetch(url)
            new = parse_list_links(h, url)
            if new:
                arts.extend(new)
            time.sleep(SLEEP)
        except Exception:
            break
    # 保序去重
    seen, out = set(), []
    for a in arts:
        if a["id"] not in seen:
            seen.add(a["id"])
            out.append(a)
    return out


def _feed_articles_all(feed, max_pages=400, stop_on_stale=2):
    """全量翻页(真分页 {base}{N}/): 连续 stop_on_stale 页无新 id 即收工。"""
    arts, stale = [], 0
    seen = set()
    for page_no in range(1, max_pages + 1):
        url = feed["url"] if page_no == 1 else "%s%d/" % (feed["url"], page_no)
        try:
            new = parse_list_links(fetch(url), url)
        except Exception as e:
            print("   [翻页中断] page%d: %r" % (page_no, e), flush=True)
            break
        fresh = [a for a in new if a["id"] not in seen]
        for a in fresh:
            seen.add(a["id"])
        arts.extend(fresh)
        stale = stale + 1 if not fresh else 0
        if page_no % 20 == 1 or not fresh:
            print("   [列表] %s page%d: 本页 %d, 累计 %d" % (feed["label"], page_no, len(new), len(arts)), flush=True)
        if stale >= stop_on_stale:
            break
        time.sleep(SLEEP)
    return arts


def sanitize(name):
    return re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip().strip(".")


def glob_md_has(feed_dir, article_id):
    """intake/<feed>/ 下是否已有该 id 的归档(文件名前缀 <id>_)。"""
    import glob as _g
    if not os.path.isdir(feed_dir):
        return False
    return bool(_g.glob(os.path.join(feed_dir, "%d_*.md" % article_id)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="干跑: 只拉不写盘")
    ap.add_argument("--all", dest="all_pages", action="store_true",
                    help="全量翻页(直到连续2页无新id或页数上限)")
    ap.add_argument("--max-pages", type=int, default=60, help="--all 时的页数上限")
    ap.add_argument("--feed-url", default=None, help="只抓指定栏目 URL")
    ap.add_argument("--pages", type=int, default=2, help="每个栏目最多拉几页")
    ap.add_argument("--max-new", type=int, default=10, help="本轮最多抓多少条新文章")
    args = ap.parse_args()

    feeds, cand = discover_feeds()
    if cand:
        print("[发现] 首页栏目候选:",
              ", ".join("%s->%s" % (c["u"].rstrip("/").split("/")[-1], c["t"]) for c in cand[:12]))
    if args.feed_url:
        feeds = [{"key": "manual", "url": args.feed_url, "label": "手动"}]

    state = {}
    if not args.dry_run and os.path.exists(STATE_PATH):
        state = json.load(open(STATE_PATH, encoding="utf-8"))

    new_total = 0
    for feed in feeds:
        key, label = feed["key"], feed["label"]
        print("\n[栏目] %s (%s)" % (label, feed["url"]))
        arts = (_feed_articles_all(feed, args.max_pages) if args.all_pages
                else _feed_articles(feed, pages=args.pages))
        if feed.get("filter_select_only"):
            arts = [a for a in arts if "选股指标公式" in a["title"] and "主图" not in a["title"]]
        if not arts:
            print("  无文章(可能栏目页结构变了, 上面候选供排查)")
            continue
        print("  列表取到 %d 篇, 最新 id=%d (%s)" % (len(arts), arts[0]["id"], arts[0]["title"][:24]))
        last_seen = state.get(key, {}).get("last_seen", 0)
        if args.all_pages:
            # 全量模式: 归档里没有这篇(id 前缀)就算新, 不看 last_seen
            feed_dir = os.path.join(DATA_ROOT, "intake", key)
            fresh = [a for a in arts if not glob_md_has(feed_dir, a["id"])]
            # 主图类预过滤: 这类文章的选股子公式被验证码墙挡住(实测),
            # 抓详情也是浪费 → 列表阶段直接跳过
            walled_title = [a for a in fresh if "主图" in a["title"]]
            fresh = [a for a in fresh if "主图" not in a["title"]]
            print("  全量模式: 已归档跳过 %d, 主图墙文预跳过 %d, 待抓详情 %d"
                  % (len(arts) - len(fresh) - len(walled_title), len(walled_title), len(fresh)))
            # 墙文抽样判定: 该栏目整栏目被墙的(改编选股实测)就抽 2 篇验证,
            # 都是墙 → 跳过详情抓取只记清单, 不浪费 ~1.5h 无效请求
            if fresh and feed["key"] == "gaibianxuangu":
                probes = [fresh[0], fresh[len(fresh) // 2]]
                walls = 0
                for a in probes:
                    try:
                        if extract_article(a["url"], fetch(a["url"]))["wall"]:
                            walls += 1
                    except Exception:
                        pass
                    time.sleep(SLEEP)
                if walls == len(probes):
                    print("  ⚠ 抽样 %d/%d 全是验证码墙 → 本栏目只记清单不抓详情" % (walls, len(probes)))
                    for a in fresh:
                        print("   - [wall-跳过] %s %s %s" % (a.get("ym", ""), a["id"], a["title"][:40]))
                    new_total += len(fresh)
                    # last_seen 仍按列表最大 id 推进, 明天增量不重扫
                    if not args.dry_run:
                        cur = state.setdefault(key, {})
                        cur["last_seen"] = max(cur.get("last_seen", 0), max(x["id"] for x in arts))
                    continue
        else:
            fresh = [a for a in arts if a["id"] > last_seen][: args.max_new]
            print("  新文章(last_seen=%d): %d 条" % (last_seen, len(fresh)))
        got = 0
        for a in fresh:
            try:
                rec = extract_article(a["url"], fetch(a["url"]))
                status = "wall-跳过" if rec["wall"] else ("ok 码%dB" % len(rec["code"]))
                print("   - [%s] %s %s %s" % (status, rec["date"], a["id"], a["title"][:40]))
                if args.dry_run and rec is not None and a == fresh[0]:
                    print("      [md样本] %s / %s / date=%s" % (rec["title"][:30], rec["url"], rec["date"]))
                    for l in rec["code"].splitlines()[:6]:
                        print("        | " + l[:110])
                    print("        ...(共%d行)" % len(rec["code"].splitlines()))
                if not args.dry_run and not rec["wall"]:
                    feed_dir = os.path.join(DATA_ROOT, "intake", key)
                    os.makedirs(feed_dir, exist_ok=True)
                    fn = os.path.join(feed_dir, "%d_%s.md" % (a["id"], sanitize(rec["title"])[:60]))
                    md = ("# %s\n\n> 来源: %s\n> 归档时间: %s 00:00:00\n\n```\n%s\n```\n"
                          % (rec["title"], rec["url"], rec["date"] or "1970-01-01", rec["code"]))
                    with open(fn, "w", encoding="utf-8") as f:
                        f.write(md)
                    got += 1
                new_total += 1
            except Exception as e:
                print("   - [err] %d %s: %s" % (a["id"], a["title"][:30], repr(e)[:80]))
            time.sleep(SLEEP)
        # 更新 state: last_seen = 本轮看到的最大 id(--all 模式按全部列表, 增量模式按新文章)
        if not args.dry_run:
            cur = state.setdefault(key, {})
            base_ids = arts if args.all_pages else fresh
            if base_ids:
                cur["last_seen"] = max(cur.get("last_seen", 0), max(x["id"] for x in base_ids))

    if not args.dry_run:
        os.makedirs(DATA_ROOT, exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        print("\n[state] 已写", STATE_PATH)
    else:
        print("\n[dry-run] 未写盘(本次共识别新文章 %d 条)" % new_total)


if __name__ == "__main__":
    main()
