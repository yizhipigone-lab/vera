"""check_index_sources.py — 诊断 399673.SZ 日线的三个数据源连通性。

用途: 评估 ETF 轮动信号的降级链 (QMT → TDX → 腾讯 → 东财)。
用法: python -X utf8 tools/check_index_sources.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CODE = "399673.SZ"     # 项目内代码格式 (带交易所后缀)
SZ_SYM = "sz399673"   # akshare 格式 (小写交易所前缀 + 6 位代码)


def _test_tdx():
    """TDX (通达信): 项目自有 DataFetcher 直连。"""
    try:
        import datetime as _dt
        from core.data_fetcher import DataFetcher
        end = _dt.date.today().strftime("%Y%m%d")
        kl = DataFetcher.get_kline(
            [CODE], "20240101", end, period="1d",
            dividend_type="front", use_cache=True)
        close = (kl or {}).get("Close")
        if close is not None and CODE in close.columns:
            s = close[CODE].dropna()
            if not s.empty:
                return True, len(s), float(s.iloc[-1]), str(s.index[-1])
        return False, 0, None, "0 根"
    except Exception as e:
        return False, 0, None, repr(e)


def _test_tencent():
    """腾讯: akshare stock_zh_index_daily_tx。"""
    try:
        import akshare as ak
        df = ak.stock_zh_index_daily_tx(symbol=SZ_SYM)
        if df is not None and not df.empty:
            last = df.iloc[-1]
            return True, len(df), float(last["close"]), str(last["date"])
        return False, 0, None, "空 DataFrame"
    except Exception as e:
        return False, 0, None, repr(e)


def _test_eastmoney():
    """东财 (东方财富): akshare stock_zh_index_daily_em。"""
    try:
        import akshare as ak
        df = ak.stock_zh_index_daily_em(symbol=SZ_SYM)
        if df is not None and not df.empty:
            last = df.iloc[-1]
            return True, len(df), float(last["close"]), str(last["date"])
        return False, 0, None, "空 DataFrame"
    except Exception as e:
        return False, 0, None, repr(e)


def main() -> None:
    print(f"=== {CODE} 日线数据源连通性测试 ===\n")
    for name, fn in (("TDX(通达信)", _test_tdx),
                     ("腾讯", _test_tencent),
                     ("东财", _test_eastmoney)):
        try:
            ok, n, close, extra = fn()
        except Exception as e:  # 防御: 单个源挂不影响其他
            ok, n, close, extra = False, 0, None, repr(e)
        if ok:
            print(f"[OK]   {name}: {n} 根, 末根收盘 {close}, 日期 {extra}")
        else:
            print(f"[FAIL] {name}: {extra or '无数据'}")
    print("\n=== 结论 ===")
    print("哪个 [OK] 就说明哪个源可用, 可纳入轮动信号降级链。")
    print("注: 东财历史接口在 2026-08-12/13 多次实测限连 (被远端断开), 本次再验。")



if __name__ == "__main__":
    main()
