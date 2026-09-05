"""brain/data_tools.py — 对话大脑统一取数工具层（2026-08-12，P2）。

> 2026-09-05 治理III W4-c: 市场面 (zt_pool/market_health/market_snapshot)
> 已迁出到 brain/market_panel.py, 本文件保留兼容 re-export (见文件中部)。
> 其余数据域 (新闻/技术面/估值/公告/诊断) 仍在本文件。

动机：大脑查行情原来要现场发十几条 search_web / 裸 akshare 命令摸索
（2026-08-12 简报会话实测约 20 分钟），慢、易超时、烧钱。本模块把高频取数
固化成 CLI 子命令，akshare 直取 + 紧凑文本输出，由 prompts.py「固定打法」调用。
每个数据源独立 try/except，挂了标【缺】不拖死其他源。

数据源口径（2026-08-13 用户拍板修正：实测砍失效源，省时间省 token）：
- 个股日线：tushare → 腾讯 → 新浪 三级降级（东财 hist 限连已砍，2026-08-13 实测
  连接被远端断开，两轮复测均失败）；
- 个股估值：tushare daily_basic（真实 PE-TTM/PB/股息率/市值）；
- 指数现货：新浪（快照），降级腾讯指数日线；
- 财务指标：新浪（按报告期）；最新价：腾讯主源、新浪兜底；
- 涨停池、南向资金：akshare 1.18.64 里只有东财源（hsgt/zt_pool 实测可用，
  与限连的 hist/individual_info/news_em 是不同主机，不砍）；
- 舆情三层（2026-08-13 换源）：公告=巨潮资讯（tushare anns_d 无权限已砍）；
  互动易问答=巨潮（东财个股新闻 stock_news_em 接口崩溃已砍）；
  快讯=新浪/同花顺（财联社 stock_info_global_cls 超时已砍；雪球/腾讯无可用
  新闻接口，实测全挂）。

用法：
    python -m brain.data_tools market_snapshot [YYYYMMDD] [--fresh]  # 指数+南向资金+涨停池+市场体检（缓存 2h）
    python -m brain.data_tools market_health                        # 市场体检表（4指数×4窗口，TDX）
    python -m brain.data_tools stock_news [N]                        # 财新要闻 N 条（默认 15）
    python -m brain.data_tools zt_pool [YYYYMMDD]                    # 涨停池（非交易日自动回退）
    python -m brain.data_tools stock_technicals <代码>               # 个股 MA/MACD/RSI/BOLL
    python -m brain.data_tools stock_fundamentals <代码>             # 个股 PE/PB/PS/股息率/行业
    python -m brain.data_tools stock_diagnosis <代码>                # 个股诊断一站式（技术面+估值+舆情+模板，并发）
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import os
import time

from utils.sysutil import ensure_utf8_stdout, project_root

_HAS_AK = importlib.util.find_spec("akshare") is not None
_HAS_TS = importlib.util.find_spec("tushare") is not None


def _ak():
    import akshare as ak
    return ak


def _ts_pro():
    """tushare pro_api。token 取环境变量 TUSHARE_TOKEN 或项目根 .env
    （与 tools/verify_tushare_*.py 同款约定，不写第二份秘密）。
    未装 tushare / 无 token → None（调用方按【缺】降级）。"""
    if not _HAS_TS:
        return None
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if not token:
        env = project_root() / ".env"
        try:
            for line in env.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("TUSHARE_TOKEN"):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        except Exception:
            pass
    if not token:
        return None
    import tushare as ts
    ts.set_token(token)  # pro_bar 走全局 token
    return ts.pro_api(token)


def _ts_code(c: str) -> str:
    """6 位代码 → tushare 格式（300750.SZ / 600000.SH / 430047.BJ）。"""
    suf = "SH" if c[0] == "6" else "BJ" if c[0] in "48" else "SZ"
    return f"{c}.{suf}"


def _kg_company_name(c: str) -> str | None:
    """本地知识图谱查公司官方名称（不联网，永远可用）。

    2026-08-12 事故驱动：大脑凭训练记忆把 300687 叫成"赛义"（实为赛意信息）。
    名称以 kg/graph.db 为准，别信 LLM 记忆。"""
    try:
        import sqlite3
        db = project_root() / "kg" / "graph.db"
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT name FROM kg_nodes "
                "WHERE node_type='company' AND code=?", (c,)).fetchone()
        finally:
            conn.close()
        return row[0] if row and row[0] else None
    except Exception:
        return None


# 全量公司 (code, name) 进程级缓存（名字→代码反查用）。
# graph.db mtime 变化即失效重载（新股进库/改名后自动跟新，不用重启 server）。
_kg_companies_cache: list[tuple[str, str]] | None = None
_kg_companies_mtime: float = -1.0


def _kg_all_companies() -> list[tuple[str, str]]:
    """本地知识图谱全量公司 (code, name) 列表，进程级缓存 + mtime 失效。

    供快路径把"宁德时代怎么样"这类名字问法反查成代码（快路径提速 2026-08-13）。
    任何失败返 []（不抛，调用方按"反查失败→走原 agent loop"降级）。
    """
    global _kg_companies_cache, _kg_companies_mtime
    try:
        db = project_root() / "kg" / "graph.db"
        mtime = db.stat().st_mtime if db.exists() else -1.0
        if _kg_companies_cache is not None and _kg_companies_mtime == mtime:
            return _kg_companies_cache
        import sqlite3
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT code, name FROM kg_nodes "
                "WHERE node_type='company' AND name IS NOT NULL AND name != ''"
            ).fetchall()
        finally:
            conn.close()
        _kg_companies_cache = [(r[0], r[1]) for r in rows]
        _kg_companies_mtime = mtime
        return _kg_companies_cache
    except Exception:
        return []


def _call_with_timeout(fn, timeout: float = 25.0):
    """给无内部超时的数据调用包硬超时（akshare stock_info_global_cls
    实测挂死无返回，2026-08-12）。daemon 线程泄漏可接受：主进程退出不等它。
    超时/异常都抛给调用方，由调用方标【缺】。"""
    import threading
    box: dict = {}

    def _runner():
        try:
            box["v"] = fn()
        except Exception as e:
            box["e"] = e

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"数据调用超时（>{timeout:.0f}s）")
    if "e" in box:
        raise box["e"]
    return box.get("v")


def _section(title: str, body: str) -> str:
    return f"## {title}\n\n{body}"


def _no_ak() -> str:
    return "【缺】未安装 akshare（pip install akshare）"


def _norm_code(code: str) -> str:
    """'300750' / '300750.SZ' / 'sz300750' → '300750'。"""
    c = (code or "").strip().lower()
    for pre in ("sh", "sz", "bj"):
        if c.startswith(pre):
            c = c[len(pre):]
    return c.split(".")[0]


def stock_news(n: int = 15) -> str:
    """财新要闻 → Markdown 段（政策/公告/宏观线索的主要来源）。"""
    if not _HAS_AK:
        return _section("财新要闻", _no_ak())
    ak = _ak()
    try:
        df = ak.stock_news_main_cx()
        cols = [c for c in ["pub_time", "tag", "summary", "url"] if c in df.columns]
        return _section(f"财新要闻（最近 {min(n, len(df))} 条）",
                        df[cols].head(n).to_string(index=False))
    except Exception as e:
        return _section("财新要闻", f"【缺】{e}")


# 市场面 (zt_pool / market_health / market_snapshot) 已迁 brain/market_panel
# (治理III W4-c, 2026-09-05): 本处兼容 re-export, 旧引用方零改动。
# 新代码请直接 `from brain.market_panel import market_health, market_snapshot, zt_pool`。
from brain.market_panel import market_health, market_snapshot, zt_pool  # noqa: E402


def _sina_code(c: str) -> str:
    """6 位代码 → 新浪格式（6 开头沪市 sh，4/8 北交所 bj，其余深市 sz）。"""
    return ("sh" if c[0] == "6" else "bj" if c[0] in "48" else "sz") + c


def stock_technicals(code: str) -> str:
    """个股技术面四件套：MA/MACD/RSI/BOLL + 关键价位 + 量能 → Markdown。

    指标在 Python 侧算好（约 160 根日线，前复权），LLM 只负责解读 ——
    对照 TradingAgentsCN 市场分析师的报告模板数据需求。
    数据源：新浪主源（2026-08-12 用户确认东财限连，改新浪为主），东财降级。
    """
    if not _HAS_AK:
        return _no_ak()
    ak = _ak()
    c = _norm_code(code)
    if len(c) != 6 or not c.isdigit():
        return f"【错】无法识别股票代码: {code!r}（要 6 位数字，如 300750 或 300750.SZ）"
    start = (dt.datetime.now() - dt.timedelta(days=200)).strftime("%Y%m%d")
    end = dt.datetime.now().strftime("%Y%m%d")
    df, source, vol_unit = None, "", "股"

    def _try_ts():  # tushare（最高优先，2026-08-12 用户拍板）: vol 是成交量(手)
        pro = _ts_pro()
        if pro is None:
            return None
        import tushare as ts
        # 2026-08-13: pro_bar 内部 requests 无默认超时，理论可挂死；
        # 包硬超时（stock_diagnosis 并发合并后，单源挂死不能再白拖整包）
        raw = _call_with_timeout(
            lambda: ts.pro_bar(ts_code=_ts_code(c), adj="qfq",
                               start_date=start, end_date=end), timeout=30)
        if raw is None or raw.empty:
            return None
        return raw.sort_values("trade_date").rename(
            columns={"trade_date": "日期", "close": "收盘", "high": "最高",
                     "low": "最低", "vol": "成交量"})

    def _try_tx():  # 腾讯: amount 是成交量(手)
        raw = ak.stock_zh_a_hist_tx(symbol=_sina_code(c), start_date=start,
                                    end_date=end, adjust="qfq")
        if raw is None or raw.empty:
            return None
        return raw.rename(columns={"date": "日期", "close": "收盘",
                                   "high": "最高", "low": "最低",
                                   "amount": "成交量"})

    def _try_sina():  # 新浪: volume 是成交量(股)
        raw = ak.stock_zh_a_daily(symbol=_sina_code(c), adjust="qfq")
        if raw is None or raw.empty:
            return None
        return raw.rename(columns={"date": "日期", "close": "收盘",
                                   "high": "最高", "low": "最低",
                                   "volume": "成交量"})

    # 2026-08-13: 东财 stock_zh_a_hist 限连（两轮实测连接被远端断开），从降级链
    # 移除——腾讯/新浪都挂时再试东财也只是白等超时，浪费 token。
    for name, fn in (("tushare", _try_ts), ("腾讯", _try_tx),
                     ("新浪", _try_sina)):
        try:
            got = fn()
            if got is not None and not got.empty:
                df, source = got.tail(160), name
                vol_unit = "手" if name in ("tushare", "腾讯") else "股"
                break
        except Exception:
            continue
    if df is None:
        return f"【缺】{c} 日线拉取失败（tushare/腾讯/新浪都挂）"
    if len(df) < 30:
        return f"【缺】{c} 日线数据不足（{len(df)} 根），算不了指标"

    close = df["收盘"].astype(float)
    high, low = df["最高"].astype(float), df["最低"].astype(float)
    vol = df["成交量"].astype(float)
    ma = {n: close.rolling(n).mean() for n in (5, 10, 20, 60)}
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif, dea = ema12 - ema26, (ema12 - ema26).ewm(span=9, adjust=False).mean()
    hist = (dif - dea) * 2
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rsi = 100 - 100 / (1 + gain / loss)
    mid = ma[20]
    std = close.rolling(20).std()
    upper, lower = mid + 2 * std, mid - 2 * std

    def _f(s) -> str:
        v = s.iloc[-1]
        return f"{v:.2f}" if v == v else "NaN（数据不够长）"  # v==v 排 NaN

    price = close.iloc[-1]
    out = [f"# {c} 技术面数据包",
           f"（{df['日期'].iloc[-1]} 收盘 {price:.2f}，近 {len(df)} 根日线，"
           f"前复权，数据源 {source}）"]
    # 均线
    ma_now = {n: ma[n].iloc[-1] for n in ma}
    if price > ma_now[5] > ma_now[10] > ma_now[20]:
        align = "多头排列"
    elif price < ma_now[5] < ma_now[10] < ma_now[20]:
        align = "空头排列"
    else:
        align = "纠缠/转换中"
    ma20_note = ("站上" if price > ma_now[20] else "跌破") if ma_now[20] == ma_now[20] else "缺"
    out.append(_section("均线系统",
        f"MA5 {_f(ma[5])} / MA10 {_f(ma[10])} / MA20 {_f(ma[20])} / MA60 {_f(ma[60])}\n"
        f"现价 vs MA20: {(price / ma_now[20] - 1) * 100:+.1f}%（{ma20_note}）\n"
        f"排列形态: {align}" if ma_now[20] == ma_now[20] else
        f"MA5 {_f(ma[5])} / MA10 {_f(ma[10])} / MA20 {_f(ma[20])} / MA60 {_f(ma[60])}\n"
        f"排列形态: {align}"))
    # MACD
    cross = ""
    if dif.iloc[-1] > dea.iloc[-1] and dif.iloc[-2] <= dea.iloc[-2]:
        cross = "（最新一根金叉）"
    elif dif.iloc[-1] < dea.iloc[-1] and dif.iloc[-2] >= dea.iloc[-2]:
        cross = "（最新一根死叉）"
    out.append(_section("MACD(12,26,9)",
                        f"DIF {_f(dif)} / DEA {_f(dea)} / 柱 {_f(hist)}{cross}"))
    # RSI
    r = rsi.iloc[-1]
    zone = "超买区（>70）" if r > 70 else ("超卖区（<30）" if r < 30 else "中性区")
    out.append(_section("RSI14", f"{r:.1f} — {zone}"))
    # BOLL
    u, m, l = upper.iloc[-1], mid.iloc[-1], lower.iloc[-1]
    if price > u:
        pos = "突破上轨（强势/超买风险）"
    elif price > m:
        pos = "中轨与上轨之间（偏强）"
    elif price > l:
        pos = "中轨与下轨之间（偏弱）"
    else:
        pos = "跌破下轨（超卖/弱势）"
    out.append(_section("布林带(20,2)",
                        f"上轨 {_f(upper)} / 中轨 {_f(mid)} / 下轨 {_f(lower)}\n"
                        f"现价位置: {pos}"))
    # 关键价位 + 量能
    out.append(_section("关键参考价位（近 20 日）",
        f"20 日最低 {low.tail(20).min():.2f}（支撑参考）/ "
        f"20 日最高 {high.tail(20).max():.2f}（压力参考）\n"
        f"MA20 {_f(mid)}（动态支撑/压力）"))
    v5 = vol.tail(5).mean()
    out.append(_section("量能",
        f"最新成交量 {vol.iloc[-1]:.0f} {vol_unit} / 5 日均量 {v5:.0f} {vol_unit} / "
        f"量比（今÷5日均）{vol.iloc[-1] / v5:.2f}"))
    return "\n\n".join(out)


def stock_fundamentals(code: str) -> str:
    """个股基本面/估值包 → Markdown。

    数据源（2026-08-13 用户拍板修正）：
    - tushare daily_basic：真实 PE/PE-TTM/PB/PS/股息率/市值（主估值来源）；
    - 新浪：财务指标（EPS/ROE/增长率/负债率，按报告期）；
    - 最新价：腾讯主源、新浪兜底；PE 年化估算仅供交叉验证（标【估算】）。
    2026-08-13: 东财 individual_info_em（名称/行业补充段）限连两轮实测失败，
    已移除——名称以 kg/graph.db 为准，市值 tushare 段已含。
    """
    if not _HAS_AK:
        return _no_ak()
    ak = _ak()
    c = _norm_code(code)
    if len(c) != 6 or not c.isdigit():
        return f"【错】无法识别股票代码: {code!r}（要 6 位数字，如 300750 或 300750.SZ）"
    out = [f"# {c} 基本面/估值数据包"]
    # tushare daily_basic：真实估值（最高优先，2026-08-12 用户拍板）——
    # PE/PE-TTM/PB/PS/股息率/总市值/流通市值，正是东财限连后缺的那块
    try:
        pro = _ts_pro()
        if pro is None:
            raise RuntimeError("无 TUSHARE_TOKEN 或未装 tushare")
        db = None
        for i in range(5):  # 非交易日往前回退
            d = (dt.datetime.now() - dt.timedelta(days=i)).strftime("%Y%m%d")
            # 2026-08-13: 同 pro_bar，requests 无默认超时，包硬超时防挂死
            db = _call_with_timeout(
                lambda: pro.daily_basic(ts_code=_ts_code(c), trade_date=d),
                timeout=20)
            if db is not None and not db.empty:
                break
        if db is None or db.empty:
            raise RuntimeError("近 5 日无 daily_basic 数据")
        r = db.iloc[0]
        rows = [f"交易日 {r['trade_date']} / 收盘 {r['close']}"]
        for col, label in [("pe", "PE（静态）"), ("pe_ttm", "PE-TTM"),
                           ("pb", "PB"), ("ps_ttm", "PS-TTM"),
                           ("dv_ttm", "股息率TTM%")]:
            if col in db.columns:
                rows.append(f"{label}: {r[col]}")
        for col, label in [("total_mv", "总市值"), ("circ_mv", "流通市值")]:
            if col in db.columns:
                rows.append(f"{label}: {float(r[col]) / 1e4:.0f} 亿")
        out.append(_section("估值指标（tushare daily_basic，真实值）",
                            "\n".join(rows)))
    except Exception as e:
        out.append(_section("估值指标（tushare daily_basic）", f"【缺】{e}"))
    eps, rpt, growth = None, None, None
    # 新浪：财务指标（按报告期，取最近一期）
    try:
        fi = ak.stock_financial_analysis_indicator(
            symbol=c, start_year=str(dt.datetime.now().year - 1))
        last = fi.tail(1)
        keep = [col for col in fi.columns
                if any(k in col for k in ("日期", "每股收益", "净资产收益率",
                                          "净利润增长率", "主营业务收入增长率",
                                          "资产负债率"))]
        out.append(_section("财务指标（新浪，最近报告期）",
                            last[keep].to_string(index=False)))
        try:
            rpt = str(last.iloc[0]["日期"])
            eps = float(last.iloc[0]["摊薄每股收益(元)"])
            growth = float(last.iloc[0]["净利润增长率(%)"])
        except Exception:
            pass  # 列名变动就只输出表格, 不算估算
    except Exception as e:
        out.append(_section("财务指标（新浪，最近报告期）", f"【缺】{e}"))
    # 最新价 + 估算 PE（最新价 ÷ 年化 EPS）：腾讯主源，新浪兜底
    try:
        try:
            px = float(ak.stock_zh_a_hist_tx(
                symbol=_sina_code(c),
                start_date=(dt.datetime.now() - dt.timedelta(days=15)).strftime("%Y%m%d"),
                end_date=dt.datetime.now().strftime("%Y%m%d"),
                adjust="qfq").tail(1).iloc[0]["close"])
            px_src = "腾讯"
        except Exception:
            px = float(ak.stock_zh_a_daily(
                symbol=_sina_code(c)).tail(1).iloc[0]["close"])
            px_src = "新浪"
        line = f"最新价 {px:.2f}（{px_src}）"
        if eps and eps > 0 and rpt:
            factor = {3: 4.0, 6: 2.0, 9: 4.0 / 3, 12: 1.0}.get(int(rpt[5:7]))
            if factor:
                pe = px / (eps * factor)
                line += (f"\n估算 PE（{rpt} 报告期摊薄 EPS {eps:.2f} 年化）: "
                         f"{pe:.1f}【估算，非 PE-TTM】")
                if growth and growth > 0:
                    line += f"\n净利润增长率 {growth:.1f}% → PEG≈{pe / growth:.2f}【估算】"
        out.append(_section("估值估算（新浪口径）", line))
    except Exception as e:
        out.append(_section("估值估算（新浪口径）", f"【缺】{e}"))
    out.append("> tushare 段是真实估值（PE-TTM/PB/股息率/市值）；"
               "「估值估算」段的 PE/PEG 为年化粗算（非 TTM），仅供交叉验证；"
               "行业估值对比本包不含，成文时标【待验证】。")
    return "\n\n".join(out)


def stock_notices(code: str, days: int = 90) -> str:
    """个股舆情/消息面包 → Markdown（补 TradingAgentsCN 新闻/社媒分析师的岗）。

    三层信息源（2026-08-13 用户拍板换源，旧三源两轮实测全挂，砍掉省 token）：
    - 公告：巨潮资讯 stock_zh_a_disclosure_report_cninfo（官方披露，最硬；
      旧源 tushare anns_d 无接口权限，砍）；
    - 互动易问答：巨潮 stock_irm_cninfo（投资者提问+公司回复，个股特有舆情；
      旧源东财 stock_news_em 接口崩溃 ArrowInvalid，砍）；
    - 快讯按公司名过滤：新浪 stock_info_global_sina → 同花顺 stock_info_global_ths
      （旧源财联社 stock_info_global_cls 超时两轮复测均挂，砍；
      雪球/腾讯无可用新闻接口，2026-08-13 实测全挂）。
    公司官方名称取自本地 kg/graph.db（不联网，防 LLM 记错名字）。
    """
    c = _norm_code(code)
    if len(c) != 6 or not c.isdigit():
        return f"【错】无法识别股票代码: {code!r}（要 6 位数字，如 300687 或 300687.SZ）"
    name = _kg_company_name(c)
    title = f"# {c}（{name or '名称未入kg'}）舆情/消息面数据包"
    out = [title]
    # ① 公告（巨潮资讯，官方披露）
    try:
        ak = _ak()
        start = (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y%m%d")
        end = dt.datetime.now().strftime("%Y%m%d")
        df = _call_with_timeout(
            lambda: ak.stock_zh_a_disclosure_report_cninfo(
                symbol=c, market="沪深京", start_date=start, end_date=end),
            timeout=40)
        if df is None or df.empty:
            body = f"近 {days} 天无公告记录"
        else:
            cols = [x for x in df.columns
                    if any(k in x for k in ("公告标题", "公告时间"))]
            body = df[cols].head(30).to_string(index=False)
        out.append(_section(f"公告（巨潮资讯，近 {days} 天）", body))
    except Exception as e:
        out.append(_section("公告（巨潮资讯）", f"【缺】{e}"))
    # ② 互动易问答（巨潮：投资者提问 + 公司回复，个股特有舆情）
    try:
        ak = _ak()
        qa = _call_with_timeout(lambda: ak.stock_irm_cninfo(symbol=c), timeout=40)
        if qa is None or qa.empty:
            body = "无互动易问答记录"
        else:
            # 未回答的问题没有信息量，只看公司已回复的（88 条里约 6 成已答，实测）
            acol = next((x for x in qa.columns if "回答内容" in x), None)
            if acol:
                qa = qa[qa[acol].notna()]
            tcol = next((x for x in qa.columns if "提问时间" in x), None)
            if tcol:
                qa = qa.sort_values(tcol, ascending=False)
            cols = [x for x in qa.columns
                    if any(k in x for k in ("问题", "回答内容", "提问时间", "更新时间"))
                    and "编号" not in x]
            body = qa[cols].head(10).to_string(index=False, max_colwidth=60)
        out.append(_section("互动易问答（巨潮，最近 10 条已回复）", body))
    except Exception as e:
        out.append(_section("互动易问答（巨潮）", f"【缺】{e}"))
    # ③ 快讯按公司名过滤（新浪主源、同花顺兜底；没有公司名就跳过）
    if name:
        got, src = None, ""
        for src_name, attr in (("新浪", "stock_info_global_sina"),
                               ("同花顺", "stock_info_global_ths")):
            try:
                ak = _ak()
                got = _call_with_timeout(getattr(ak, attr), timeout=20)
                if got is not None and not got.empty:
                    src = src_name
                    break
            except Exception:
                continue
        if got is None or got.empty:
            out.append(_section("快讯", "【缺】新浪/同花顺快讯都拉取失败"))
        else:
            hit = got[got.apply(
                lambda r: r.astype(str).str.contains(name).any(), axis=1)]
            if hit.empty:
                body = f"最新 {len(got)} 条{src}快讯无「{name}」相关内容"
            else:
                body = hit.head(10).to_string(index=False)
            out.append(_section(f"{src}快讯含「{name}」（最新 {len(got)} 条过滤）",
                                body))
    out.append("> 舆情三层只是『有什么消息』；消息真假/影响由大脑成文时判断："
               "公告=【事实】，互动易/快讯=【事实但需读内容】，概念炒作=【传闻，待验证】。")
    return "\n\n".join(out)


def stock_diagnosis(code: str, per_source_timeout: float = 60.0) -> str:
    """个股诊断一站式数据包 → Markdown（技术面 + 估值 + 舆情 + 成文模板）。

    给对话大脑「固定打法」用：原来 LLM 要分 3 次 Bash 调 stock_technicals /
    stock_fundamentals / stock_notices（3 轮 LLM 往返 + 3 次子进程冷启动 +
    串行网络），合并成单命令后 1 次调用拿全（2026-08-13 提速计划书 §4.1）。

    设计要点（深模块：调用方只给代码，其余全藏内部）：
    - 三源并发：各起 1 个 daemon 线程（复用 _call_with_timeout 的线程哲学——
      daemon 泄漏可接受，主进程退出不等它；刻意不用 ThreadPoolExecutor：
      其 worker 非 daemon，atexit 会 join 挂死线程，进程退出反被卡住）；
    - fail-soft 三保险：① 每源内部已有 try/except（挂标【缺】）；
      ② 外层 per-source 超时（默认 60s：内部各源 _call_with_timeout 最坏
      串行 ~40s+，30s 会误判慢源死刑，故放宽）；③ 超时/异常只丢该源；
    - per_source_timeout 做成参数：测试传小值，不用真等 60s；
    - 输出末尾附成文模板（templates/stock_diagnosis.md），LLM 看数据即拿
      成文结构，省一次模板 Read 往返；模板读不到就静默跳过（不因此挂掉取数）。
    """
    import threading
    c = _norm_code(code)
    if len(c) != 6 or not c.isdigit():
        return f"【错】无法识别股票代码: {code!r}（要 6 位数字，如 300750 或 300750.SZ）"
    sources = (("技术面", stock_technicals),
               ("估值", stock_fundamentals),
               ("舆情", stock_notices))
    boxes: list[dict] = [{} for _ in sources]

    def _run(i: int, fn) -> None:
        try:
            boxes[i]["v"] = fn(c)
        except Exception as e:
            boxes[i]["e"] = e

    threads = [threading.Thread(target=_run, args=(i, fn), daemon=True)
               for i, (_, fn) in enumerate(sources)]
    for t in threads:
        t.start()
    parts = []
    for (title, _), t, box in zip(sources, threads, boxes):
        t.join(per_source_timeout)
        if t.is_alive():
            parts.append(_section(title, f"【缺】数据源超时（>{per_source_timeout:.0f}s）"))
        elif "e" in box:
            parts.append(_section(title, f"【缺】{box['e']}"))
        else:
            parts.append(box["v"])
    pack = f"# {c} 个股诊断数据包\n\n" + "\n\n".join(parts)
    try:  # 附成文模板（读不到不影响数据包）
        tpl = (project_root() / "brain" / "templates" / "stock_diagnosis.md"
               ).read_text(encoding="utf-8").strip()
        if tpl:
            pack += f"\n\n---\n\n## 成文模板（按此结构成文，无需再 Read 模板文件）\n\n{tpl}"
    except Exception:
        pass
    return pack


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdout()  # Windows GBK 控制台打 emoji/特殊字符会炸
    ap = argparse.ArgumentParser(prog="python -m brain.data_tools",
                                 description="对话大脑统一取数工具层")
    ap.add_argument("cmd", choices=["market_snapshot", "market_health",
                                    "stock_news", "zt_pool",
                                    "stock_technicals", "stock_fundamentals",
                                    "stock_notices", "stock_diagnosis"])
    ap.add_argument("arg", nargs="?", default=None,
                    help="stock_news=条数；zt_pool=YYYYMMDD；"
                         "stock_technicals/stock_fundamentals/stock_notices/"
                         "stock_diagnosis=股票代码；"
                         "market_snapshot=YYYYMMDD（缺省今天）")
    ap.add_argument("--fresh", action="store_true",
                    help="market_snapshot 绕过 2 小时缓存强制重取")
    args = ap.parse_args(argv)
    if args.cmd == "market_snapshot":
        print(market_snapshot(args.arg, fresh=args.fresh))
    elif args.cmd == "market_health":
        print(market_health())
    elif args.cmd == "stock_news":
        print(stock_news(int(args.arg) if args.arg else 15))
    elif args.cmd == "zt_pool":
        print(zt_pool(args.arg))
    elif args.cmd == "stock_diagnosis":
        if not args.arg:
            ap.error("stock_diagnosis 需要股票代码")
        print(stock_diagnosis(args.arg))
    elif args.cmd == "stock_notices":
        if not args.arg:
            ap.error("stock_notices 需要股票代码")
        print(stock_notices(args.arg))
    elif args.cmd == "stock_technicals":
        if not args.arg:
            ap.error("stock_technicals 需要股票代码")
        print(stock_technicals(args.arg))
    else:
        if not args.arg:
            ap.error("stock_fundamentals 需要股票代码")
        print(stock_fundamentals(args.arg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
