"""pytest 全局 fixture — TDX 路径注入 + TdxConnector 生命周期管理 + Mock 工厂。

审计 T-C1 (2026-07-15): 原本 TDX 路径注入散落在多个测试文件的 fixture 里重复定义,
且 test_end_to_end_real_pipeline.py:42 引用不存在的 conftest。此处统一收口。

T-H-2 (2026-07-15): 加 FakeTq/FakeConnector mock 工厂 + autouse teardown
(FormulaRunner + DataFetcher connector 共享重置)。
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pytest

# TDX 插件路径 (通达信 PYPlugins\user 目录; 单一真相: core/tdx_path.py)
from core.tdx_path import tdx_plugins_user

_TDX_PATH = tdx_plugins_user()


@pytest.fixture(scope="session")
def tdx_path():
    """确保 TDX 模块路径在 sys.path 中。session 级别, 所有测试共享。"""
    if _TDX_PATH not in sys.path:
        sys.path.insert(0, _TDX_PATH)
    return _TDX_PATH


@pytest.fixture(scope="module")
def tdx_connector():
    """module 级别 TdxConnector 生命周期: initialize → yield → close。"""
    from core.connector import TdxConnector
    TdxConnector.initialize()
    yield TdxConnector
    TdxConnector.close()


# ═══════════════════════════════════════════════════════════════
# T-H-2 Mock 工厂 (2026-07-15)
# ═══════════════════════════════════════════════════════════════

class FakeTq:
    """Mock TDX TQ API, 可配置返回数据."""

    def __init__(self, ohlc_data=None, formula_result=None):
        self.calls = []
        self._ohlc = ohlc_data or {}
        self._formula = formula_result or []

    def get_market_data(self, **kw):
        self.calls.append(("get_market_data", kw))
        return self._ohlc if self._ohlc else {
            "ErrorId": "0",
        }

    def get_stock_list(self, *args, **kwargs):
        self.calls.append(("get_stock_list", args, kwargs))
        return [{"Code": "000001.SZ"}, {"Code": "600519.SH"}]

    def formula_process_mul_xg(self, formula_name, formula_arg="",
                                return_count=0, return_date=True,
                                stock_list=None, stock_period="1d",
                                start_time="", end_time="", count=3000,
                                dividend_type=1):
        self.calls.append(("formula_process_mul_xg", {
            "formula_name": formula_name, "stock_list": stock_list,
        }))
        return self._formula


class FakeConnector:
    """Mock TdxConnector, 包裹 FakeTq."""

    def __init__(self, ohlc_data=None, formula_result=None):
        self.connected = False
        self.tq_obj = FakeTq(ohlc_data, formula_result)

    def ensure_connected(self):
        self.connected = True

    def tq(self):
        return self.tq_obj

    @staticmethod
    def initialize():
        pass

    @staticmethod
    def close():
        pass


@pytest.fixture(autouse=True)
def _reset_all_connectors():
    """每个测试后自动重置所有 connector seam (防测试间污染)."""
    yield
    from core.data_fetcher import DataFetcher
    from core.formula_runner import FormulaRunner
    DataFetcher.reset_connector()
    FormulaRunner.reset_connector()


@pytest.fixture(autouse=True)
def _isolate_caches(tmp_path):
    """缓存根目录隔离 (2026-07-27 缓存投毒事件)。

    事件: test_sector_selection 把 mock 的 3 股池经 selector 接缝写入真实
    data/universe_cache, key 与用户 QUANTQQ 配置完全相同 → 用户回测命中
    毒缓存, 300 只池变 3 只, 1.5 年只有 21 笔交易。

    2026-08-01 批次2: 改经 utils.parquet_cache 注册表一行式覆盖, 不再
    import/monkeypatch 四个缓存模块。四个缓存根统一指到 per-test tmp:
    测试自己的缓存行为不受影响 (各 cache 测试本来就显式传 root 或自行 patch),
    但任何"忘了隔离"的测试从此不可能再污染生产缓存。
    """
    from utils import parquet_cache as pcu
    for name in ("universe_cache", "selection_cache",
                 "signal_day_cache", "matrix_cache"):
        pcu.set_root(name, tmp_path / name)

    # E1 (2026-09-16 审计): kline_cache 不走 parquet_cache 注册表, 接缝是
    # DataFetcher._KLINE_CACHE_DIR (core/data_fetcher.py:48) —— 指到 per-test tmp,
    # 保存原值 yield 后恢复 (该类属性可能被其他测试自行 monkeypatch, 恢复原值不硬写 None)
    from core.data_fetcher import DataFetcher
    orig_kline_dir = DataFetcher._KLINE_CACHE_DIR
    DataFetcher._KLINE_CACHE_DIR = str(tmp_path / "kline_cache")

    # E4 (2026-09-16 审计): 影子日志是相对路径 data/shadow_rotation.jsonl,
    # 测试真实触发 _shadow_tick 会把假影子状态写进生产 jsonl —— 同法隔离。
    # 注意 trade/rotation.py:57 按值 import 了 SHADOW_LOG_PATH, 若已加载需一并改。
    import trade.shadow as _shadow
    orig_shadow_path = _shadow.SHADOW_LOG_PATH
    tmp_shadow = str(tmp_path / "shadow_rotation.jsonl")
    _shadow.SHADOW_LOG_PATH = tmp_shadow
    _rot = sys.modules.get("trade.rotation")
    orig_rot_shadow = None
    if _rot is not None:
        orig_rot_shadow = _rot.SHADOW_LOG_PATH
        _rot.SHADOW_LOG_PATH = tmp_shadow
    # 2026-09-17: 大盘位置连续录像 + 日线缓存目录 —— 同一投毒风险 (测试若真跑
    # collect 会把假指标写进生产 data/market_position/daily.jsonl)。
    import core.market_position_runner as _mpr
    orig_mp_path = _mpr.DAILY_PATH
    orig_mp_kline = _mpr.KLINE_1D_DIR
    orig_mp_erp = _mpr.ERP_PATH
    _mpr.DAILY_PATH = tmp_path / "market_position" / "daily.jsonl"
    _mpr.KLINE_1D_DIR = tmp_path / "kline_cache" / "1d"
    _mpr.ERP_PATH = tmp_path / "market_position" / "erp.jsonl"
    # ERP 是**联网**取数 —— 测试里必须关掉 (否则跑一次测试就真去拉网络,
    # 既不隔离也不可重复)。要测取数逻辑的用例自己 monkeypatch 打开并打桩。
    orig_erp_fetch = os.environ.get(_mpr.ERP_FETCH_ENV)
    os.environ[_mpr.ERP_FETCH_ENV] = "1"
    # 2026-09-17 事故修复 (深度审查 HIGH-1): 大脑向量索引漏隔离 ——
    # test_brain_archive / test_brain 只 monkeypatch 了 arch.ARCHIVE_DIR, 没隔离
    # brain.search_engine.INDEX_DIR; 于是 archive 落盘后的 update_file(f) 拿到一个
    # tmp 绝对路径, source 退化成绝对路径并写进**生产** data/brain_vectors/。
    # 实测: meta.jsonl 641 个 source 里 639 条是 pytest 临时路径, company/ 0 条,
    # eval 命中率 0.0。与 2026-07-27 缓存投毒事件同一病类 (当时漏了这个缓存)。
    import brain.search_engine as _se
    orig_index_dir = _se.INDEX_DIR
    _se.INDEX_DIR = tmp_path / "brain_vectors"
    # 2026-09-17 M7（第二轮审计 F4）: M5/M6 又新增了两条落盘路径，
    # 它们当时只靠"各用例自己传 tmp / 模块内 fixture"兜着 —— 与 07-27、09-17
    # 两次投毒事故同一个病类（**新增落盘路径必须一并进全局隔离**，不是"记得传 tmp"）。
    import notes_gen.daily as _drev
    orig_review_dir = _drev.REVIEW_DIR
    _drev.REVIEW_DIR = tmp_path / "daily_review"
    import tools.mail_to_corpus as _mtc
    orig_corpus_dir = _mtc.CORPUS_DIR
    _mtc.CORPUS_DIR = tmp_path / "research_inbox"
    # 双保险: archive 侧有现成开关, 直接关掉归档同步 (brain/archive.py 读它)
    orig_no_sync = os.environ.get("BRAIN_NO_INDEX_SYNC")
    os.environ["BRAIN_NO_INDEX_SYNC"] = "1"
    try:
        yield
    finally:
        DataFetcher._KLINE_CACHE_DIR = orig_kline_dir
        _shadow.SHADOW_LOG_PATH = orig_shadow_path
        if _rot is not None:
            _rot.SHADOW_LOG_PATH = orig_rot_shadow
        _mpr.DAILY_PATH = orig_mp_path
        _mpr.KLINE_1D_DIR = orig_mp_kline
        _mpr.ERP_PATH = orig_mp_erp
        _drev.REVIEW_DIR = orig_review_dir
        _mtc.CORPUS_DIR = orig_corpus_dir
        if orig_erp_fetch is None:
            os.environ.pop(_mpr.ERP_FETCH_ENV, None)
        else:
            os.environ[_mpr.ERP_FETCH_ENV] = orig_erp_fetch
        _se.INDEX_DIR = orig_index_dir
        if orig_no_sync is None:
            os.environ.pop("BRAIN_NO_INDEX_SYNC", None)
        else:
            os.environ["BRAIN_NO_INDEX_SYNC"] = orig_no_sync


@pytest.fixture(autouse=True)
def _stop_trade_apps(monkeypatch):
    """每个测试结束后停掉所有 TradeApp / FeishuNotifier (2026-08-04 飞书骚扰事件)。

    事件: 多个测试 (test_e2e_trade / test_auto_buy 等) 起了真实 TradeApp
    却从不调 stop(), 引擎/通知 worker 等 daemon 线程活到 pytest 会话结束,
    期间及之后持续向 .env 里的真实飞书 webhook 投递测试成交卡片。

    裁决: 测试期间该发发, 不拦网络; 但测试一停必须收尾干净 ——
    所有 TradeApp.stop() (含 notifier 线程 join) 全部补调, 保证后续零投递。
    对已自行 stop 的 app 再 stop 一次无副作用 (各 stop 均幂等/可重入)。
    """
    try:
        import trade_main
        from trade.notifier import FeishuNotifier
    except Exception:
        yield
        return
    apps: list = []
    notifiers: list = []
    orig_app_init = trade_main.TradeApp.__init__
    orig_notifier_init = FeishuNotifier.__init__

    def _app_init(self, *a, **k):
        orig_app_init(self, *a, **k)
        apps.append(self)

    def _notifier_init(self, *a, **k):
        orig_notifier_init(self, *a, **k)
        notifiers.append(self)

    monkeypatch.setattr(trade_main.TradeApp, "__init__", _app_init)
    monkeypatch.setattr(FeishuNotifier, "__init__", _notifier_init)
    yield
    for app in apps:
        try:
            app.stop()
        except Exception:
            pass
    for n in notifiers:
        try:
            n.stop()
        except Exception:
            pass


@pytest.fixture(autouse=True, scope="session")
def _block_network_egress():
    """session 级无条件焊死 urllib.request.urlopen (2026-08-07 飞书泄漏根治)。

    事件: 8-04 / 8-07 两次测试期间向 .env 里真实飞书 webhook 投递测试成交卡片。
    根因不在 FeishuNotifier 本身, 而在测试治理层只拦了"线程"没拦"网络":
      - _stop_trade_apps (上) 只在测试结束后调 app.stop() / notifier.stop();
      - 但 stop() 是 drain 语义 (worker 把队列剩余消息发完才返回, 见
        notifier.py:99-108), 且 worker 走的是真 urllib.request.urlopen;
      - 于是"收尾"这一下反而把测试消息真发了出去 —— 必须拦发送才能根治。

    裁决: session 级把 urllib.request.urlopen 无条件换成假响应, 整个 pytest
    进程零真实网络出口 (与 test_policy_sources "零网络零外部依赖" 哲学一致):
      - 不依赖 fixture teardown 顺序 —— 焊死是 session 级永久的, 不像 function
        scope monkeypatch 会在测试间复原, 留下 "stop drain 时恰好没被 mock"
        的窗口 (那正是两次泄漏的窗口);
      - function-scope monkeypatch 仍能覆盖做精确断言 —— test_notifier 的
        captured fixture (monkeypatch.setattr urlopen) 在其生效的测试内照常
        捕获 req.data, teardown 后回落到本 session 假, 链条不断;
      - gov_cn 测试 mock 的是上游 _http_get, 根本不触 urlopen, 不受影响。

    与 _stop_trade_apps 分工: 那个管"线程生命周期收尾", 这个管"网络出口焊死",
    双保险 —— 线程即便泄漏到会话外也发不出去。teardown 时若拦截数 >0 打印
    一行摘要, 方便定位"哪个测试还在试图触网"。
    """
    import urllib.request as _urlreq

    _orig = _urlreq.urlopen

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"code":0}'

    blocked = {"n": 0}

    def _fake_urlopen(url_or_req, timeout=None, *args, **kwargs):
        blocked["n"] += 1
        return _FakeResp()

    _urlreq.urlopen = _fake_urlopen

    # E3 (2026-09-16 审计): 同一 fixture 内把 requests 与 smtplib 也焊死。
    # requests: 假 200 空响应 (与 urlopen 同哲学, "安全假成功");
    # smtplib: 发邮件没有"安全假成功"语义, 假类直接抛 RuntimeError 拦死。
    import requests as _requests
    import smtplib as _smtplib

    _orig_session_request = _requests.sessions.Session.request
    _orig_smtp = _smtplib.SMTP
    _orig_smtp_ssl = _smtplib.SMTP_SSL

    class _FakeRequestsResp:
        status_code = 200
        text = ""

        def json(self):
            return {}

    def _fake_session_request(self, *args, **kwargs):
        blocked["n"] += 1
        return _FakeRequestsResp()

    class _FakeSMTP:
        def __init__(self, *args, **kwargs):
            blocked["n"] += 1
            raise RuntimeError(
                "[conftest] 测试期间禁止真实 SMTP 发信 (E3 网络焊死)")

    _requests.sessions.Session.request = _fake_session_request
    _smtplib.SMTP = _FakeSMTP
    _smtplib.SMTP_SSL = _FakeSMTP
    try:
        yield
    finally:
        _urlreq.urlopen = _orig
        _requests.sessions.Session.request = _orig_session_request
        _smtplib.SMTP = _orig_smtp
        _smtplib.SMTP_SSL = _orig_smtp_ssl
        if blocked["n"]:
            print(f"\n[conftest] session 焊死拦截网络出口 "
                  f"{blocked['n']} 次 (urlopen/requests/smtplib 合计, 全部未触网)")


class FakeLoop:
    """Mock BacktestLoop for monkeypatch tests. Captures run() args, returns stub equity/trades."""
    def __init__(self, equity=None, trades=None):
        self._equity = equity
        self._trades = trades if trades is not None else np.empty((0, 9))
        self.captured = {}

    def run(self, price_np, entry_np, high_np=None, low_np=None, open_np=None,
            tradable_np=None, last_tradable_idx=None, formula_exit_np=None,
            degraded_np=None, turnover_day_np=None):
        import numpy as np
        self.captured = dict(price_np=price_np, entry_np=entry_np,
                             high_np=high_np, tradable_np=tradable_np,
                             last_tradable_idx=last_tradable_idx)
        eq = self._equity if self._equity is not None else np.full(price_np.shape[0], 100000.0)
        return eq, np.atleast_2d(self._trades)
