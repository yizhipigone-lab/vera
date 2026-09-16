"""brain/market_panel.py — 市场面数据包 (治理III W4-c 自 brain/data_tools 分家, 2026-09-05)。

把「市场体检 / 市场快照 / 涨停池」三家从 brain/data_tools.py (789 行杂物间) 迁出,
照 dsh_channel 范式: 小接口 (3 个模块函数) + 内部 fail-soft, 让新会话找"市场体检"
不必在 789 行里翻。

【迁移纪律】纯移动不改行为: 三个函数体与 data_tools 迁移前逐字节一致。
2026-09-15 审计收口: 迁移时**原样复制**的私有助手 (_section/_no_ak/_ak)
已收编 brain/ak_sections.py 公共模块 (本文件 docstring 早就写了"应收公共
模块", 本次清偿); 缓存路径/指数代码集为本域私有, 保留。
牛熊口径走 core/index_regime.py 单一真相源 (治理III W1-b), 见 market_health。
"""
from __future__ import annotations

import datetime as dt
import time

from utils.sysutil import project_root

from brain.ak_sections import HAS_AK as _HAS_AK  # noqa: E402
from brain.ak_sections import ak as _ak  # noqa: E402
from brain.ak_sections import no_ak as _no_ak  # noqa: E402
from brain.ak_sections import section as _section  # noqa: E402

_A_INDEX_CODES = {"sh000001", "sz399001", "sz399006",
                  "sh000688", "sh000300", "sh000016"}
_CACHE_DIR = project_root() / "data" / "brain_model_cache"
_SNAPSHOT_CACHE_TTL = 2 * 3600  # 2 小时
_HEALTH_INDEXES = (("shanghai", "上证指数"), ("hs300", "沪深300"),
                   ("zz500", "中证500"), ("chuangyeban", "创业板指"))
_HEALTH_WINDOWS = (("近1月", 21), ("近3月", 63), ("近半年", 126), ("近1年", 252))


# ── 市场面公开接口 (3) ────────────────────────────────────────


def zt_pool(trade_date: str | None = None) -> str:
    """涨停池 → Markdown 段。指定日/今天起往前回退 4 天，覆盖周末/节假日。"""
    if not _HAS_AK:
        return _section("涨停池", _no_ak())
    ak = _ak()
    base = (dt.datetime.strptime(trade_date, "%Y%m%d") if trade_date
            else dt.datetime.now())
    last_err: Exception | None = None
    for i in range(5):
        d = (base - dt.timedelta(days=i)).strftime("%Y%m%d")
        try:
            df = ak.stock_zt_pool_em(date=d)
        except Exception as e:
            last_err = e
            continue
        if df is None or df.empty:
            continue  # 非交易日，继续往前找
        cols = [c for c in ["名称", "涨跌幅", "连板数", "所属行业", "封板资金"]
                if c in df.columns]
        body = (f"日期 {d}，涨停 {len(df)} 家（前 25 条）\n\n"
                + df[cols].head(25).to_string(index=False))
        return _section("涨停池", body)
    return _section("涨停池", f"【缺】近 5 日均无数据（{last_err}）")


def market_health() -> str:
    """市场体检表：4 指数 × 4 窗口收益 + MA250 牛熊状态 → Markdown 段。

    只读参考（2026-09-04 决策记录 docs/plan/2026-09-04_市场体检表与研究雷达_决策记录.md：
    Q1 只读参考 / Q2 四指数并行不合成）——不进交易闸门、不合成总分。
    牛熊口径走 core/index_regime.py 单一真相源（治理III W1-b 收口，门槛
    min_periods=200 用户拍板）：价>MA250 且 MA250 的 20 日斜率>0 为牛，
    价<MA250 且斜率<0 为熊，其余为震荡。数据源 TDX 指数日线；
    单指数失败只标【缺】不拖死其他（本模块既有 fail-soft 风格）。
    """
    try:
        from core.data_fetcher import DataFetcher
        from core.index_regime import classify  # 牛熊口径单一真相源 (治理III W1-b)
    except Exception as e:
        return _section("市场体检（只读参考）", f"【缺】TDX 数据层不可用（{e}）")
    start = (dt.datetime.now() - dt.timedelta(days=800)).strftime("%Y%m%d")
    end = dt.datetime.now().strftime("%Y%m%d")
    head = ("| 指数 | 近1月 | 近3月 | 近半年 | 近1年 | vs MA250 | 牛熊 |\n"
            "|---|---|---|---|---|---|---|")
    rows, last_dates = [], []
    import pandas as pd
    for key, name in _HEALTH_INDEXES:
        try:
            df = DataFetcher.get_index_data(key, start, end,
                                            dividend_type="none", period="1d")
            if df is None or len(df) == 0:
                raise RuntimeError("无数据")
            if "date" not in df.columns:
                df = df.reset_index().rename(
                    columns={df.index.name or "index": "date"})
            df = df.sort_values("date").reset_index(drop=True)
            last_dates.append(str(df["date"].iloc[-1])[:10])
            c = pd.Series([float(x) for x in df["close"]
                           if x == x and float(x) > 0])  # 排 NaN/0/负价
            if len(c) < 2:
                raise RuntimeError("有效收盘价不足")
            price = float(c.iloc[-1])
            rets = [f"{(price / c.iloc[-1 - n] - 1) * 100:+.1f}%"
                    if len(c) > n else "【缺】" for _, n in _HEALTH_WINDOWS]
            state_en, ma_now, _sl = classify(c)  # 单一真相源, 门槛 min_periods=200
            if state_en is None:  # 样本不足门槛 → 短样本不冒充
                pos, state = "【缺】", "【缺】"
            else:
                pos = f"{(price / ma_now - 1) * 100:+.1f}%"
                state = {"bull": "牛", "bear": "熊", "range": "震荡"}[state_en]
            rows.append(f"| {name} | {' | '.join(rets)} | {pos} | {state} |")
        except Exception as e:
            rows.append(f"| {name} | 【缺】 | 【缺】 | 【缺】 | 【缺】 | 【缺】 | 【缺】（{e}） |")
    foot = (f"数据截至 {max(last_dates) if last_dates else '缺'}，TDX 指数日线。"
            "牛熊口径（core/index_regime.py 单一真相源）：价>MA250（门槛200）"
            " 且 MA250 的 20 日斜率>0 为牛，价<MA250 且斜率<0 为熊，"
            "其余为震荡（猴市）。只读参考，不影响 MA200 交易闸门。")
    return _section("市场体检（只读参考）", head + "\n" + "\n".join(rows)
                    + "\n\n" + foot)


def market_snapshot(trade_date: str | None = None, fresh: bool = False) -> str:
    """A股/港股/美股指数 + 南向资金 + 涨停池 + 市场体检 → Markdown 数据包（2h 缓存）。"""
    key = trade_date or "today"
    cache_file = _CACHE_DIR / f"market_snapshot_{key}.txt"
    if not fresh and cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < _SNAPSHOT_CACHE_TTL:
            return (cache_file.read_text(encoding="utf-8")
                    + f"\n\n（以上为缓存，取数于 {age / 60:.0f} 分钟前；"
                      "加 --fresh 强制重取）")
    if not _HAS_AK:
        # 体检表只依赖 TDX，不随 akshare 缺失连坐隐藏（2026-09-04 审计修复）
        return ("# 市场快照数据包\n\n"
                + _section("A股主要指数", _no_ak()) + "\n\n" + market_health())
    ak = _ak()
    out = ["# 市场快照数据包",
           f"（生成时间 {dt.datetime.now():%Y-%m-%d %H:%M}，akshare 直取，权威数字）"]
    # A 股指数（新浪现货快照，盘后为收盘价；腾讯指数日线降级）
    try:
        df = ak.stock_zh_index_spot_sina()
        sub = df[df["代码"].isin(_A_INDEX_CODES)]
        cols = [c for c in ["代码", "名称", "最新价", "涨跌幅", "成交额"]
                if c in sub.columns]
        out.append(_section("A股主要指数", sub[cols].to_string(index=False)))
    except Exception as e:
        try:
            rows = []
            for idx_code in sorted(_A_INDEX_CODES):
                t = ak.stock_zh_index_daily_tx(symbol=idx_code).tail(1).iloc[0]
                rows.append(f"{idx_code}: 收 {t['close']}（{t['date']}，腾讯日线）")
            out.append(_section("A股主要指数（腾讯日线降级，非实时）",
                                "\n".join(rows)))
        except Exception as e2:
            out.append(_section("A股主要指数", f"【缺】新浪 {e}；腾讯 {e2}"))
    # 市场体检（2026-09-04 决策：只读参考，4 指数并行不合成）
    out.append(market_health())
    # 港股指数（只看恒生系）
    try:
        h = ak.stock_hk_index_spot_sina()
        name_col = "名称" if "名称" in h.columns else h.columns[0]
        sub = h[h[name_col].astype(str).str.contains("恒生")]
        cols = [c for c in [name_col, "最新价", "涨跌幅"] if c in sub.columns]
        out.append(_section("港股指数", sub[cols].to_string(index=False)))
    except Exception as e:
        out.append(_section("港股指数", f"【缺】{e}"))
    # 美股三大指数（最近一根日线 = 隔夜收盘，涨跌幅用最近两日 close 算）
    rows = []
    for sym, name in [(".DJI", "道指"), (".IXIC", "纳指"), (".INX", "标普500")]:
        try:
            t = ak.index_us_stock_sina(symbol=sym).tail(2)
            prev, cur = float(t.iloc[0]["close"]), float(t.iloc[1]["close"])
            rows.append(f"{name}: 收 {cur:.1f}  涨跌幅 {(cur / prev - 1) * 100:+.2f}%"
                        f"（日期 {t.iloc[1]['date']}）")
        except Exception as e:
            rows.append(f"{name}: 【缺】{e}")
    out.append(_section("美股三大指数（隔夜收盘）", "\n".join(rows)))
    # 南向资金（港股通，最近 3 日）
    try:
        s = ak.stock_hsgt_hist_em(symbol="南向资金").tail(3)
        out.append(_section("南向资金（最近 3 日）", s.to_string(index=False)))
    except Exception as e:
        out.append(_section("南向资金（最近 3 日）", f"【缺】{e}"))
    # 涨停池（非交易日自动回退）
    out.append(zt_pool(trade_date))
    text = "\n\n".join(out)
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(text, encoding="utf-8")
    except Exception:
        pass  # 缓存写失败不影响返回（松耦合）
    return text
