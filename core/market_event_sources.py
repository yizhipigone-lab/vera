# -*- coding: utf-8 -*-
"""core/market_event_sources.py — 大盘事件·候选源取数层 (2026-09-19)。

定位：只做"联网取数 + 归一化"，对事件分级/衰减/落库**一无所知**；
判定辅助与落库在 `core/market_event_scan.py`（同 "market_position.py 纯数学 /
market_position_runner.py IO" 的分层）。**不 import trade**（业务铁律 1，
由 tests/test_market_event_sources.py 的 AST 断言守护）。

公开接口仅 3 个（深模块：小接口厚实现）：
- fetch_all_candidates(days, today)  —— 遍历 _SOURCES 注册表拉全部候选源；
- fetch_treasury_2y_rows()           —— 美财政部 2 年期收益率序列（fed_rate 一手源）；
- fetch_akshare_2y_rows()            —— akshare 美债 2 年（fed_rate 兜底源）。

候选统一结构：{title, content, date, source, url, fact_level, hint}
- fact_level: "primary"   = 一手事实源（财联社电报/同花顺/新浪/美联储 RSS），可指原文；
              "secondary" = 转述源（华尔街见闻快讯，有发布时间），普通重大及以上档
                            必须复核到一手原文才可入库（事实溯源铁律 2026-09-19）。
- hint: 给扫描 Agent 的提示语（如"美联储一手·货币政策类=高优先候选"）。

数据源清单（2026-09-19 本机实测连通性，详见
docs/plan/2026-09-19_事件跟踪数据源扩充_计划书.md §前置实测）：
- akshare 三源（财联社电报/同花顺/新浪）——原有，从 market_event_scan 迁入本层；
- 美联储公告 RSS —— 一手，category=Monetary Policy 为确定性高优先触发；
- 华尔街见闻快讯 —— 转述，海外宏观进中文世界最快的一站；
- 美财政部收益率 CSV —— fed_rate 跟踪器一手源（美债 2 年）。
被墙/失效源（FRED/GDELT/BLS/金十 flash-api/RSSHub 公共实例）一律不接。

两个已被 fixture 锁死的坑（tests/fixtures/market_event_sources/ 为 2026-09-19 真实响应）：
- 美联储 RSS 带 UTF-8 BOM（EF BB BF），必须 utf-8-sig 解码；
- 美联储 pubDate 是 GMT，FOMC 声明 18:00 GMT = 北京时间次日凌晨 2 点，
  事件日期一律折算北京时间（UTC+8，不用 ZoneInfo —— Windows 无 tzdata 时会炸）。
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime


def _ak():
    """懒加载 akshare (可选依赖)。**不许顶层 import**: 没装它的环境 (CI)
    会在 pytest 收集期就炸成 ModuleNotFoundError —— 2026-09-20 CI 的 Test
    步骤史上首跑即因此 8 秒 exit 2 (实测定位)。
    (tests/test_market_event_sources.py 有子进程屏蔽 akshare 的回归锁)"""
    import akshare as ak
    return ak

#: 北京时间固定偏移（不用 ZoneInfo：Windows 缺 tzdata 包时 ZoneInfo 直接抛）
_CST = dt.timezone(dt.timedelta(hours=8))

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

#: 美联储公告 RSS（全量公告；category 过滤在归一化里做）
FED_RSS_URL = "https://www.federalreserve.gov/feeds/press_all.xml"

#: 华尔街见闻快讯（全球频道，免 key）
WSCN_LIVES_URL = ("https://api-one-wscn.awtmt.com/apiv1/content/lives"
                  "?channel=global-channel&limit=50")

#: 美财政部日度收益率曲线 CSV（{year} 占位；最新行在最上）
TREASURY_CSV_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
    "&field_tdr_date_value={year}&page&_format=csv")

#: 美联储 RSS 中属于"货币政策"的分类（= RUBRIC 普通重大档常客，确定性高优先触发）
_FED_MONETARY_CATEGORIES = {"Monetary Policy"}


def _http_get(url: str, timeout: float = 10.0) -> bytes | None:
    """GET 取原始字节；任何异常返回 None（fail-soft，绝不拖垮扫描）。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        return None


def _parse_dt(date_str, time_str=None) -> dt.date | None:
    """从财联社/同花顺/新浪的时间字段解析出 date。容错多种格式。"""
    s = f"{date_str} {time_str or ''}".strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except Exception:
            continue
    try:
        return dt.datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


# ── akshare 三源（自 market_event_scan 迁入；取数是取数层的职责）─────────────

def _safe_df(fn):
    """安全调用 akshare 函数; 网络/解析异常返回 None 而非抛错拖垮扫描。"""
    try:
        df = fn()
    except Exception:
        return None
    if df is None or not hasattr(df, "shape") or df.shape[0] == 0:
        return None
    return df


def _norm_cls(df) -> list[dict]:
    out = []
    for _, r in df.iterrows():
        d = _parse_dt(r.get("发布日期"), r.get("发布时间"))
        if d is None:
            continue
        out.append({
            "title": str(r.get("标题", ""))[:80],
            "content": str(r.get("内容", "")),
            "date": d.isoformat(),
            "source": "财联社电报",
            "url": "",
            "fact_level": "primary",
            "hint": "财联社电报一手",
        })
    return out


def _norm_ths(df) -> list[dict]:
    out = []
    for _, r in df.iterrows():
        d = _parse_dt(r.get("发布时间"))
        if d is None:
            continue
        out.append({
            "title": str(r.get("标题", ""))[:80],
            "content": str(r.get("内容", "")),
            "date": d.isoformat(),
            "source": "同花顺",
            "url": str(r.get("链接", "")),
            "fact_level": "primary",
            "hint": "同花顺一手",
        })
    return out


def _norm_sina(df) -> list[dict]:
    out = []
    for _, r in df.iterrows():
        d = _parse_dt(r.get("时间"))
        if d is None:
            continue
        out.append({
            "title": str(r.get("内容", ""))[:40],
            "content": str(r.get("内容", "")),
            "date": d.isoformat(),
            "source": "新浪财经",
            "url": "",
            "fact_level": "primary",
            "hint": "新浪一手",
        })
    return out


def _fetch_akshare_cls() -> list[dict]:
    return _norm_cls(_safe_df(_ak().stock_info_global_cls))


def _fetch_akshare_ths() -> list[dict]:
    return _norm_ths(_safe_df(_ak().stock_info_global_ths))


def _fetch_akshare_sina() -> list[dict]:
    return _norm_sina(_safe_df(_ak().stock_info_global_sina))


def fetch_akshare_2y_rows() -> list[tuple[dt.date, float]] | None:
    """akshare bond_zh_us_rate → [(date, 2年期收益率), ...] 旧→新; 失败 None。

    fed_rate 跟踪器的兜底源（主源 = 美财政部 CSV，见 fetch_treasury_2y_rows）。
    放在取数层而不是 scan 层：取数全归本层，scan 只做主备选择与交叉校验。
    """
    df = _safe_df(_ak().bond_zh_us_rate)
    col = "美国国债收益率2年"
    if df is None or col not in df.columns:
        return None
    df = df.dropna(subset=[col])
    rows = []
    for _, r in df.iterrows():
        try:
            d = dt.datetime.strptime(str(r["日期"])[:10], "%Y-%m-%d").date()
            rows.append((d, float(r[col])))
        except Exception:
            continue
    return rows or None


# ── 美联储 RSS（一手事实源）────────────────────────────────────────────────

def _fetch_fed_rss() -> list[dict]:
    """拉美联储公告 RSS，归一化为候选。

    坑①：响应带 UTF-8 BOM（实测 EF BB BF），必须 utf-8-sig 解码，否则 XML 解析炸。
    坑②：pubDate 是 GMT —— FOMC 声明 18:00 GMT = 北京时间次日凌晨 2 点，
          事件日期一律折北京时间（否则"哪天发生的"整体错一天）。
    category ∈ Monetary Policy（FOMC 声明/纪要/经济预测）标高优先 —— 这类条目
    是 RUBRIC 普通重大档的确定性触发器，比关键词猜可靠。
    """
    raw = _http_get(FED_RSS_URL)
    if not raw:
        return []
    try:
        root = ET.fromstring(raw.decode("utf-8-sig"))
    except Exception:
        return []
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        cat = (item.findtext("category") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if not title or not pub:
            continue
        try:
            d = parsedate_to_datetime(pub).astimezone(_CST).date()
        except Exception:
            continue
        is_monetary = cat in _FED_MONETARY_CATEGORIES
        out.append({
            "title": title[:80],
            "content": desc or title,
            "date": d.isoformat(),
            "source": "美联储RSS",
            "url": link,
            "fact_level": "primary",
            "hint": ("美联储一手·货币政策类=高优先候选" if is_monetary
                     else f"美联储一手·{cat or '其他'}类"),
        })
    return out


# ── 华尔街见闻快讯（转述源，有发布时间）─────────────────────────────────────

_TAGS_RE = re.compile(r"<[^>]+>")


def _fetch_wscn_lives() -> list[dict]:
    """拉华尔街见闻全球快讯，归一化为候选（fact_level=secondary：转述源）。

    display_time 是 Unix 秒，按本机时区（部署机=北京）折算日期；
    uri 字段是快讯原文页链接，可直接溯源到华尔街见闻。
    """
    raw = _http_get(WSCN_LIVES_URL)
    if not raw:
        return []
    try:
        items = json.loads(raw.decode("utf-8"))["data"]["items"]
    except Exception:
        return []
    out = []
    for it in items:
        text = _TAGS_RE.sub("", str(it.get("content_text") or it.get("content") or "")).strip()
        ts = it.get("display_time")
        if not text or not ts:
            continue
        try:
            d = dt.datetime.fromtimestamp(int(ts)).date()  # 本机时区=北京
        except Exception:
            continue
        out.append({
            "title": text[:80],
            "content": text,
            "date": d.isoformat(),
            "source": "华尔街见闻快讯",
            "url": str(it.get("uri") or ""),
            "fact_level": "secondary",
            "hint": "华尔街见闻转述(有发布时间); 普通重大及以上须复核一手原文",
        })
    return out


#: 候选源注册表：(源名, 拉取函数)。**加新源 = 这里加一行**，公开接口不涨。
_SOURCES = [
    ("财联社电报", _fetch_akshare_cls),
    ("同花顺", _fetch_akshare_ths),
    ("新浪财经", _fetch_akshare_sina),
    ("美联储RSS", _fetch_fed_rss),
    ("华尔街见闻快讯", _fetch_wscn_lives),
]


def fetch_all_candidates(days: int = 7, today: dt.date | None = None) -> list[dict]:
    """从全部候选源拉取近 `days` 天候选（统一结构见模块 docstring）。

    逐源独立 try/except + 时间窗过滤；任一源失败不影响其它源。
    宏观相关性粗筛与排序在调用方（market_event_scan.fetch_candidates）——
    本层只管"取到、归一化、在窗内"。
    """
    today = today or dt.date.today()
    out: list[dict] = []
    for _name, fn in _SOURCES:
        try:
            cands = fn() or []
        except Exception:
            continue  # 单个源炸了不拖垮整轮扫描
        for c in cands:
            try:
                d = dt.date.fromisoformat(c["date"])
            except Exception:
                continue
            if 0 <= (today - d).days <= days:
                out.append(c)
    return out


# ── 美财政部收益率 CSV（fed_rate 跟踪器一手源）──────────────────────────────

def fetch_treasury_2y_rows(today: dt.date | None = None) -> list[tuple[dt.date, float]] | None:
    """取美财政部 2 年期国债收益率序列，返回 [(date, rate), ...] **旧→新**；失败 None。

    - 年度 CSV，最新行在最上（返回前反转为时间升序，对齐 akshare df.iloc[-1]=最新的语义）；
    - 个别单元格为空（假日错位）跳过；
    - 年末/年初交界：当年文件不足 25 行时才补拉上一年文件拼在前面
      （fed_rate 的基准要倒数第 21 行；1 月初单年文件不够。先拉当年，
      够就不拉旧年 —— 一年省 ~360 次多余请求）。
    """
    today = today or dt.date.today()

    def _year_rows(year: int) -> list[tuple[dt.date, float]]:
        raw = _http_get(TREASURY_CSV_URL.format(year=year))
        if not raw:
            return []
        try:
            reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
            out = []
            for r in reader:
                try:
                    d = dt.datetime.strptime((r.get("Date") or "").strip(), "%m/%d/%Y").date()
                    v = float((r.get("2 Yr") or "").strip())
                except Exception:
                    continue
                out.append((d, v))
            out.sort(key=lambda x: x[0])  # CSV 最新在最上 → 反转升序
            return out
        except Exception:
            return []

    rows = _year_rows(today.year)
    if len(rows) < 25:  # 年初交界: 拼上一年尾部
        rows = _year_rows(today.year - 1) + rows
    # 防御性按日期去重（交界年本不重叠）
    dedup: dict[dt.date, float] = {}
    for d, v in rows:
        dedup[d] = v
    out = sorted(dedup.items())
    return out or None
