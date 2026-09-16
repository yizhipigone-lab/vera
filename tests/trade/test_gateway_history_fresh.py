"""日线新鲜度判定 (2026-09-16): "有数据但陈旧"也必须补下载。

背景: 原实现只在取空时 download_history_data —— 本机 9/16 盘后 513100 末根
仍停在 9/14, 属"有数据但陈旧", 静默污染两处:
  ① 轮动动量: 尾部缺 9/15, 追加实时价后参照点整体前移 ~1 个交易日;
  ② 停机日补算: 缺 9/15 收盘价 → fail-closed 拒写 9/11、9/14 两格。

本文件锁三件事: ①"应有一根日线"的时点语义 (盘后要求当日 / 盘中与非交易日
只要求上一交易日); ②陈旧判定与防抖; ③两个取数口都接上这条判定。
"""
import datetime as dt
import sys
import time
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import trade.gateway as gw_mod
from trade.gateway import RealGateway, _expected_last_bar_day, _last_index_day


def _df(days: list[str], closes: list[float] | None = None) -> pd.DataFrame:
    """构造日线 DataFrame (index=DatetimeIndex, 与 xtdata 返回同形)。

    days 用 'YYYY-MM-DD'; 内部转成 xtdata 的 YYYYMMDD 索引形态。
    """
    idx = pd.to_datetime([d.replace("-", "") for d in days], format="%Y%m%d")
    vals = closes if closes is not None else [10.0] * len(days)
    return pd.DataFrame({"close": vals}, index=idx)


class FakeXtdata:
    """get_market_data_ex 按调用次序回放 (最后一个可重复); 记录补下载调用。"""

    def __init__(self, frames: list[pd.DataFrame]):
        self._frames = list(frames)
        self.downloads: list[tuple] = []

    def get_market_data_ex(self, fields, codes, period, start="", end="",
                           count=-1, dividend_type="none", fill=False):
        frame = self._frames.pop(0) if len(self._frames) > 1 else self._frames[0]
        return {codes[0]: frame}

    def download_history_data(self, code, period, start, end):
        self.downloads.append((code, period, start, end))
        return 0


@pytest.fixture()
def gw(monkeypatch):
    """网关 + 固定"应有一根=20260916"(不受跑测试当天日期影响)。"""
    g = RealGateway(account_id="TEST")
    monkeypatch.setattr(gw_mod, "_expected_last_bar_day",
                        lambda today, now_hm: "20260916")
    return g


# ── 纯函数: "应有一根"时点语义 ──────────────────────────────────────

@pytest.mark.parametrize("day,now_hm,expect", [
    ("2026-09-16", 15 * 60 + 5, "20260916"),    # 交易日 15:05 后 → 要求当日
    ("2026-09-16", 14 * 60 + 54, "20260915"),   # 交易日盘中 → 只要求上一交易日
    ("2026-09-19", 15 * 60 + 30, "20260918"),   # 周六 → 上周五
    ("2026-09-21", 9 * 60, "20260918"),         # 周一盘前 → 上周五
])
def test_expected_last_bar_day(day, now_hm, expect):
    assert _expected_last_bar_day(dt.date.fromisoformat(day), now_hm) == expect


def test_expected_last_bar_day_calendar_broken(monkeypatch):
    """日历炸了 → 返回 '' (判不出就不猜), 不抛。"""
    import scheduler.trading_calendar as cal

    def _boom(_d):
        raise RuntimeError("日历不可用")

    monkeypatch.setattr(cal, "is_trading_day", _boom)
    assert _expected_last_bar_day(dt.date(2026, 9, 16), 16 * 60) == ""


def test_last_index_day():
    assert _last_index_day(_df(["2026-09-14"])) == "20260914"
    assert _last_index_day(None) == ""
    assert _last_index_day(pd.DataFrame()) == ""


# ── 陈旧判定与防抖 ─────────────────────────────────────────────────

def test_fresh_series_needs_no_download(gw):
    need, why = gw._should_download_history("513100.SH", ["20260916"], 1789000000.0)
    assert need is False and "已达应有" in why


def test_stale_series_downloads_then_debounced(gw):
    ts = 1789000000.0
    need, why = gw._should_download_history("513100.SH", ["20260914"], ts)
    assert need is True and "落后于应有" in why

    need2, why2 = gw._should_download_history("513100.SH", ["20260914"], ts + 1.0)
    assert need2 is False and "已补过" in why2

    need3, _ = gw._should_download_history(
        "513100.SH", ["20260914"], ts + gw_mod._REFRESH_MIN_INTERVAL_SEC + 1.0)
    assert need3 is True


def test_empty_series_needs_download(gw):
    need, why = gw._should_download_history("399006.SZ", [], 1789000000.0)
    assert need is True and "空" in why


def test_cap_day_in_past_not_stale(gw):
    """历史区间查询 (右端在过去) 不该被判陈旧 —— 否则每次调用都白下载。"""
    need, _ = gw._should_download_history(
        "513100.SH", ["20260901"], 1789000000.0, cap_day="20260901")
    assert need is False


def test_calendar_unknown_no_download(monkeypatch):
    g = RealGateway(account_id="TEST")
    monkeypatch.setattr(gw_mod, "_expected_last_bar_day", lambda today, hm: "")
    need, why = g._should_download_history("513100.SH", ["20260914"], 1789000000.0)
    assert need is False and "判不出" in why


# ── 两个取数口都接上了这条判定 ──────────────────────────────────────

def test_query_daily_closes_refetches_after_download(gw):
    """陈旧 → 补下载 → 重取 (返回的是重取后的新序列)。"""
    stale = _df(["2026-09-13", "2026-09-14"], [2.190, 2.191])
    fresh = _df(["2026-09-13", "2026-09-14", "2026-09-15", "2026-09-16"],
                [2.190, 2.191, 2.196, 2.218])
    fake = FakeXtdata([stale, fresh])
    gw._xtdata = lambda: fake

    out = gw.query_daily_closes("513100.SH", count=21)
    assert [d[0] for d in fake.downloads] == ["513100.SH"]
    assert fake.downloads[0][1] == "1d"
    assert out[:3] == pytest.approx([2.190, 2.191, 2.196])   # 末尾可能被盘中裁掉


def test_query_daily_closes_fresh_no_download(gw):
    fake = FakeXtdata([_df(["2026-09-15", "2026-09-16"], [2.196, 2.218])])
    gw._xtdata = lambda: fake

    out = gw.query_daily_closes("513100.SH", count=21)
    assert fake.downloads == []
    assert out[0] == pytest.approx(2.196)


def test_query_daily_closes_empty_returns_empty(gw):
    fake = FakeXtdata([pd.DataFrame(), pd.DataFrame()])
    gw._xtdata = lambda: fake

    assert gw.query_daily_closes("399673.SZ", count=21) == []
    assert [d[0] for d in fake.downloads] == ["399673.SZ"]


def test_query_daily_closes_range_refetches_when_stale(gw):
    """停机日补算路径: 陈旧同样补下载 (9/15 收盘价就是这么丢的)。"""
    stale = _df(["2026-09-13", "2026-09-14"], [2.190, 2.191])
    fresh = _df(["2026-09-13", "2026-09-14", "2026-09-15"],
                [2.190, 2.191, 2.196])
    fake = FakeXtdata([stale, fresh])
    gw._xtdata = lambda: fake

    out = gw.query_daily_closes_range("513100.SH", "20260901", "")
    assert [d[0] for d in fake.downloads] == ["513100.SH"]
    assert sorted(out) == ["2026-09-13", "2026-09-14", "2026-09-15"]


def test_intraday_bar_dropped_before_close(gw, monkeypatch):
    """盘前/盘中: 末根是"今天"且 <15:05 → 裁掉 (无未来函数, 老语义不破)。"""
    today = dt.date.today()
    day8 = today.strftime("%Y%m%d")
    monkeypatch.setattr(gw_mod, "_expected_last_bar_day", lambda t, h: day8)
    frame = _df(["2026-09-15", today.strftime("%Y-%m-%d")], [2.196, 2.218])
    fake = FakeXtdata([frame])
    gw._xtdata = lambda: fake

    monkeypatch.setattr(gw_mod.time, "localtime", lambda *a: time.struct_time(
        (today.year, today.month, today.day, 10, 0, 0, 0, 0, -1)))
    assert gw.query_daily_closes("513100.SH", count=21) == pytest.approx([2.196])

    monkeypatch.setattr(gw_mod.time, "localtime", lambda *a: time.struct_time(
        (today.year, today.month, today.day, 16, 0, 0, 0, 0, -1)))
    assert gw.query_daily_closes("513100.SH", count=21) == pytest.approx(
        [2.196, 2.218])


# ── 2026-09-16 审计修复回归 (报告 docs/audit/2026-09-16_QMT日线新鲜度判定_审计报告.md) ──

class FailingXtdata(FakeXtdata):
    """补下载永远抛异常 (模拟 QMT 未连接/接口报错)。"""

    def download_history_data(self, code, period, start, end):
        self.downloads.append((code, period, start, end))
        raise RuntimeError("模拟补下载失败")


def test_still_stale_after_download_is_marked_not_silent(gw):
    """修复①: 补下载后仍陈旧 → 行为不变 (仍返回序列) 但不再静默。"""
    stale = _df(["2026-09-13", "2026-09-14"], [2.190, 2.191])
    gw._xtdata = lambda: FakeXtdata([stale])      # 补下载后拿到的还是同一份

    out = gw.query_daily_closes("513100.SH", count=21)
    assert out == pytest.approx([2.190, 2.191])
    assert gw.history_stale["513100.SH"] == "末根 20260914 < 应有 20260916"


def test_still_stale_range_keeps_past_days(gw):
    """修复① (区间口): 仍返回已有历史价 —— 不因"最新一根不够新"丢掉过去那几天。"""
    stale = _df(["2026-09-13", "2026-09-14"], [2.190, 2.191])
    gw._xtdata = lambda: FakeXtdata([stale])

    out = gw.query_daily_closes_range("513100.SH", "20260901", "")
    assert sorted(out) == ["2026-09-13", "2026-09-14"]
    assert gw.history_stale.get("513100.SH")


def test_history_stale_cleared_when_fresh(gw):
    """标记是"当下状态": 下一次取到新鲜数据要清掉。"""
    stale = _df(["2026-09-14"], [2.190])
    fresh = _df(["2026-09-15", "2026-09-16"], [2.196, 2.218])
    fake = FakeXtdata([stale, stale, fresh])       # 同一实例: 帧按调用次序消耗
    gw._xtdata = lambda: fake

    gw.query_daily_closes("513100.SH", count=21)
    assert gw.history_stale.get("513100.SH")

    gw.query_daily_closes("513100.SH", count=21)   # 这次拿到新鲜数据
    assert "513100.SH" not in gw.history_stale


def test_download_failure_retries_in_60s_not_600s(gw):
    """修复②: 补下载失败 → 短间隔重试, 一次失败不再压住一整天。"""
    gw._xtdata = lambda: FailingXtdata([_df(["2026-09-14"], [2.190])])

    out = gw.query_daily_closes("513100.SH", count=21)
    assert out == pytest.approx([2.190])           # 失败也要给调用方旧数据
    assert "513100.SH" in gw._refresh_failed

    need, why = gw._should_download_history(
        "513100.SH", ["20260914"], time.time() + 1.0)
    assert need is False and "60s 内已补过" in why

    need2, _ = gw._should_download_history(
        "513100.SH", ["20260914"],
        time.time() + gw_mod._REFRESH_FAIL_RETRY_SEC + 1.0)
    assert need2 is True


def test_download_success_keeps_600s_debounce(gw):
    """对照: 补下载成功仍按 600s 防抖 (失败一次别把每个调用都拖成 20s 阻塞)。"""
    gw._xtdata = lambda: FakeXtdata([_df(["2026-09-14"], [2.190])])

    gw.query_daily_closes("513100.SH", count=21)
    assert gw._refresh_failed == set()

    need, why = gw._should_download_history(
        "513100.SH", ["20260914"], time.time() + 61.0)
    assert need is False and "600s 内已补过" in why


def test_untrusted_calendar_drops_today_demand(monkeypatch, caplog):
    """修复③: 精确历不可用 + 内置表未覆盖该年 (2027) → 盘后只要求上一交易日。

    否则 2027 年落在工作日里的法定假日会被粗判成交易日, "应有一根=当日"
    永远校验不过 → 每 600s 空补一次。
    """
    import scheduler.trading_calendar as cal

    monkeypatch.setattr(cal, "_XCAL", None)
    monkeypatch.setattr(cal, "_XCAL_TRIED", True)
    monkeypatch.setattr(gw_mod, "_CAL_NOT_COVERED_WARNED", set())

    with caplog.at_level("WARNING", logger="trade.gateway"):
        assert _expected_last_bar_day(dt.date(2027, 1, 8), 16 * 60) == "20270107"
        assert _expected_last_bar_day(dt.date(2027, 1, 8), 16 * 60) == "20270107"
    assert caplog.text.count("交易日历不可信") == 1     # 每天一条, 不刷屏


def test_covered_calendar_still_demands_today(monkeypatch, caplog):
    """对照: 内置表覆盖的 2026 年 (精确历缺失) 仍按严格判据要求当日。"""
    import scheduler.trading_calendar as cal

    monkeypatch.setattr(cal, "_XCAL", None)
    monkeypatch.setattr(cal, "_XCAL_TRIED", True)
    monkeypatch.setattr(gw_mod, "_CAL_NOT_COVERED_WARNED", set())

    with caplog.at_level("WARNING", logger="trade.gateway"):
        assert _expected_last_bar_day(dt.date(2026, 9, 16), 16 * 60) == "20260916"
    assert "交易日历不可信" not in caplog.text
