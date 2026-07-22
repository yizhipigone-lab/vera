"""缓存复权一致性扫描 (2026-07-22, F4 专项)。

逐股比对 data/kline_cache 的 5m 缓存 (日聚合 close) 与 1d 缓存 close,
|平均比率 - 1| > 0.3% 记为漂移。输出 CSV 清单 + 汇总。

根因 (2026-07-22 实证):
- TDX K线"前复权"只应用落在请求窗口内的除权事件 (窗口锚定语义);
- KlineCache 逐股独立增量刷新, 每股锚定在其各自最后写入的窗口末端;
- TDX 5m 数据本身滞后 (实测最新只到 2026-07-17, 比 1d 晚 2 个交易日),
  其后的除权事件进不了 5m 复权 — 这是 1d/5m 漂移的主要来源。
实测 (2026-07-22): 5517 只中 231 只漂移 (4.2%), 幅度 -3.1% ~ +48% (送转类)。

修复流程: ① 通达信终端在线同步 (或终端内数据下载) 把 5m 数据更新到最近交易日 →
② --probe 验证 → ③ 删除本清单股票的 parquet (两周期) 或 force_invalidate,
下次读取按当前口径全量重建 → ④ 复跑本扫描确认清零。
⚠ 5m 数据未同步前不要重建 — 新拉的仍是滞后口径。

用法:
  python tools/scan_adjust_drift.py            # 全量扫描
  python tools/scan_adjust_drift.py --probe 000090.SZ  # 单股数据源体检
"""
import argparse
import glob
import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CACHE_5M = "data/kline_cache/5m"
CACHE_1D = "data/kline_cache/1d"
OUT_CSV = "output/f4_adjust_drift_scan.csv"
THRESH = 0.003


def scan() -> pd.DataFrame:
    files = sorted(glob.glob(f"{CACHE_5M}/*.parquet"))
    rows = []
    for i, f in enumerate(files):
        code = os.path.basename(f).replace(".parquet", "")
        try:
            d5 = pd.read_parquet(f, columns=["date", "close"])
            d1 = pd.read_parquet(f"{CACHE_1D}/{code}.parquet", columns=["date", "close"])
            d5["date"] = pd.to_datetime(d5["date"])
            d1["date"] = pd.to_datetime(d1["date"])
            c5 = d5.set_index("date")["close"].resample("D").last().dropna()
            c1 = d1.set_index("date")["close"]
            m = pd.DataFrame({"a": c5, "b": c1}).dropna()
            if len(m) < 20:
                continue
            r = m["a"] / m["b"]
            rows.append((code, float(r.mean()), float((abs(r - 1) > THRESH).mean())))
        except Exception:
            continue
        if (i + 1) % 1000 == 0:
            print(f"progress {i + 1}/{len(files)}", flush=True)
    return pd.DataFrame(rows, columns=["code", "ratio", "dev_pct"])


def probe(code: str) -> None:
    """单股数据源体检 (2026-07-22 修正版):
    ① TDX 权息事件列表是否完整 (get_divid_factors);
    ② TDX 5m 数据最新到哪天 (5m 滞后会导致其后的除权事件进不了 5m 复权);
    ③ 长窗口 front 与缓存 5m/1d 是否一致 (锚定漂移是否已自愈)。
    TDX K线"前复权"只应用落在请求窗口内的除权事件 — 验证必须用长窗口。
    """
    from core.data_fetcher import DataFetcher
    from core.connector import TdxConnector
    TdxConnector.ensure_connected()
    tq = TdxConnector.tq()

    # ① 权息事件
    try:
        fac = tq.get_divid_factors(code)
        print(f"① 权息事件 {len(fac)} 条", end="")
        print(f", 最新 {fac.index.max().date()}" if len(fac) else " (空!)")
    except Exception as e:
        print(f"① 权息事件获取失败: {e}")

    # ② 5m 数据新鲜度
    k5 = DataFetcher.get_kline([code], "", "", period="5m",
                               dividend_type="front", fill_data=False, count=48)
    c5 = k5["Close"][code].dropna()
    c5.index = pd.to_datetime(c5.index)
    print(f"② 5m 最新 bar: {c5.index.max()} (今日之前 2 个交易日以上 → 5m 滞后, 先同步数据再重建缓存)")

    # ③ 长窗口 front 与缓存对比
    d1 = DataFetcher.get_kline_single(code, "20240601", "", period="1d", dividend_type="front")
    d1 = d1.set_index(pd.to_datetime(d1.index))
    c5d = c5.resample("D").last()
    m = pd.DataFrame({"5m": c5d, "1d_tdx": d1["close"]}).dropna()
    if len(m) > 20:
        r = (m["5m"] / m["1d_tdx"]).mean()
        print(f"③ TDX 直拉长窗口 5m日聚合/1d 平均比率 = {r:.5f} "
              f"({'一致' if abs(r - 1) <= 0.003 else '仍有漂移, 缓存需重建'})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", metavar="CODE", help="验证单股 TDX 权息 (如 000090.SZ)")
    args = ap.parse_args()
    if args.probe:
        probe(args.probe)
        return
    df = scan()
    Path("output").mkdir(exist_ok=True)
    df.to_csv(OUT_CSV, index=False)
    drift = df[abs(df["ratio"] - 1) > THRESH]
    print(f"scanned: {len(df)}, drifted: {len(drift)} ({len(drift) / max(len(df), 1) * 100:.1f}%)")
    if len(drift):
        print(f"magnitude: mean {drift['ratio'].mean():.4f} "
              f"min {drift['ratio'].min():.4f} max {drift['ratio'].max():.4f}")
        print(f"清单: {OUT_CSV}")
        print("重建 (5m 数据同步到最近交易日后): 删除清单股票 "
              "data/kline_cache/{5m,1d}/<code>.parquet, 或 KlineCache.force_invalidate")


if __name__ == "__main__":
    main()
