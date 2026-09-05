# -*- coding: utf-8 -*-
"""GS公式批量回测 HTML 交互报告生成器
用法: python tools/gs_report.py --results-dir output/gs_rank_2005/results --out output/gs_rank_2005/report.html
"""
import argparse
import json
import sys
from pathlib import Path
from collections import Counter

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

def load_results(results_dir: str) -> list:
    results = []
    for p in sorted(Path(results_dir).glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if d.get("status") == "ok" and d.get("metrics"):
                results.append(d)
        except Exception:
            continue
    return results

def load_excluded(clean_json: str) -> list:
    try:
        d = json.loads(Path(clean_json).read_text(encoding="utf-8"))
        return d.get("excluded", [])
    except Exception:
        return []

def pct(v, d=2):
    return f"{v*100:.{d}f}%" if isinstance(v, (int, float)) else "—"

def num(v, d=2):
    return f"{v:.{d}f}" if isinstance(v, (int, float)) else "—"

def gen_html(results: list, excluded: list, args) -> str:
    ok = sorted(results, key=lambda r: r["metrics"].get("cumulative_return", -9), reverse=True)

    # 汇总统计
    n_ok = len(ok)
    n_total = n_ok + len(excluded)
    returns = [r["metrics"]["cumulative_return"] for r in ok]
    win_rates = [r["metrics"]["win_rate"] for r in ok]
    sharpes = [r["metrics"].get("sharpe_ratio", 0) for r in ok]
    max_dds = [r["metrics"]["max_drawdown"] for r in ok]
    n_trades_list = [r["n_trades"] for r in ok]
    ann_returns = [r["metrics"].get("annualized_return", 0) for r in ok]

    # Top20 表
    top20_rows = ""
    for i, r in enumerate(ok[:20], 1):
        m = r["metrics"]
        top20_rows += f"""<tr>
<td>{i}</td><td class="fname">{r['formula']}</td>
<td class="{'pos' if m['cumulative_return']>0 else 'neg'}">{pct(m['cumulative_return'])}</td>
<td class="{'pos' if m.get('annualized_return',0)>0 else 'neg'}">{pct(m.get('annualized_return'))}</td>
<td>{pct(m['win_rate'])}</td><td>{pct(m['max_drawdown'])}</td>
<td>{num(m.get('sharpe_ratio'))}</td><td>{num(m.get('calmar_ratio'))}</td>
<td>{r['n_trades']}</td><td>{r['signals_used']}/{r['signals_raw']}</td>
</tr>"""

    # 完整排名表
    all_rows = ""
    for i, r in enumerate(ok, 1):
        m = r["metrics"]
        all_rows += f"""<tr>
<td>{i}</td><td class="fname">{r['formula']}</td>
<td class="{'pos' if m['cumulative_return']>0 else 'neg'}">{pct(m['cumulative_return'])}</td>
<td class="{'pos' if m.get('annualized_return',0)>0 else 'neg'}">{pct(m.get('annualized_return'))}</td>
<td>{pct(m['win_rate'])}</td><td>{pct(m['max_drawdown'])}</td>
<td>{num(m.get('sharpe_ratio'))}</td><td>{num(m.get('calmar_ratio'))}</td>
<td>{r['n_trades']}</td><td>{r['signals_used']}/{r['signals_raw']}</td>
<td>{r['elapsed_s']:.0f}</td>
</tr>"""

    # ECharts 数据
    # 1. 收益分布直方图
    ret_bins = [-10, -5, -2, -1, -0.5, 0, 0.5, 1, 2, 5, 10, 50, 100, 500]
    ret_hist = [0] * (len(ret_bins) - 1)
    for v in returns:
        for j in range(len(ret_bins) - 1):
            if ret_bins[j] <= v < ret_bins[j + 1]:
                ret_hist[j] += 1
                break
    ret_labels = [f"{ret_bins[j]}~{ret_bins[j+1]}" for j in range(len(ret_bins) - 1)]

    # 2. 胜率 vs 夏普散点
    scatter_data = [[round(s, 2), round(w * 100, 1), r["formula"]] for r, s, w in zip(ok, sharpes, win_rates)]

    # 3. Top20 柱状图
    top20_names = [r["formula"] for r in ok[:20]]
    top20_returns = [round(r["metrics"]["cumulative_return"] * 100, 1) for r in ok[:20]]

    # 4. 回撤分布
    dd_bins = [0, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 0.8, 1.0]
    dd_hist = [0] * (len(dd_bins) - 1)
    for v in max_dds:
        v_abs = abs(v)
        for j in range(len(dd_bins) - 1):
            if dd_bins[j] <= v_abs < dd_bins[j + 1]:
                dd_hist[j] += 1
                break
    dd_labels = [f"{dd_bins[j]*100:.0f}%~{dd_bins[j+1]*100:.0f}%" for j in range(len(dd_bins) - 1)]

    # 5. 交易数 vs 收益散点
    trade_scatter = [[r["n_trades"], round(r["metrics"]["cumulative_return"] * 100, 1), r["formula"]] for r in ok]

    # 6. 年化收益分布
    ann_bins = [-0.5, -0.2, -0.1, 0, 0.1, 0.2, 0.3, 0.5, 1.0, 5.0]
    ann_hist = [0] * (len(ann_bins) - 1)
    for v in ann_returns:
        for j in range(len(ann_bins) - 1):
            if ann_bins[j] <= v < ann_bins[j + 1]:
                ann_hist[j] += 1
                break
    ann_labels = [f"{ann_bins[j]*100:.0f}%~{ann_bins[j+1]*100:.0f}%" for j in range(len(ann_bins) - 1)]

    # 排除清单
    excluded_rows = ""
    for e in excluded:
        excluded_rows += f"<tr><td>{e['gs']}</td><td>{e['file']}</td><td>{', '.join(e['tokens'])}</td></tr>"

    # 统计卡片
    pos_count = sum(1 for v in returns if v > 0)
    neg_count = n_ok - pos_count
    avg_ret = sum(returns) / n_ok if n_ok else 0
    med_ret = sorted(returns)[n_ok // 2] if n_ok else 0
    avg_wr = sum(win_rates) / n_ok if n_ok else 0
    avg_sharpe = sum(sharpes) / n_ok if n_ok else 0
    best = ok[0] if ok else None
    worst = ok[-1] if ok else None

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GS公式批量回测报告 ({args.start} ~ {args.end})</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: -apple-system, "Microsoft YaHei", sans-serif; background:#0a0e1a; color:#e0e6f0; padding:20px; }}
.container {{ max-width:1400px; margin:0 auto; }}
h1 {{ text-align:center; color:#4fc3f7; margin-bottom:8px; font-size:28px; }}
.subtitle {{ text-align:center; color:#8899aa; margin-bottom:24px; font-size:14px; }}
h2 {{ color:#4fc3f7; margin:32px 0 16px; font-size:20px; border-left:4px solid #4fc3f7; padding-left:12px; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(200px,1fr)); gap:12px; margin-bottom:24px; }}
.card {{ background:#111827; border:1px solid #1e3a5f; border-radius:8px; padding:16px; text-align:center; }}
.card .label {{ font-size:12px; color:#8899aa; margin-bottom:4px; }}
.card .value {{ font-size:24px; font-weight:700; }}
.pos {{ color:#ef5350; }} .neg {{ color:#66bb6a; }}
table {{ width:100%; border-collapse:collapse; margin:12px 0; font-size:13px; }}
th {{ background:#1a2332; color:#4fc3f7; padding:8px 6px; text-align:left; position:sticky; top:0; }}
td {{ padding:6px; border-bottom:1px solid #1e3a5f; }}
tr:hover {{ background:#1a2332; }}
.fname {{ font-weight:600; color:#ce93d8; }}
.chart {{ background:#111827; border:1px solid #1e3a5f; border-radius:8px; padding:16px; margin:12px 0; }}
.chart-title {{ font-size:14px; color:#8899aa; margin-bottom:8px; }}
.chart-container {{ width:100%; height:400px; }}
.chart-row {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
@media (max-width:900px) {{ .chart-row {{ grid-template-columns:1fr; }} }}
.note {{ background:#1a2332; border-left:4px solid #ff9800; padding:12px; margin:16px 0; font-size:13px; color:#ccc; }}
</style>
</head>
<body>
<div class="container">
<h1>GS公式批量回测报告</h1>
<p class="subtitle">{args.start} ~ {args.end} | 日线级别 | 标准止盈止损 | 全A股(剔ST)</p>

<h2>📊 总体概览</h2>
<div class="cards">
<div class="card"><div class="label">参与回测</div><div class="value" style="color:#4fc3f7">{n_ok}</div></div>
<div class="card"><div class="label">排除(未来函数等)</div><div class="value" style="color:#ff9800">{len(excluded)}</div></div>
<div class="card"><div class="label">盈利公式</div><div class="value pos">{pos_count}</div></div>
<div class="card"><div class="label">亏损公式</div><div class="value neg">{neg_count}</div></div>
<div class="card"><div class="label">平均收益</div><div class="value {'pos' if avg_ret>0 else 'neg'}">{pct(avg_ret)}</div></div>
<div class="card"><div class="label">中位收益</div><div class="value {'pos' if med_ret>0 else 'neg'}">{pct(med_ret)}</div></div>
<div class="card"><div class="label">平均胜率</div><div class="value" style="color:#4fc3f7">{pct(avg_wr)}</div></div>
<div class="card"><div class="label">平均夏普</div><div class="value" style="color:#4fc3f7">{avg_sharpe:.2f}</div></div>
</div>

{f'<div class="note">🏆 最佳: <b>{best["formula"]}</b> 累计收益 {pct(best["metrics"]["cumulative_return"])} (胜率 {pct(best["metrics"]["win_rate"])}, 交易 {best["n_trades"]} 笔) | 最差: <b>{worst["formula"]}</b> 累计收益 {pct(worst["metrics"]["cumulative_return"])}</div>' if best else ''}

<h2>📈 图表分析</h2>

<div class="chart"><div class="chart-title">Top 20 公式累计收益</div><div id="chart-top20" class="chart-container"></div></div>

<div class="chart-row">
<div class="chart"><div class="chart-title">收益分布</div><div id="chart-ret-dist" class="chart-container"></div></div>
<div class="chart"><div class="chart-title">最大回撤分布</div><div id="chart-dd-dist" class="chart-container"></div></div>
</div>

<div class="chart-row">
<div class="chart"><div class="chart-title">胜率 vs 夏普比率</div><div id="chart-scatter" class="chart-container"></div></div>
<div class="chart"><div class="chart-title">交易次数 vs 累计收益</div><div id="chart-trade-scatter" class="chart-container"></div></div>
</div>

<div class="chart"><div class="chart-title">年化收益分布</div><div id="chart-ann-dist" class="chart-container"></div></div>

<h2>🏆 Top 20 公式</h2>
<div style="overflow-x:auto"><table>
<tr><th>#</th><th>公式</th><th>累计收益</th><th>年化</th><th>胜率</th><th>最大回撤</th><th>夏普</th><th>Calmar</th><th>交易数</th><th>信号(用/原始)</th></tr>
{top20_rows}
</table></div>

<h2>📋 完整排名</h2>
<div style="overflow-x:auto;max-height:600px;overflow-y:auto"><table>
<tr><th>#</th><th>公式</th><th>累计收益</th><th>年化</th><th>胜率</th><th>最大回撤</th><th>夏普</th><th>Calmar</th><th>交易数</th><th>信号(用/原始)</th><th>耗时s</th></tr>
{all_rows}
</table></div>

<h2>🚫 排除公式 ({len(excluded)} 条)</h2>
<div style="overflow-x:auto;max-height:300px;overflow-y:auto"><table>
<tr><th>编号</th><th>文件</th><th>排除原因</th></tr>
{excluded_rows}
</table></div>

<h2>⚙️ 回测设置</h2>
<table>
<tr><th>项目</th><th>值</th></tr>
<tr><td>回测区间</td><td>{args.start} ~ {args.end}</td></tr>
<tr><td>股票池</td><td>全部A股 (剔ST/退市/港股, 含北交所)</td></tr>
<tr><td>初始资金</td><td>1,000,000 元</td></tr>
<tr><td>单票买入上限</td><td>10,000 元</td></tr>
<tr><td>周期/复权</td><td>日线 / 前复权</td></tr>
<tr><td>入场</td><td>尾盘选股, 信号日T以收盘价买入 (含0.1%滑点)</td></tr>
<tr><td>成本</td><td>佣金万三(双边) + 滑点0.1%(双边) + 印花税0.05%(卖出)</td></tr>
<tr><td>成本止损</td><td>浮亏 -12% 清仓</td></tr>
<tr><td>移动止盈</td><td>浮盈 +3.5% 激活, 自最高价回撤 1% 触发</td></tr>
<tr><td>阶梯止盈</td><td>+6% 卖30%, +15% 再卖30%</td></tr>
<tr><td>时间止损</td><td>持仓满20个交易日清仓</td></tr>
<tr><td>30日首信号</td><td>近30个交易日内已有过信号的丢弃不买</td></tr>
<tr><td>卖出冷却</td><td>全清仓后20个交易日内不买回同票</td></tr>
</table>

</div>
<script>
const dark = {{ backgroundColor:'transparent', textStyle:{{color:'#8899aa'}} }};
function makeChart(id, opt) {{
    const el = document.getElementById(id);
    if(!el) return;
    const c = echarts.init(el, null, {{renderer:'canvas'}});
    c.setOption(Object.assign({{}}, dark, opt));
    window.addEventListener('resize', ()=>c.resize());
}}

// Top20
makeChart('chart-top20', {{
    xAxis:{{type:'category', data:{json.dumps(top20_names)}, axisLabel:{{rotate:45, fontSize:10}}}},
    yAxis:{{type:'value', name:'收益%', axisLabel:{{formatter:'{{value}}%'}}}},
    series:[{{type:'bar', data:{json.dumps(top20_returns)},
        itemStyle:{{color: function(p){{ return p.value>=0?'#ef5350':'#66bb6a'; }}}},
        label:{{show:true, position:'top', fontSize:9, formatter:'{{c}}%'}} }}],
    grid:{{bottom:80}},
    tooltip:{{trigger:'axis'}}
}});

// 收益分布
makeChart('chart-ret-dist', {{
    xAxis:{{type:'category', data:{json.dumps(ret_labels)}, axisLabel:{{rotate:45, fontSize:10}}}},
    yAxis:{{type:'value', name:'公式数'}},
    series:[{{type:'bar', data:{json.dumps(ret_hist)}, itemStyle:{{color:'#4fc3f7'}}}}],
    grid:{{bottom:60}},
    tooltip:{{trigger:'axis'}}
}});

// 回撤分布
makeChart('chart-dd-dist', {{
    xAxis:{{type:'category', data:{json.dumps(dd_labels)}, axisLabel:{{rotate:45, fontSize:10}}}},
    yAxis:{{type:'value', name:'公式数'}},
    series:[{{type:'bar', data:{json.dumps(dd_hist)}, itemStyle:{{color:'#ff9800'}}}}],
    grid:{{bottom:60}},
    tooltip:{{trigger:'axis'}}
}});

// 胜率vs夏普散点
makeChart('chart-scatter', {{
    xAxis:{{type:'value', name:'夏普比率'}},
    yAxis:{{type:'value', name:'胜率%', axisLabel:{{formatter:'{{value}}%'}}}},
    series:[{{type:'scatter', data:{json.dumps(scatter_data)},
        symbolSize:8, itemStyle:{{color:'#ce93d8', opacity:0.7}},
        tooltip:{{formatter: function(p){{ return p.data[2]+'<br>夏普:'+p.data[0]+'<br>胜率:'+p.data[1]+'%'; }}}}
    }}],
    tooltip:{{trigger:'item'}}
}});

// 交易数vs收益散点
makeChart('chart-trade-scatter', {{
    xAxis:{{type:'value', name:'交易次数'}},
    yAxis:{{type:'value', name:'累计收益%', axisLabel:{{formatter:'{{value}}%'}}}},
    series:[{{type:'scatter', data:{json.dumps(trade_scatter)},
        symbolSize:8, itemStyle:{{color:'#4fc3f7', opacity:0.7}},
        tooltip:{{formatter: function(p){{ return p.data[2]+'<br>交易:'+p.data[0]+'<br>收益:'+p.data[1]+'%'; }}}}
    }}],
    tooltip:{{trigger:'item'}}
}});

// 年化分布
makeChart('chart-ann-dist', {{
    xAxis:{{type:'category', data:{json.dumps(ann_labels)}, axisLabel:{{rotate:45, fontSize:10}}}},
    yAxis:{{type:'value', name:'公式数'}},
    series:[{{type:'bar', data:{json.dumps(ann_hist)}, itemStyle:{{color:'#66bb6a'}}}}],
    grid:{{bottom:60}},
    tooltip:{{trigger:'axis'}}
}});
</script>
</body>
</html>"""
    return html

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-dir", default="output/gs_rank_2005/results")
    ap.add_argument("--clean-json", default="E:/1target/gongshi/_clean_formulas.json")
    ap.add_argument("--out", default="output/gs_rank_2005/report.html")
    ap.add_argument("--start", default="20050101")
    ap.add_argument("--end", default="20260826")
    args = ap.parse_args()

    results = load_results(args.results_dir)
    excluded = load_excluded(args.clean_json)
    print(f"有效结果: {len(results)}, 排除: {len(excluded)}")

    html = gen_html(results, excluded, args)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(html, encoding="utf-8")
    print(f"报告: {args.out}")

if __name__ == "__main__":
    main()
