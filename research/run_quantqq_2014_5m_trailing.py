"""research/run_quantqq_2014_5m_trailing.py — QUANTQQ 5m 回测 (用户指定参数) 单跑 + 图文 HTML 报告

用户指定参数 (2026-08-24):
  公式        : QUANTQQ (尾盘选股, 沪深A股 type=50, 排ST, 前复权)
  移动止盈    : 盈利 3% 激活, 回撤 0.5% 清仓, 条件单语义(confirm=real)
  止损优先    : priority = stop_first (硬止损 > 移动止盈 > 时间止损)
  无阶梯      : ladder_tp 关闭
  12天全卖    : time_stop.max_hold_days = 12
  硬止损      : cost_stop.threshold = -0.06
  5分钟K线    : period = 5m, degrade_5m = True
  买入口径    : 尾盘信号日收盘价 (close_t, QUANTQQ 原生)
  区间        : 2014-01-01 ~ 今 (默认 20260824)

用法:
  python research/run_quantqq_2014_5m_trailing.py [START] [END] [PERIOD]
  默认: 20140101 20260824 5m
  (冒烟测试示例: ... 20240101 20240301 5m)

产出:
  output/quantqq_2014_5m_trailing/{equity_curve.csv, trades.csv, metrics.json, report.html}
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE_CFG = "config/strategy_QUANTQQ.yaml"
OUT = Path("output/quantqq_2014_5m_trailing")
ECHARTS_SRC = Path("web/echarts.min.js")

START = sys.argv[1] if len(sys.argv) > 1 else "20140101"
END = sys.argv[2] if len(sys.argv) > 2 else "20260824"
PERIOD = sys.argv[3] if len(sys.argv) > 3 else "5m"


# ── 用户指定 stop_config (显式构造, 不依赖 default.yaml) ──
def build_stop_config() -> dict:
    return {
        "priority": "stop_first",
        "cost_stop": {"enabled": True, "threshold": -0.06},
        "trailing_stop": {
            "enabled": True,
            "activation": 0.03,
            "drawdown": 0.005,
            "confirm": "real",  # 条件单语义: 创新高bar不判/跳空按开盘价/触线按线价
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
    """递归把 numpy / pandas 标量转成 JSON 可序列化原生类型。"""
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


def run_backtest() -> dict:
    from pipeline.pipeline import Pipeline
    from research.signals.cyw_signal import scan_universe
    import re

    cfg = yaml.safe_load(open(BASE_CFG, encoding="utf-8"))
    cfg["time_range"]["start"] = START
    cfg["time_range"]["end"] = END
    cfg["backtest"]["period"] = PERIOD
    cfg["backtest"]["entry_price_mode"] = "close_t"
    if PERIOD != "1d":
        cfg["backtest"]["degrade_5m"] = True
    cfg["stop_loss"] = build_stop_config()

    tmp = OUT / "_tmp_run.yaml"
    tmp.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    print(f"\n{'=' * 64}\n[QUANTQQ 5m] 回测 {START}~{END} period={PERIOD}\n{'=' * 64}", flush=True)

    # 本地 QUANTQQ 信号 (cyw_signal replica = 复刻 TDX 官方带未来函数信号)。
    # 绕开 signal_day_cache 的 60天TTL: TDX 逐交易日重选 2014~今需按小时计且依赖 TDX 长稳。
    # 本地扫描只读一遍 1d 缓存 (~70s, 与区间长短无关), 同一套 5m 回测引擎原样生效。
    print("[本地选股] cyw_signal replica 扫描全市场 1d 缓存...", flush=True)
    sel = scan_universe("data/kline_cache/1d", mode="replica", start=START, end=END)
    # 收敛到沪深A股 (近似 TDX type=50: 沪市 60/68 + 深市 00/30, 排除指数/ETF/转债/北交/B股)
    pat = re.compile(r"^(60|68)\d{4}\.SH$|^(00|30)\d{4}\.SZ$")
    sel = sel[sel["stock_code"].str.match(pat)].reset_index(drop=True)
    print(f"[本地选股] 信号 {len(sel)} 条 / {sel['stock_code'].nunique()} 只 (已收敛沪深A股)", flush=True)

    pipe = Pipeline(str(tmp))
    bt = pipe.step2_backtest(sel)
    return {
        "metrics": dict(bt.get("metrics", {}) or {}),
        "equity_curve": bt.get("equity_curve"),
        "trades": bt.get("trades"),
        "stop_config_summary": bt.get("stop_config_summary", ""),
        "degradation": bt.get("degradation"),
        "open_positions": bt.get("open_positions"),
        "entry_mode_info": bt.get("entry_mode_info"),
        "stock_count": bt.get("stock_count"),
    }


def compute_chart_data(equity: pd.DataFrame, trades: pd.DataFrame) -> dict:
    """从 equity/trades 计算图表所需的聚合数据。"""
    data: dict = {}

    # 权益 + 回撤序列
    eq = equity.copy()
    eq["date"] = pd.to_datetime(eq["date"])
    eq = eq.sort_values("date")
    data["equity"] = [
        [d.strftime("%Y-%m-%d"), round(float(e), 2), round(float(dd) * 100, 3)]
        for d, e, dd in zip(eq["date"], eq["equity"], eq["drawdown"])
    ]

    # 年度收益
    eq_idx = eq.set_index("date")["equity"]
    yearly = []
    for yr, grp in eq_idx.groupby(eq_idx.index.year):
        if len(grp) >= 2:
            ret = float(grp.iloc[-1] / grp.iloc[0] - 1)
            yearly.append([str(yr), round(ret * 100, 2)])
    data["yearly"] = yearly

    # 月度收益 (heatmap: [year, month, ret%])
    monthly = []
    for (yr, mo), grp in eq_idx.groupby([eq_idx.index.year, eq_idx.index.month]):
        if len(grp) >= 2:
            ret = float(grp.iloc[-1] / grp.iloc[0] - 1)
            monthly.append([yr, mo, round(ret * 100, 2)])
    data["monthly"] = monthly

    # 退出原因分布
    if trades is not None and not trades.empty:
        rc = trades["exit_reason"].value_counts()
        data["exit_reason_count"] = [[k, int(v)] for k, v in rc.items()]
        pnl_by_reason = trades.groupby("exit_reason")["pnl"].sum()
        data["exit_reason_pnl"] = [[k, round(float(v), 2)] for k, v in pnl_by_reason.items()]

        # 单笔盈亏分布 (bins of 1% on return)
        rets = trades["return"].dropna().astype(float) * 100
        bins = list(range(-15, 16, 1))
        hist, _ = np.histogram(rets, bins=bins)
        data["pnl_hist"] = [[f"{bins[i]}~{bins[i+1]}%", int(hist[i])] for i in range(len(hist))]

        # 持仓天数分布
        hd = trades["hold_days"].dropna().astype(int)
        hd_hist = hd.value_counts().sort_index()
        data["hold_days_hist"] = [[int(k), int(v)] for k, v in hd_hist.items()]

        # 最差回撤区间 (水下段)
        eq2 = eq.set_index("date")["equity"]
        peak = eq2.cummax()
        dd = eq2 / peak - 1
        # 找回撤段: 从新峰值开始到新峰值结束
        episodes = []
        in_dd = False
        start = None
        peak_start = None
        for d, v in dd.items():
            if not in_dd and v < 0:
                in_dd = True
                start = d
            elif in_dd and v >= 0:
                # 结束于 d 之前
                seg = dd.loc[start:d]
                trough_date = seg.idxmin()
                depth = float(seg.min())
                episodes.append([start.strftime("%Y-%m-%d"),
                                 trough_date.strftime("%Y-%m-%d"),
                                 round(depth * 100, 2)])
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


def build_html(metrics: dict, chart_data: dict, stop_cfg: dict,
               entry_info, degrad, stock_count) -> str:
    echarts_js = ""
    if ECHARTS_SRC.exists():
        echarts_js = ECHARTS_SRC.read_text(encoding="utf-8")
    payload = {
        "metrics": to_native(metrics),
        "chart": chart_data,
        "stop_config": to_native(stop_cfg),
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "start": START, "end": END, "period": PERIOD,
        "entry_mode_info": to_native(entry_info) if entry_info is not None else None,
        "stock_count": to_native(stock_count),
        "degradation": to_native(degrad) if degrad is not None else None,
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
  .card {{ background:#1a1e26; border:1px solid #262b36; border-radius:10px;
           padding:14px 16px; }}
  .card .k {{ color:#8b93a1; font-size:12px; }}
  .card .v {{ font-size:22px; font-weight:700; margin-top:4px; }}
  .pos {{ color:#3fb950; }} .neg {{ color:#f85149; }}
  .panel {{ background:#161a22; border:1px solid #262b36; border-radius:10px;
            padding:16px; margin-bottom:20px; }}
  .panel h2 {{ font-size:16px; margin:0 0 12px; color:#cdd3df; }}
  .chart {{ width:100%; height:380px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th,td {{ padding:7px 10px; text-align:left; border-bottom:1px solid #262b36; }}
  th {{ color:#8b93a1; font-weight:600; }}
  pre {{ background:#0c0e12; padding:12px; border-radius:8px; overflow:auto;
         font-size:12px; color:#9da7b5; }}
  .note {{ color:#8b93a1; font-size:12px; margin-top:8px; line-height:1.6; }}
  .grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; }}
  @media(max-width:820px){{ .grid2 {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<div class="wrap">
  <h1>QUANTQQ · 5分钟K线回测报告</h1>
  <div class="sub">区间 {START} ~ {END} · 精度 {PERIOD} · 买入口径 尾盘收盘价(close_t) · 生成于 {payload_json and ''} </div>

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
    <div class="panel">
      <h2>② 年度收益</h2>
      <div id="c_yearly" class="chart" style="height:320px"></div>
    </div>
    <div class="panel">
      <h2>③ 月度收益热力 (年×月, %)</h2>
      <div id="c_monthly" class="chart" style="height:320px"></div>
    </div>
  </div>

  <div class="grid2">
    <div class="panel">
      <h2>④ 退出原因分布 (笔数)</h2>
      <div id="c_reason" class="chart" style="height:320px"></div>
    </div>
    <div class="panel">
      <h2>⑤ 各退出原因累计盈亏 (元)</h2>
      <div id="c_reason_pnl" class="chart" style="height:320px"></div>
    </div>
  </div>

  <div class="grid2">
    <div class="panel">
      <h2>⑥ 单笔收益率分布</h2>
      <div id="c_pnlhist" class="chart" style="height:320px"></div>
    </div>
    <div class="panel">
      <h2>⑦ 持仓天数分布</h2>
      <div id="c_hold" class="chart" style="height:320px"></div>
    </div>
  </div>

  <div class="panel">
    <h2>⑧ 最深回撤区间 (Top)</h2>
    <table id="t_dd"><thead><tr><th>回撤起点</th><th>最低点日期</th><th>深度</th></tr></thead>
    <tbody></tbody></table>
  </div>

  <div class="panel">
    <h2>⑨ 本次回测参数 (stop_config)</h2>
    <pre>{cfg_summary}</pre>
    <div class="note">买入口径: 尾盘信号日收盘价(close_t) · 优先级 stop_first(硬止损&gt;移动止盈&gt;时间止损) ·
    移动止盈 3%激活/0.5%回撤/条件单语义(real) · 硬止损 -6% · 12交易日全卖 · 无阶梯。</div>
  </div>

  <div class="note">
    口径与可信度提示: 本结果为 VERA 回测引擎产出, 绝对值含已知乐观偏差(前视/成交价假设/未计税费),
    相对排序在同口径下可作参考。5m 精度下 0.5% 回撤清仓属极紧移动止盈, 触发频繁属预期。
  </div>
</div>

<script>
const P = {payload_json};
const M = P.metrics, C = P.chart;
function mk(id){{ return echarts.init(document.getElementById(id)); }}
// ① 权益 + 回撤
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
// ② 年度
(function(){{
  const y = C.yearly;
  mk('c_yearly').setOption({{
    backgroundColor:'transparent',
    grid:{{left:50,right:20,top:20,bottom:30}},
    tooltip:{{trigger:'axis',valueFormatter:v=>v+'%'}},
    xAxis:{{type:'category',data:y.map(r=>r[0]),axisLabel:{{color:'#8b93a1'}}}},
    yAxis:{{type:'value',axisLabel:{{color:'#8b93a1',formatter:'{{value}}%'}}}},
    series:[{{type:'bar',data:y.map(r=>({{value:r[1],itemStyle:{{color:r[1]>=0?'#3fb950':'#f85149'}}}}))}}]
  }});
}})();
// ③ 月度热力
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
// ④ 退出原因笔数
(function(){{
  const r = C.exit_reason_count||[];
  mk('c_reason').setOption({{
    backgroundColor:'transparent',
    tooltip:{{trigger:'item'}},
    legend:{{type:'scroll',orient:'vertical',right:10,top:10,textStyle:{{color:'#8b93a1'}}}},
    series:[{{type:'pie',radius:['35%','65%'],data:r.map(x=>({{name:x[0],value:x[1]}})),
      label:{{color:'#e6e6e6'}}}}]
  }});
}})();
// ⑤ 退出原因盈亏
(function(){{
  const r = C.exit_reason_pnl||[];
  mk('c_reason_pnl').setOption({{
    backgroundColor:'transparent',
    grid:{{left:90,right:30,top:20,bottom:30}},
    tooltip:{{trigger:'axis',axisPointer:{{type:'shadow'}},valueFormatter:v=>v+'元'}},
    xAxis:{{type:'value',axisLabel:{{color:'#8b93a1'}}}},
    yAxis:{{type:'category',data:r.map(x=>x[0]).reverse(),axisLabel:{{color:'#8b93a1'}}}},
    series:[{{type:'bar',data:r.map(x=>({{value:x[1],itemStyle:{{color:x[1]>=0?'#3fb950':'#f85149'}}}})).reverse()}}]
  }});
}})();
// ⑥ 单笔收益分布
(function(){{
  const h = C.pnl_hist||[];
  mk('c_pnlhist').setOption({{
    backgroundColor:'transparent',
    grid:{{left:50,right:20,top:20,bottom:60}},
    tooltip:{{trigger:'axis'}},
    xAxis:{{type:'category',data:h.map(x=>x[0]),axisLabel:{{color:'#8b93a1',rotate:60,fontSize:9}}}},
    yAxis:{{type:'value',axisLabel:{{color:'#8b93a1'}}}},
    series:[{{type:'bar',data:h.map(x=>({{value:x[1],itemStyle:{{color:x[1]>=0?'#3fb950':'#f85149'}}}}))}}]
  }});
}})();
// ⑦ 持仓天数
(function(){{
  const h = C.hold_days_hist||[];
  mk('c_hold').setOption({{
    backgroundColor:'transparent',
    grid:{{left:50,right:20,top:20,bottom:40}},
    tooltip:{{trigger:'axis'}},
    xAxis:{{type:'category',data:h.map(x=>x[0]+'天'),axisLabel:{{color:'#8b93a1'}}}},
    yAxis:{{type:'value',axisLabel:{{color:'#8b93a1'}}}},
    series:[{{type:'bar',data:h.map(x=>x[1]),itemStyle:{{color:'#58a6ff'}}}}]
  }});
}})();
// ⑧ 回撤表
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


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    res = run_backtest()

    metrics = res["metrics"]
    equity = res["equity_curve"]
    trades = res["trades"]

    if equity is not None and len(equity):
        equity.to_csv(OUT / "equity_curve.csv", index=False)
    if trades is not None and len(trades):
        trades.to_csv(OUT / "trades.csv", index=False)

    (OUT / "metrics.json").write_text(
        json.dumps(to_native(metrics), ensure_ascii=False, indent=2), encoding="utf-8")

    chart_data = compute_chart_data(equity, trades)
    html = build_html(metrics, chart_data, build_stop_config(),
                      res["entry_mode_info"], res["degradation"], res["stock_count"])
    (OUT / "report.html").write_text(html, encoding="utf-8")

    # 控制台摘要
    print("\n=== 回测结果摘要 ===")
    print(f"累计收益 : {metrics.get('cumulative_return',0):+.2%}")
    print(f"年化收益 : {metrics.get('annualized_return',0):+.2%}")
    print(f"最大回撤 : {metrics.get('max_drawdown',0):.2%}")
    print(f"夏普     : {metrics.get('sharpe_ratio',0):.2f}")
    print(f"Calmar   : {metrics.get('calmar_ratio',0):.2f}")
    print(f"胜率     : {metrics.get('win_rate',0):.1%}")
    print(f"交易笔数 : {metrics.get('total_trades',0)}")
    print(f"\n已保存: {OUT}/report.html, equity_curve.csv, trades.csv, metrics.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
