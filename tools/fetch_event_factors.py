"""
事件因子历史数据拉取(阶段 A)

按 trade_date 逐日拉三因子(moneyflow / top_inst / block_trade),缓存 parquet。
单日均 <6000(moneyflow 最多 ~5200),无截断风险(审计 H3)。
用交易日历跳过非交易日。

用法:
    python tools/fetch_event_factors.py --start 20260713 --end 20260717   # 1 周验证
    python tools/fetch_event_factors.py --start 20250719 --end 20260718   # 全 1 年

缓存:data/factors/{moneyflow,top_inst,block_trade}_<start>_<end>.parquet
"""
import os
import sys
import time
import argparse
from pathlib import Path

import pandas as pd

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "factors"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def load_token() -> str:
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if token:
        return token
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("TUSHARE_TOKEN"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="开始日期 YYYYMMDD")
    ap.add_argument("--end", required=True, help="结束日期 YYYYMMDD")
    ap.add_argument("--sleep", type=float, default=0.35, help="每次调用间隔(避频控)")
    args = ap.parse_args()

    token = load_token()
    if not token:
        print("[FAIL] 未找到 TUSHARE_TOKEN")
        sys.exit(1)
    import tushare as ts
    pro = ts.pro_api(token)

    # 交易日历(跳非交易日)
    cal = pro.trade_cal(exchange="SSE", start_date=args.start, end_date=args.end, is_open="1")
    dates = sorted(cal["cal_date"].tolist())
    print(f"[INFO] {args.start}~{args.end} 共 {len(dates)} 交易日,sleep={args.sleep}s")

    all_mf, all_ti, all_bt = [], [], []
    fail_days = []
    for i, d in enumerate(dates):
        try:
            all_mf.append(pro.moneyflow(trade_date=d))
            time.sleep(args.sleep)
            all_ti.append(pro.top_inst(trade_date=d))
            time.sleep(args.sleep)
            all_bt.append(pro.block_trade(trade_date=d))
            time.sleep(args.sleep)
        except Exception as e:
            print(f"  [WARN] {d} 失败: {str(e)[:70]}")
            fail_days.append(d)
        if (i + 1) % 10 == 0 or i == len(dates) - 1:
            print(f"  进度 {i + 1}/{len(dates)}")

    mf_df = pd.concat([x for x in all_mf if len(x) > 0], ignore_index=True) if all_mf else pd.DataFrame()
    ti_df = pd.concat([x for x in all_ti if len(x) > 0], ignore_index=True) if all_ti else pd.DataFrame()
    bt_df = pd.concat([x for x in all_bt if len(x) > 0], ignore_index=True) if all_bt else pd.DataFrame()

    tag = f"{args.start}_{args.end}"
    mf_df.to_parquet(CACHE_DIR / f"moneyflow_{tag}.parquet")
    ti_df.to_parquet(CACHE_DIR / f"top_inst_{tag}.parquet")
    bt_df.to_parquet(CACHE_DIR / f"block_trade_{tag}.parquet")

    print(f"\n[OK] moneyflow {len(mf_df)} 条 | top_inst {len(ti_df)} 条 | block_trade {len(bt_df)} 条")
    print(f"[OK] 失败 {len(fail_days)} 天: {fail_days[:5]}{'...' if len(fail_days)>5 else ''}")
    print(f"[OK] 缓存到 {CACHE_DIR}")


if __name__ == "__main__":
    main()
