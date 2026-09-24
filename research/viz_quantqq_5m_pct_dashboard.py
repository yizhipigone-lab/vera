# -*- coding: utf-8 -*-
"""QUANTQQ 5m 真·2% 回测可视化仪表盘生成脚本 (v2)。

v2 变更：图表 options 全部改由内嵌 JS 构建（formatter 用真实函数），
根治 v1 中「字符串函数 formatter 被 ECharts 当纯文本渲染」的 bug；
视觉升级：渐变面积/柱状圆角/阴影/格内数值/新增气泡散点图，两列 dashboard 布局。

数据流：读 CSV/JSON 落盘产物 -> Python 算纯数据包(JSON) -> 内嵌 JS 拼图表。

口径红线：真·2% 年化 212% 是「2% 仓位 + 资金滚存 + 不限流动性」的数学结果，
未计入流动性约束，不得当作真实可实现收益。

用法: python research/viz_quantqq_5m_pct_dashboard.py
"""
import csv
import json
import os
import random
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "output", "quantqq_2014_5m_pct2")

DIRS = {
    "pct2": OUT_DIR,
    "trailing": os.path.join(ROOT, "output", "quantqq_2014_5m_trailing"),
    "pct10": os.path.join(ROOT, "output", "quantqq_2014_5m_pct10"),
}
LABELS = {"pct2": "真·2%（资金滚存）", "trailing": "固定2万", "pct10": "真·10%"}
COLORS = {"pct2": "#e0444c", "trailing": "#8fa3b8", "pct10": "#f0a24a"}

UP, DOWN = "#e0444c", "#1cbf8b"  # A股：红涨绿跌
BG = "#0f1115"
CARD_BG = "#161b24"
BORDER = "#262d3a"
TXT = "#d3d8e0"
MUTED = "#8a93a3"

ECHARTS_SRC = os.path.join(ROOT, "web", "echarts.min.js")


# ---------------------------------------------------------------- 数据加载
def load_daily_eq(path):
    """equity_curve.csv (5m bars) -> 按日聚合 [(date, equity, drawdown)]。"""
    last = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            last[row["date"][:10]] = (float(row["equity"]), float(row["drawdown"]))
    days = sorted(last)
    return [(d, last[d][0], last[d][1]) for d in days]


def load_metrics(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_trades(path):
    """trades.csv -> 逐笔 dict 列表。"""
    trades = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            trades.append(row)
    return trades


# ---------------------------------------------------------------- 指标计算
def yearly_returns(daily):
    """年收益 = 当年末 equity / 上年末 equity - 1；首年用年初首值作基数。"""
    by_year = defaultdict(list)
    for d, eq, _ in daily:
        by_year[d[:4]].append(eq)
    out = {}
    prev = None
    for y in sorted(by_year):
        vals = by_year[y]
        base = prev if prev is not None else vals[0]
        out[y] = vals[-1] / base - 1.0
        prev = vals[-1]
    return out


def monthly_returns(daily):
    """月收益 = 月末 equity / 上月末 equity - 1；首月用当月首值作基数。"""
    by_month = defaultdict(list)
    for d, eq, _ in daily:
        by_month[d[:7]].append(eq)
    out = {}
    prev = None
    for ym in sorted(by_month):
        vals = by_month[ym]
        base = prev if prev is not None else vals[0]
        out[ym] = vals[-1] / base - 1.0
        prev = vals[-1]
    return out


def trades_stats(trades):
    """逐笔汇总：出场原因 / 盈亏 / 持仓 / 股票聚合 / 资金利用。"""
    reason = Counter()
    reason_pct = defaultdict(list)
    pcts = []
    holds = []
    stock_pnl = defaultdict(float)
    year_amount = defaultdict(list)
    year_n = Counter()
    for t in trades:
        r = t["exit_reason"]
        reason[r] += 1
        reason_pct[r].append(float(t["profit_pct"]))
        pcts.append(float(t["profit_pct"]))
        holds.append(int(t["hold_days"]))
        stock_pnl[t["stock_code"]] += float(t["pnl"])
        y = t["entry_date"][:4]
        year_amount[y].append(float(t["entry_amount"]))
        year_n[y] += 1
    return reason, reason_pct, pcts, holds, stock_pnl, year_amount, year_n


def fmt_pct(x, digits=1):
    return ("%+." + str(digits) + "f%%") % (x * 100)


def fmt_sci_pct(multiplier):
    """累计收益倍数 -> 科学计数法百分比，如 1795682.08 -> +1.8e8%。"""
    r = (multiplier - 1) * 100
    mant, exp = ("%.1e" % r).split("e")
    return "%+se%d" % (mant, int(exp))


def fmt_thousands(n):
    return format(n, ",")


# ---------------------------------------------------------------- 数据包
def _profit_hist(pcts):
    """单笔盈亏不等宽分箱 -> {labels, counts, colors}。"""
    bins = [(-1.0, -0.10, "-10%以下", DOWN), (-0.10, -0.05, "-10~-5%", DOWN),
            (-0.05, -0.02, "-5~-2%", DOWN), (-0.02, -0.01, "-2~-1%", DOWN),
            (-0.01, 0.0, "-1~0%", DOWN), (0.0, 0.01, "0~1%", UP),
            (0.01, 0.02, "1~2%", UP), (0.02, 0.03, "2~3%", UP),
            (0.03, 0.04, "3~4%", UP), (0.04, 0.05, "4~5%", UP),
            (0.05, 0.08, "5~8%", UP), (0.08, 0.12, "8~12%", UP),
            (0.12, 0.20, "12~20%", UP), (0.20, 100.0, "20%以上", UP)]
    counts = [0] * len(bins)
    for p in pcts:
        for i, (lo, hi, _, _) in enumerate(bins):
            if lo <= p < hi:
                counts[i] += 1
                break
    return {"labels": [b[2] for b in bins], "counts": counts,
            "colors": [b[3] for b in bins]}


def _hold_hist(holds):
    bins = [(0, 1, "0~1天"), (1, 2, "1~2天"), (2, 3, "2~3天"), (3, 5, "3~5天"),
            (5, 10, "5~10天"), (10, 20, "10~20天"), (20, 40, "20~40天"),
            (40, 60, "40~60天"), (60, 10000, "60天+")]
    counts = [0] * len(bins)
    for h in holds:
        for i, (lo, hi, _) in enumerate(bins):
            if lo <= h < hi:
                counts[i] += 1
                break
    return {"labels": [b[2] for b in bins], "counts": counts}


def _scatter_sample(trades, max_points=9000):
    """气泡散点采样：亏损全保留 + 盈利按比例抽，最多 max_points 点。
    每点 [hold_days, profit_pct*100, size(按单笔金额 log 归一 3~16), stock_code]。"""
    points = []
    max_amt = max(float(t["entry_amount"]) for t in trades) or 1.0
    losers = []
    winners = []
    for t in trades:
        amt = float(t["entry_amount"])
        size = 3 + 13 * (math_log10(amt) / math_log10(max_amt))
        pt = [int(t["hold_days"]), round(float(t["profit_pct"]) * 100, 2),
              round(size, 2), t["stock_code"]]
        (losers if float(t["profit_pct"]) <= 0 else winners).append(pt)
    target_win = max(0, max_points - len(losers))
    if len(winners) > target_win:
        winners = random.sample(winners, target_win)
    return losers + winners


def math_log10(x):
    import math
    return math.log10(x) if x > 1 else 0.0


def build_payload(daily_map, metrics, yearly_map, trades):
    reason, reason_pct, pcts, holds, stock_pnl, year_amount, year_n = trades_stats(trades)

    dates = [d for d, _, _ in daily_map["pct2"]]
    dd = [round(d * 100, 2) for _, _, d in daily_map["pct2"]]
    eq_series = {}
    for k in ("pct2", "pct10", "trailing"):
        eq_by_date = {d: eq for d, eq, _ in daily_map[k]}
        eq_series[k] = [round(eq_by_date[d], 1) for d in dates]

    years = sorted(yearly_map["pct2"])
    yearly = {y: {k: round(yearly_map[k].get(y, 0) * 100, 1) for k in yearly_map}
              for y in years}

    monthly = monthly_returns(daily_map["pct2"])
    y0 = int(min(monthly)[:4])
    heat = [[int(ym[5:7]) - 1, int(ym[:4]) - y0, round(r * 100, 1)]
            for ym, r in sorted(monthly.items())]

    reasons = []
    total = sum(reason.values())
    for r, n in reason.most_common():
        avg = sum(reason_pct[r]) / len(reason_pct[r])
        reasons.append({"name": r, "value": n, "pct": round(n / total * 100, 1),
                        "avg": round(avg * 100, 2)})

    ranked = sorted(stock_pnl.items(), key=lambda kv: kv[1])
    top_loss = [{"name": k, "value": round(v)} for k, v in ranked[:10][::-1]]
    top_win = [{"name": k, "value": round(v)} for k, v in ranked[-10:][::-1]]

    cap = {
        "years": sorted(year_n),
        "amt": [round(sum(year_amount[y]) / len(year_amount[y])) for y in sorted(year_n)],
        "n": [year_n[y] for y in sorted(year_n)],
    }

    return {
        "dates": dates,
        "eq2": eq_series["pct2"], "eq10": eq_series["pct10"], "eqF": eq_series["trailing"],
        "dd": dd,
        "yearly": yearly,
        "heat": heat, "heatYears": [str(y) for y in years],
        "reasons": reasons,
        "prof": _profit_hist(pcts),
        "holds": _hold_hist(holds),
        "topWin": top_win, "topLoss": top_loss,
        "cap": cap,
        "scatter": _scatter_sample(trades),
        "tradeN": len(trades),
    }


# ---------------------------------------------------------------- 指标卡 HTML
def build_metric_cards(metrics):
    order = ("trailing", "pct2", "pct10")
    cards = []
    for key in order:
        m = metrics[key]
        main = " main" if key == "pct2" else ""
        cards.append(f"""
        <div class="mcard{main}" style="--accent:{COLORS[key]}">
          <div class="mhead">{LABELS[key]}</div>
          <div class="mrow"><span>年化</span><b>{fmt_pct(m['annualized_return'])}</b></div>
          <div class="mrow"><span>累计收益</span><b>{fmt_sci_pct(m['cumulative_return'])}</b></div>
          <div class="mrow"><span>最大回撤</span><b>{m['max_drawdown']*100:.1f}%</b></div>
          <div class="mrow"><span>Sharpe</span><b>{m['sharpe_ratio']:.2f}</b></div>
          <div class="mrow"><span>Calmar</span><b>{m['calmar_ratio']:.1f}</b></div>
          <div class="mrow"><span>胜率</span><b>{m['win_rate']*100:.1f}%</b></div>
          <div class="mrow"><span>总笔数</span><b>{fmt_thousands(m['total_trades'])}</b></div>
        </div>""")
    return "\n".join(cards)


# ---------------------------------------------------------------- HTML 模板
CHART_JS = r"""
// ================= 公共 =================
var UP='#e0444c', DOWN='#1cbf8b', TXT='#d3d8e0', MUTED='#8a93a3', BORDER='#262d3a', BG='#0f1115';
function grad(c, a){ return new echarts.graphic.LinearGradient(0,0,0,1,[
  {offset:0, color: hexA(c,a)}, {offset:1, color: hexA(c,0.02)} ]); }
function hexA(hex, a){ var r=parseInt(hex.slice(1,3),16), g=parseInt(hex.slice(3,5),16), b=parseInt(hex.slice(5,7),16);
  return 'rgba('+r+','+g+','+b+','+a+')'; }
function fmtMoney(v){ if(v>=1e12) return (v/1e12).toFixed(2)+'万亿'; if(v>=1e8) return (v/1e8).toFixed(1)+'亿';
  if(v>=1e4) return (v/1e4).toFixed(1)+'万'; return v.toFixed(0); }
function tip(){ return {backgroundColor:'#1b212c', borderColor:BORDER, borderWidth:1,
  textStyle:{color:TXT, fontSize:12}, extraCssText:'box-shadow:0 4px 14px rgba(0,0,0,.5);border-radius:6px;'}; }
var AXIS = { axisLine:{lineStyle:{color:BORDER}}, axisLabel:{color:MUTED} };
var SPLIT = { splitLine:{lineStyle:{color:'#1e2530'}} };
function title(t, sub){ return {text:t, subtext:sub||'', left:12, top:8,
  textStyle:{color:TXT, fontSize:15, fontWeight:700}, subtextStyle:{color:MUTED, fontSize:11}}; }

var charts = [];
function mk(id, opt){ var el=document.getElementById(id); if(!el) return;
  var c=echarts.init(el); c.setOption(opt); charts.push(c); return c; }

// ================= 图1 三口径权益对比（log 轴 + 渐变面积） =================
mk('c1', {
  backgroundColor:'transparent',
  title: title('三口径权益曲线对比（log 轴）', '2014-01 ~ 2026-08 · 未计入流动性约束'),
  tooltip: Object.assign(tip(), {trigger:'axis', formatter:function(ps){
    var s=['<b>'+ps[0].axisValue+'</b>']; ps.forEach(function(p){
      s.push(p.marker+' '+p.seriesName+'：<b>'+fmtMoney(p.value)+'</b>'); });
    return s.join('<br/>'); }}),
  legend: {data:['真·2%','真·10%','固定2万'], top:36, textStyle:{color:MUTED},
    itemWidth:18, itemHeight:10, icon:'roundRect'},
  grid:{left:78, right:26, top:66, bottom:44},
  xAxis: Object.assign({type:'category', data:DATA.dates, boundaryGap:false}, AXIS,
    {axisLabel:Object.assign({}, AXIS.axisLabel, {formatter:function(v){return v.slice(0,7);}})}),
  yAxis: Object.assign({type:'log', min:1e5}, SPLIT, {axisLabel:Object.assign({}, AXIS.axisLabel, {formatter:function(v){return fmtMoney(v);}})}),
  series:[
    {name:'固定2万', type:'line', data:DATA.eqF, showSymbol:false,
      lineStyle:{width:1.6, color:'#8fa3b8'}, itemStyle:{color:'#8fa3b8'}, areaStyle:{color:grad('#8fa3b8',0.35)}},
    {name:'真·10%', type:'line', data:DATA.eq10, showSymbol:false,
      lineStyle:{width:1.8, color:'#f0a24a'}, itemStyle:{color:'#f0a24a'}, areaStyle:{color:grad('#f0a24a',0.35)}},
    {name:'真·2%', type:'line', data:DATA.eq2, showSymbol:false,
      lineStyle:{width:2.4, color:UP, shadowColor:'rgba(224,68,76,.35)', shadowBlur:8},
      itemStyle:{color:UP}, areaStyle:{color:grad(UP,0.5)}},
  ]
});

// ================= 图2 回撤水下 =================
mk('c2', {
  backgroundColor:'transparent',
  title: title('回撤水下曲线 · 真·2%（资金滚存）', '最深 -5.94%'),
  tooltip: Object.assign(tip(), {trigger:'axis', formatter:function(ps){
    return '<b>'+ps[0].axisValue+'</b><br/>回撤：<b style="color:'+DOWN+'">'+ps[0].value.toFixed(2)+'%</b>'; }}),
  grid:{left:58, right:22, top:44, bottom:40},
  xAxis: Object.assign({type:'category', data:DATA.dates, boundaryGap:false}, AXIS,
    {axisLabel:Object.assign({}, AXIS.axisLabel, {formatter:function(v){return v.slice(0,7);}})}),
  yAxis: Object.assign({type:'value', max:0}, SPLIT, {axisLabel:Object.assign({}, AXIS.axisLabel, {formatter:function(v){return v+'%';}})}),
  series:[{name:'回撤', type:'line', data:DATA.dd, showSymbol:false,
    lineStyle:{width:1.4, color:DOWN}, itemStyle:{color:DOWN},
    areaStyle:{color:grad(DOWN,0.55)},
    markLine:{silent:true, symbol:'none', lineStyle:{color:'#3a4452', type:'dashed'},
      data:[{yAxis:0, label:{formatter:'0', color:MUTED, position:'end'}}]}}]
});

// ================= 图3 逐年收益三柱 =================
(function(){
  var years = Object.keys(DATA.yearly);
  var mkSeries = function(key, name, color){
    return {name:name, type:'bar', barMaxWidth:26,
      data: years.map(function(y){ return {value: DATA.yearly[y][key], itemStyle:{color:grad(color, 0.9)} }; }),
      itemStyle:{borderRadius:[4,4,0,0]},
      label:{show:true, position:'top', color:MUTED, fontSize:9,
        formatter:function(p){ return (p.value>0?'+':'')+p.value.toFixed(0)+'%'; }}};
  };
  mk('c3', {
    backgroundColor:'transparent',
    title: title('逐年收益对比（%）· 三口径', '真·2% 为主视角 · 抽查 2015 +362% / 2020 +212% / 2024 +241%'),
    tooltip: Object.assign(tip(), {trigger:'axis',
      formatter:function(ps){ var s=['<b>'+ps[0].axisValue+' 年</b>'];
        ps.forEach(function(p){ s.push(p.marker+' '+p.seriesName+'：<b>'+(p.value>0?'+':'')+p.value.toFixed(1)+'%</b>'); });
        return s.join('<br/>'); }}),
    legend:{data:['固定2万','真·2%','真·10%'], top:36, textStyle:{color:MUTED}, itemWidth:18, itemHeight:10, icon:'roundRect'},
    grid:{left:60, right:30, top:74, bottom:36},
    xAxis: Object.assign({type:'category', data:years}, AXIS),
    yAxis: Object.assign({type:'value'}, SPLIT, {axisLabel:Object.assign({}, AXIS.axisLabel, {formatter:function(v){return v+'%';}})}),
    series:[
      mkSeries('trailing','固定2万','#8fa3b8'),
      mkSeries('pct2','真·2%',UP),
      mkSeries('pct10','真·10%','#f0a24a'),
    ]
  });
})();

// ================= 图4 月度收益热力图 =================
(function(){
  var hmin = Infinity, hmax = -Infinity;
  DATA.heat.forEach(function(d){ if(d[2]<hmin)hmin=d[2]; if(d[2]>hmax)hmax=d[2]; });
  var bound = Math.max(5, Math.min(40, Math.max(Math.abs(hmin), Math.abs(hmax))));
  mk('c4', {
    backgroundColor:'transparent',
    title: title('月度收益热力图（%）· 真·2%（资金滚存）', '红=涨 绿=跌 · 2014-01 ~ 2026-08'),
    tooltip: Object.assign(tip(), {formatter:function(p){
      var y = Number(p.value[1]) + 2014, m = p.value[0] + 1;
      return '<b>'+y+' 年 '+m+' 月</b><br/>收益：<b style="color:'+(p.value[2]>=0?UP:DOWN)+'">'+(p.value[2]>0?'+':'')+p.value[2]+'%</b>'; }}),
    grid:{left:44, right:92, top:40, bottom:36},
    xAxis: Object.assign({type:'category', data:['1月','2月','3月','4月','5月','6月','7月','8月','9月','10月','11月','12月'],
      splitArea:{show:true, areaStyle:{color:['rgba(30,37,48,.4)','rgba(22,27,36,.4)']}}}, AXIS),
    yAxis: Object.assign({type:'category', data:DATA.heatYears,
      splitArea:{show:true, areaStyle:{color:['rgba(30,37,48,.4)','rgba(22,27,36,.4)']}}}, AXIS),
    visualMap:{min:-bound, max:bound, calculable:true, orient:'vertical', right:6, top:'center',
      text:['涨','跌'], textStyle:{color:MUTED},
      inRange:{color:['#0d5c46','#1cbf8b','#2a3542','#e0444c','#8c1f2b']}},
    series:[{name:'月收益', type:'heatmap', data:DATA.heat,
      label:{show:true, fontSize:9, color:'#e8ecf2',
        formatter:function(p){ var v=p.value[2]; return v>0?('+'+v.toFixed(0)):(v.toFixed(0)); }},
      itemStyle:{borderColor:'#10141b', borderWidth:1.5},
      emphasis:{itemStyle:{shadowBlur:8, shadowColor:'rgba(255,255,255,.25)'}}}]
  });
})();

// ================= 图5 出场原因环形饼图 =================
(function(){
  var total = DATA.tradeN;
  var palette = ['#e0444c','#f0a24a','#8fa3b8','#5b7fb9','#9a7cc4','#4aa3a3','#c46a8a'];
  mk('c5', {
    backgroundColor:'transparent',
    title: title('出场原因分布 · 真·2%', '共 '+total.toLocaleString()+' 笔 · 移动止盈占大头'),
    tooltip: Object.assign(tip(), {formatter:function(p){
      return '<b>'+p.name+'</b><br/>笔数：'+p.value.toLocaleString()+'（'+p.data.pct+'%）<br/>平均单笔盈亏：<b style="color:'+(p.data.avg>=0?UP:DOWN)+'">'+(p.data.avg>0?'+':'')+p.data.avg+'%</b>'; }}),
    legend:{type:'scroll', bottom:2, textStyle:{color:MUTED, fontSize:11}, itemWidth:12, itemHeight:12},
    graphic:[
      {type:'text', left:'center', top:'36%', style:{text:total.toLocaleString(), fill:TXT, fontSize:24, fontWeight:700, textAlign:'center'}},
      {type:'text', left:'center', top:'48%', style:{text:'总笔数', fill:MUTED, fontSize:12, textAlign:'center'}},
    ],
    series:[{name:'出场原因', type:'pie', radius:['42%','70%'], center:['50%','45%'],
      itemStyle:{borderColor:BG, borderWidth:2, borderRadius:6,
        shadowBlur:10, shadowColor:'rgba(0,0,0,.4)'},
      label:{color:MUTED, formatter:'{b}\n{d}%', fontSize:11},
      labelLine:{lineStyle:{color:BORDER}},
      data:DATA.reasons.map(function(r, i){
        return {name:r.name, value:r.value, pct:r.pct, avg:r.avg, itemStyle:{color:palette[i % palette.length]}};
      })}]
  });
})();

// ================= 图6 单笔盈亏分布 =================
(function(){
  var p = DATA.prof;
  mk('c6', {
    backgroundColor:'transparent',
    title: title('单笔盈亏分布 · 真·2%', 'n='+DATA.tradeN.toLocaleString()+' · 两端截断 · 红盈绿亏'),
    tooltip: Object.assign(tip(), {trigger:'axis', axisPointer:{type:'shadow'},
      formatter:function(ps){ var d=ps[0]; return d.name+'<br/>笔数：<b>'+d.value.toLocaleString()+'</b>（'+(100*d.value/DATA.tradeN).toFixed(1)+'%）'; }}),
    grid:{left:56, right:22, top:44, bottom:64},
    xAxis: Object.assign({type:'category', data:p.labels, axisLabel:{color:MUTED, rotate:45, fontSize:10}}, AXIS),
    yAxis: Object.assign({type:'value', name:'笔数'}, SPLIT, {axisLabel:AXIS.axisLabel}),
    series:[{name:'笔数', type:'bar', data:p.counts.map(function(c, i){
        return {value:c, itemStyle:{color:grad(p.colors[i], 0.85), borderRadius:[4,4,0,0]}}; }),
      barMaxWidth:30,
      label:{show:true, position:'top', color:MUTED, fontSize:9, formatter:function(d){ return d.value? d.value.toLocaleString() : ''; }}}]
  });
})();

// ================= 图7 持仓天数分布 =================
(function(){
  var h = DATA.holds;
  mk('c7', {
    backgroundColor:'transparent',
    title: title('持仓天数分布 · 真·2%', '中位数 1 天 · 高频快进快出'),
    tooltip: Object.assign(tip(), {trigger:'axis', axisPointer:{type:'shadow'},
      formatter:function(ps){ var d=ps[0]; return d.name+'<br/>笔数：<b>'+d.value.toLocaleString()+'</b>（'+(100*d.value/DATA.tradeN).toFixed(1)+'%）'; }}),
    grid:{left:56, right:22, top:44, bottom:56},
    xAxis: Object.assign({type:'category', data:h.labels, axisLabel:{color:MUTED, rotate:30, fontSize:10}}, AXIS),
    yAxis: Object.assign({type:'value', name:'笔数'}, SPLIT, {axisLabel:AXIS.axisLabel}),
    series:[{name:'笔数', type:'bar', data:h.counts.map(function(c){
        return {value:c, itemStyle:{color:grad('#5b7fb9', 0.9), borderRadius:[4,4,0,0]}}; }),
      barMaxWidth:34,
      label:{show:true, position:'top', color:MUTED, fontSize:9, formatter:function(d){ return d.value? d.value.toLocaleString() : ''; }}}]
  });
})();

// ================= 图8 / 图10 Top10 股票 =================
function mkTop(id, t, data, color, unit){
  mk(id, {
    backgroundColor:'transparent',
    title: title(t, '按累计盈亏（元）聚合 · 代码去重'),
    tooltip: Object.assign(tip(), {formatter:function(p){
      return '<b>'+p.name+'</b><br/>累计盈亏：<b style="color:'+(p.value>=0?UP:DOWN)+'">'+fmtMoney(p.value)+'</b>'; }}),
    grid:{left:92, right:56, top:44, bottom:30},
    xAxis: Object.assign({type:'value'}, SPLIT, {axisLabel:Object.assign({}, AXIS.axisLabel, {formatter:function(v){ return fmtMoney(v); }})}),
    yAxis: Object.assign({type:'category', data:data.map(function(d){return d.name;}),
      axisLabel:{color:MUTED, fontSize:10}}, AXIS),
    series:[{name:'累计盈亏', type:'bar', data:data.map(function(d){
        return {value:d.value, itemStyle:{color:grad(color, 0.9), borderRadius:[0,4,4,0]}}; }),
      barMaxWidth:16,
      label:{show:true, position:'right', color:MUTED, fontSize:9,
        formatter:function(p){ return fmtMoney(p.value); }}}]
  });
}
mkTop('c8', 'Top10 盈利股票（红）· 真·2%', DATA.topWin, UP);
mkTop('c10', 'Top10 亏损股票（绿）· 真·2%', DATA.topLoss, DOWN);

// ================= 图9 资金利用率双轴 =================
mk('c9', {
  backgroundColor:'transparent',
  title: title('资金利用率 · 真·2%（年均单笔投入 vs 年交易笔数）', '投入额 log 轴：从几万滚到百亿级 → 流动性约束是硬天花板'),
  tooltip: Object.assign(tip(), {trigger:'axis',
    formatter:function(ps){ var s=['<b>'+ps[0].axisValue+' 年</b>'];
      ps.forEach(function(p){ s.push(p.marker+' '+p.seriesName+'：<b>'+fmtMoney(p.value)+'</b>'); });
      return s.join('<br/>'); }}),
  legend:{data:['年均单笔投入','年交易笔数'], top:36, textStyle:{color:MUTED}, itemWidth:18, itemHeight:10, icon:'roundRect'},
  grid:{left:70, right:60, top:66, bottom:36},
  xAxis: Object.assign({type:'category', data:DATA.cap.years}, AXIS),
  yAxis:[
    Object.assign({type:'log', name:'年均单笔投入(元)'}, SPLIT, {axisLabel:Object.assign({}, AXIS.axisLabel, {formatter:function(v){ return fmtMoney(v); }})}),
    Object.assign({type:'value', name:'笔数'}, {splitLine:{show:false}, axisLabel:AXIS.axisLabel}),
  ],
  series:[
    {name:'年均单笔投入', type:'bar', data:DATA.cap.amt.map(function(v){
        return {value:v, itemStyle:{color:grad('#5b7fb9', 0.9), borderRadius:[4,4,0,0]}}; }),
      barMaxWidth:26},
    {name:'年交易笔数', type:'line', yAxisIndex:1, data:DATA.cap.n,
      showSymbol:true, symbolSize:7, lineStyle:{width:2.2, color:UP}, itemStyle:{color:UP},
      label:{show:true, position:'top', color:MUTED, fontSize:9}},
  ]
});

// ================= 图11 单笔盈亏气泡散点 =================
(function(){
  mk('c11', {
    backgroundColor:'transparent',
    title: title('单笔盈亏 vs 持仓天数（气泡=单笔资金量，log）· 真·2%', '采样 '+DATA.scatter.length.toLocaleString()+' 笔 · 横轴持仓天数，纵轴单笔收益%'),
    tooltip: Object.assign(tip(), {formatter:function(p){
      return '<b>'+p.data[3]+'</b><br/>持仓：'+p.data[0]+' 天<br/>单笔收益：<b style="color:'+(p.data[1]>=0?UP:DOWN)+'">'+(p.data[1]>0?'+':'')+p.data[1]+'%</b><br/>单笔资金：'+fmtMoney(p.data[2]); }}),
    grid:{left:58, right:30, top:48, bottom:52},
    xAxis: Object.assign({type:'value', name:'持仓天数'}, SPLIT, {axisLabel:AXIS.axisLabel}),
    yAxis: Object.assign({type:'value', name:'单笔收益 %'}, SPLIT, {axisLabel:AXIS.axisLabel}),
    series:[{name:'单笔', type:'scatter', data:DATA.scatter,
      symbolSize:function(v){ return v[2]; },
      itemStyle:{opacity:0.62, shadowBlur:2, shadowColor:'rgba(0,0,0,.4)',
        color:function(p){ return p.data[1] >= 0 ? UP : DOWN; }},
      emphasis:{itemStyle:{opacity:0.95, borderColor:'#fff', borderWidth:1}},
      markLine:{silent:true, symbol:'none', lineStyle:{color:'#3a4452', type:'dashed'},
        data:[{yAxis:0, label:{formatter:'盈亏平衡线', color:MUTED, position:'insideEndTop'}}]}}]
  });
})();

window.addEventListener('resize', function(){ charts.forEach(function(c){ c.resize(); }); });
"""


def render(echarts_js, payload_json, cards_html):
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QUANTQQ 5m 真·2% 回测可视化仪表盘 · 2014-01 ~ 2026-08</title>
<script>""" + echarts_js + """</script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background:#0f1115; color:#d3d8e0; font-family:"Microsoft YaHei","PingFang SC",sans-serif; padding:24px 28px 48px; }
  h1 { font-size:22px; margin-bottom:4px; }
  .sub { color:#8a93a3; font-size:13px; margin-bottom:20px; }
  .cards { display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin-bottom:18px; }
  .mcard { background:#161b24; border:1px solid #262d3a; border-radius:12px; padding:14px 18px;
           box-shadow:0 2px 10px rgba(0,0,0,.3); }
  .mcard.main { border-color:var(--accent); box-shadow:0 0 0 1px var(--accent), 0 4px 18px rgba(0,0,0,.45); }
  .mhead { font-size:14px; font-weight:700; color:var(--accent); padding-bottom:8px; margin-bottom:6px;
           border-bottom:2px solid var(--accent); }
  .mrow { display:flex; justify-content:space-between; padding:3.5px 0; font-size:13px; color:#8a93a3; }
  .mrow b { color:#d3d8e0; font-weight:600; font-variant-numeric:tabular-nums; }
  .mcard.main .mrow b { color:#fff; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
  .panel { background:#161b24; border:1px solid #262d3a; border-radius:12px; padding:10px;
           box-shadow:0 2px 10px rgba(0,0,0,.3); }
  .panel.wide { grid-column:1 / -1; }
  .chart { width:100%; height:380px; }
  .chart.tall { height:430px; }
  .redline { background:#241319; border:1px solid #5a2430; border-radius:12px; padding:14px 18px; margin-top:18px; }
  .redline h3 { color:#e0444c; font-size:15px; margin-bottom:8px; }
  .redline li { font-size:13px; line-height:1.8; color:#c8b9bd; margin-left:18px; }
  .note { color:#8a93a3; font-size:12px; margin-top:12px; }
  @media (max-width:960px) { .cards { grid-template-columns:1fr; } .grid2 { grid-template-columns:1fr; } }
</style>
</head>
<body>
<h1>QUANTQQ 5m 真·2% 回测可视化仪表盘</h1>
<div class="sub">周期 2014-01 ~ 2026-08 · 5m · bpday=48 · 三口径对比：固定2万 / 真·2%（资金滚存）/ 真·10% · 红涨绿跌 · 未计入流动性约束</div>

<div class="cards">""" + cards_html + """</div>

<div class="grid2">
  <div class="panel wide"><div id="c1" class="chart tall"></div></div>
  <div class="panel"><div id="c2" class="chart"></div></div>
  <div class="panel"><div id="c3" class="chart"></div></div>
  <div class="panel wide"><div id="c4" class="chart tall"></div></div>
  <div class="panel"><div id="c5" class="chart"></div></div>
  <div class="panel"><div id="c6" class="chart"></div></div>
  <div class="panel"><div id="c7" class="chart"></div></div>
  <div class="panel"><div id="c8" class="chart"></div></div>
  <div class="panel wide"><div id="c9" class="chart tall"></div></div>
  <div class="panel"><div id="c10" class="chart"></div></div>
  <div class="panel wide"><div id="c11" class="chart tall"></div></div>
</div>

<div class="redline">
  <h3>⚠ 红线声明（勿删）</h3>
  <ul>
    <li><b>未计入流动性约束</b>：年化 212%（真·2%）是「2% 仓位 + 资金滚存 + 不限流动性」下的数学结果，现实中单笔 2% 资金也会撞上成交额/涨跌停限制，不可当作可实现收益。</li>
    <li>本次仅改仓位口径，信号、费率、滑点逻辑与基线完全一致，并非新策略。</li>
    <li>回测绝对值系统性偏乐观（历史审计结论）；横向对比（2% vs 10% 风险调整后）相对更可信：2% 的 Sharpe 1.38 / 回撤 -5.94% 优于 10% 的 1.12 / -10.92%。</li>
  </ul>
</div>
<div class="note">数据源：output/quantqq_2014_5m_pct2（equity_curve.csv / trades.csv / metrics.json）与两个对比口径目录 · 生成脚本 research/viz_quantqq_5m_pct_dashboard.py</div>

<script>
var DATA = __DATA_JSON__;
if (typeof echarts === 'undefined') {
  document.body.insertAdjacentHTML('afterbegin',
    '<div style="background:#241319;color:#e0444c;padding:12px;font-size:14px;">ECharts 加载失败：请确认 web/echarts.min.js 与本报告相对路径正确。</div>');
} else {
__CHART_JS__
}
</script>
</body>
</html>"""


# ---------------------------------------------------------------- 主流程
def main():
    daily_map = {}
    metrics = {}
    yearly_map = {}
    for key, d in DIRS.items():
        daily_map[key] = load_daily_eq(os.path.join(d, "equity_curve.csv"))
        metrics[key] = load_metrics(os.path.join(d, "metrics.json"))
        yearly_map[key] = yearly_returns(daily_map[key])

    trades = load_trades(os.path.join(DIRS["pct2"], "trades.csv"))

    # 自检（对照接力包关键数字）
    for y in ("2015", "2020", "2024"):
        print("  逐年抽查 %s: 真·2%% = %s" % (y, fmt_pct(yearly_map["pct2"].get(y, 0))))
    print("  总笔数 = %d (期望 37525)" % len(trades))
    win = sum(1 for t in trades if float(t["profit_pct"]) > 0) / len(trades)
    print("  胜率 = %.1f%% (期望 81.4%%)" % (win * 100))

    payload = build_payload(daily_map, metrics, yearly_map, trades)

    echarts_js = ""
    if os.path.exists(ECHARTS_SRC):
        echarts_js = open(ECHARTS_SRC, encoding="utf-8").read()
    else:
        CHART_JS_PLACEHOLDER = 'document.write(\'<script src="../../web/echarts.min.js"><\\/script>\');\n' + CHART_JS
        print("WARN: web/echarts.min.js 不存在，回退为相对路径引用")

    html = render(
        echarts_js,
        json.dumps(payload, ensure_ascii=False),
        build_metric_cards(metrics),
    )
    if echarts_js:
        html = html.replace("__CHART_JS__", CHART_JS)
    else:
        html = html.replace("__CHART_JS__", CHART_JS_PLACEHOLDER)
    html = html.replace("__DATA_JSON__", json.dumps(payload, ensure_ascii=False))

    out = os.path.join(OUT_DIR, "analysis_dashboard.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print("已生成:", out, "%.1f MB" % (os.path.getsize(out) / 1e6))


if __name__ == "__main__":
    main()
