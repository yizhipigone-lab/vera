"""幸存者偏差验证: list_type=5 (全A) 是否包含 2019-2026 退市的股票。

探针: 一批 2019 年前上市、在 2019-2026 期间退市的 A 股代码。
若它们不在 get_stock_list("5") 返回的池里 → 当前全A = 当前存活股快照 → 幸存者偏差成立。

退出码: 0=成功跑完 (无论结论如何), 2=TDX 连不上。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 2019 年前上市、2019-2026 期间退市的 A 股探针 (代码.SH/.SZ)
DELISTED_PROBES = [
    "300104.SZ",  # 乐视网   2019-05 退市
    "002450.SZ",  # 康得新   2021-05 退市
    "002143.SZ",  # 印纪传媒 2019-10 退市
    "600074.SH",  # 保千里   2019-01 退市
    "600286.SH",  # 中丝集团/ST信威 2021-03 退市
    "300156.SZ",  # 神雾环保 2020-05 退市
    "600175.SH",  # 美都能源 2021-05 退市
    "000691.SZ",  # 亚太实业 2021-03 退市
    "002604.SZ",  # 龙力生物 2021-01 退市
    "000981.SZ",  # 银亿股份 2021-03 退市
    "600146.SH",  # 商赢环球 2021-03 退市
    "002220.SZ",  # 富控互动 2020-09 退市
    "601558.SH",  # 华锐风电 2022-08 退市
    "600722.SH",  # 东方金钰 2021-03 退市
    "600656.SH",  # *ST博元  2016 退市 (更早, 参照)
    "000033.SZ",  # ST新都   2017 退市 (更早, 参照)
]

try:
    from core.connector import TdxConnector
    TdxConnector.initialize()
    if not TdxConnector.is_ready():
        print("TDX 未就绪, 请确认通达信客户端已启动并登录", flush=True)
        sys.exit(2)
    from tqcenter import tq

    # 1. 当前全 A 池规模
    raw_5 = tq.get_stock_list("5", list_type=1)
    pool_5 = []
    for s in raw_5:
        if isinstance(s, dict):
            pool_5.append(s.get("Code", ""))
        elif isinstance(s, str):
            pool_5.append(s)
    pool_5 = set(c for c in pool_5 if c)
    print(f"\n[list_type=5 全A] 当前规模: {len(pool_5)} 只", flush=True)

    # 2. 探针: 退市股是否在池里
    print(f"\n[退市探针] 共 {len(DELISTED_PROBES)} 只 2019-2026 退市股:", flush=True)
    in_pool, not_in_pool = [], []
    for code in DELISTED_PROBES:
        if code in pool_5:
            in_pool.append(code)
            print(f"  [在池]   {code}", flush=True)
        else:
            not_in_pool.append(code)
            print(f"  [不在池] {code}", flush=True)

    print(f"\n汇总: 在池 {len(in_pool)} / 不在池 {len(not_in_pool)}", flush=True)

    # 3. 同时拉 IsQuitGP 标记直接验证 TDX 对这些代码的认知
    print(f"\n[TDX IsQuitGP 标记验证] 直接问 TDX 这些代码是不是退市股:", flush=True)
    quit_cnt = 0
    for code in DELISTED_PROBES:
        try:
            info = tq.get_stock_info(code)
            is_quit = str(info.get("IsQuitGP", "0")) == "1" if isinstance(info, dict) else "?"
            name = info.get("Name", "?") if isinstance(info, dict) else "?"
            if is_quit:
                quit_cnt += 1
            print(f"  {code}  IsQuitGP={is_quit}  Name={name}", flush=True)
        except Exception as e:
            print(f"  {code}  get_stock_info 异常: {e}", flush=True)
    print(f"IsQuitGP=1 的有 {quit_cnt} 只", flush=True)

    # 4. 试着拉一只退市股 2019 的 K 线, 看 TDX 是否还存历史数据 (用于判断退市股能否被回测)
    print(f"\n[退市股历史 K 线可用性] 拉 300104.SZ 乐视网 2019 上半年日线:", flush=True)
    try:
        kd = tq.get_market_data(
            field_list=[], stock_list=["300104.SZ"],
            start_time="20190101", end_time="20190601",
            period="1d", dividend_type="front", count=-1,
        )
        if kd and "Close" in kd:
            closes = kd["Close"]
            if hasattr(closes, "columns") and "300104.SZ" in closes.columns:
                n = closes["300104.SZ"].notna().sum()
                print(f"  乐视网 2019H1 有 {n} 个有效收盘日 (退市股历史K线 {'可用' if n > 0 else '不可用'})", flush=True)
            else:
                print(f"  乐视网返回列: {list(getattr(closes,'columns',[]))[:5]}", flush=True)
        else:
            print(f"  乐视网 K线返回空或无 Close: {list(kd.keys()) if kd else 'None'}", flush=True)
    except Exception as e:
        print(f"  拉乐视网K线异常: {e}", flush=True)

    TdxConnector.close()
    print("\n=== 验证完成 ===", flush=True)

except SystemExit:
    raise
except Exception as e:
    import traceback
    traceback.print_exc()
    sys.exit(3)
