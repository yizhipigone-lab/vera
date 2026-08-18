"""trade/daily_report.py 纯函数单测 (2026-08-19 深模块治理)。"""
from trade.daily_report import build_trade_summary, diff_positions, enrich_sell_quotes


def test_build_trade_summary_win_rate_and_sort():
    """sell_count=0 → win_rate=None (不除零); 有卖出按 ts 升序 + 胜率。"""
    s0 = build_trade_summary(
        [{"code": "A", "direction": 23, "amount": 1000.0, "pnl_amount": 0.0}], [])
    assert s0["sell_count"] == 0 and s0["win_rate"] is None
    assert s0["buy_count"] == 1 and s0["turnover"] == 1000.0
    assert s0["trade_details"][0]["direction"] == 23
    assert s0["trade_details"][0]["pnl_amount"] is None

    s1 = build_trade_summary([
        {"code": "B", "direction": 24, "amount": 100.0,
         "pnl_amount": 50.0, "pnl_pct": 5.0, "ts": 2.0},
        {"code": "C", "direction": 24, "amount": 100.0,
         "pnl_amount": -30.0, "pnl_pct": -3.0, "ts": 1.0},
    ], [])
    assert s1["sell_count"] == 2 and s1["win_rate"] == 0.5
    assert s1["realized_pnl"] == 20.0
    assert s1["sell_details"][0]["code"] == "C"


def test_build_trade_summary_trade_details_cap():
    """>12 笔 trade_details: 前 12 + trade_details_folded。"""
    td = [{"code": f"C{i}", "direction": 24, "amount": 100.0,
           "price": 10.0, "qty": 10, "pnl_amount": float(i),
           "pnl_pct": float(i), "reason": "", "ts": float(i)}
          for i in range(14)]
    s = build_trade_summary(td, [])
    assert len(s["trade_details"]) == 12
    assert s["trade_details_folded"]["count"] == 2
    assert s["trade_details_folded"]["sum_sell_pnl"] == 25.0


def test_build_trade_summary_with_remaining():
    """重放全历史算剩余/卖出比例 (卖出比例=本次卖出÷累计买入)。"""
    s = build_trade_summary(
        [{"traded_id": "T2", "code": "X", "direction": 24, "price": 11.0,
          "qty": 300, "amount": 3300.0, "pnl_amount": 300.0,
          "pnl_pct": 10.0, "reason": "移动止盈", "ts": 2.0}],
        [{"traded_id": "T1", "order_id": "O1", "code": "X", "direction": 23,
          "price": 10.0, "qty": 1000, "ts": 1.0},
         {"traded_id": "T2", "order_id": "O2", "code": "X", "direction": 24,
          "price": 11.0, "qty": 300, "ts": 2.0}],
    )
    d = s["trade_details"][0]
    assert d["remaining_vol"] == 700
    assert d["remaining_value"] == 7700.0
    assert d["sell_ratio"] == 0.3


def test_enrich_sell_quotes_adds_selloff_pcts():
    """卖单补「盘中最高涨幅 / 卖出时点涨幅」; 缺 quotes 或昨收 0 跳过。"""
    details = [{"code": "600000.SH", "direction": 24, "price": 11.0}]
    enrich_sell_quotes(details, {
        "600000.SH": {"prev_close": 10.0, "high": 10.9},
    })
    assert details[0]["intraday_high_pct"] == 9.0
    assert details[0]["sell_pct_vs_prev"] == 10.0
    details2 = [{"code": "600000.SH", "direction": 24, "price": 11.0}]
    enrich_sell_quotes(details2, {"600000.SH": {"prev_close": 0.0, "high": 0.0}})
    assert "intraday_high_pct" not in details2[0]
    enrich_sell_quotes(details, {})


def test_diff_positions_classifies_changes():
    """昨仓 vs 今仓 → 新进/清仓/加仓/减仓。"""
    prev = {"600000.SH": {"volume": 500}, "600001.SH": {"volume": 300}}
    curr = {"600000.SH": type("P", (), {"volume": 800})(),
            "600002.SH": type("P", (), {"volume": 200})()}
    ch = diff_positions(prev, curr)
    assert {"code": "600002.SH", "delta": 200} in ch["new"]
    assert {"code": "600001.SH", "delta": -300} in ch["closed"]
    assert {"code": "600000.SH", "delta": 300} in ch["added"]
    assert ch["reduced"] == []
