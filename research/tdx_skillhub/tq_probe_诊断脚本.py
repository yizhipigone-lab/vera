# -*- coding: utf-8 -*-
"""TQ 数据面诊断 v3: 收尾三个疑点
1. trackzs CSI 大指数失败是否偶发 (重试 + 上证50/创业板指对照)
2. kzz 空 stock_code 能否全量拉
3. bkjy 板块代码带 .SH 后缀重试
4. get_financial_data FN1 快测 (专业财务包是否已下载)
"""
import sys, json

sys.path.insert(0, r'E:/NEW_TDX/PYPlugins/user')
from tqcenter import tq

tq.initialize(__file__)

def probe(name, fn, max_len=280):
    try:
        r = fn()
        s = json.dumps(r, ensure_ascii=False, default=str)
        n = len(r) if isinstance(r, (list, dict)) else '-'
        print(f"[{name}] {'OK' if r else 'EMPTY'} (n={n}) -> {s[:max_len]}")
        return r
    except Exception as e:
        print(f"[{name}] FAIL: {type(e).__name__} {str(e)[:160]}")
        return None

try:
    print("--- 1. trackzs 重试与对照 ---")
    probe("trackzs 000300.CSI 重试1", lambda: tq.get_trackzs_etf_info(zs_code='000300.CSI'), 150)
    probe("trackzs 000300.CSI 重试2", lambda: tq.get_trackzs_etf_info(zs_code='000300.CSI'), 150)
    probe("trackzs 000016.SH 上证50", lambda: tq.get_trackzs_etf_info(zs_code='000016.SH'), 150)
    probe("trackzs 399006.SZ 创业板指", lambda: tq.get_trackzs_etf_info(zs_code='399006.SZ'), 150)

    print("--- 2. kzz 全量 ---")
    probe("kzz 空(全量)", lambda: tq.get_kzz_info(), 300)
    kz = None
    try:
        kz = tq.get_kzz_info()
    except Exception:
        pass
    if isinstance(kz, list) and kz:
        print(f"    可转债总数={len(kz)}; 首条: {json.dumps(kz[0], ensure_ascii=False)[:250]}")

    print("--- 3. bkjy 板块带后缀 ---")
    probe("bkjy 880976.SH", lambda: tq.get_bkjy_value(stock_list=['880976.SH'], field_list=['BK5','BK6'], start_time='20260801', end_time='20260822'), 280)

    print("--- 4. 专业财务 FN ---")
    probe("fin FN1,FN4 600519", lambda: tq.get_financial_data(stock_list=['600519.SH'], field_list=['FN1','FN4'], start_time='20250101', end_time='20260822'), 300)
finally:
    try:
        tq.close()
    except Exception:
        pass
print("DONE")
