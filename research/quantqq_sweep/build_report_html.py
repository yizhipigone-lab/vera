"""research/quantqq_sweep/build_report_html.py — 图表分析 HTML 生成器 (2026-08-15)

读 equity_curves.csv + index_daily.csv + regime_filter_results.csv,
产出自包含 HTML (内联 ECharts, 双击即开, 无需服务):
  2026-08-15_QUANTQQ_冠军参数_过滤器权益曲线分析.html
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

OUT_DIR = Path(__file__).resolve().parent
ROOT = OUT_DIR.parent.parent
ERA_LABEL = {"y2015_2016": "2015-2016", "y2017_2018": "2017-2018",
             "y2019_2020": "2019-2020", "y2021_2022": "2021-2022",
             "y2023_2024": "2023-2024", "y2025_2026h1": "2025-2026H1"}
ERAS = list(ERA_LABEL)


def _series(df, col_v):
    return [[d, round(v, 4)] for d, v in zip(df["date"], df[col_v])]


def main() -> int:
    eq = pd.read_csv(OUT_DIR / "equity_curves.csv", dtype={"date": str})
    idx = pd.read_csv(OUT_DIR / "index_daily.csv",
                      dtype={"code": str}, parse_dates=["date"])
    idx["date"] = idx["date"].dt.strftime("%Y-%m-%d")
    summ = pd.read_csv(OUT_DIR / "regime_filter_results.csv")

    # ── 每段图表数据: 策略(±过滤) + 指数, 全部归一化到期初=1.0 ──
    era_charts = []
    for era in ERAS:
        sub = eq[eq["era"] == era]
        d0, d1 = sub["date"].min(), sub["date"].max()
        series = []
        for fname, label, color in (
                ("zz1000_ma20", "冠军+过滤器", "#e0455a"),
                ("none", "冠军不过滤", "#9aa0a6")):
            s = sub[sub["filter"] == fname].sort_values("date")
            # 权益曲线是逐 5m bar 的, 按日取末值降采样 (HTML 体积/渲染减负)
            s = s.assign(day=s["date"].str[:10]).groupby("day").last().reset_index()
            base = s["equity"].iloc[0]
            series.append({"name": label, "color": color,
                           "data": _series(s.assign(norm=s["equity"] / base), "norm")})
        for code, label, color in (("zz1000", "中证1000", "#3a7bd5"),
                                   ("hs300", "沪深300", "#7b9e3a")):
            s = idx[(idx["code"] == code)
                    & (idx["date"] >= d0) & (idx["date"] <= d1)].sort_values("date")
            base = s["close"].iloc[0]
            series.append({"name": label, "color": color,
                           "data": _series(s.assign(norm=s["close"] / base), "norm")})
        era_charts.append({"era": era, "label": ERA_LABEL[era],
                           "series": series})

    # ── 汇总表 (冠军配置, 过滤器 none vs zz1000_ma20) ──
    s = summ[(summ["config"] == "champion")
             & (summ["filter"].isin(["none", "zz1000_ma20"]))]
    table_rows = []
    for era in ERAS:
        a = s[(s["era"] == era) & (s["filter"] == "none")].iloc[0]
        b = s[(s["era"] == era) & (s["filter"] == "zz1000_ma20")].iloc[0]
        table_rows.append({
            "era": ERA_LABEL[era],
            "ann0": round(a["annret"] * 100, 1), "ann1": round(b["annret"] * 100, 1),
            "dd0": round(a["maxdd"] * 100, 1), "dd1": round(b["maxdd"] * 100, 1),
            "cal0": round(a["calmar"], 2), "cal1": round(b["calmar"], 2),
            "tr0": int(a["trades"]), "tr1": int(b["trades"]),
        })

    payload = {"era_charts": era_charts, "table": table_rows}

    # ── 连续口径 (2015~2026H1 单曲线, 3 仓位模式 × 过滤开关) ──
    cont_eq = OUT_DIR / "continuous_equity.csv"
    cont_rs = OUT_DIR / "continuous_results.csv"
    if cont_eq.exists() and cont_rs.exists():
        ce = pd.read_csv(cont_eq, dtype={"date": str})
        cr = pd.read_csv(cont_rs)
        d0, d1 = ce["date"].min(), ce["date"].max()
        c_series = []
        style = {  # (sizing, filter) → (标签, 颜色, 线宽)
            ("fixed5w", "zz1000_ma20"): ("5万固定+过滤", "#e0455a", 2.6),
            ("fixed5w", "none"): ("5万固定", "#c9a0a8", 1.2),
            ("fixed5w", "cyb50_ma200"): ("5万固定+创业板50MA200", "#0a8f6c", 2.2),
            ("pct2", "zz1000_ma20"): ("2%动态+过滤", "#e8912d", 1.8),
            ("pct2", "none"): ("2%动态", "#d9c8a9", 1.2),
            ("pct2", "cyb50_ma200"): ("2%动态+创业板50MA200", "#7fd1b9", 1.2),
            ("fixed2w", "zz1000_ma20"): ("2万固定(实盘现行)+过滤", "#8a5cf6", 1.8),
            ("fixed2w", "none"): ("2万固定(实盘现行)", "#c5bdf0", 1.2),
            ("fixed2w", "cyb50_ma200"): ("2万固定+创业板50MA200", "#5a4a8a", 1.2),
        }
        for (sz, fl), (label, color, width) in style.items():
            s = (ce[(ce["sizing"] == sz) & (ce["filter"] == fl)]
                 .sort_values("date"))
            if s.empty:
                continue
            base = s["equity"].iloc[0]
            c_series.append({"name": label, "color": color, "width": width,
                             "data": _series(s.assign(norm=s["equity"] / base),
                                             "norm")})
        for code, label, color in (("zz1000", "中证1000", "#3a7bd5"),
                                   ("hs300", "沪深300", "#7b9e3a")):
            s = idx[(idx["code"] == code)
                    & (idx["date"] >= d0) & (idx["date"] <= d1)].sort_values("date")
            base = s["close"].iloc[0]
            c_series.append({"name": label, "color": color, "width": 1.2,
                             "data": _series(s.assign(norm=s["close"] / base),
                                             "norm")})
        c_table = [{"sizing": r["sizing"], "filter": r["filter"],
                    "cumret": round(r["cumret"] * 100, 0),
                    "annret": round(r["annret"] * 100, 1),
                    "maxdd": round(r["maxdd"] * 100, 1),
                    "calmar": round(r["calmar"], 2),
                    "winrate": round(r["winrate"] * 100, 0),
                    "trades": int(r["trades"])}
                   for _, r in cr.iterrows()]
        payload["continuous"] = {"series": c_series, "table": c_table,
                                 "range": f"{d0} ~ {d1}"}

    echarts_js = (ROOT / "web" / "echarts.min.js").read_text(encoding="utf-8")

    html = _TEMPLATE.replace("__ECHARTS__", echarts_js).replace(
        "__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
    out = OUT_DIR / "2026-08-15_QUANTQQ_冠军参数_过滤器权益曲线分析.html"
    out.write_text(html, encoding="utf-8")
    print("saved:", out, f"{out.stat().st_size / 1e6:.1f}MB")
    return 0


_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>QUANTQQ 冠军参数 × 市场过滤器 — 图表分析 (2026-08-15)</title>
<style>
body{font-family:'Microsoft YaHei',sans-serif;background:#f5f6f8;color:#222;margin:0;padding:24px;max-width:1280px;margin:0 auto}
h1{font-size:22px} h2{font-size:17px;margin-top:36px;border-left:4px solid #e0455a;padding-left:10px}
.card{background:#fff;border-radius:10px;padding:18px 20px;margin:14px 0;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.kpi{display:flex;gap:14px;flex-wrap:wrap}
.kpi>div{flex:1;min-width:170px;background:#fff;border-radius:10px;padding:14px 16px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.kpi b{display:block;font-size:24px;margin-top:4px}
.kpi .up{color:#e0455a} .kpi .dn{color:#3a7bd5}
.muted{color:#888;font-size:12px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.chart{height:300px} .chart-tall{height:360px}
table{border-collapse:collapse;width:100%;font-size:13px;background:#fff}
th,td{padding:7px 10px;border-bottom:1px solid #eee;text-align:right}
th:first-child,td:first-child{text-align:left}
tr:hover{background:#faf5f6}
.pos{color:#e0455a;font-weight:600} .neg{color:#3a7bd5}
ul.bc{line-height:1.9;font-size:14px}
.tag{display:inline-block;background:#fdeef0;color:#c03;padding:1px 8px;border-radius:8px;font-size:12px;margin-right:6px}
</style></head><body>

<h1>QUANTQQ 冠军参数 × 市场过滤器 — 图表分析</h1>
<p class="muted">生成: 2026-08-15 | 口径: 5m / 沪深A股 / 100万 / 单票5万 / 条件单语义 | 过滤器: 中证1000 收盘 > MA20 才允许开仓 | 冠军参数: 止损-8% · 移动止盈激活3.5%+回撤0.5% · 无阶梯 · 时间12天</p>

<div class="kpi">
  <div><span class="muted">六段均值年化 · 不过滤</span><b class="dn" id="k0"></b></div>
  <div><span class="muted">六段均值年化 · 加过滤</span><b class="up" id="k1"></b></div>
  <div><span class="muted">最差段回撤 · 不过滤</span><b class="dn" id="k2"></b></div>
  <div><span class="muted">最差段回撤 · 加过滤</span><b class="up" id="k3"></b></div>
</div>

<div class="card"><h2 style="border:none;padding:0;margin-top:0">① 各段年化：过滤 vs 不过滤</h2>
<div id="c_bar" class="chart-tall"></div></div>

<div class="card" id="c_cont_card" style="display:none"><h2 style="border:none;padding:0;margin-top:0">①+ 连续 11.5 年权益曲线（100 万复利起跑，3 种仓位模式 × 过滤开关）vs 指数</h2>
<div id="c_cont" class="chart-tall"></div>
<table id="c_cont_table" style="margin-top:10px"><thead><tr>
<th>仓位模式</th><th>过滤器</th><th>累计收益</th><th>年化</th><th>最大回撤</th><th>Calmar</th><th>胜率</th><th>交易数</th>
</tr></thead><tbody></tbody></table></div>

<h2>② 分段权益曲线（归一化到期初=1.0） vs 指数</h2>
<div class="grid" id="c_eras"></div>

<div class="card"><h2 style="border:none;padding:0;margin-top:0">③ 汇总表（冠军配置）</h2>
<table id="c_table"><thead><tr>
<th>区间</th><th>年化·不过滤</th><th>年化·加过滤</th><th>回撤·不过滤</th><th>回撤·加过滤</th><th>Calmar·前</th><th>Calmar·后</th><th>交易·前</th><th>交易·后</th>
</tr></thead><tbody></tbody></table></div>

<div class="card"><h2 style="border:none;padding:0;margin-top:0">④ 边界条件（读数前必看）</h2>
<ul class="bc">
<li><span class="tag">幸存者偏差</span>股票池是"今天还上市"的沪深A股名单，历史上退市的票不在回测中——<b>所有数字按上限理解</b>，真实成绩会打折。</li>
<li><span class="tag">样本内</span>参数与过滤器都是在这 6 段历史里选出的，"跨段稳定"提高可信度，但不构成对未来的保证。MA20 只扫了常识档、未精调，过拟合风险可控但存在。</li>
<li><span class="tag">熊市无药</span>2023-24 小微盘流动性危机段，324 组出场参数全灭；过滤器最好也只到 -1.9%（沪深300>MA60），本质是把深亏变打平，<b>不是熊市提款机</b>。</li>
<li><span class="tag">分段口径</span>②③段的分段图每段以 100 万独立起跑；①+ 为连续 11.5 年单曲线（复利），两口径互相印证, 连续口径的数字更贴近真实复利体验。</li>
<li><span class="tag">极端段待复核</span>2023-24 段 324 组全负过于极端，方向符合行情背景，但建议抽交易明细人工复核后再对外引用。</li>
<li><span class="tag">成本口径</span>已计万三佣金+千一滑点+印花税，模拟了涨停买不进与 T+1。若实盘滑点大于千一（小票急单），成绩会衰减。</li>
<li><span class="tag">资金容量</span>按单票 5 万分散持仓。资金量上到数百万后小票冲击成本上升，成绩会衰减。</li>
<li><span class="tag">过滤代价</span>过滤器在牛市少赚约 25%（2019-20: 96.3%→70.0%）。它的价值是回撤腰斩（最差段 -57%→-32%），不是提高期望收益。</li>
<li><span class="tag">精度口径</span>5m + 条件单语义（创新高 bar 不触发/跳空按开盘价/触线按线价），与实盘可 1:1 复现；1m 对照见同日 1m 扫描。</li>
</ul></div>

<p class="muted">数据: output/quantqq_5m_sweep_2010/all_eras_merged.csv (1944组) · research/quantqq_sweep/regime_filter_results.csv · equity_curves.csv | 研究报告: 2026-08-15_QUANTQQ_5m分段寻优_跨区间验证_研究报告.md</p>

<script>__ECHARTS__</script>
<script>
const P = __PAYLOAD__;
const fmt = v => (v>0?'+':'') + v + '%';
// KPI
const T = P.table;
const mean = k => (T.reduce((a,r)=>a+r[k],0)/T.length).toFixed(1);
const worst = k => Math.min(...T.map(r=>r[k])).toFixed(1);
document.getElementById('k0').textContent = fmt(mean('ann0'));
document.getElementById('k1').textContent = fmt(mean('ann1'));
document.getElementById('k2').textContent = worst('dd0')+'%';
document.getElementById('k3').textContent = worst('dd1')+'%';

// ① 柱状图
echarts.init(document.getElementById('c_bar')).setOption({
  tooltip:{trigger:'axis'}, legend:{},
  grid:{left:50,right:20,bottom:60,top:40},
  xAxis:{type:'category',data:T.map(r=>r.era),axisLabel:{interval:0}},
  yAxis:{type:'value',name:'年化 %'},
  series:[
    {name:'不过滤',type:'bar',data:T.map(r=>r.ann0),itemStyle:{color:'#9aa0a6'}},
    {name:'加过滤(中证1000>MA20)',type:'bar',data:T.map(r=>r.ann1),itemStyle:{color:'#e0455a'}},
  ]
});

// ①+ 连续曲线 (有数据才渲染)
if (P.continuous) {
  document.getElementById('c_cont_card').style.display = '';
  echarts.init(document.getElementById('c_cont')).setOption({
    tooltip:{trigger:'axis'},
    legend:{top:0,textStyle:{fontSize:11}},
    grid:{left:50,right:16,bottom:24,top:34},
    xAxis:{type:'time'},
    yAxis:{type:'value',scale:true,name:'净值'},
    series:P.continuous.series.map(s=>({name:s.name,type:'line',data:s.data,
      showSymbol:false,lineStyle:{width:s.width},itemStyle:{color:s.color}}))
  });
  const ctb = document.querySelector('#c_cont_table tbody');
  P.continuous.table.forEach(r=>{
    ctb.innerHTML += '<tr><td>'+r.sizing+'</td><td>'+r.filter+'</td>'
      +'<td class="'+(r.cumret>=0?'pos':'neg')+'">+'+r.cumret+'%</td>'
      +'<td class="'+(r.annret>=0?'pos':'neg')+'">'+r.annret+'%</td>'
      +'<td>'+r.maxdd+'%</td><td>'+r.calmar+'</td>'
      +'<td>'+r.winrate+'%</td><td>'+r.trades+'</td></tr>';
  });
}

// ② 分段权益曲线
const holder = document.getElementById('c_eras');
P.era_charts.forEach((c,i)=>{
  const div = document.createElement('div');
  div.className='card'; div.innerHTML='<b>'+c.label+'</b><div class="chart" id="ce'+i+'"></div>';
  holder.appendChild(div);
  echarts.init(document.getElementById('ce'+i)).setOption({
    tooltip:{trigger:'axis'},
    legend:{top:0,textStyle:{fontSize:11}},
    grid:{left:45,right:12,bottom:24,top:30},
    xAxis:{type:'time'},
    yAxis:{type:'value',scale:true},
    series:c.series.map(s=>({name:s.name,type:'line',data:s.data,showSymbol:false,
      lineStyle:{width:s.name.includes('过滤')?2.2:1.2},itemStyle:{color:s.color}}))
  });
});

// ③ 汇总表
const tb = document.querySelector('#c_table tbody');
T.forEach(r=>{
  const cls = v => v>=0?'pos':'neg';
  tb.innerHTML += '<tr><td>'+r.era+'</td>'
    +'<td class="'+cls(r.ann0)+'">'+fmt(r.ann0)+'</td>'
    +'<td class="'+cls(r.ann1)+'">'+fmt(r.ann1)+'</td>'
    +'<td>'+r.dd0+'%</td><td>'+r.dd1+'%</td>'
    +'<td>'+r.cal0+'</td><td>'+r.cal1+'</td>'
    +'<td>'+r.tr0+'</td><td>'+r.tr1+'</td></tr>';
});
</script></body></html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
