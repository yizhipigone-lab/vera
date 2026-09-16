// ====== VERA 大盘位置 TAB (2026-09-17) — 位置 / 宽度 / 照镜子 / 择时影子 ======
// 零 import 依赖（data_cache.js 同款 seam 模式）：fetch 由浏览器提供，
// Node 测试通过 module.exports 拿纯函数。
// 生命周期钩子由 vera-ui.js switchTab 调用: window.marketPageEnter。
//
// 口径说明（页面上的数字全部来自后端，前端不做任何二次计算）:
//   十年百分位 = 现在的位置比过去十年百分之多少的交易日高；
//   站上20日均线占比 = 市场宽度（战场上还有多少士兵在冲锋）；
//   连续录像 = data/market_position/daily.jsonl，一天一行，由调度器盘后写入。

(function () {

// ── 纯函数（Node 可测）─────────────────────────────────

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
  });
}

// 位置分档：只描述"贵不贵"，不做任何买卖建议（业务铁律 1）
function positionLabel(pct) {
  if (pct == null) return { text: '【缺】', cls: 'warn' };
  // 这 5 个词必须与体温表 core/market_position_runner.py::_position_plain 逐字相同：
  // 同一条分档规则的第二份实现（浏览器跑不了 Python），两侧各有测试把这 5 个词锁死。
  if (pct >= 80) return { text: '偏贵区', cls: 'hot' };
  if (pct >= 60) return { text: '偏高', cls: 'warm' };
  if (pct >= 40) return { text: '中间', cls: 'mid' };
  if (pct >= 20) return { text: '偏低', cls: 'cool' };
  return { text: '便宜区', cls: 'cold' };
}

function fmtPct(v, signed) {
  if (v == null) return '—';
  var n = Number(v);
  if (!isFinite(n)) return '—';
  return (signed && n > 0 ? '+' : '') + n.toFixed(1) + '%';
}

function fmtNum(v, nd) {
  if (v == null) return '—';
  var n = Number(v);
  if (!isFinite(n)) return '—';
  return n.toFixed(nd == null ? 2 : nd);
}

var REGIME_CN = { bull: '牛', bear: '熊', range: '震荡' };
// 牛熊背景带的颜色（照外部研究报告的做法：牛绿 / 震荡灰 / 熊红，浅色打底不盖线）
var REGIME_BAND_COLOR = { bull: 'rgba(61,190,120,0.10)',
                          range: 'rgba(150,150,150,0.10)',
                          bear: 'rgba(230,90,90,0.10)' };

// 三条候选规则的"人话名字"（页面上不许出现裸英文键名）
var RULE_CN = {
  ma20: '沪深300 收盘站上自己的 20 日均线就满仓',
  breadth50: '全市场一半以上股票站上 20 日均线就满仓',
  regime: '项目牛熊口径判为「牛」就满仓',
  buy_hold: '什么都不做，一直拿着（对照用）'
};

function pctColor(v) {
  if (v == null) return 'var(--text2)';
  var n = Number(v);
  if (!isFinite(n) || n === 0) return 'var(--text2)';
  return n > 0 ? 'var(--up)' : 'var(--down)';
}

// 三大指数位置表（口径与体温表 Markdown 完全一致）
function positionRowsHtml(rec) {
  var idx = (rec && rec.indices) || {};
  var order = [['shanghai', '上证指数', '000001.SH'],
               ['hs300', '沪深300', '000300.SH'],
               ['chuangyeban', '创业板指', '399006.SZ']];
  return order.map(function (o) {
    var it = idx[o[0]] || {};
    var lab = positionLabel(it.pct_10y);
    var color = { hot: 'var(--up)', warm: 'var(--up)', mid: 'var(--text2)',
                  cool: 'var(--down)', cold: 'var(--down)',
                  warn: 'var(--text2)' }[lab.cls];
    return '<tr>'
      + '<td>' + esc(o[1]) + '(' + esc(o[2]) + ')</td>'
      + '<td>' + fmtNum(it.close, 2) + '</td>'
      + '<td style="color:' + color + ';font-weight:600">' + fmtPct(it.pct_10y)
      + ' <span style="font-weight:400">' + lab.text + '</span></td>'
      + '<td>' + fmtPct(it.from_high_pct, true) + '</td>'
      + '<td>' + fmtPct(it.vol_ann_20) + '</td>'
      + '<td>' + fmtPct(it.ret_1y_pct, true) + '</td>'
      + '<td>' + fmtPct(it.ma250_dev_pct, true) + '</td>'
      + '<td>' + (REGIME_CN[it.regime] || '【缺】') + '</td>'
      + '</tr>';
  }).join('');
}

// 照镜子表格：历史上最像的几天，之后实际怎么走
function mirrorRowsHtml(md) {
  var ms = (md && md.matches) || [];
  if (!ms.length) return '<tr><td colspan="5" style="text-align:left;color:var(--text2)">'
    + '没有可用的相似日（连续录像太短？先跑一次全量回填）</td></tr>';
  return ms.map(function (m) {
    function cell(v) {
      if (v == null) return '<td>—</td>';
      return '<td style="color:' + pctColor(v) + '">' + fmtPct(v, true) + '</td>';
    }
    return '<tr><td>' + esc(m.date) + '</td><td>' + fmtNum(m.distance, 2) + '</td>'
      + cell(m.fwd_20_hs300_pct) + cell(m.fwd_60_hs300_pct) + cell(m.fwd_20_sh_pct)
      + '</tr>';
  }).join('');
}

// 照镜子结论句（基于"距离最近的一档"，不是 top-5 的中位数 —— 5 个样本不构成统计量）
function mirrorSummaryHtml(md) {
  if (!md || !md.ok) return esc((md && md.reason) || '不可用');
  var s = md.summary || {};
  return '能当参照的历史交易日有 <b>' + s.n + '</b> 天（最像的一档，'
    + '已排除最近 ' + md.exclude_recent + ' 个交易日 —— 不许拿上个月冒充"历史"）。'
    + '这些日子之后 20 个交易日（约一个月），沪深300 涨跌的<b>中位数是 '
    + fmtPct(s.fwd_20_median, true) + '</b>，平均 ' + fmtPct(s.fwd_20_mean, true)
    + '，中间一半落在 ' + fmtPct(s.fwd_20_q25, true) + ' 到 '
    + fmtPct(s.fwd_20_q75, true) + ' 之间，上涨的占 ' + fmtPct(s.fwd_20_up_ratio)
    + '。<br><b>但要狠狠打个折</b>：这些命中日挨得很近、涨跌高度重叠，'
    + '<b>真正独立的信息只有大约 ' + fmtNum(s.n_eff_20, 1) + ' 份</b>；'
    + '往后看 60 个交易日（约三个月）中位数 ' + fmtPct(s.fwd_60_median, true)
    + '、上涨占比 ' + fmtPct(s.fwd_60_up_ratio)
    + '（独立信息约 ' + fmtNum(s.n_eff_60, 1) + ' 份）。';
}

// 按年份拆解：看有没有哪一年在唱独角戏
function mirrorYearsHtml(md) {
  var ys = (md && md.years) || [];
  if (!ys.length) return '<tr><td colspan="4" style="text-align:left;color:var(--text2)">'
    + '没有可拆解的年份</td></tr>';
  return ys.map(function (y) {
    return '<tr><td>' + esc(y.year) + ' 年</td><td>' + y.n + ' 天</td>'
      + '<td style="color:' + pctColor(y.median_pct) + '">' + fmtPct(y.median_pct, true)
      + '</td><td>' + fmtPct(y.up_ratio_pct) + '</td></tr>';
  }).join('');
}

function shadowRowsHtml(sd) {
  if (!sd || !sd.ok) {
    return '<tr><td colspan="8" style="text-align:left;color:var(--text2)">'
      + esc((sd && sd.reason) || '不可用') + '</td></tr>';
  }
  var rows = (sd.rows || []).concat(sd.buy_hold ? [sd.buy_hold] : []);
  return rows.map(function (r) {
    if (!r) return '';
    var net = r.net || {}, seg = r.segments || {};
    return '<tr><td>' + esc(RULE_CN[r.rule] || r.rule) + '</td>'
      + '<td>' + (r.round_trips == null ? '—' : r.round_trips + ' 次') + '</td>'
      + '<td>' + fmtPct(r.annualized_pct, true) + '</td>'
      + '<td style="font-weight:600;color:' + pctColor(net.annualized_pct) + '">'
      + fmtPct(net.annualized_pct, true) + '</td>'
      + '<td>' + fmtPct(net.ci_low_pct, true) + ' ~ ' + fmtPct(net.ci_high_pct, true)
      + '</td>'
      + '<td style="color:' + pctColor(net.max_drawdown_pct) + '">'
      + fmtPct(net.max_drawdown_pct, true) + '</td>'
      + '<td>' + fmtPct(r.exposure_pct) + '</td>'
      + '<td style="color:' + pctColor(seg.mean_return_pct) + '">'
      + fmtPct(seg.mean_return_pct, true)
      + ' <span style="color:var(--text2)">(' + fmtPct(seg.win_ratio_pct)
      + ' 笔赚钱)</span></td></tr>';
  }).join('');
}

// 逐条人话结论（措辞纪律：区间跨过 0 只说"看不出显著优劣"，不说"无效/跑输"）
function shadowVerdictHtml(sd) {
  if (!sd || !sd.ok) return '';
  return (sd.rows || []).map(function (r) {
    var net = r.net || {}, seg = r.segments || {};
    var lo = net.ci_low_pct, hi = net.ci_high_pct, verdict;
    if (lo == null || hi == null) verdict = '数据不足，判不了';
    else if (lo <= 0 && hi >= 0) verdict = '看不出显著的优势或劣势（区间跨过 0）';
    else if (hi < 0) verdict = '明显比一直拿着差（整个区间都在 0 以下）';
    else verdict = '明显比一直拿着好（整个区间都在 0 以上）';
    return '<div><b>' + esc(RULE_CN[r.rule] || r.rule) + '</b>：'
      + '一共建仓 ' + r.round_trips + ' 次，每次持仓中位 '
      + fmtNum(seg.median_days, 0) + ' 个交易日（最短 ' + seg.min_days
      + ' 天、最长 ' + seg.max_days + ' 天），在场时间占 ' + fmtPct(r.exposure_pct)
      + '。按每一笔算（已扣费）：平均 ' + fmtPct(seg.mean_return_pct, true)
      + '、中位 ' + fmtPct(seg.median_return_pct, true) + '、赚钱的只占 '
      + fmtPct(seg.win_ratio_pct) + ' —— 多数小亏、少数大赚，是趋势类规则的典型长相。'
      + '结论：<b>' + verdict + '</b>。'
      + (seg.note ? '<span style="color:var(--text2)">（' + esc(seg.note) + '）</span>' : '')
      + '</div>';
  }).join('');
}

function shadowWindowsHtml(sd) {
  if (!sd || !sd.ok) return '';
  var rows = (sd.rows || []).concat(sd.buy_hold ? [sd.buy_hold] : []);
  return rows.map(function (r) {
    if (!r) return '';
    var w = r.windows || {}, wi = w['in'] || {}, wo = w.out || {};
    return '<tr><td>' + esc(RULE_CN[r.rule] || r.rule) + '</td>'
      + '<td style="color:' + pctColor(wi.annualized_pct) + '">'
      + fmtPct(wi.annualized_pct, true) + '</td>'
      + '<td style="color:' + pctColor(wo.annualized_pct) + '">'
      + fmtPct(wo.annualized_pct, true) + '</td>'
      + '<td>' + (w.consistent ? '✅ 同向，算数' : '⚠ 不一致，待复核') + '</td></tr>';
  }).join('');
}

// 趋势图数据：把连续录像转成 ECharts 需要的两个序列
function trendSeries(items) {
  var dates = [], width = [], pct = [];
  (items || []).forEach(function (r) {
    dates.push(r.date);
    var b = r.breadth || {};
    var sh = ((r.indices || {}).shanghai) || {};
    width.push(b.above_ma20_pct == null ? null : Number(b.above_ma20_pct));
    pct.push(sh.pct_10y == null ? null : Number(sh.pct_10y));
  });
  return { dates: dates, width: width, pct: pct };
}

// 取某条录像里指定指数/口径的牛熊标签
function regimeOf(rec, key, caliber) {
  var it = ((rec || {}).indices || {})[key || 'hs300'] || {};
  var v = caliber === 'regime_20' ? it.regime_20 : it.regime;
  return v == null ? null : String(v);
}

// **牛熊背景着色带**（照抄外部研究报告的做法）：把连续的同一状态合成一段，
// 供 ECharts markArea 在时间轴上按牛/震荡/熊涂底色。一眼看出"位置是在什么大势里"。
function regimeBands(items, key, caliber) {
  var out = [], cur, prev = null;
  (items || []).forEach(function (r) {
    var st = regimeOf(r, key, caliber);
    if (st !== cur) {
      if (cur != null && prev != null) { out[out.length - 1].end = prev; }
      out.push({ start: r.date, end: r.date, state: st });
      cur = st;
    }
    prev = r.date;
  });
  if (out.length) { out[out.length - 1].end = prev; }
  return out.filter(function (b) { return b.state; });
}

// 箱线图的五个数：最小 / 下四分位 / 中位 / 上四分位 / 最大
function boxStats(vals) {
  var s = (vals || []).filter(function (v) {
    return v != null && isFinite(Number(v));
  }).map(Number).sort(function (a, b) { return a - b; });
  if (!s.length) return null;
  function q(p) {
    var i = (s.length - 1) * p, lo = Math.floor(i), hi = Math.ceil(i);
    return lo === hi ? s[lo] : s[lo] + (s[hi] - s[lo]) * (i - lo);
  }
  return [+s[0].toFixed(1), +q(0.25).toFixed(1), +q(0.5).toFixed(1),
          +q(0.75).toFixed(1), +s[s.length - 1].toFixed(1)];
}

// **按牛/震荡/熊分组的箱线图**（外部研究里的那张"验证图"）：
// 如果某个指标真有区分度，箱体应该明显错开；箱体叠在一起就说明它没区分度。
function boxByRegime(items, key, caliber) {
  return ['bull', 'range', 'bear'].map(function (st) {
    var vals = [];
    (items || []).forEach(function (r) {
      if (regimeOf(r, key, caliber) !== st) return;
      var it = ((r.indices || {})[key || 'hs300']) || {};
      var v = it.pct_10y;
      if (v != null) vals.push(Number(v));
    });
    var b = boxStats(vals);
    return b ? { state: st, n: vals.length, box: b } : null;
  }).filter(Boolean);
}

// 估值（贵不贵）：一句话 + 十年百分位。数据来自记录里的 valuation（一天一行）
function valuationHtml(rec) {
  var v = (rec && rec.valuation) || null;
  if (!v) {
    return '<div style="color:var(--text2)">【缺】没有本地 ERP（股债性价比）缓存 —— '
      + '补的办法：点上面的「立即采集」（会联网拉一次）。</div>';
  }
  var hi = v.erp_pct_10y;
  var feel = hi == null ? '' :
    (hi >= 70 ? '<b>过去十年里只有很少的时间比现在更划算</b> —— 股票相对国债的吸引力偏高'
      : hi >= 40 ? '处在中间水平，谈不上特别划算也谈不上特别贵'
        : '比过去十年大多数时候都贵 —— 股票相对国债的吸引力偏低');
  return '<div style="font-size:var(--fs-sm);line-height:1.8">'
    + '<b>股债性价比 ' + fmtNum(v.erp_pct, 2) + '%</b>'
    + '（= 沪深300 的盈利收益率 1/PE 减掉 10 年期国债收益率），'
    + '处在<b>过去十年 ' + fmtPct(v.erp_pct_10y) + ' 分位</b>。<br>'
    + '<b>怎么读</b>：这个数<b>越高越划算</b>'
    + '（拿着股票的预期回报比拿着国债强多少）。' + feel + '。<br>'
    + '十年中位数 ' + fmtNum(v.erp_median_10y_pct, 2) + '%，'
    + '十年区间 ' + fmtNum(v.erp_min_10y_pct, 2) + '% ~ '
    + fmtNum(v.erp_max_10y_pct, 2) + '%。'
    + '<span style="color:var(--text2)">口径：' + esc(v.caliber)
    + '（数据日 ' + esc(v.asof) + '，十年窗口 ' + v.n_obs + ' 个交易日）。'
    + '<b>注意这是沪深300 口径，不是「全市场」口径。</b>'
    + '体检结果：这是唯一同时通过 3/6/12 个月检验的正向维度。</span></div>';
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { esc: esc, positionLabel: positionLabel, fmtPct: fmtPct,
                     fmtNum: fmtNum, pctColor: pctColor, RULE_CN: RULE_CN,
                     positionRowsHtml: positionRowsHtml,
                     mirrorRowsHtml: mirrorRowsHtml,
                     mirrorSummaryHtml: mirrorSummaryHtml,
                     mirrorYearsHtml: mirrorYearsHtml,
                     shadowRowsHtml: shadowRowsHtml,
                     shadowVerdictHtml: shadowVerdictHtml,
                     shadowWindowsHtml: shadowWindowsHtml,
                     trendSeries: trendSeries,
                     regimeOf: regimeOf, regimeBands: regimeBands,
                     boxStats: boxStats, boxByRegime: boxByRegime,
                     valuationHtml: valuationHtml,
                     REGIME_BAND_COLOR: REGIME_BAND_COLOR };
  return;
}

// ── 浏览器侧（DOM + fetch）─────────────────────────────

function $(id) { return document.getElementById(id); }

function setHint(id, text, color) {
  var el = $(id);
  if (!el) return;
  el.textContent = text || '';
  el.style.color = color || 'var(--text2)';
}

function refresh() {
  setHint('mpHint', '加载中…');
  return fetch('/api/market_position/latest').then(function (r) { return r.json(); })
    .then(function (d) {
      if (!d.success) { setHint('mpHint', d.error || '加载失败', 'var(--up)'); return; }
      var rec = d.record;
      if (!rec) {
        setHint('mpHint', d.reason || '还没有连续录像', 'var(--up)');
        $('mpSummary').textContent = '【缺】还没有采集过。点「全量回填」先建十年录像。';
        return;
      }
      var stale = rec.stale
        ? ' ⚠ 数据滞后（最新有效交易日 ' + rec.date + '，应有 ' + rec.expected_date + '）' : '';
      setHint('mpHint', '数据日期 ' + rec.date + ' · 连续录像 ' + d.lines + ' 条' + stale,
              rec.stale ? 'var(--up)' : 'var(--down)');
      var b = rec.breadth || {}, t = rec.turnover || {}, lim = rec.limit || {};
      $('mpSummary').textContent =
        '全A ' + b.traded + ' 只有成交；站上20日均线 ' + fmtPct(b.above_ma20_pct)
        + '；创60日新高 ' + b.new_high_60 + ' 家 vs 新低 ' + b.new_low_60
        + ' 家（差 ' + b.hl_spread + '）；全A成交额 '
        + (t.amount_yi == null ? '—' : Number(t.amount_yi).toLocaleString() + '亿元')
        + '（一年百分位 ' + fmtPct(t.amount_pct_1y) + '）；涨停 ' + lim.up
        + ' 家 / 跌停 ' + lim.down + ' 家。';
      $('mpPositionBody').innerHTML = positionRowsHtml(rec);
      $('mpValuation').innerHTML = valuationHtml(rec);
      var sh = rec.shadow || {};
      $('mpShadowNow').textContent = '今天这三条规则各自怎么说：' + ['ma20', 'breadth50', 'regime']
        .map(function (k) {
          return RULE_CN[k] + ' → ' + (sh[k] === 'on' ? '在场内' : '空仓');
        }).join('；') + '（只是影子记录，系统绝不会按它下单）';
    }).catch(function (e) { setHint('mpHint', '加载失败: ' + e, 'var(--up)'); });
}

function drawTrend() {
  var box = $('mpTrend');
  if (!box || !window.echarts) return;
  fetch('/api/market_position/history?limit=250').then(function (r) { return r.json(); })
    .then(function (d) {
      if (!d.success) return;
      var s = trendSeries(d.items);
      // 牛熊背景着色带（照外部研究报告的做法）：一眼看出"这段位置处在什么大势里"
      var bands = regimeBands(d.items, 'hs300', 'regime').map(function (b) {
        return [{ xAxis: b.start, itemStyle: { color: REGIME_BAND_COLOR[b.state] } },
                { xAxis: b.end }];
      });
      var chart = echarts.getInstanceByDom(box) || echarts.init(box);
      chart.setOption({
        tooltip: { trigger: 'axis' },
        legend: { data: ['站上20日均线占比', '上证十年百分位'], textStyle: { color: '#aaa' } },
        grid: { left: 48, right: 56, top: 36, bottom: 28 },
        xAxis: { type: 'category', data: s.dates, axisLabel: { color: '#888' } },
        yAxis: [
          { type: 'value', name: '宽度%', max: 100, min: 0, axisLabel: { color: '#888' } },
          { type: 'value', name: '十年分位%', max: 100, min: 0, axisLabel: { color: '#888' } }
        ],
        series: [
          { name: '站上20日均线占比', type: 'line', showSymbol: false, data: s.width,
            lineStyle: { width: 1.5 }, color: '#4da3ff',
            markArea: { silent: true, data: bands },
            markLine: { silent: true, symbol: 'none', label: { color: '#888', formatter: '50%' },
                        lineStyle: { color: '#888', type: 'dashed' },
                        data: [{ yAxis: 50 }] } },
          { name: '上证十年百分位', type: 'line', yAxisIndex: 1, showSymbol: false,
            data: s.pct, lineStyle: { width: 1.2, type: 'dotted' }, color: '#e0a458' }
        ]
      });
    }).catch(function () { /* 图表失败不影响表格 */ });
}

// 按牛/震荡/熊分组的箱线图：这张图是"指标有没有区分度"的照妖镜 ——
// 真有区分度 → 三个箱子明显错开；没区分度 → 三个箱子叠在一起。
function drawBox() {
  var box = $('mpBox');
  if (!box || !window.echarts) return;
  fetch('/api/market_position/history?limit=0').then(function (r) { return r.json(); })
    .then(function (d) {
      if (!d.success) return;
      var items = d.items || [];
      var groups = boxByRegime(items, 'shanghai', 'regime');
      var chart = echarts.getInstanceByDom(box) || echarts.init(box);
      if (!groups.length) {
        chart.clear();
        chart.setOption({ title: { text: '数据不足，画不了', textStyle: { color: '#888', fontSize: 13 } } });
        return;
      }
      chart.setOption({
        tooltip: { trigger: 'item' },
        grid: { left: 48, right: 24, top: 36, bottom: 28 },
        xAxis: { type: 'category',
                 data: groups.map(function (g) { return REGIME_CN[g.state] + '（' + g.n + ' 天）'; }),
                 axisLabel: { color: '#888' } },
        yAxis: { type: 'value', name: '上证十年百分位%', max: 100, min: 0,
                 axisLabel: { color: '#888' } },
        series: [{ name: '上证十年百分位', type: 'boxplot',
                   data: groups.map(function (g) { return g.box; }),
                   itemStyle: { color: '#4da3ff', borderColor: '#4da3ff' } }]
      });
    }).catch(function () { /* 图表失败不影响表格 */ });
}

function loadMirror() {
  fetch('/api/market_position/mirror?top_n=5').then(function (r) { return r.json(); })
    .then(function (d) {
      if (!d.success) { $('mpMirrorBody').innerHTML = '<tr><td colspan="5">加载失败</td></tr>'; return; }
      if (!d.ok) {
        var msg = '<tr><td colspan="5" style="text-align:left;color:var(--text2)">'
          + esc(d.reason || '不可用') + '</td></tr>';
        $('mpMirrorBody').innerHTML = msg;
        $('mpMirrorYears').innerHTML = '<tr><td colspan="4" style="text-align:left;color:var(--text2)">不可用</td></tr>';
        $('mpMirrorSummary').textContent = '';
        $('mpMirrorWarn').textContent = '';
        return;
      }
      $('mpMirrorBody').innerHTML = mirrorRowsHtml(d);
      $('mpMirrorSummary').innerHTML = mirrorSummaryHtml(d);
      $('mpMirrorYears').innerHTML = mirrorYearsHtml(d);
      $('mpMirrorWarn').textContent = d.warning || '';
    }).catch(function () { });
}

function loadShadow() {
  fetch('/api/market_position/shadow').then(function (r) { return r.json(); })
    .then(function (d) {
      if (!d.success) return;
      $('mpShadowBody').innerHTML = shadowRowsHtml(d);
      $('mpShadowVerdict').innerHTML = shadowVerdictHtml(d);
      $('mpShadowWindows').innerHTML = shadowWindowsHtml(d)
        || '<tr><td colspan="4" style="text-align:left;color:var(--text2)">不可用</td></tr>';
      $('mpShadowCaliber').textContent = d.ok
        ? ('回放口径: ' + d.caliber + '（' + d.start + ' ~ ' + d.end + '）') : '';
    }).catch(function () { });
}

function showReport() {
  var box = $('mpReport');
  if (!box) return;
  if (box.style.display === 'block') { box.style.display = 'none'; return; }
  fetch('/api/market_position/report').then(function (r) { return r.json(); })
    .then(function (d) {
      box.textContent = d.success ? d.markdown : ('生成失败: ' + (d.error || d.detail || ''));
      box.style.display = 'block';
    }).catch(function (e) { box.textContent = '生成失败: ' + e; box.style.display = 'block'; });
}

function showReview() {
  var box = $('mpReview');
  if (!box) return;
  if (box.style.display === 'block') { box.style.display = 'none'; return; }
  box.textContent = '生成中…';
  box.style.display = 'block';
  fetch('/api/market_position/review').then(function (r) { return r.json(); })
    .then(function (d) {
      box.textContent = d.success ? d.markdown
        : ('生成失败: ' + (d.error || d.detail || ''));
    }).catch(function (e) { box.textContent = '生成失败: ' + e; });
}

function doCollect(backfill) {
  var btn = $(backfill ? 'mpBackfillBtn' : 'mpCollectBtn');
  if (btn) { btn.disabled = true; btn.dataset.old = btn.textContent; btn.textContent = '采集中…'; }
  setHint('mpHint', backfill ? '全量回填中（约 1 分钟）…' : '采集中（约 30 秒）…');
  fetch('/api/market_position/collect?backfill=' + (backfill ? 'true' : 'false'),
        { method: 'POST' })
    .then(function (r) { return r.json().then(function (j) { return [r.status, j]; }); })
    .then(function (pair) {
      var st = pair[0], d = pair[1];
      if (st >= 400 || !d.success) {
        setHint('mpHint', d.detail || d.error || d.reason || '采集失败', 'var(--up)');
      } else {
        setHint('mpHint', '采集完成: 数据日期 ' + d.asof + '，本次 ' + d.records
                + ' 条，录像共 ' + d.lines + ' 条', 'var(--down)');
        refresh(); drawTrend(); drawBox(); loadMirror(); loadShadow();
      }
    }).catch(function (e) { setHint('mpHint', '采集失败: ' + e, 'var(--up)'); })
    .then(function () {
      if (btn) { btn.disabled = false; btn.textContent = btn.dataset.old || '采集'; }
    });
}

function enter() {
  refresh(); drawTrend(); drawBox(); loadMirror(); loadShadow();
  var c = $('mpCollectBtn'), b = $('mpBackfillBtn'), r = $('mpRefreshBtn'),
      rep = $('mpReportBtn'), rev = $('mpReviewBtn');
  if (c && !c.dataset.bound) { c.dataset.bound = '1'; c.addEventListener('click', function () { doCollect(false); }); }
  if (b && !b.dataset.bound) { b.dataset.bound = '1'; b.addEventListener('click', function () { doCollect(true); }); }
  if (r && !r.dataset.bound) { r.dataset.bound = '1'; r.addEventListener('click', function () { enter(); }); }
  if (rep && !rep.dataset.bound) { rep.dataset.bound = '1'; rep.addEventListener('click', showReport); }
  if (rev && !rev.dataset.bound) { rev.dataset.bound = '1'; rev.addEventListener('click', showReview); }
}

window.marketPageEnter = enter;

})();
