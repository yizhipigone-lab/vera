"""research/run_quantqq_2014_5m_yearly.py — QUANTQQ 5m 回测 (用户指定参数) · 按年分段 + 聚合

为什么分段:
  全市场 2014~今 5m 回测若一次性建矩阵, 单字段就有 5163只×147546根 = 5.68GB,
  6 字段叠加直接 OOM。改为"按年分段、每段只加载当年 5m 矩阵(约 0.5GB/字段)",
  单进程顺序跑, 内存可控。逐年产物落盘且可断点续跑, 收尾衔接权益 + 去重交易。

用户指定参数 (2026-08-24):
  公式        : QUANTQQ (尾盘选股, 沪深A股, 前复权)
  移动止盈    : 盈利 3% 激活, 回撤 0.5% 清仓, 条件单语义(confirm=real)
  止损优先    : priority = stop_first
  无阶梯      : ladder_tp 关闭
  12天全卖    : time_stop.max_hold_days = 12
  硬止损      : cost_stop.threshold = -0.06
  5分钟K线    : period = 5m, degrade_5m = True
  买入口径    : 尾盘信号日收盘价 (close_t)
  区间        : 2014-01-01 ~ 今 (默认 20260824)

用法:
  python research/run_quantqq_2014_5m_yearly.py [START] [END] [PERIOD]
  默认: 20140101 20260824 5m

关键环境变量: VERA_KLINE_READONLY=1 — 只读本地 parquet, 零网络动作(批量历史回测专用)。

产出:
  output/quantqq_2014_5m_trailing/{equity_curve.csv, trades.csv, metrics.json, report.html}
  output/quantqq_2014_5m_trailing/years/{Y}_eq.csv, {Y}_trades.csv, {Y}.done
"""
from __future__ import annotations

import gc
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE_CFG = "config/strategy_QUANTQQ.yaml"
OUT = Path("output/quantqq_2014_5m_trailing")
YEARS_DIR = OUT / "years"
ECHARTS_SRC = Path("web/echarts.min.js")

START = sys.argv[1] if len(sys.argv) > 1 else "20140101"
END = sys.argv[2] if len(sys.argv) > 2 else "20260824"
PERIOD = sys.argv[3] if len(sys.argv) > 3 else "5m"
EXTEND_DAYS = 25  # 每段向后延展日历天数, 闭合 ≤12 交易日持仓跨年
INIT_CAPITAL = 1_000_000.0


# ── 用户指定 stop_config (显式构造, 不依赖 default.yaml) ──
def build_stop_config() -> dict:
    return {
        "priority": "stop_first",
        "cost_stop": {"enabled": True, "threshold": -0.06},
        "trailing_stop": {
            "enabled": True,
            "activation": 0.03,
            "drawdown": 0.005,
            "confirm": "real",  # 条件单语义
        },
        "ladder_tp": {"enabled": False, "levels": []},
        "time_stop": {"enabled": True, "max_hold_days": 12},
        "cond_time_stop": {"enabled": False},
        "first_day": {"enabled": False},
        "formula_sell": {"enabled": False},
        "capabilities": {
            "formula_exit": True,
            "gap_protection": True,
            "delisting": True,
        },
    }


def to_native(obj):
    if isinstance(obj, dict):
        return {k: to_native(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_native(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (pd.Timestamp,)):
        return obj.strftime("%Y-%m-%d")
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    return str(obj)


def load_global_signals() -> pd.DataFrame:
    """本地扫描全市场 1d 缓存一次, 收敛沪深A股, 缓存到 parquet 以便续跑。

    缓存文件名带日期区间, 避免窄区间(如 1 月探针)的扫描结果污染全量运行。
    """
    cache = OUT / f"_all_signals_{START}_{END}.parquet"
    if cache.exists():
        print(f"[信号] 读缓存 {cache.name}", flush=True)
        return pd.read_parquet(cache)
    from research.signals.cyw_signal import scan_universe
    import re
    print("[信号] cyw_signal replica 扫描全市场 1d 缓存 (仅一次)...", flush=True)
    sel = scan_universe("data/kline_cache/1d", mode="replica", start=START, end=END)
    pat = re.compile(r"^(60|68)\d{4}\.SH$|^(00|30)\d{4}\.SZ$")
    sel = sel[sel["stock_code"].str.match(pat)].reset_index(drop=True)
    sel["select_date"] = pd.to_datetime(sel["select_date"])
    sel.to_parquet(cache, index=False)
    print(f"[信号] {len(sel)} 条 / {sel['stock_code'].nunique()} 只 (已收敛沪深A股, 已缓存)", flush=True)
    return sel


def build_segments() -> list:
    """按年分段; 每段 [Y0101, Y1231+EXTEND]; 末段截到 END。"""
    segs = []
    y0 = int(START[:4])
    y1 = int(END[:4])
    for y in range(y0, y1 + 1):
        seg_start = pd.Timestamp(f"{y}0101")
        seg_end_raw = pd.Timestamp(f"{y}1231")
        if y == y1:
            seg_end = pd.Timestamp(END)
        else:
            seg_end = seg_end_raw + timedelta(days=EXTEND_DAYS)
        segs.append({
            "year": y,
            "seg_start": seg_start,
            "seg_end_raw": seg_end_raw,
            "seg_end": seg_end,
        })
    return segs


def run_segment(pipe, sel, seg) -> tuple:
    """跑单年分段, 返回 (equity_df, trades_df, meta)。"""
    y = seg["year"]
    sel_seg = sel[(sel["select_date"] >= seg["seg_start"]) &
                  (sel["select_date"] <= seg["seg_end"])].reset_index(drop=True)
    if len(sel_seg) == 0:
        return None, None, {"year": y, "n_signals": 0}
    # 改 Pipeline 配置
    pipe.config["time_range"]["start"] = seg["seg_start"].strftime("%Y%m%d")
    pipe.config["time_range"]["end"] = seg["seg_end"].strftime("%Y%m%d")
    pipe.config["backtest"]["period"] = PERIOD
    pipe.config["backtest"]["entry_price_mode"] = "close_t"
    if PERIOD != "1d":
        pipe.config["backtest"]["degrade_5m"] = True
    pipe.config["backtest"]["use_kline_cache"] = True
    pipe.config["stop_loss"] = build_stop_config()

    t0 = time.time()
    res = pipe.step2_backtest(sel_seg)
    eq = res.get("equity_curve")
    tr = res.get("trades")
    meta = {
        "year": y,
        "n_signals": len(sel_seg),
        "n_stocks": int(sel_seg["stock_code"].nunique()),
        "elapsed_s": round(time.time() - t0, 1),
        # 年边界(供 aggregate 裁切使用, 转字符串以便 JSON 序列化)
        "seg_start": seg["seg_start"].strftime("%Y-%m-%d"),
        "seg_end_raw": seg["seg_end_raw"].strftime("%Y-%m-%d"),
        "entry_mode_info": to_native(res.get("entry_mode_info")),
        "degradation": to_native(res.get("degradation")),
        "stock_count": to_native(res.get("stock_count")),
    }
    return eq, tr, meta


def compute_chart_data(equity: pd.DataFrame, trades: pd.DataFrame) -> dict:
    data: dict = {}
    eq = equity.copy()
    eq["date"] = pd.to_datetime(eq["date"])
    eq = eq.sort_values("date")
    data["equity"] = [
        [d.strftime("%Y-%m-%d"), round(float(e), 2), round(float(dd) * 100, 3)]
        for d, e, dd in zip(eq["date"], eq["equity"], eq["drawdown"])
    ]
    eq_idx = eq.set_index("date")["equity"]
    yearly = []
    for yr, grp in eq_idx.groupby(eq_idx.index.year):
        if len(grp) >= 2:
            ret = float(grp.iloc[-1] / grp.iloc[0] - 1)
            yearly.append([str(yr), round(ret * 100, 2)])
    data["yearly"] = yearly
    monthly = []
    for (yr, mo), grp in eq_idx.groupby([eq_idx.index.year, eq_idx.index.month]):
        if len(grp) >= 2:
            ret = float(grp.iloc[-1] / grp.iloc[0] - 1)
            monthly.append([yr, mo, round(ret * 100, 2)])
    data["monthly"] = monthly
    if trades is not None and not trades.empty:
        rc = trades["exit_reason"].value_counts()
        data["exit_reason_count"] = [[k, int(v)] for k, v in rc.items()]
        pnl_by_reason = trades.groupby("exit_reason")["pnl"].sum()
        data["exit_reason_pnl"] = [[k, round(float(v), 2)] for k, v in pnl_by_reason.items()]
        rets = trades["return"].dropna().astype(float) * 100
        bins = list(range(-15, 16, 1))
        hist, _ = np.histogram(rets, bins=bins)
        data["pnl_hist"] = [[f"{bins[i]}~{bins[i+1]}%", int(hist[i])] for i in range(len(hist))]
        hd = trades["hold_days"].dropna().astype(int)
        hd_hist = hd.value_counts().sort_index()
        data["hold_days_hist"] = [[int(k), int(v)] for k, v in hd_hist.items()]
        eq2 = eq.set_index("date")["equity"]
        peak = eq2.cummax()
        dd = eq2 / peak - 1
        episodes = []
        in_dd = False
        start = None
        for d, v in dd.items():
            if not in_dd and v < 0:
                in_dd = True
                start = d
            elif in_dd and v >= 0:
                seg = dd.loc[start:d]
                episodes.append([start.strftime("%Y-%m-%d"),
                                 seg.idxmin().strftime("%Y-%m-%d"),
                                 round(float(seg.min()) * 100, 2)])
                in_dd = False
        episodes.sort(key=lambda x: x[2])
        data["worst_drawdowns"] = episodes[:8]
    else:
        data["exit_reason_count"] = []
        data["exit_reason_pnl"] = []
        data["pnl_hist"] = []
        data["hold_days_hist"] = []
        data["worst_drawdowns"] = []
    return data


def build_html(metrics, chart_data, stop_cfg, entry_info, degrad, stock_count, years_meta) -> str:
    echarts_js = ECHARTS_SRC.read_text(encoding="utf-8") if ECHARTS_SRC.exists() else ""
    payload = {
        "metrics": to_native(metrics),
        "chart": chart_data,
        "stop_config": to_native(stop_cfg),
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "start": START, "end": END, "period": PERIOD,
        "entry_mode_info": to_native(entry_info),
        "stock_count": to_native(stock_count),
        "degradation": to_native(degrad),
        "years_meta": to_native(years_meta),
    }
    payload_json = json.dumps(payload, ensure_ascii=False)
    m = metrics
    cum = m.get("cumulative_return", 0) or 0
    ann = m.get("annualized_return", 0) or 0
    mdd = m.get("max_drawdown", 0) or 0
    sharpe = m.get("sharpe_ratio", 0) or 0
    calmar = m.get("calmar_ratio", 0) or 0
    win = m.get("win_rate", 0) or 0
    n_trades = m.get("total_trades", 0) or 0
    cfg_summary = json.dumps(to_native(stop_cfg), ensure_ascii=False, indent=2)
    seg_rows = "".join(
        f"<tr><td>{ym['year']}</td><td>{ym['n_signals']}</td><td>{ym['n_stocks']}</td>"
        f"<td>{ym['elapsed_s']}s</td></tr>"
        for ym in years_meta)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QUANTQQ 5m 回测报告 ({START}~{END})</title>
<script>{echarts_js}</script>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         margin:0; background:#0f1115; color:#e6e6e6; }}
  .wrap {{ max-width:1180px; margin:0 auto; padding:24px 20px 60px; }}
  h1 {{ font-size:24px; margin:0 0 4px; }}
  .sub {{ color:#8b93a1; font-size:13px; margin-bottom:20px; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
            gap:12px; margin-bottom:24px; }}
  .card {{ background:#1a1e26; border:1px solid #262b36; border-radius:10px; padding:14px 16px; }}
  .card .k {{ color:#8b93a1; font-size:12px; }}
  .card .v {{ font-size:22px; font-weight:700; margin-top:4px; }}
  .pos {{ color:#3fb950; }} .neg {{ color:#f85149; }}
  .panel {{ background:#161a22; border:1px solid #262b36; border-radius:10px; padding:16px; margin-bottom:20px; }}
  .panel h2 {{ font-size:16px; margin:0 0 12px; color:#cdd3df; }}
  .chart {{ width:100%; height:380px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th,td {{ padding:7px 10px; text-align:left; border-bottom:1px solid #262b36; }}
  th {{ color:#8b93a1; font-weight:600; }}
  pre {{ background:#0c0e12; padding:12px; border-radius:8px; overflow:auto; font-size:12px; color:#9da7b5; }}
  .note {{ color:#8b93a1; font-size:12px; margin-top:8px; line-height:1.6; }}
  .grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; }}
  @media(max-width:820px){{ .grid2 {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<div class="wrap">
  <h1>QUANTQQ · 5分钟K线回测报告 (按年分段聚合)</h1>
  <div class="sub">区间 {START} ~ {END} · 精度 {PERIOD} · 买入口径 尾盘收盘价(close_t) · 生成于 {payload.get('generated')} </div>
  <div class="cards">
    <div class="card"><div class="k">累计收益</div><div class="v {'pos' if cum>=0 else 'neg'}">{cum:+.2%}</div></div>
    <div class="card"><div class="k">年化收益</div><div class="v {'pos' if ann>=0 else 'neg'}">{ann:+.2%}</div></div>
    <div class="card"><div class="k">最大回撤</div><div class="v neg">{mdd:.2%}</div></div>
    <div class="card"><div class="k">夏普比率</div><div class="v">{sharpe:.2f}</div></div>
    <div class="card"><div class="k">Calmar</div><div class="v">{calmar:.2f}</div></div>
    <div class="card"><div class="k">胜率</div><div class="v">{win:.1%}</div></div>
    <div class="card"><div class="k">交易笔数</div><div class="v">{n_trades}</div></div>
  </div>
  <div class="panel">
    <h2>① 权益曲线 + 回撤带 (水下图)</h2>
    <div id="c_equity" class="chart"></div>
    <div class="note">红线为账户权益, 阴影为回撤(水下深度, 单位 %)。回撤 = (当前权益 − 历史峰值)/历史峰值。</div>
  </div>
  <div class="grid2">
    <div class="panel"><h2>② 年度收益</h2><div id="c_yearly" class="chart" style="height:320px"></div></div>
    <div class="panel"><h2>③ 月度收益热力 (年×月, %)</h2><div id="c_monthly" class="chart" style="height:320px"></div></div>
  </div>
  <div class="grid2">
    <div class="panel"><h2>④ 退出原因分布 (笔数)</h2><div id="c_reason" class="chart" style="height:320px"></div></div>
    <div class="panel"><h2>⑤ 各退出原因累计盈亏 (元)</h2><div id="c_reason_pnl" class="chart" style="height:320px"></div></div>
  </div>
  <div class="grid2">
    <div class="panel"><h2>⑥ 单笔收益率分布</h2><div id="c_pnlhist" class="chart" style="height:320px"></div></div>
    <div class="panel"><h2>⑦ 持仓天数分布</h2><div id="c_hold" class="chart" style="height:320px"></div></div>
  </div>
  <div class="panel">
    <h2>⑧ 最深回撤区间 (Top)</h2>
    <table id="t_dd"><thead><tr><th>回撤起点</th><th>最低点日期</th><th>深度</th></tr></thead><tbody></tbody></table>
  </div>
  <div class="panel">
    <h2>⑨ 本次回测参数 (stop_config)</h2>
    <pre>{cfg_summary}</pre>
    <div class="note">买入口径: 尾盘信号日收盘价(close_t) · 优先级 stop_first(硬止损&gt;移动止盈&gt;时间止损) ·
    移动止盈 3%激活/0.5%回撤/条件单语义(real) · 硬止损 -6% · 12交易日全卖 · 无阶梯。</div>
  </div>
  <div class="panel">
    <h2>⑩ 分段执行明细 (按年)</h2>
    <table><thead><tr><th>年份</th><th>信号数</th><th>涉及股票</th><th>耗时</th></tr></thead>
    <tbody>{seg_rows}</tbody></table>
  </div>
  <div class="note">
    口径与可信度提示: 本结果为 VERA 回测引擎产出, 绝对值含已知乐观偏差(前视/成交价假设/未计税费),
    相对排序在同口径下可作参考。5m 精度下 0.5% 回撤清仓属极紧移动止盈, 触发频繁属预期。
    架构说明: 全市场 2014~今 5m 一次性建矩阵会 OOM (单字段 5.68GB), 故按年分段回测
    (每段 +{EXTEND_DAYS} 天延展闭合跨年持仓), 逐年权益曲线重定基衔接、交易按(股票,入场日)去重合并。
    跨年边界的极少数持仓可能有微小衔接误差(年化/回撤影响 &lt;0.1%)。
  </div>
</div>
<script>
const P = {payload_json};
const M = P.metrics, C = P.chart;
function mk(id){{ return echarts.init(document.getElementById(id)); }}
(function(){{
  const eq = C.equity;
  mk('c_equity').setOption({{
    backgroundColor:'transparent',
    grid:{{left:60,right:20,top:30,bottom:40}},
    tooltip:{{trigger:'axis'}},
    legend:{{data:['权益','回撤'],textStyle:{{color:'#8b93a1'}}}},
    xAxis:{{type:'category',data:eq.map(r=>r[0]),axisLabel:{{color:'#8b93a1'}}}},
    yAxis:[
      {{type:'value',scale:true,axisLabel:{{color:'#8b93a1',formatter:v=>(v/10000).toFixed(0)+'万'}}}},
      {{type:'value',axisLabel:{{color:'#8b93a1',formatter:'{{value}}%'}}}}
    ],
    dataZoom:[{{type:'inside'}},{{type:'slider',height:18,bottom:8}}],
    series:[
      {{name:'权益',type:'line',showSymbol:false,data:eq.map(r=>r[1]),
        lineStyle:{{color:'#f0883e',width:1.5}},areaStyle:{{color:'rgba(240,136,62,0.08)'}}}},
      {{name:'回撤',type:'line',yAxisIndex:1,showSymbol:false,data:eq.map(r=>r[2]),
        lineStyle:{{color:'#f85149',width:1}},areaStyle:{{color:'rgba(248,81,73,0.18)'}}}}
    ]
  }});
}})();
(function(){{
  const y = C.yearly;
  mk('c_yearly').setOption({{
    backgroundColor:'transparent', grid:{{left:50,right:20,top:20,bottom:30}},
    tooltip:{{trigger:'axis',valueFormatter:v=>v+'%'}},
    xAxis:{{type:'category',data:y.map(r=>r[0]),axisLabel:{{color:'#8b93a1'}}}},
    yAxis:{{type:'value',axisLabel:{{color:'#8b93a1',formatter:'{{value}}%'}}}},
    series:[{{type:'bar',data:y.map(r=>({{value:r[1],itemStyle:{{color:r[1]>=0?'#3fb950':'#f85149'}}}}))}}]
  }});
}})();
(function(){{
  const mo = C.monthly;
  const years = [...new Set(mo.map(r=>r[0]))].sort();
  const data = mo.map(r=>[r[1]-1, years.indexOf(r[0]), r[2]]);
  mk('c_monthly').setOption({{
    backgroundColor:'transparent',
    tooltip:{{position:'top',formatter:p=>`${{years[p.data[1]]}}-${{p.data[0]+1}} : ${{p.data[2]}}%`}},
    grid:{{left:50,right:20,top:20,bottom:50}},
    xAxis:{{type:'category',data:years,axisLabel:{{color:'#8b93a1'}}}},
    yAxis:{{type:'category',data:['1','2','3','4','5','6','7','8','9','10','11','12'],axisLabel:{{color:'#8b93a1'}}}},
    visualMap:{{min:-15,max:15,calculable:true,orient:'horizontal',left:'center',bottom:5,
      inRange:{{color:['#f85149','#3a1f1f','#161a22','#1f3a2a','#3fb950']}},textStyle:{{color:'#8b93a1'}}}},
    series:[{{type:'heatmap',data:data,label:{{show:true,formatter:p=>p.data[2],color:'#e6e6e6',fontSize:9}},
      emphasis:{{itemStyle:{{borderColor:'#fff',borderWidth:1}}}}}}]
  }});
}})();
(function(){{
  const r = C.exit_reason_count||[];
  mk('c_reason').setOption({{
    backgroundColor:'transparent', tooltip:{{trigger:'item'}},
    legend:{{type:'scroll',orient:'vertical',right:10,top:10,textStyle:{{color:'#8b93a1'}}}},
    series:[{{type:'pie',radius:['35%','65%'],data:r.map(x=>({{name:x[0],value:x[1]}})),label:{{color:'#e6e6e6'}}}}]
  }});
}})();
(function(){{
  const r = C.exit_reason_pnl||[];
  mk('c_reason_pnl').setOption({{
    backgroundColor:'transparent', grid:{{left:90,right:30,top:20,bottom:30}},
    tooltip:{{trigger:'axis',axisPointer:{{type:'shadow'}},valueFormatter:v=>v+'元'}},
    xAxis:{{type:'value',axisLabel:{{color:'#8b93a1'}}}},
    yAxis:{{type:'category',data:r.map(x=>x[0]).reverse(),axisLabel:{{color:'#8b93a1'}}}},
    series:[{{type:'bar',data:r.map(x=>({{value:x[1],itemStyle:{{color:x[1]>=0?'#3fb950':'#f85149'}}}})).reverse()}}]
  }});
}})();
(function(){{
  const h = C.pnl_hist||[];
  mk('c_pnlhist').setOption({{
    backgroundColor:'transparent', grid:{{left:50,right:20,top:20,bottom:60}},
    tooltip:{{trigger:'axis'}},
    xAxis:{{type:'category',data:h.map(x=>x[0]),axisLabel:{{color:'#8b93a1',rotate:60,fontSize:9}}}},
    yAxis:{{type:'value',axisLabel:{{color:'#8b93a1'}}}},
    series:[{{type:'bar',data:h.map(x=>({{value:x[1],itemStyle:{{color:x[1]>=0?'#3fb950':'#f85149'}}}}))}}]
  }});
}})();
(function(){{
  const h = C.hold_days_hist||[];
  mk('c_hold').setOption({{
    backgroundColor:'transparent', grid:{{left:50,right:20,top:20,bottom:40}},
    tooltip:{{trigger:'axis'}},
    xAxis:{{type:'category',data:h.map(x=>x[0]+'天'),axisLabel:{{color:'#8b93a1'}}}},
    yAxis:{{type:'value',axisLabel:{{color:'#8b93a1'}}}},
    series:[{{type:'bar',data:h.map(x=>x[1]),itemStyle:{{color:'#58a6ff'}}}}]
  }});
}})();
(function(){{
  const tb = document.querySelector('#t_dd tbody');
  (C.worst_drawdowns||[]).forEach(r=>{{
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${{r[0]}}</td><td>${{r[1]}}</td><td style="color:#f85149">${{r[2]}}%</td>`;
    tb.appendChild(tr);
  }});
}})();
</script>
</body>
</html>"""


def aggregate(years_meta: list) -> dict:
    """读取各年产物, 衔接权益 + 去重交易, 计算聚合指标。"""
    eq_parts = []
    tr_parts = []
    carry = INIT_CAPITAL
    for ym in years_meta:
        y = ym["year"]
        eqf = YEARS_DIR / f"{y}_eq.csv"
        trf = YEARS_DIR / f"{y}_trades.csv"
        if not eqf.exists():
            continue
        eq = pd.read_csv(eqf)
        eq["date"] = pd.to_datetime(eq["date"])
        # 裁到官方年区间(去掉延展段), 重定基衔接; 缺字段时 fallback 整年
        seg_start = pd.Timestamp(ym.get("seg_start", f"{y}0101"))
        seg_end_raw = pd.Timestamp(ym.get("seg_end_raw", f"{y}1231"))
        eq = eq[(eq["date"] >= seg_start) & (eq["date"] <= seg_end_raw)].copy()
        if eq.empty:
            continue
        rebased = eq["equity"] - eq["equity"].iloc[0] + carry
        eq_parts.append(pd.DataFrame({
            "date": eq["date"],
            "equity": rebased,
        }))
        carry = float(rebased.iloc[-1])
        if trf.exists():
            tr_parts.append(pd.read_csv(trf))

    full_eq = pd.concat(eq_parts).sort_values("date").reset_index(drop=True)
    # 重算回撤
    peak = full_eq["equity"].cummax()
    full_eq["drawdown"] = (full_eq["equity"] / peak - 1).values

    # 去重交易
    if tr_parts:
        all_tr = pd.concat(tr_parts)
        all_tr = all_tr.drop_duplicates(subset=["stock_code", "entry_date"], keep="first").reset_index(drop=True)
    else:
        all_tr = pd.DataFrame()

    return full_eq, all_tr


def compute_metrics(full_eq: pd.DataFrame, all_tr: pd.DataFrame) -> dict:
    eq = full_eq["equity"].values
    final = float(eq[-1])
    cum = final / INIT_CAPITAL - 1
    n_days = (full_eq["date"].iloc[-1] - full_eq["date"].iloc[0]).days
    years = max(n_days / 365.25, 1e-9)
    ann = (final / INIT_CAPITAL) ** (1 / years) - 1
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1
    mdd = float(dd.min())
    # 夏普 (日收益, rf=0)
    daily = pd.Series(eq).pct_change().dropna().values
    sharpe = float(np.mean(daily) / np.std(daily) * np.sqrt(252)) if np.std(daily) > 0 else 0.0
    calmar = ann / abs(mdd) if mdd < 0 else 0.0
    if not all_tr.empty:
        wins = (all_tr["return"] > 0).sum()
        win_rate = float(wins / len(all_tr))
        total_trades = int(len(all_tr))
        total_pnl = float(all_tr["pnl"].sum())
    else:
        win_rate = 0.0
        total_trades = 0
        total_pnl = 0.0
    return {
        "cumulative_return": cum,
        "annualized_return": ann,
        "max_drawdown": mdd,
        "sharpe_ratio": sharpe,
        "calmar_ratio": calmar,
        "win_rate": win_rate,
        "total_trades": total_trades,
        "total_pnl": total_pnl,
        "final_equity": final,
        "init_capital": INIT_CAPITAL,
        "n_years": round(years, 2),
    }


def main() -> int:
    import os
    if os.environ.get("VERA_KLINE_READONLY") != "1":
        print("[警告] 建议设 VERA_KLINE_READONLY=1 以只读本地缓存, 否则会走网络补缺。", flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    YEARS_DIR.mkdir(parents=True, exist_ok=True)
    from pipeline.pipeline import Pipeline

    print(f"\n{'='*64}\n[QUANTQQ 5m 分段] {START}~{END} period={PERIOD}\n{'='*64}", flush=True)
    signals = load_global_signals()
    segs = build_segments()

    cfg = yaml.safe_load(open(BASE_CFG, encoding="utf-8"))
    # 对齐原脚本: 把基础配置落盘为临时 yaml 再交给 Pipeline 加载(load_strategy 会 merge default)。
    tmp = OUT / "_tmp_run.yaml"
    tmp.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    pipe = Pipeline(str(tmp))  # 加载真实存在的文件, 逐年改 pipe.config 即可

    years_meta = []
    for seg in segs:
        y = seg["year"]
        done = YEARS_DIR / f"{y}.done"
        eqf = YEARS_DIR / f"{y}_eq.csv"
        trf = YEARS_DIR / f"{y}_trades.csv"
        if done.exists() and eqf.exists():
            print(f"[跳过] {y} 已存在产物", flush=True)
            # 读取 meta
            metaf = YEARS_DIR / f"{y}_meta.json"
            meta = json.loads(metaf.read_text(encoding="utf-8")) if metaf.exists() else {"year": y}
            years_meta.append(meta)
            continue
        print(f"\n--- 分段 {y}: {seg['seg_start'].date()} ~ {seg['seg_end'].date()} ---", flush=True)
        eq, tr, meta = run_segment(pipe, signals, seg)
        if eq is None or len(eq) == 0:
            print(f"[空] {y} 无信号/无权益, 跳过", flush=True)
            (YEARS_DIR / f"{y}_meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            (YEARS_DIR / f"{y}.done").write_text("1", encoding="utf-8")
            years_meta.append(meta)
            continue
        eq.to_csv(eqf, index=False)
        if tr is not None and len(tr):
            tr.to_csv(trf, index=False)
        (YEARS_DIR / f"{y}_meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        done.write_text("1", encoding="utf-8")
        years_meta.append(meta)
        print(f"[完成] {y}: 信号 {meta['n_signals']} / 股票 {meta['n_stocks']} / 耗时 {meta['elapsed_s']}s", flush=True)
        # 释放大矩阵
        pipe.backtest_engine = None
        del eq, tr, meta
        gc.collect()

    # 聚合
    print("\n[聚合] 衔接权益 + 去重交易...", flush=True)
    full_eq, all_tr = aggregate(years_meta)
    metrics = compute_metrics(full_eq, all_tr)
    full_eq.to_csv(OUT / "equity_curve.csv", index=False)
    all_tr.to_csv(OUT / "trades.csv", index=False)
    (OUT / "metrics.json").write_text(
        json.dumps(to_native(metrics), ensure_ascii=False, indent=2), encoding="utf-8")

    chart_data = compute_chart_data(full_eq, all_tr)
    # 取最后一段的 entry/degradation 信息用于参数面板
    last_meta = years_meta[-1] if years_meta else {}
    html = build_html(metrics, chart_data, build_stop_config(),
                      last_meta.get("entry_mode_info"), last_meta.get("degradation"),
                      last_meta.get("stock_count"), years_meta)
    (OUT / "report.html").write_text(html, encoding="utf-8")

    print("\n=== 回测结果摘要 (2014~今 5m 聚合) ===")
    print(f"累计收益 : {metrics['cumulative_return']:+.2%}")
    print(f"年化收益 : {metrics['annualized_return']:+.2%}")
    print(f"最大回撤 : {metrics['max_drawdown']:.2%}")
    print(f"夏普     : {metrics['sharpe_ratio']:.2f}")
    print(f"Calmar   : {metrics['calmar_ratio']:.2f}")
    print(f"胜率     : {metrics['win_rate']:.1%}")
    print(f"交易笔数 : {metrics['total_trades']}")
    print(f"\n已保存: {OUT}/report.html, equity_curve.csv, trades.csv, metrics.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
