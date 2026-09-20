# -*- coding: utf-8 -*-
"""tests/test_market_event_sources.py — 事件候选源取数层 + fed_rate 双源 单元测试 (2026-09-19)。

fixture 全部来自 2026-09-19 真实联网响应（tests/fixtures/market_event_sources/），
**测试零联网**：一律 monkeypatch `_http_get` / akshare 行函数。
"""
from __future__ import annotations

import ast
import datetime as dt
import pathlib

import pytest

from core import market_event_scan as ms
from core import market_event_sources as mes

FIX = pathlib.Path(__file__).parent / "fixtures" / "market_event_sources"


def _fx(name: str) -> bytes:
    return (FIX / name).read_bytes()


# ── 美联储 RSS ────────────────────────────────────────────────────────────

def test_fed_rss_bom_and_monetary_priority(monkeypatch):
    """坑①锁: 响应带 UTF-8 BOM(EF BB BF)也能解析; Monetary Policy 类标高优先。"""
    raw = _fx("fed_press_all.xml")
    assert raw[:3] == b"\xef\xbb\xbf", "fixture 本身应带 BOM(防 fixture 被无意转码)"
    monkeypatch.setattr(mes, "_http_get", lambda url: raw)
    cands = mes._fetch_fed_rss()
    assert cands, "应解析出公告条目"
    monetary = [c for c in cands if "货币政策" in c["hint"]]
    assert monetary, "fixture 含 9月16日 FOMC 声明(Monetary Policy), 应被标高优先"
    assert all(c["fact_level"] == "primary" for c in cands)
    assert all(c["url"].startswith("https://www.federalreserve.gov/") for c in cands)


def test_fed_rss_gmt_to_beijing_date(monkeypatch):
    """坑②锁: pubDate 是 GMT —— FOMC 声明 2026-09-16 18:00 GMT = 北京 9月17日 02:00,
    事件日期必须折成 2026-09-17(不折会整体错一天)。"""
    monkeypatch.setattr(mes, "_http_get", lambda url: _fx("fed_press_all.xml"))
    cands = mes._fetch_fed_rss()
    fomc = [c for c in cands if "FOMC statement" in c["title"]]
    assert fomc, "fixture 应含 FOMC 声明"
    assert fomc[0]["date"] == "2026-09-17"


def test_fed_rss_network_fail_returns_empty(monkeypatch):
    monkeypatch.setattr(mes, "_http_get", lambda url: None)
    assert mes._fetch_fed_rss() == []


# ── 华尔街见闻快讯 ──────────────────────────────────────────────────────────

def test_wscn_lives_parse(monkeypatch):
    """转述源: Unix 秒→日期(本机=北京时区)、HTML 剥离、uri 溯源链接、secondary 标记。"""
    monkeypatch.setattr(mes, "_http_get", lambda url: _fx("wscn_lives.json"))
    cands = mes._fetch_wscn_lives()
    assert cands, "应解析出快讯条目"
    c = cands[0]
    assert c["fact_level"] == "secondary"
    assert "复核" in c["hint"]
    assert "<" not in c["content"], "HTML 标签应已剥离"
    assert c["url"].startswith("https://wallstreetcn.com/")
    # display_time=1789816866 → 2026-09-19 19:21 北京(本机时区)
    assert c["date"] == "2026-09-19"


def test_wscn_network_fail_returns_empty(monkeypatch):
    monkeypatch.setattr(mes, "_http_get", lambda url: None)
    assert mes._fetch_wscn_lives() == []


# ── 美财政部 CSV ────────────────────────────────────────────────────────────

def _treasury_http(url: str):
    """当年(2026)返回真实 fixture, 上一年返回 None(模拟拿不到)。"""
    return _fx("us_treasury_yields_2026.csv") if "2026" in url else None


def test_treasury_csv_parse_ascending(monkeypatch):
    """CSV 最新行在最上 → 返回必须旧→新升序; 最后一行 = 2026-09-18 的 4.76。"""
    monkeypatch.setattr(mes, "_http_get", _treasury_http)
    rows = mes.fetch_treasury_2y_rows(today=dt.date(2026, 9, 19))
    assert rows and len(rows) > 100
    assert rows == sorted(rows), "必须升序(对齐 akshare df.iloc[-1]=最新的语义)"
    assert rows[-1] == (dt.date(2026, 9, 18), 4.76)


def test_treasury_all_fail_returns_none(monkeypatch):
    monkeypatch.setattr(mes, "_http_get", lambda url: None)
    assert mes.fetch_treasury_2y_rows(today=dt.date(2026, 9, 19)) is None


# ── 源注册表隔离 ────────────────────────────────────────────────────────────

def test_fetch_all_candidates_source_isolation(monkeypatch):
    """任一源抛异常不拖垮其它源; 时间窗过滤生效。"""
    def _boom():
        raise RuntimeError("源炸了")

    def _ok():
        return [{"title": "央行降准", "content": "x", "date": dt.date.today().isoformat(),
                 "source": "测试源", "url": "", "fact_level": "primary", "hint": ""},
                {"title": "旧闻", "content": "x", "date": "2020-01-01",
                 "source": "测试源", "url": "", "fact_level": "primary", "hint": ""}]

    monkeypatch.setattr(mes, "_SOURCES", [("坏源", _boom), ("好源", _ok)])
    out = mes.fetch_all_candidates(days=7)
    assert len(out) == 1 and out[0]["title"] == "央行降准", "坏源被跳过、超窗旧闻被滤掉"


# ── fetch_candidates 宏观粗筛(判定辅助层) ───────────────────────────────────

def test_fetch_candidates_macro_filter(monkeypatch):
    """取数层的输出再过宏观关键词粗筛: 无关条目被滤掉, 英文一手源关键词也放行。"""
    fake = [
        {"title": "美联储宣布降息25bp", "content": "federal funds rate", "date": "2026-09-19",
         "source": "美联储RSS", "url": "u", "fact_level": "primary", "hint": ""},
        {"title": "某明星演唱会门票开售", "content": "娱乐", "date": "2026-09-19",
         "source": "美联储RSS", "url": "u", "fact_level": "primary", "hint": ""},
    ]
    monkeypatch.setattr(ms.mes, "fetch_all_candidates", lambda days, today: fake)
    out = ms.fetch_candidates(days=7, today=dt.date(2026, 9, 19))
    assert len(out) == 1 and "美联储" in out[0]["title"]


# ── fed_rate 双源: 主备 + 交叉校验 ─────────────────────────────────────────

def _rows(base: float, n: int = 25, start: dt.date = dt.date(2026, 8, 20)):
    return [(start + dt.timedelta(days=i), round(base + i * 0.001, 3)) for i in range(n)]


def test_proxy_treasury_primary_no_warning_when_consistent(monkeypatch):
    """双源一致(差≤5bp): 用财政部一手源, 无 cross_check 告警。"""
    monkeypatch.setattr(ms.mes, "fetch_treasury_2y_rows", lambda today=None: _rows(4.50))
    monkeypatch.setattr(ms.mes, "fetch_akshare_2y_rows", lambda: _rows(4.50))
    p = ms.fetch_fed_rate_proxy(today=dt.date(2026, 9, 19))
    assert "美财政部" in p["source"]
    assert "cross_check" not in p


def test_proxy_cross_check_warns_on_divergence(monkeypatch):
    """双源偏差 >5bp: 仍用一手源, 但带 cross_check 告警串。"""
    monkeypatch.setattr(ms.mes, "fetch_treasury_2y_rows", lambda today=None: _rows(4.50))
    monkeypatch.setattr(ms.mes, "fetch_akshare_2y_rows", lambda: _rows(4.60))  # 全程高 10bp
    p = ms.fetch_fed_rate_proxy(today=dt.date(2026, 9, 19))
    assert "美财政部" in p["source"]
    assert "cross_check" in p and "人工复核" in p["cross_check"]


def test_proxy_no_warning_when_dates_mismatch(monkeypatch):
    """两源最新日期不齐(一方滞后一天)时不比数、不告警 —— 防把"滞后"误报成"数据冲突"
    (2026-09-19 自检抓到: 9/17→9/18 只差一天就差 9bp, 超 5bp 阈值必误报)。"""
    monkeypatch.setattr(ms.mes, "fetch_treasury_2y_rows", lambda today=None: _rows(4.50))
    # akshare 滞后一天: 同一序列但整体早一天开始 → 末行日期不同
    monkeypatch.setattr(ms.mes, "fetch_akshare_2y_rows",
                        lambda: _rows(4.50, start=dt.date(2026, 8, 19)))
    p = ms.fetch_fed_rate_proxy(today=dt.date(2026, 9, 19))
    assert "美财政部" in p["source"]
    assert "cross_check" not in p


def test_proxy_fallback_to_akshare(monkeypatch):
    """财政部挂 → akshare 兜底接上, source 字段如实标注。"""
    monkeypatch.setattr(ms.mes, "fetch_treasury_2y_rows", lambda today=None: None)
    monkeypatch.setattr(ms.mes, "fetch_akshare_2y_rows", lambda: _rows(4.50))
    p = ms.fetch_fed_rate_proxy(today=dt.date(2026, 9, 19))
    assert "akshare" in p["source"]


def test_proxy_both_fail_returns_none(monkeypatch):
    monkeypatch.setattr(ms.mes, "fetch_treasury_2y_rows", lambda today=None: None)
    monkeypatch.setattr(ms.mes, "fetch_akshare_2y_rows", lambda: None)
    assert ms.fetch_fed_rate_proxy(today=dt.date(2026, 9, 19)) is None
    r = ms.update_fed_rate_event(today=dt.date(2026, 9, 19))
    assert r["updated"] is False and r["reason"] == "no_data"


def test_proxy_treasury_insufficient_rows_falls_back(monkeypatch):
    """财政部行数 <21(年初交界也补不齐) → 落 akshare。"""
    monkeypatch.setattr(ms.mes, "fetch_treasury_2y_rows", lambda today=None: _rows(4.50, n=10))
    monkeypatch.setattr(ms.mes, "fetch_akshare_2y_rows", lambda: _rows(4.50))
    p = ms.fetch_fed_rate_proxy(today=dt.date(2026, 9, 19))
    assert "akshare" in p["source"]


# ── 铁律 1 AST 守护(此前本模块裸奔, 本次补上) ──────────────────────────────

def test_no_import_trade():
    """事件扫描两个模块绝不 import trade(业务铁律 1)。"""
    for mod in (mes, ms):
        tree = ast.parse(pathlib.Path(mod.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert a.name.split(".")[0] != "trade", f"{mod.__name__} import 了 trade"
            elif isinstance(node, ast.ImportFrom):
                assert node.module is None or node.module.split(".")[0] != "trade", \
                    f"{mod.__name__} from trade import ..."

# ── 可选依赖懒加载 (2026-09-20 CI 史上首跑修复) ─────────────────────────────

def test_顶层import不依赖akshare():
    """akshare 是可选依赖, 顶层 import 必须懒加载。

    实证: 2026-09-20 CI 的 Test 步骤史上首跑 (此前全被 lint 短路 skipped),
    8 秒即 exit 2 —— pytest 收集期 import 本文件 → core.market_event_sources
    顶层 `import akshare` → CI 不装 akshare → ModuleNotFoundError 收集中断。
    本用例在子进程里屏蔽 akshare 后 import 该模块, 锁住"顶层不炸"。
    """
    import subprocess
    import sys
    code = ("import sys; sys.modules['akshare'] = None; "
            "import core.market_event_sources")
    r = subprocess.run([sys.executable, "-c", code],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"屏蔽 akshare 后 import 失败: {r.stderr[-400:]}"

