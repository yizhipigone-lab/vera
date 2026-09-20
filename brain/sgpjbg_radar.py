"""brain/sgpjbg_radar.py — sgpjbg(三个皮匠)研究热度雷达（2026-09-04 决策记录第二批）。

定位：机构研究注意力雷达。新闻舆情回答"记者在喊什么"，本模块回答
"研究员/机构在扎堆研究什么"——两者互补。只抓免费元数据
（标题/机构/页数/日期/行业分类/热度），不碰 PDF 全文（会员+版权红线，
见 docs/plan/2026-09-04_市场体检表与研究雷达_决策记录.md Q3/Q4）。

用法：
    python -m brain.sgpjbg_radar fetch [页数]   # 抓最新列表页落库（默认 4 页，幂等）
    python -m brain.sgpjbg_radar weekly         # 生成上周机构研究动向周报

fail-soft：网络/解析/落库任何一步挂只记日志返回空，绝不抛给调度器。
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sqlite3

from utils.logger import get_logger
from utils.sysutil import ensure_utf8_stdout, project_root

_log = get_logger("brain.sgpjbg_radar")

_LIST_URL = ("https://www.sgpjbg.com/Search.html?page={page}&q=&d=-1&type=0&cd=1"
             "&qws=0&esp=100&hybg=0&vt=list&kqs=0&hgjc=1&iszc=1_1"
             "&kyw=1&kthyb=1&gslx=&khyhj=0")
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_TIMEOUT = 20
_DB_PATH = project_root() / "data" / "sgpjbg_radar.db"
_REPORT_DIR = project_root() / "output" / "reports"

# 行结构（2026-09-04 实测）：baogao/{id}.html 单引号 + title 双引号 →
# 110px 页数 → 120px 日期 → baogaolist 分类 → z-re.png 热度
_ROW_RE = re.compile(
    r"baogao/(\d+)\.html'\s+title=\"([^\"]+)\".*?"
    r"width:\s*110px;\">(\d+)页</td>.*?"
    r"width:\s*120px;\">(\d{4}-\d{2}-\d{2})</td>.*?"
    r"baogaolist-(\d+)\.html'[^>]*>([^<]+)</a>.*?"
    r"z-re\.png[^>]*>(\d+)</td>",
    re.S)

_TITLE_TAIL_RE = re.compile(r"（\d+页）(?:\.pdf)?$|\.pdf$")


def _db() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS reports(
               id INTEGER PRIMARY KEY,
               title TEXT, org TEXT, pages INTEGER,
               pub_date TEXT, category TEXT, category_id TEXT,
               heat INTEGER, url TEXT,
               fetched_at TEXT)""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reports_pubdate ON reports(pub_date)")
    return conn


def _split_title(raw: str) -> tuple[str, str]:
    """'尼尔森IQ：2026开学季...（52页）.pdf' → ('尼尔森IQ', '2026开学季...')。"""
    t = _TITLE_TAIL_RE.sub("", raw).strip()
    m = re.split(r"[：:]", t, maxsplit=1)
    if len(m) == 2:
        return m[0].strip(), m[1].strip()
    return "", t


def _md(text: str, max_len: int = 80) -> str:
    """外部抓取文本进 Markdown 前的消毒：转义表格分隔符、去换行、截断。"""
    t = re.sub(r"[\r\n]+", " ", str(text)).replace("|", "\\|").strip()
    return t[:max_len] + "…" if len(t) > max_len else t


def _holding_names() -> tuple[list[str], list[str]]:
    """当前持仓关键词反查：返回 (可匹配的名称列表, 未能反查名称的代码列表)。

    数据源 data/trade/trade.db（只读）+ kg 图谱反查名称。ETF 等非公司节点
    反查不到名称属正常，归入第二项如实展示。任何一步失败返 ([], [])。
    """
    try:
        db = project_root() / "data" / "trade" / "trade.db"
        if not db.exists():
            return [], []
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            codes = [r[0] for r in conn.execute(
                "SELECT code FROM position_snapshot WHERE volume>0")]
        finally:
            conn.close()
        names, unresolved = [], []
        kg = project_root() / "kg" / "graph.db"
        for code in codes:
            name = None
            if kg.exists():
                conn = sqlite3.connect(f"file:{kg.as_posix()}?mode=ro", uri=True)
                try:
                    row = conn.execute(
                        "SELECT name FROM kg_nodes WHERE node_type='company' "
                        "AND code=?", (code.split(".")[0],)).fetchone()
                    name = row[0] if row else None
                finally:
                    conn.close()
            (names if name else unresolved).append(name or code)
        return names, unresolved
    except Exception:
        return [], []


def _fetch_page(page: int) -> list[dict]:
    import requests
    html = ""
    # 反爬是"先发质询卡再放行"：首次 403 只发 cookie，带 cookie 重试才给真页面
    with requests.Session() as s:
        s.headers.update(_UA)
        for attempt in range(3):
            r = s.get(_LIST_URL.format(page=page), timeout=_TIMEOUT)
            if r.status_code == 200:
                html = r.text
                break
            if r.status_code != 403:
                r.raise_for_status()
    if not html:
        raise RuntimeError(f"第 {page} 页持续 403（反爬未放行）")
    items = []
    for m in _ROW_RE.finditer(html):
        rid, raw_title, pages, pub_date, cat_id, cat, heat = m.groups()
        org, title = _split_title(raw_title)
        items.append({
            "id": int(rid), "title": title, "org": org,
            "pages": int(pages), "pub_date": pub_date,
            "category": cat.strip(), "category_id": cat_id,
            "heat": int(heat),
            "url": f"https://www.sgpjbg.com/baogao/{rid}.html",
        })
    return items


def fetch_latest(max_pages: int = 4) -> dict:
    """抓最新列表页落库（INSERT OR IGNORE 幂等，重跑/补跑安全）。

    翻页提前停止：整页日期都早于今天，说明今日新增已抓完。
    返回统计 dict 供调度器记日志。
    """
    today = dt.date.today().isoformat()
    total, new, empty_pages = 0, 0, 0
    conn = _db()
    try:
        for page in range(1, max_pages + 1):
            try:
                items = _fetch_page(page)
            except Exception as e:
                _log.warning("sgpjbg 第 %d 页抓取失败: %s", page, e)
                break
            if not items:
                # 200 但解析 0 条：大概率网站改版正则失配，绝不能静默当成"今日无新增"
                empty_pages += 1
                _log.warning("sgpjbg 第 %d 页解析 0 条（页面结构可能已改版，"
                             "需检查 _ROW_RE）", page)
                break
            total += len(items)
            for it in items:
                exists = conn.execute("SELECT 1 FROM reports WHERE id=?",
                                      (it["id"],)).fetchone()
                conn.execute(
                    """INSERT INTO reports
                       (id, title, org, pages, pub_date, category,
                        category_id, heat, url, fetched_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET
                       heat=excluded.heat, fetched_at=excluded.fetched_at""",
                    (it["id"], it["title"], it["org"], it["pages"],
                     it["pub_date"], it["category"], it["category_id"],
                     it["heat"], it["url"], dt.datetime.now().isoformat()))
                if not exists:
                    new += 1
            if max(it["pub_date"] for it in items) < today:
                break  # 整页都是旧报告，今日新增已抓完
    finally:
        conn.commit()
        conn.close()
    stats = {"fetched": total, "new": new, "empty_pages": empty_pages}
    _log.info("sgpjbg 抓取完成: %s", stats)
    return stats


def _watch_concepts() -> list[str]:
    """舆情同款关注概念（config/sentiment.yaml watch.concepts）；读不到返 []。"""
    try:
        from pathlib import Path
        from brain.alert_rules import load_config
        cfg = load_config(Path("config/sentiment.yaml"))
        return list(cfg.get("watch", {}).get("concepts", []) or [])
    except Exception:
        return []


def weekly_report(days: int = 7) -> str | None:
    """生成最近 days 天 vs 前一周期 的机构研究动向周报 → 写 output/reports/，返路径。

    结构（决策记录 Q4）：开头摘要（本周 N 篇/关注相关 M 篇）→
    行业热度环比 → 活跃机构 → 高热度报告 → 关注概念命中清单。
    """
    conn = _db()
    try:
        end = dt.date.today()
        start = end - dt.timedelta(days=days)
        prev_start = start - dt.timedelta(days=days)
        cur = conn.execute(
            "SELECT * FROM reports WHERE pub_date>=? AND pub_date<=?",
            (start.isoformat(), end.isoformat())).fetchall()
        prev = conn.execute(
            "SELECT category, COUNT(*) FROM reports "
            "WHERE pub_date>=? AND pub_date<? GROUP BY category",
            (prev_start.isoformat(), start.isoformat())).fetchall()
    finally:
        conn.close()
    if not cur:
        _log.warning("sgpjbg 周报: 近 %d 天无数据，跳过", days)
        return None

    cols = ("id", "title", "org", "pages", "pub_date", "category",
            "category_id", "heat", "url", "fetched_at")
    rows = [dict(zip(cols, r)) for r in cur]
    concepts = _watch_concepts()
    holdings, holdings_unresolved = _holding_names()
    keywords = concepts + holdings  # 关注概念 + 持仓个股名，共同决定"与我相关"

    def _hit(r: dict) -> bool:
        text = r["title"] + r["org"] + r["category"]
        return any(k in text for k in keywords)

    hits = [r for r in rows if _hit(r)]

    cat_cur: dict[str, int] = {}
    for r in rows:
        cat_cur[r["category"]] = cat_cur.get(r["category"], 0) + 1
    cat_prev = dict(prev)
    top_cats = sorted(cat_cur.items(), key=lambda x: -x[1])[:10]

    org_cnt: dict[str, int] = {}
    for r in rows:
        if r["org"]:
            org_cnt[r["org"]] = org_cnt.get(r["org"], 0) + 1
    top_orgs = sorted(org_cnt.items(), key=lambda x: -x[1])[:10]

    top_heat = sorted(rows, key=lambda x: -x["heat"])[:10]

    lines = [
        f"# sgpjbg 机构研究动向周报（{start.isoformat()} ~ {end.isoformat()}）",
        "",
        f"**本周收录 {len(rows)} 篇报告，其中与你关注概念相关的 {len(hits)} 篇。**",
        "",
        "> 数据来源：三个皮匠报告（sgpjbg.com）免费元数据（标题/机构/行业/热度），"
        "不含 PDF 全文。回答的问题：机构最近在扎堆研究什么。",
        "",
        "## 行业热度 Top10（环比上一周期）",
        "",
        "| 行业 | 本周篇数 | 上周期篇数 | 环比 |",
        "|---|---|---|---|",
    ]
    for cat, n in top_cats:
        p = cat_prev.get(cat, 0)
        delta = f"{n - p:+d}" if p else "新上榜"
        lines.append(f"| {_md(cat)} | {n} | {p} | {delta} |")
    lines += ["", "## 最活跃机构 Top10（按报告篇数）", ""]
    lines += [f"{i + 1}. {_md(org)}（{n} 篇）" for i, (org, n) in enumerate(top_orgs)]
    lines += ["", "## 本周热度最高 Top10", ""]
    lines += [f"{i + 1}. [{_md(r['org']) + '：' if r['org'] else ''}{_md(r['title'])}]"
              f"({r['url']})（{_md(r['category'])}，热度 {r['heat']}，{r['pub_date']}）"
              for i, r in enumerate(top_heat)]
    lines += ["", f"## 关注命中（{len(hits)} 篇）",
              "",
              f"关注概念：{'、'.join(concepts) if concepts else '（未配置）'}"
              f"；持仓个股：{'、'.join(holdings) if holdings else '（无可匹配名称）'}"
              + (f"（{ '、'.join(holdings_unresolved) } 未能反查名称，或为 ETF）"
                 if holdings_unresolved else ""), ""]
    lines += [f"- [{_md(r['title'])}]({r['url']})（{_md(r['org']) or '机构未标注'}，"
              f"{_md(r['category'])}，{r['pub_date']}）" for r in hits[:30]]
    if len(hits) > 30:
        lines.append(f"- ……其余 {len(hits) - 30} 篇见库表 data/sgpjbg_radar.db")

    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = _REPORT_DIR / f"sgpjbg_研究雷达周报_{end.isoformat()}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    _log.info("sgpjbg 周报已生成: %s（本周 %d 篇/相关 %d 篇）",
              path, len(rows), len(hits))
    return str(path)


def recent_reports(days: int = 7, limit: int = 50) -> list[dict]:
    """近 days 天入库的报告元数据，按热度降序（舆情页「机构研究雷达」块用）。

    2026-09-20 新增：周报只给 Top10 摘要，页面要能直接翻原始元数据。SQL 留在本模块
    （属主），sentiment_api 不碰 schema。fail-soft：任何异常返 []。
    """
    cols = ("id", "title", "org", "pages", "pub_date", "category", "heat", "url")
    try:
        conn = _db()
        try:
            end = dt.date.today()
            start = end - dt.timedelta(days=days)
            rows = conn.execute(
                "SELECT id,title,org,pages,pub_date,category,heat,url FROM reports "
                "WHERE pub_date>=? AND pub_date<=? "
                "ORDER BY heat DESC, pub_date DESC LIMIT ?",
                (start.isoformat(), end.isoformat(), int(limit))).fetchall()
        finally:
            conn.close()
    except Exception as e:
        _log.warning("sgpjbg 近期报告查询失败 (降级空): %s", e)
        return []
    return [dict(zip(cols, r)) for r in rows]


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdout()
    ap = argparse.ArgumentParser(prog="python -m brain.sgpjbg_radar",
                                 description="sgpjbg 研究热度雷达（免费元数据）")
    ap.add_argument("cmd", choices=["fetch", "weekly"])
    ap.add_argument("arg", nargs="?", default=None,
                    help="fetch=抓取页数（默认 4）")
    args = ap.parse_args(argv)
    if args.cmd == "fetch":
        print(fetch_latest(int(args.arg) if args.arg else 4))
    else:
        path = weekly_report()
        print(path or "（本周无数据，未生成）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
