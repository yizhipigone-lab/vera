"""30 日滚动窗口首信号过滤 (selection/signal_rules.py, 2026-07-23) 测试。

语义 (用户拍板): 一只票近 30 个交易日内已有过信号 —— 无论那个信号
当时是否成交/是否被保留 —— 本次信号丢弃不买。
"""
from __future__ import annotations

import pandas as pd

from selection.signal_rules import filter_first_signal_in_window


def _sel(pairs, cal):
    """pairs: [(stock, cal_idx), ...] → selections DataFrame"""
    return pd.DataFrame({
        "stock_code": [s for s, _ in pairs],
        "select_date": [cal[i] for _, i in pairs],
        "formula_name": "T",
    })


CAL = pd.bdate_range("2026-01-01", periods=90)  # 工作日历模拟交易日


class TestFirstSignalInWindow:
    def test_basic_window(self):
        """A: 0,10,41,50 → 留 0 (首个), 丢 10 (距0为10), 留 41 (距10为31>30), 丢 50 (距41为9)"""
        df = _sel([("A", 0), ("A", 10), ("A", 41), ("A", 50),
                   ("B", 5), ("B", 36)], CAL)
        out = filter_first_signal_in_window(df, window_td=30, calendar=CAL)
        got = {(r.stock_code, pd.Timestamp(r.select_date)) for r in out.itertuples()}
        assert got == {("A", CAL[0]), ("A", CAL[41]), ("B", CAL[5]), ("B", CAL[36])}

    def test_dropped_signal_still_blocks(self):
        """被丢弃的信号也参与压制: 0,20,45 → 留 0, 丢 20, 45 距 20 为 25 ≤ 30 → 丢
        (若按"上一保留信号"算, 45 距 0 为 45 会被误留)"""
        df = _sel([("A", 0), ("A", 20), ("A", 45)], CAL)
        out = filter_first_signal_in_window(df, window_td=30, calendar=CAL)
        assert list(out["select_date"]) == [CAL[0]]

    def test_real_start_crops_after_rule(self):
        """real_start 之前的信号参与压制但不输出:
        信号 0,10, real_start=cal[5] → cal[10] 被 cal[0] 压制, 输出为空"""
        df = _sel([("A", 0), ("A", 10)], CAL)
        out = filter_first_signal_in_window(df, window_td=30,
                                            real_start="20260101", calendar=CAL)
        # real_start=2026-01-01 是 cal[0] 当天, 先验证不裁剪时 cal[10] 被压
        assert len(out) == 1  # 只剩 cal[0]
        out2 = filter_first_signal_in_window(df, window_td=30,
                                             real_start=str(CAL[5].date()).replace("-", ""),
                                             calendar=CAL)
        assert len(out2) == 0  # cal[0] 被裁掉, cal[10] 仍被压 → 空

    def test_exact_boundary_not_passed(self):
        """距离恰好 = window_td 不放行 (必须 > window_td)"""
        df = _sel([("A", 0), ("A", 30), ("A", 31)], CAL)
        out = filter_first_signal_in_window(df, window_td=30, calendar=CAL)
        assert list(out["select_date"]) == [CAL[0]]

    def test_empty_passthrough(self):
        df = pd.DataFrame(columns=["stock_code", "select_date", "formula_name"])
        out = filter_first_signal_in_window(df, window_td=30, calendar=CAL)
        assert len(out) == 0

    def test_no_calendar_fallback(self):
        """不传日历: 用信号自身日期去重排序 (降级路径, 不抛错)"""
        df = _sel([("A", 0), ("A", 1), ("A", 40)], CAL)
        out = filter_first_signal_in_window(df, window_td=30)
        # 自身日历下 0 与 1 相邻 (距1), 40 距 1 为 2 (也只有3个不同日期)
        assert len(out) == 1  # 只有第一个信号 (窗口 30 > 任何距离)
