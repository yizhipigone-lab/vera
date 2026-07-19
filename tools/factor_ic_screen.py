# -*- coding: utf-8 -*-
"""因子 IC 筛选流水线 — 以信号池为底座,批量评估因子与未来收益的相关性,出排行榜。

思路(用户拍板 2026-07-19): QUANTQQ 公式有效性已被 5m 优化报告证明,
接下来的问题 = "哪些因子与信号后续收益相关性大"。每个因子 = 一个注册函数,
日频截面 Rank IC(Spearman) + ICIR + 五分位多空价差,加因子只需加一行注册。

目标变量: T 收盘买入口径下 close[T+N]/close[T]-1 (N=5/10/20), trailing-only。
评估口径(预注册):
    - 日频截面 Rank IC 均值 / IC 标准差 / ICIR(均值/标准差) / IC>0 占比
    - 五分位价差(Q5-Q1 均值) — 单调性检查
    - 值得关注的门槛: |IC 均值| ≥ 0.03 且 |ICIR| ≥ 0.3 (宽松, 单因子初筛)

用法:
    python tools/factor_ic_screen.py --formula QUANTQQ --tag 20250719_20260718
    python tools/factor_ic_screen.py --formula QUANTQQ --tag 20230719_20260718 --skip-event-factors
"""
import sys
import os
import argparse
import json
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "output" / "gs_filter"

HORIZONS = (5, 10, 20)
IC_MIN, ICIR_MIN = 0.03, 0.3   # 初筛关注门槛(预注册)


# ═══════════════════════════════════════════════════════════════
# 面板因子(纯函数, 输入 close/volume/amount 宽表 → 因子宽表, trailing-only)
# ═══════════════════════════════════════════════════════════════

def f_mom5(p):  return p["close"].pct_change(5)
def f_mom20(p): return p["close"].pct_change(20)
def f_mom60(p): return p["close"].pct_change(60)

def f_vol20(p): return p["close"].pct_change().rolling(20).std()

def f_volr5_20(p):
    v = p["volume"]
    return v.rolling(5).mean() / v.rolling(20).mean()

def f_dist_ma20(p):
    c = p["close"]
    return c / c.rolling(20).mean() - 1

def f_dist_high20(p):
    c = p["close"]
    return c / c.rolling(20).max() - 1

def f_amt20(p):  # 流动性代理: 20 日平均成交额
    return p["amount"].rolling(20).mean()

def f_turnover5(p):  # 5 日平均成交额 / 60 日平均成交额(活跃度)
    a = p["amount"]
    return a.rolling(5).mean() / a.rolling(60).mean()


# ── 技术指标族(2026-07-19 扩充: 主流因子筛) ─────────────────

def f_ret1(p):  # 单日反转
    return p["close"].pct_change()

def f_maxret20(p):  # MAX 彩票因子: 20 日内最大单日涨幅
    return p["close"].pct_change().rolling(20).max()

def f_rsi14(p):
    d = p["close"].diff()
    up = d.clip(lower=0).rolling(14).mean()
    dn = (-d.clip(upper=0)).rolling(14).mean()
    return up / (up + dn) * 100

def f_macd_hist(p):
    c = p["close"]
    dif = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    dea = dif.ewm(span=9, adjust=False).mean()
    return dif - dea

def f_boll_pct(p):  # 布林位置: (close - 下轨)/(带宽), 20日±2σ
    c = p["close"]
    m = c.rolling(20).mean()
    s = c.rolling(20).std()
    return (c - (m - 2 * s)) / (4 * s)

def f_kdj_k(p):  # KDJ 的 K 值: RSV(9) 的 3 日递推均值(SMA(X,3,1) ≡ ewm alpha=1/3)
    lo = p["low"].rolling(9).min()
    hi = p["high"].rolling(9).max()
    rsv = (p["close"] - lo) / (hi - lo) * 100
    return rsv.ewm(alpha=1 / 3, adjust=False).mean()


# ── 第二梯队(kline 现成, 2026-07-19 扩充: 覆盖缺族) ─────────

def f_dist_high250(p):  # 距 52 周高点
    c = p["close"]
    return c / c.rolling(250).max() - 1

def f_price(p):  # 价格水平(低价股效应)
    return p["close"]

def f_amihud20(p):  # Amihud 非流动性: |ret|/成交额 的 20 日均值
    r = p["close"].pct_change().abs()
    return (r / p["amount"]).rolling(20).mean()

def f_ret_skew20(p):  # 收益偏度(彩票偏好反向)
    return p["close"].pct_change().rolling(20).skew()

def f_volvol20(p):  # 成交量波动(换手率波动代理)
    return p["volume"].pct_change().rolling(20).std()

def f_pv_corr20(p):  # 量价相关性(背离)
    return p["close"].rolling(20).corr(p["volume"], pairwise=False)

def f_overnight20(p):  # 隔夜收益 20 日均(昨收→今开)
    return (p["open"] / p["close"].shift(1) - 1).rolling(20).mean()

def f_intraday20(p):  # 日内收益 20 日均(今开→今收)
    return (p["close"] / p["open"] - 1).rolling(20).mean()


# 指数关联(需 ctx["index_ret"]: 沪深300 日收益, 已 reindex 到面板日期)
def _idx_cov_stats(ret: pd.DataFrame, y: pd.Series, win: int):
    """rolling cov / var 的向量化解(总体矩, 防混用 ddof)。"""
    m_x = ret.rolling(win).mean()
    m_y = y.rolling(win).mean()
    cov = ret.mul(y, axis=0).rolling(win).mean() - m_x.mul(m_y, axis=0)
    v_x = ret.pow(2).rolling(win).mean() - m_x.pow(2)
    v_y = y.pow(2).rolling(win).mean() - m_y.pow(2)
    return cov, v_x.clip(lower=0), v_y.clip(lower=0)

def f_corr_index20(p, ctx):  # 个股与沪深300 的 20 日相关性
    ret = p["close"].pct_change()
    cov, v_x, v_y = _idx_cov_stats(ret, ctx["index_ret"], 20)
    return cov.div(np.sqrt(v_y), axis=0).div(np.sqrt(v_x))

def f_beta60(p, ctx):  # 对沪深300 的 60 日贝塔
    ret = p["close"].pct_change()
    cov, _, v_y = _idx_cov_stats(ret, ctx["index_ret"], 60)
    return cov.div(v_y, axis=0)


# 板块热度(需要 sector_map, ctx 传入)
def _sector_panel(p, ctx, win):
    """板块等权 win 日收益 → 截面排名分位 → 映射回个股宽表。"""
    sec_map = ctx["sector_map"]          # stock → sector_code
    ret = p["close"].pct_change()
    # 个股 → 板块聚合(等权均值)
    sec_ret = ret.T.groupby(pd.Series(sec_map).reindex(ret.columns)).mean().T
    heat = np.expm1(np.log1p(sec_ret.clip(lower=-0.999999)).rolling(win).sum())
    rank = heat.rank(axis=1, pct=True)   # 当日 128 板块横截面分位 [0,1]
    # 映射回个股: 每只股票取其所属板块的分位
    stock2sec = pd.Series(sec_map).reindex(p["close"].columns)
    out = pd.DataFrame(np.nan, index=rank.index, columns=p["close"].columns)
    for sec in rank.columns:
        members = stock2sec[stock2sec == sec].index
        if len(members):
            out[members] = np.repeat(rank[[sec]].values, len(members), axis=1)
    return out

def f_sector_heat5(p, ctx):  return _sector_panel(p, ctx, 5)
def f_sector_heat20(p, ctx): return _sector_panel(p, ctx, 20)


# 因子注册表: (名称, 函数, 是否需要ctx, 族) — 加新因子 = 这里加一行(族随行, S3 选臂依赖)
# 族别: 过热反转 / 波动 / 流动性 / 规模 / 价值 / 板块 / 日内结构 / 指数关联 / 情绪 / 事件 / 其他
PANEL_FACTORS = [
    ("mom5", f_mom5, False, "过热反转"),
    ("mom20", f_mom20, False, "过热反转"),
    ("mom60", f_mom60, False, "过热反转"),
    ("vol20", f_vol20, False, "波动"),
    ("volr5_20", f_volr5_20, False, "过热反转"),
    ("dist_ma20", f_dist_ma20, False, "过热反转"),
    ("dist_high20", f_dist_high20, False, "过热反转"),
    ("amt20", f_amt20, False, "流动性"),
    ("turnover5", f_turnover5, False, "过热反转"),
    ("sector_heat5", f_sector_heat5, True, "板块"),
    ("sector_heat20", f_sector_heat20, True, "板块"),
    # 技术指标族(2026-07-19 扩充)
    ("ret1", f_ret1, False, "过热反转"),
    ("maxret20", f_maxret20, False, "过热反转"),
    ("rsi14", f_rsi14, False, "过热反转"),
    ("macd_hist", f_macd_hist, False, "过热反转"),
    ("boll_pct", f_boll_pct, False, "过热反转"),
    ("kdj_k", f_kdj_k, False, "过热反转"),
    # 第二梯队(2026-07-19 扩充)
    ("dist_high250", f_dist_high250, False, "过热反转"),
    ("price", f_price, False, "其他"),
    ("amihud20", f_amihud20, False, "流动性"),
    ("ret_skew20", f_ret_skew20, False, "其他"),
    ("volvol20", f_volvol20, False, "波动"),
    ("pv_corr20", f_pv_corr20, False, "过热反转"),
    ("overnight20", f_overnight20, False, "日内结构"),
    ("intraday20", f_intraday20, False, "日内结构"),
    ("corr_index20", f_corr_index20, True, "指数关联"),
    ("beta60", f_beta60, True, "指数关联"),
]

# 行级因子族归属(与注册表同一文件, 单一数据源)
ROW_FACTOR_FAMILY = {
    # daily_basic
    "turnover_rate": "过热反转", "volume_ratio": "过热反转",
    "pe_ttm": "价值", "pb": "价值", "ps_ttm": "价值", "dv_ratio": "价值",
    "total_mv": "规模", "circ_mv": "规模",
    # 情绪
    "rzye_ratio": "情绪", "hk_ratio": "情绪",
    # 事件(三因子评分)
    "mf_score": "事件", "dragon_score": "事件", "block_score": "事件", "total_score": "事件",
}

# 因子 → 族 总表(S3 选臂用)
FAMILY_OF = {name: fam for name, _, _, fam in PANEL_FACTORS}
FAMILY_OF.update(ROW_FACTOR_FAMILY)

# daily_basic 行级因子(tushare, T 日收盘快照 = T 收盘决策点已知, 与 T 收盘买入铁律一致)
DAILY_BASIC_FACTORS = ["turnover_rate", "volume_ratio", "pe_ttm", "pb",
                       "ps_ttm", "dv_ratio", "total_mv", "circ_mv"]


# ═══════════════════════════════════════════════════════════════
# 数据装载(kline_cache → 面板; TDX → 板块映射; 均带 parquet 缓存)
# ═══════════════════════════════════════════════════════════════

def load_panels(start: str, warmup_days: int, tag: str) -> dict:
    """读全市场 1d, 返回 {'close','high','low','volume','amount'} 宽表(date × code)。"""
    CACHE.mkdir(parents=True, exist_ok=True)
    cache = CACHE / f"_panels_v3_{tag}.parquet"
    keys = ("close", "high", "low", "open", "volume", "amount")
    if cache.exists():
        df = pd.read_parquet(cache)
        return {k: df[k].unstack() for k in keys}
    need_start = pd.Timestamp(start) - pd.Timedelta(days=warmup_days)
    kdir = ROOT / "data" / "kline_cache" / "1d"
    files = sorted(kdir.glob("*.parquet"))
    print(f"[INFO] 读全市场日线 {len(files)} 只(一次性, 约2-3分钟)...")
    frames = []
    t0 = time.time()
    for i, f in enumerate(files):
        try:
            d = pd.read_parquet(f, columns=["date", "close", "high", "low", "open", "volume", "amount"])
        except Exception:
            continue
        d = d[d["date"] >= need_start]
        if len(d):
            d["code"] = f.stem
            frames.append(d)
        if (i + 1) % 1500 == 0:
            print(f"  ... {i + 1}/{len(files)} ({time.time() - t0:.0f}s)")
    alldf = pd.concat(frames, ignore_index=True)
    alldf["date"] = pd.to_datetime(alldf["date"])
    panels = {k: alldf.pivot_table(index="date", columns="code", values=k).sort_index()
              for k in keys}
    # 缓存(stack 成长表省空间; pandas 3.x stack 默认新行为)
    stacked = pd.concat({k: v.stack() for k, v in panels.items()}, axis=1)
    stacked.columns = list(keys)
    stacked.to_parquet(cache)
    print(f"[INFO] 面板: {panels['close'].shape}, 已缓存 {cache}")
    return panels


def load_sector_map(panels_cols) -> dict:
    """TDX 128 细分行业 → stock→sector 映射, 带 json 缓存。"""
    cache = CACHE / "_sector_map.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    from core.connector import TdxConnector
    from core.data_fetcher import DataFetcher
    print("[INFO] 拉 128 板块成份股(TDX, 一次性)...")
    TdxConnector.initialize()
    try:
        sectors = DataFetcher.get_sector_list()
        mapping = {}
        for s in sectors:
            try:
                for code in DataFetcher.get_sector_stocks(s["code"]):
                    mapping.setdefault(code, s["code"])  # 多板块取第一个
            except Exception:
                continue
    finally:
        TdxConnector.close()
    cache.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    print(f"[INFO] 板块映射 {len(mapping)} 只 → 128 板块, 已缓存")
    return mapping


def load_daily_basic(dates: pd.DatetimeIndex, tag: str) -> pd.DataFrame:
    """tushare daily_basic 按日全市场拉取, 断点续拉缓存到 data/factors/daily_basic_{tag}.parquet。

    口径: T 日快照(换手率/量比/PE/PB/PS/市值)在 T 收盘决策点已知, 与 T 收盘买入铁律一致。
    """
    cache = ROOT / "data" / "factors" / f"daily_basic_{tag}.parquet"
    got = pd.DataFrame()
    if cache.exists():
        got = pd.read_parquet(cache)
    have = set(got["trade_date"]) if len(got) else set()
    want = [d.strftime("%Y%m%d") for d in dates]
    missing = [d for d in want if d not in have]
    if missing:
        from fetch_event_factors import load_token
        import tushare as ts
        pro = ts.pro_api(load_token())
        print(f"[INFO] 拉 daily_basic {len(missing)} 个交易日(约 {len(missing) // 180 + 1} 分钟)...")
        frames = []
        t0 = time.time()
        for i, d in enumerate(missing):
            for attempt in range(3):
                try:
                    df = pro.daily_basic(trade_date=d, fields=["ts_code", "trade_date"] + DAILY_BASIC_FACTORS)
                    frames.append(df)
                    break
                except Exception as e:
                    if attempt == 2:
                        print(f"  [WARN] {d} 拉取失败(跳过): {e}")
                    time.sleep(2)
            time.sleep(0.34)  # tushare 限流
            if (i + 1) % 60 == 0:
                print(f"  ... {i + 1}/{len(missing)} ({time.time() - t0:.0f}s)")
        if frames:
            got = pd.concat([got] + frames, ignore_index=True)
            got.to_parquet(cache, index=False)
            print(f"[INFO] daily_basic 缓存更新: {len(got)} 行 → {cache}")
    return got[got["trade_date"].isin(want)] if len(got) else got


def _pull_tushare_daily(api_name: str, fields: list, dates: pd.DatetimeIndex, cache: Path) -> pd.DataFrame:
    """通用 tushare 按日拉取 + 断点续拉缓存。"""
    got = pd.DataFrame()
    if cache.exists():
        got = pd.read_parquet(cache)
    have = set(got["trade_date"]) if len(got) else set()
    missing = [d.strftime("%Y%m%d") for d in dates if d.strftime("%Y%m%d") not in have]
    if missing:
        from fetch_event_factors import load_token
        import tushare as ts
        pro = ts.pro_api(load_token())
        print(f"[INFO] 拉 {api_name} {len(missing)} 个交易日...")
        frames = []
        for i, d in enumerate(missing):
            for attempt in range(3):
                try:
                    frames.append(getattr(pro, api_name)(trade_date=d, fields=fields))
                    break
                except Exception as e:
                    if attempt == 2:
                        print(f"  [WARN] {d} 拉取失败(跳过): {e}")
                    time.sleep(2)
            time.sleep(0.34)
            if (i + 1) % 60 == 0:
                print(f"  ... {i + 1}/{len(missing)}")
        if frames:
            got = pd.concat([got] + frames, ignore_index=True)
            got.to_parquet(cache, index=False)
    return got


def load_margin(dates: pd.DatetimeIndex, tag: str) -> pd.DataFrame:
    """融资余额明细(仅两融标的 ~2000 只, 覆盖率天然受限)。"""
    return _pull_tushare_daily("margin_detail", ["ts_code", "trade_date", "rzye"], dates,
                               ROOT / "data" / "factors" / f"margin_{tag}.parquet")


def load_hkhold(dates: pd.DatetimeIndex, tag: str) -> pd.DataFrame:
    """北向持股(仅陆股通标的, ratio=持股占流通股比)。"""
    return _pull_tushare_daily("hk_hold", ["ts_code", "trade_date", "vol", "ratio"], dates,
                               ROOT / "data" / "factors" / f"hkhold_{tag}.parquet")


# ═══════════════════════════════════════════════════════════════
# IC 评估
# ═══════════════════════════════════════════════════════════════

def forward_returns(close: pd.DataFrame, n: int) -> pd.DataFrame:
    return close.shift(-n) / close - 1


def lookup(panel: pd.DataFrame, dates: pd.Series, codes: pd.Series) -> np.ndarray:
    """按 (date, code) 从宽表取值, 向量化。"""
    di = panel.index.get_indexer(dates)
    ci = panel.columns.get_indexer(codes)
    vals = np.full(len(dates), np.nan)
    ok = (di >= 0) & (ci >= 0)
    v = panel.values
    vals[ok] = v[di[ok], ci[ok]]
    return vals


def ic_stats(df: pd.DataFrame, factor: str, target: str, by: str = "select_date") -> dict:
    """日频截面 Rank IC: 每日 spearman(factor, target) → 均值/ICIR/正值率。"""
    ics = []
    for _, g in df.groupby(by):
        if len(g) >= 5:
            ic = g[factor].corr(g[target], method="spearman")
            if pd.notna(ic):
                ics.append(ic)
    ics = pd.Series(ics)
    if len(ics) < 10:
        return {"ic_mean": np.nan, "ic_std": np.nan, "icir": np.nan, "ic_pos_rate": np.nan, "n_days": len(ics)}
    return {"ic_mean": ics.mean(), "ic_std": ics.std(),
            "icir": ics.mean() / ics.std() if ics.std() > 0 else np.nan,
            "ic_pos_rate": (ics > 0).mean(), "n_days": len(ics)}


def quintile_spread(df: pd.DataFrame, factor: str, target: str) -> float:
    """五分位 Q5-Q1 均值差(池化, 单调性粗查)。"""
    d = df.dropna(subset=[factor, target])
    if len(d) < 100:
        return np.nan
    try:
        q = pd.qcut(d[factor], 5, labels=False, duplicates="drop")
    except ValueError:
        return np.nan
    grp = d.groupby(q)[target].mean()
    return grp.iloc[-1] - grp.iloc[0] if len(grp) >= 2 else np.nan


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--formula", default="QUANTQQ")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--skip-event-factors", action="store_true",
                    help="跳过三因子评分(事件因子 parquet 缺失的长窗用)")
    ap.add_argument("--skip-daily-basic", action="store_true",
                    help="跳过 tushare daily_basic 行级因子(换手率/估值/市值)")
    args = ap.parse_args()
    start, end = args.tag.split("_")

    sel = pd.read_parquet(ROOT / "data" / "baseline" / f"{args.formula}_selections_{args.tag}.parquet")
    sel["select_date"] = pd.to_datetime(sel["select_date"])
    print(f"[INFO] {args.formula} 信号 {len(sel)} 条 {sel['select_date'].min().date()} → {sel['select_date'].max().date()}")

    # 1. 面板 + 目标变量
    panels = load_panels(start, warmup_days=400, tag=args.tag)
    for n in HORIZONS:
        sel[f"fwd{n}"] = lookup(forward_returns(panels["close"], n), sel["select_date"], sel["stock_code"])

    # 2. 面板因子
    need_ctx = any(need for _, _, need, _ in PANEL_FACTORS)
    ctx = {}
    if need_ctx:
        from filter_signal_tournament import load_hs300
        ctx["sector_map"] = load_sector_map(panels["close"].columns)
        hs300 = load_hs300(start, end)
        ctx["index_ret"] = hs300.pct_change().reindex(panels["close"].index)
    factor_cols = []
    for name, fn, _, _fam in PANEL_FACTORS:
        t0 = time.time()
        panel = fn(panels, ctx) if fn.__code__.co_argcount == 2 else fn(panels)
        sel[name] = lookup(panel, sel["select_date"], sel["stock_code"])
        factor_cols.append(name)
        cov = sel[name].notna().mean()
        print(f"  [因子] {name}: 覆盖率 {cov:.1%} ({time.time() - t0:.0f}s)")

    # 3. daily_basic 行级因子(换手率/估值/市值)
    if not args.skip_daily_basic:
        db = load_daily_basic(pd.DatetimeIndex(sel["select_date"].unique()), args.tag)
        if len(db):
            sel["_td"] = sel["select_date"].dt.strftime("%Y%m%d")
            sel = sel.merge(db.rename(columns={"ts_code": "stock_code"}),
                            left_on=["stock_code", "_td"], right_on=["stock_code", "trade_date"],
                            how="left").drop(columns=["_td", "trade_date"])
            for c in DAILY_BASIC_FACTORS:
                sel[c] = pd.to_numeric(sel[c], errors="coerce")
            factor_cols += DAILY_BASIC_FACTORS
            print(f"  [因子] daily_basic 8 个: 覆盖率 {sel['turnover_rate'].notna().mean():.1%}")

            # 3b. 情绪族: 融资余额占比(需 daily_basic 的 circ_mv) + 北向持股占比
            mg = load_margin(pd.DatetimeIndex(sel["select_date"].unique()), args.tag)
            if len(mg):
                sel["_td"] = sel["select_date"].dt.strftime("%Y%m%d")
                sel = sel.merge(mg.rename(columns={"ts_code": "stock_code"}),
                                left_on=["stock_code", "_td"], right_on=["stock_code", "trade_date"],
                                how="left").drop(columns=["_td", "trade_date"])
                sel["rzye"] = pd.to_numeric(sel["rzye"], errors="coerce")
                sel["rzye_ratio"] = sel["rzye"] / (sel["circ_mv"] * 1e4)  # rzye元 / circ_mv万元→统一
                factor_cols += ["rzye_ratio"]
                print(f"  [因子] rzye_ratio: 覆盖率 {sel['rzye_ratio'].notna().mean():.1%}(仅两融标的)")
            hk = load_hkhold(pd.DatetimeIndex(sel["select_date"].unique()), args.tag)
            if len(hk):
                sel["_td"] = sel["select_date"].dt.strftime("%Y%m%d")
                sel = sel.merge(hk.rename(columns={"ts_code": "stock_code"}),
                                left_on=["stock_code", "_td"], right_on=["stock_code", "trade_date"],
                                how="left").drop(columns=["_td", "trade_date"])
                sel["hk_ratio"] = pd.to_numeric(sel["ratio"], errors="coerce")
                factor_cols += ["hk_ratio"]
                print(f"  [因子] hk_ratio: 覆盖率 {sel['hk_ratio'].notna().mean():.1%}(仅陆股通标的)")

    # 4. 三因子评分(行级, 需要事件因子 parquet)
    if not args.skip_event_factors:
        try:
            from factor_score import score_selections
            mf = pd.read_parquet(ROOT / "data" / "factors" / f"moneyflow_{args.tag}.parquet")
            ti = pd.read_parquet(ROOT / "data" / "factors" / f"top_inst_{args.tag}.parquet")
            bt = pd.read_parquet(ROOT / "data" / "factors" / f"block_trade_{args.tag}.parquet")
            sel = score_selections(sel, mf, ti, bt)
            factor_cols += ["mf_score", "dragon_score", "block_score", "total_score"]
        except FileNotFoundError as e:
            print(f"[WARN] 事件因子 parquet 缺失({e}), 跳过三因子评分")

    # 4. IC 评估 × 3 期限
    rows = []
    for fc in factor_cols:
        for n in HORIZONS:
            s = ic_stats(sel, fc, f"fwd{n}")
            s.update({"factor": fc, "horizon": n,
                      "q5_q1": quintile_spread(sel, fc, f"fwd{n}")})
            rows.append(s)
    res = pd.DataFrame(rows)
    res["family"] = res["factor"].map(FAMILY_OF)

    # 4b. 信号×因子矩阵落盘(S3/S4 的单一因子值来源, 计划书 v2)
    mat = ROOT / "output" / "gs_filter" / f"factor_matrix_{args.formula}_{args.tag}.parquet"
    mat.parent.mkdir(parents=True, exist_ok=True)
    keep = ["stock_code", "select_date", "formula_name"] + factor_cols + [f"fwd{n}" for n in HORIZONS]
    sel[[c for c in keep if c in sel.columns]].to_parquet(mat, index=False)
    print(f"[INFO] 因子矩阵: {mat} ({len(sel)} 行 × {len(factor_cols)} 因子)")

    # 5. 排行榜(以 fwd10 的 |ICIR| 排序) + 落盘
    res["关注"] = (res["ic_mean"].abs() >= IC_MIN) & (res["icir"].abs() >= ICIR_MIN)
    out = ROOT / "output" / "reports" / f"factor_ic_{args.formula}_{args.tag}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    res.to_csv(out, index=False, encoding="utf-8-sig")

    pd.set_option("display.width", 200)
    for n in HORIZONS:
        sub = res[res["horizon"] == n].sort_values("icir", key=abs, ascending=False)
        print(f"\n=== 因子排行榜(fwd{n}, 按 |ICIR|)===")
        print(sub[["factor", "ic_mean", "icir", "ic_pos_rate", "q5_q1", "n_days", "关注"]]
              .to_string(index=False, float_format=lambda x: f"{x:+.4f}"))
    print(f"\n[INFO] 明细已存 {out}")
    print(f"[判定口径] 值得关注 = |IC|≥{IC_MIN} 且 |ICIR|≥{ICIR_MIN}(预注册初筛门槛); "
          "IC>0 为正相关(值越大未来收益越高), <0 为负相关")


if __name__ == "__main__":
    main()
