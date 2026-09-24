// market_dashboard.js — 大盘环境仪表盘·前端渲染 (2026-09-18 四页签改造)
//
// 职责: 把 #pageMarket 渲染成"状态栏 + 4 个子页签(总览/指标明细/事件跟踪/历史走势)"。
// 数据源: GET /api/market_position/dashboard 系列(后端 core/market_dashboard_runner.py)。
//
// 注(2026-09-19 用户拍板): 旧视图卡片(三大指数位置/估值/照镜子/分桶/影子规则/今日复盘/
// 完整体温表)已**全删**, 本 TAB 只留四页签仪表盘; market_position.js 不再被页面引用,
// 其后端 API(/api/market_position/latest|report|review 等)与每天 15:55 的飞书复盘推送不受影响。
//
// 文案口径(用户规则): 大白话, 术语配白话, 总分是"温度分"(描述现状不预测涨跌),
// 环境参照仓位只读参照不联实盘(业务铁律1)。
//
// 2026-09-19 UIUX 改造(计划书 docs/plan/2026-09-19_UIUX综合改造_计划书.md):
//   ① 修 renderHistorySummary 运算符优先级 bug (原实现永远丢失前半句, Node 实测复现);
//   ② 写死颜色全灭 —— 样式迁入 index.html 内联 <style> 走令牌, JS 只加 class;
//   ③ 转 ES module: 复用 charts.js 的 getColors/hexToRgba/echartsInit (图表随主题换血)
//      与 decision_util.mjs 的 describeFetchError (错误口径: 连不上 ≠ 服务端报错);
//   ④ 补 marketPageLeave + visibilitychange (倒计时不再切走页签后空跑/后台白刷新);
//   ⑤ 温度分着色与涨跌红绿脱钩 (热=警示黄/温=中性/冷=信息蓝, 红绿只留给方向变化)。
// 本文件同时是 Node 单测对象 (tests/js/test_market_dashboard.js): 顶层不许碰 DOM,
// window 赋值有守卫, 纯函数 export 出去测。

import { getColors, hexToRgba, echartsInit } from './charts.js?v=20260906d';
import { describeFetchError } from './decision_util.mjs?v=20260919b';

// ── 纯函数区 (Node 可测, 不碰 DOM) ─────────────────────────────
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
  return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
function fmt(v, nd) { return (v == null || isNaN(v)) ? '—' : Number(v).toFixed(nd == null ? 2 : nd); }
function fmtSign(v, nd) { if (v == null || isNaN(v)) return '—'; var x = Number(v).toFixed(nd == null ? 2 : nd); return (v > 0 ? '+' : '') + x; }
// 中国习惯: 分数/数值上升(变好)=红, 下降(变差)=绿 —— 只用于"方向变化", 不用于"温度高低"
export function deltaCls(v) { return v > 0 ? 'md-up' : (v < 0 ? 'md-down' : 'md-flat'); }
// 温度分三档 (2026-09-19 语义修正): 高=热=警示黄橙(--warn-text), 中=中性, 低=冷=信息蓝(--info-text)。
// 不用涨红: --up 是"买入方向"色, 而估值维度"分高=便宜=好事"刷红会自相矛盾。
export function scoreClass(s) { return s == null ? 'md-flat' : (s >= 6.5 ? 'md-score-hot' : (s >= 4.5 ? 'md-score-mid' : 'md-score-cold')); }
// 得分变化(整数分): 升=红(变好) 降=绿(变差) 持平/缺=灰
function _fmtDelta(v) { return v == null ? '<span class="md-flat">—</span>' : '<span class="' + deltaCls(v) + '">' + fmtSign(v, 0) + '</span>'; }
// 事件修正分: 正=利好=红, 负=利空=绿, 0/缺=灰(0 不许落绿)
function _adjCls(v) { return v > 0 ? 'md-up' : (v < 0 ? 'md-down' : 'md-flat'); }

// 历史走势摘要文案 (2026-09-19 修): 原实现是
//   '总分从 X 到 Y（' + deltaCls(...) === 'up' ? '转暖' : '变化' + ...
// —— '+' 优先级高于 '===', 先拼完整句再和 'up' 比较, 永远为假 → 整句前缀被吞,
// 页面只剩没头没尾的「变化 +0.80）；…」。修复: 先算词再拼接; 方向词补全三档。
// 数据不足返回 null (调用方走"历史数据不足"分支)。回归锁: tests/js/test_market_dashboard.js
export function historySummaryText(items) {
  if (!items || items.length < 2) return null;
  var totals = items.map(function (r) { return r.scores.final_total; });
  var last = totals[totals.length - 1], first = totals[0];
  var recent7 = totals.slice(-7);
  var avg7 = recent7.reduce(function (a, b) { return a + b; }, 0) / recent7.length;
  var d = last - first;
  var word = d > 0 ? '转暖' : (d < 0 ? '转冷' : '持平');
  return '近' + items.length + '个交易日：总分从 ' + fmt(first, 2) + ' 到 ' + fmt(last, 2)
    + '（' + word + ' ' + fmtSign(d, 2) + '）；近7日均值 ' + fmt(avg7, 2) + '。';
}

// ── 模块状态 ──────────────────────────────────────────────────
var _countdownTimer = null;
var _nextTs = null;          // 最近一次"下次刷新"的绝对时间戳 (秒), 隐藏/切走恢复用
var _pageActive = false;     // 大盘页签是否正在前台 (leave/enter 维护)
var _activePane = 'overview';
var _histDays = 30;

function $(id) { return document.getElementById(id); }

// ── 状态栏 + 倒计时 ─────────────────────────────────────────
function renderStatusBar(status) {
  if (!status || !status.has_data) {
    $('mdLastUpdated').textContent = '还没有数据';
    $('mdLastType').textContent = '点右侧「手动刷新」生成第一份';
    $('mdNextRefresh').textContent = '—'; $('mdNextType').textContent = '—';
    $('mdDataAsof').innerHTML = '';
    return;
  }
  $('mdLastUpdated').textContent = status.last_updated || '—';
  $('mdLastType').textContent = status.last_type_name || status.last_type || '';
  $('mdNextRefresh').textContent = status.next_refresh || '—';
  $('mdNextType').textContent = status.next_refresh_type || '';
  // 分维度数据截止(滞后的标警示色)
  var da = status.data_asof || {};
  var names = { a_share: 'A股行情', margin: '两融', overseas: '海外', macro: '宏观' };
  var chips = [];
  var today = status.data_date;
  Object.keys(names).forEach(function (k) {
    var v = da[k];
    if (v == null) { chips.push('<span class="md-chip warn">' + names[k] + ' 暂无</span>'); return; }
    var lag = (k === 'margin' || k === 'overseas') && today && String(v) < today;
    chips.push('<span class="md-chip' + (lag ? ' warn' : '') + '">' + names[k] + ' '
      + esc(String(v)) + (lag ? ' · 滞后1天' : '') + '</span>');
  });
  $('mdDataAsof').innerHTML = '<span class="md-chip" style="font-weight:600">数据截止：</span>' + chips.join('');
  startCountdown(status.next_refresh_ts);
}

function startCountdown(nextTs) {
  _nextTs = nextTs || null;
  if (_countdownTimer) { clearInterval(_countdownTimer); _countdownTimer = null; }
  if (!nextTs) { $('mdCountdown').textContent = '--:--:--'; return; }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function tick() {
    var diff = Math.floor(nextTs - Date.now() / 1000);
    if (diff <= 0) {
      $('mdCountdown').textContent = '00:00:00';
      clearInterval(_countdownTimer); _countdownTimer = null;
      loadDashboard();  // 到点自动刷新
      if (_activePane === 'history') loadHistory();  // 2026-09-19: 历史页签在场时一并补刷
      return;
    }
    var h = Math.floor(diff / 3600), m = Math.floor(diff % 3600 / 60), s = diff % 60;
    $('mdCountdown').textContent = pad(h) + ':' + pad(m) + ':' + pad(s);
  }
  tick();
  _countdownTimer = setInterval(tick, 1000);
}
function stopCountdown() {
  if (_countdownTimer) { clearInterval(_countdownTimer); _countdownTimer = null; }
}

// 2026-09-19: 页面切后台停表、回前台按绝对时间重算 (手机版 9 月 16 日同款做法);
// 已过点立即补刷, 不再后台白跑 30~60 秒的全量重取
var _visBound = false;
function _bindVisibility() {
  if (_visBound) return;
  _visBound = true;
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) { stopCountdown(); return; }
    if (!_pageActive) return;
    if (_nextTs && _nextTs <= Date.now() / 1000) {
      loadDashboard();
      if (_activePane === 'history') loadHistory();
    } else if (_nextTs) {
      startCountdown(_nextTs);
    }
  });
}

// 2026-09-19: 主题切换后历史图重上色。不包 charts.js 的 toggleTheme._onToggle
// (vera-ui.js 模块顶层会直接覆盖它), 改用 MutationObserver 盯 data-theme, 无顺序依赖。
var _themeBound = false;
var _lastSnap = null;        // 最近一次快照 (主题切换时雷达/仪表重上色用)
function _redrawAllCharts() {
  if (_activePane === 'history') loadHistory();
  if (_lastSnap) drawGaugeRadar(_lastSnap);
  _drawDailyCharts();
  _drawErp();
}
function _bindThemeRecolor() {
  if (_themeBound || typeof MutationObserver === 'undefined') return;
  _themeBound = true;
  new MutationObserver(function () {
    if (_pageActive) _redrawAllCharts();
  }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
}

// ── 提示行着色 (class 制, 不写死色) ─────────────────────────
function _hint(text, cls) {
  var h = $('mdHint');
  if (!h) return;
  h.textContent = text || '';
  h.className = 'trade-hint' + (cls ? ' ' + cls : '');
}

// ── TAB1 大盘总览 ───────────────────────────────────────────
function renderOverview(snap) {
  var sc = snap.scores || {};
  var total = sc.final_total;
  var cards = ''
    + '<div class="md-card"><div class="md-card-label">最终总分</div>'
    + '<div class="md-card-value ' + scoreClass(total) + '">' + fmt(total, 1)
    + '<span class="md-card-unit"> / 10</span></div>'
    + '<div class="md-card-sub">基础 ' + fmt(sc.base_total, 2) + ' + 事件 ' + fmtSign(sc.event_adj, 2) + '</div></div>'
    + '<div class="md-card"><div class="md-card-label">大盘定性</div>'
    + '<div class="md-card-value sm">' + esc(sc.label || '—') + '</div></div>'
    + '<div class="md-card"><div class="md-card-label">环境参照仓位(只读参照)</div>'
    + '<div class="md-card-value sm">' + esc(sc.position_band || '—') + '</div></div>'
    + '<div class="md-card"><div class="md-card-label">事件修正分</div>'
    + '<div class="md-card-value ' + _adjCls(sc.event_adj) + '">'
    + fmtSign(sc.event_adj, 2) + '</div>'
    + '<div class="md-card-sub">' + (snap.events && snap.events.count ? '当前 ' + snap.events.count + ' 条生效事件' : '当前无生效事件') + '</div></div>';
  $('mdOverviewCards').innerHTML = '<div class="md-cards">' + cards + '</div>';

  // 双周期对比
  var cmp = snap.compare || {};
  var pd = cmp.prev_day || {}, d30 = cmp.day_30 || {};
  $('mdOverviewCompare').innerHTML = '<div class="md-compare">'
    + '<div><div class="md-card-label">较上一交易日</div><div class="md-status-value ' + deltaCls(pd.delta) + '" style="margin-top:4px">'
    + fmtSign(pd.delta, 2) + '　' + esc(pd.direction || '') + '</div></div>'
    + '<div><div class="md-card-label">较30个交易日前</div><div class="md-status-value ' + deltaCls(d30.delta) + '" style="margin-top:4px">'
    + (d30.delta == null ? '历史数据不足' : fmtSign(d30.delta, 2)) + '　' + esc(d30.direction || '') + '</div></div>'
    + '</div>';

  // 最强支撑/最大拖累
  var dims = sc.dimensions || {};
  var arr = Object.keys(dims).map(function (k) { return { k: k, w: dims[k].weighted }; });
  arr.sort(function (a, b) { return b.w - a.w; });
  var DIM_CN = { valuation: '估值与股权赔率', macro: '国内宏观与盈利', trend: '市场趋势与量价', sentiment: '市场微观情绪', overseas: '海外流动性' };
  var strongest = arr[0], weakest = arr[arr.length - 1];
  $('mdOverviewDrivers').innerHTML = '<div class="md-compare md-drivers">'
    + '<div><div class="md-card-label">当前最强支撑</div><div class="md-status-value md-up" style="margin-top:4px">'
    + esc(DIM_CN[strongest.k] || strongest.k) + '</div></div>'
    + '<div><div class="md-card-label">当前最大拖累</div><div class="md-status-value md-down" style="margin-top:4px">'
    + esc(DIM_CN[weakest.k] || weakest.k) + '</div></div>'
    + '</div>';

  // 可信度标注(温度分, 不是预测分)
  $('mdOverviewCredibility').innerHTML = '<div class="md-warn-box">'
    + '<b>怎么理解这个分数：</b>它回答"现在市场贵不贵、热不热"（温度计），'
    + '<b>回答不了"明天涨还是跌"</b>（不预测）。可信度标注：打分体系里有一部分指标是本次新接入的，'
    + '要攒够历史数据才能验证"分数跟未来走势到底有没有关系"，攒够前这里如实显示"历史数据不足"。'
    + '</div>'
    + '<div class="md-risk">⚠️ 风险提示：本仪表盘仅量化大盘环境（温度分），不构成投资建议，不预测指数点位与未来涨跌。'
    + '估值低位不代表立即上涨，可能长期磨底；无法预判突发黑天鹅；行业与个股可能与大盘明显分化。'
    + '环境参照仓位只读参照，不联动任何实盘仓位调度（业务铁律 1）。</div>';
}

// ── TAB2 指标明细 ───────────────────────────────────────────
function renderDetail(snap) {
  var dims = snap.scores.dimensions || {};
  var inds = snap.indicators || {};
  var DIM_CN = { valuation: '估值与股权赔率', macro: '国内宏观与盈利', trend: '市场趋势与量价', sentiment: '市场微观情绪', overseas: '海外流动性' };
  var DIM_LOGIC = {
    valuation: '贵不贵。股债利差、估值分位越高说明越便宜（越值得买）。',
    macro: '经济大方向。M1、PMI、PPI 看景气有没有回暖。',
    trend: '价格在走强还是走弱。均线、成交额、涨的股票多不多。',
    sentiment: '场内的钱慌不慌。两融（借钱炒股）和期权认沽认购比。',
    overseas: '外围资金环境。美债利率高不高、人民币偏强还是偏弱。',
  };
  var html = '';
  Object.keys(dims).forEach(function (dk) {
    var d = dims[dk];
    html += '<div class="md-dim"><div class="md-dim-head">'
      + '<span class="md-dim-name">' + esc(DIM_CN[dk] || dk) + '</span>'
      + '<span class="md-dim-meta">权重 ' + Math.round(d.weight * 100) + '% · 有效指标 ' + d.n_valid + '/' + d.n_total + '</span>'
      + '<span class="md-dim-avg">维度均分 ' + fmt(d.avg, 2) + '</span></div>';
    var keys = Object.keys(inds).filter(function (k) { return indInDim(k, dk, snap); });
    html += '<div class="md-ind-row md-ind-head">'
      + '<span class="md-ind-name">指标</span><span class="md-ind-val">数值</span>'
      + '<span class="md-ind-score">得分</span><span class="md-ind-delta">较上日</span>'
      + '<span class="md-ind-delta">较30日</span></div>';
    keys.forEach(function (k) {
      var it = inds[k];
      if (!it) return;
      var stale = it.stale ? ' <span class="md-chip warn md-chip-mini">暂缺·沿用</span>' : '';
      html += '<div class="md-ind-row">'
        + '<span class="md-ind-name" tabindex="0" title="数据截止 ' + esc(String(it.asof || '—')) + '">' + esc(it.name || k) + stale + '</span>'
        + '<span class="md-ind-val">' + (it.value == null ? '—' : esc(String(it.value))) + '</span>'
        + '<span class="md-ind-score ' + scoreClass(it.score) + '">' + (it.score == null ? '—' : it.score + '分') + '</span>'
        + '<span class="md-ind-delta">' + _fmtDelta(it.delta_prev) + '</span>'
        + '<span class="md-ind-delta">' + _fmtDelta(it.delta_d30) + '</span>'
        + '</div>';
    });
    html += '<div class="md-note md-dim-note">' + esc(DIM_LOGIC[dk] || '') + '</div></div>';
  });
  $('mdDetail').innerHTML = html;
}
function indInDim(key, dim, snap) {
  var MAP = {
    valuation: ['erp', 'pe_percentile', 'pb_percentile', 'dividend_spread'],
    macro: ['m1m2_spread', 'pmi', 'ppi'],
    trend: ['hs300_vs_ma200', 'turnover_amt', 'breadth_20d'],
    sentiment: ['margin_trend', 'option_pcr'],
    overseas: ['us10y', 'usdcny_ma_dev'],
  };
  return (MAP[dim] || []).indexOf(key) >= 0;
}

// ── TAB3 事件跟踪 ───────────────────────────────────────────
function renderEvents(snap) {
  var ev = snap.events || {};
  var list = ev.list || [];
  $('mdEventsSummary').innerHTML = '<div class="md-cards">'
    + '<div class="md-card"><div class="md-card-label">当前生效事件</div>'
    + '<div class="md-card-value">' + (ev.count || 0) + '<span class="md-card-unit"> 条</span></div></div>'
    + '<div class="md-card"><div class="md-card-label">合计修正分</div>'
    + '<div class="md-card-value ' + _adjCls(ev.total_adj) + '">'
    + fmtSign(ev.total_adj, 2) + '</div><div class="md-card-sub">直接加在基础总分上，最多 ±1 分</div></div>'
    + '</div>';
  if (!list.length) {
    $('mdEventsList').innerHTML = '<div class="md-note" style="margin-top:14px">当前没有生效中的重大事件。'
      + '（事件由 Agent 每日自动从财联社/同花顺/新浪等权威源抓取判定，含「美联储利率预期」常驻跟踪项。）</div>';
  } else {
    var rows = list.map(function (e) {
      return '<div class="md-ind-row">'
        + '<span class="md-chip">' + esc(e.level_name || e.level) + '</span>'
        + '<span class="md-ind-name">' + esc(e.title) + '</span>'
        + '<span class="md-ind-val">' + esc(e.logic || '') + '</span>'
        + '<span class="md-ind-score ' + _adjCls(e.score_now) + '">' + fmtSign(e.score_now, 2) + '</span>'
        + '<span class="md-ind-delta md-flat">' + (e.tracker ? '常驻跟踪' : '剩 ' + (e.days_left != null ? e.days_left : '—') + ' 天') + '</span>'
        + '</div>';
    }).join('');
    $('mdEventsList').innerHTML = '<div class="md-dim" style="margin-top:14px">' + rows + '</div>';
  }
  $('mdEventsRule').innerHTML = '<div class="md-note" style="margin-top:14px"><b>衰减规则</b>：'
    + '离散事件影响按天线性减弱——第一天是满分，之后每天均匀减少，到期自动清零移除。'
    + '史诗级（如印花税、平准基金）有效 30 天、单件最多 ±1 分；普通重大（如降准降息）有效 10 天、最多 ±0.5 分；'
    + '短期情绪有效 3 天、最多 ±0.2 分。多件叠加后总分修正最多 ±1 分。'
    + '<br><b>常驻跟踪</b>：带「常驻跟踪」标记的事件（如美联储利率预期）不衰减、不过期，'
    + '由每日自动刷新按最新读数重算分数，仅当读数实质性变动时才改分。</div>';
}

// ── TAB4 历史走势 ───────────────────────────────────────────
function renderHistoryControls() {
  var c = $('mdHistoryControls');
  if (!c) return;
  c.innerHTML = [7, 30, 90].map(function (d) {
    return '<button class="btn btn-xs md-hist-btn' + (d === _histDays ? ' md-hist-on' : '') + '" data-days="' + d + '" style="margin-right:6px">近' + d + '日</button>';
  }).join('');
  Array.prototype.forEach.call(document.querySelectorAll('.md-hist-btn'), function (b) {
    b.onclick = function () { _histDays = parseInt(b.dataset.days, 10); renderHistoryControls(); loadHistory(); };
  });
}
function loadHistory() {
  fetch('/api/market_position/dashboard/history?days=' + _histDays).then(function (r) { return r.json(); })
    .then(function (d) {
      if (!d.success) { $('mdHistorySummary').textContent = '历史读取失败'; return; }
      var items = d.items || [];
      drawHistoryChart(items);
      renderHistorySummary(items);
    }).catch(function (e) { $('mdHistorySummary').textContent = describeFetchError(e, '页面服务 (端口 8080)', 'server.py 没在跑，重启它'); });
}
function drawHistoryChart(items) {
  if (typeof window === 'undefined' || !window.echarts) return;
  // 2026-09-19: 走 charts.js 共享注册表 + 令牌取色 (原写死 #378add 蓝, 暗色主题下
  // 坐标轴走 echarts 白底默认色发灰发糊; 现在随主题换血, resize 也被全站监听覆盖)
  var ch = echartsInit('mdHistoryChart');
  if (!ch) return;
  var c = getColors();
  var dates = items.map(function (r) { return r.date; });
  var totals = items.map(function (r) { return r.scores ? r.scores.final_total : null; });
  ch.setOption({
    grid: { left: 40, right: 20, top: 20, bottom: 30 },
    tooltip: { trigger: 'axis', backgroundColor: c.bg, borderColor: c.border,
      textStyle: { color: c.text, fontSize: 12 } },
    xAxis: { type: 'category', data: dates,
      axisLabel: { color: c.text2, fontSize: 11 }, axisLine: { lineStyle: { color: c.border } } },
    yAxis: { type: 'value', min: 0, max: 10, name: '总分',
      nameTextStyle: { color: c.text2, fontSize: 11 },
      axisLabel: { color: c.text2, fontSize: 11 },
      splitLine: { lineStyle: { color: c.border } } },
    series: [{
      name: '大盘温度分', type: 'line', data: totals, smooth: true,
      lineStyle: { color: c.accent, width: 2 }, itemStyle: { color: c.accent },
      areaStyle: { color: hexToRgba(c.accent, 0.12) },
      markLine: { silent: true, data: [{ yAxis: 5, lineStyle: { color: c.text2, type: 'dashed' } }] },
    }],
  }, true);
}
function renderHistorySummary(items) {
  var text = historySummaryText(items);
  if (text === null) {
    $('mdHistorySummary').innerHTML = '<div class="md-note">历史数据不足（这是新功能，从'
      + (items[0] ? esc(items[0].date) : '今天') + '起才开始记录），攒够天数后这里会出现总分走势。</div>';
    return;
  }
  $('mdHistorySummary').innerHTML = '<div class="md-note">' + esc(text) + '</div>';
}

// ── 手动刷新 ────────────────────────────────────────────────
function doRefresh() {
  var btn = $('mdRefreshBtn');
  if (!btn || btn.disabled) return;
  // 2026-09-19: 30~60 秒的长操作只有"刷新中…"三个字像卡死 —— 按钮上跑已耗时秒数
  btn.disabled = true;
  var t0 = Date.now();
  btn.textContent = '刷新中… 0s';
  var elapsed = setInterval(function () {
    btn.textContent = '刷新中… ' + Math.floor((Date.now() - t0) / 1000) + 's';
  }, 1000);
  fetch('/api/market_position/dashboard/refresh', { method: 'POST' })
    .then(function (r) { return r.json().then(function (j) { return [r.status, j]; }); })
    .then(function (pair) {
      var st = pair[0], d = pair[1];
      if (st >= 400 || !d.success) {
        _hint('刷新失败: ' + (d.detail || d.error || 'HTTP ' + st), 'md-err');
      } else {
        _hint('已更新: 总分 ' + fmt(d.final_total, 1) + '（' + d.label + '）', 'md-ok');
        loadDashboard(); loadHistory();
      }
    }).catch(function (e) {
      _hint(describeFetchError(e, '页面服务 (端口 8080)', 'server.py 没在跑，重启它'), 'md-err');
    }).then(function () {
      clearInterval(elapsed);
      btn.disabled = false; btn.textContent = '手动刷新';
    });
}

// ── 子页签切换 ──────────────────────────────────────────────
function switchPane(name) {
  _activePane = name;
  Array.prototype.forEach.call(document.querySelectorAll('#mdSubtabs .md-subtab'), function (b) {
    var on = b.dataset.pane === name;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  Array.prototype.forEach.call(document.querySelectorAll('#pageMarket .md-pane'), function (p) {
    p.style.display = (p.dataset.pane === name) ? '' : 'none';
  });
  if (name === 'history') {
    renderHistoryControls();
    loadHistory();
    // 容器从隐藏变显示, echarts 需要 resize
    var el = $('mdHistoryChart');
    if (el && window.echarts) { var ch = window.echarts.getInstanceByDom(el); if (ch) ch.resize(); }
  }
}

// ── 主入口 ──────────────────────────────────────────────────
function loadDashboard() {
  fetch('/api/market_position/dashboard').then(function (r) { return r.json(); })
    .then(function (d) {
      if (!d.success) { _hint(d.error || '加载失败', 'md-err'); return; }
      renderStatusBar(d.status || {});
      if (!d.snapshot) {
        if ($('mdOverviewCards')) $('mdOverviewCards').innerHTML = '<div class="md-note">'
          + esc(d.reason || '还没有仪表盘数据') + '</div>';
        return;
      }
      renderOverview(d.snapshot);
      renderDetail(d.snapshot);
      renderEvents(d.snapshot);
      _lastSnap = d.snapshot;
      drawGaugeRadar(d.snapshot);
      _hint('', '');
    }).catch(function (e) { _hint(describeFetchError(e, '页面服务 (端口 8080)', 'server.py 没在跑，重启它'), 'md-err'); });
}

function dashboardEnter() {
  _pageActive = true;
  _bindVisibility();
  _bindThemeRecolor();
  _bindDescOnly();
  loadDashboard();
  loadCharts();
  // 绑定子页签(只绑一次)
  Array.prototype.forEach.call(document.querySelectorAll('#mdSubtabs .md-subtab'), function (b) {
    if (!b.dataset.bound) {
      b.dataset.bound = '1';
      b.addEventListener('click', function () { switchPane(b.dataset.pane); });
    }
  });
  var rb = $('mdRefreshBtn');
  if (rb && !rb.dataset.bound) { rb.dataset.bound = '1'; rb.addEventListener('click', doRefresh); }
}

function dashboardLeave() {
  // 2026-09-19: 切走页签停倒计时 (原实现没有 leave 钩子, 定时器空跑 +
  // 到点在后台白白发起一次 30~60 秒的全量重取)
  _pageActive = false;
  stopCountdown();
}

// Node 单测守卫: 顶层不碰 DOM, window 只在浏览器里存在
if (typeof window !== 'undefined') {
  window.marketPageEnter = dashboardEnter;
  window.marketPageLeave = dashboardLeave;
}

// ══════════════════════════════════════════════════════════════
// 多维图表层 (2026-09-19, 计划书 docs/plan/2026-09-19_大盘仪表盘多维图表_计划书.md)
// 规矩: 颜色一律 getColors() 令牌 (不写死 hex); 有预测证据的画主角(ERP/百分位),
// 宽度/成交/涨跌停收「只说现状」折叠区; 每张图带读法+证据徽章(在 HTML 里)。
// ══════════════════════════════════════════════════════════════

// ── 纯函数区 (Node 可测, tests/js/test_market_dashboard.js 锁) ──

// 五维快照 → 雷达图 {indicators, values}; 缺维度容忍 (缺的角跳过)
export function radarFromDims(dims) {
  var DIM_CN = { valuation: '估值赔率', macro: '宏观盈利', trend: '趋势量价',
    sentiment: '微观情绪', overseas: '海外流动性' };
  var out = { indicators: [], values: [] };
  Object.keys(DIM_CN).forEach(function (k) {
    var d = dims && dims[k];
    if (!d || d.avg == null || isNaN(d.avg)) return;
    out.indicators.push({ name: DIM_CN[k], max: 10 });
    out.values.push(d.avg);
  });
  return out;
}

// 日期 + 逐日牛熊标记 → echarts markArea 段 [[{xAxis:起},{xAxis:止,itemStyle}], ...]
// 连续同状态合并; 不足 5 天的碎段丢弃 (日频翻状态太勤的口径画出来是斑马线)
export function buildRegimeBands(dates, regimes) {
  var RC = { bull: 'rgba(46,194,126,.09)', bear: 'rgba(255,77,90,.09)', range: 'rgba(139,149,168,.06)' };
  var out = [], start = 0;
  for (var i = 1; i <= dates.length; i++) {
    if (i < dates.length && regimes[i] === regimes[start]) continue;
    if (i - start >= 5 && RC[regimes[start]]) {
      out.push([{ xAxis: dates[start] },
        { xAxis: dates[i - 1], itemStyle: { color: RC[regimes[start]] } }]);
    }
    start = i;
  }
  return out;
}

// 日期 + 具名序列组 → 热力图三元组 [xIdx, yIdx, value]; null 跳过 (不编造)
export function buildHeatRows(dates, namedSeries) {
  var out = [];
  namedSeries.forEach(function (ns, y) {
    (ns.values || []).forEach(function (v, x) {
      if (v != null && !isNaN(v)) out.push([x, y, v]);
    });
  });
  return out;
}

// ── 取数与渲染 ────────────────────────────────────────────────
var _chartsLoaded = false;   // 懒取数只取一次
var _daily = null;           // /api/market_position/history?limit=520
var _erp = null;             // /api/market_position/dashboard/erp_series
var _descDrawn = false;      // 折叠区首开才画

function _card() {  // getColors() 没有 card 色, 单行补读 (不是第二份 getColors)
  return getComputedStyle(document.documentElement).getPropertyValue('--card').trim();
}
function _token(name) {  // getColors() 清单外的单个令牌补读 (如 --info)
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function _ax(c) { return { axisLabel: { color: c.text2, fontSize: 11 }, axisLine: { lineStyle: { color: c.border } } }; }
function _tt() { return { backgroundColor: _card(), borderColor: getColors().border,
  textStyle: { color: getColors().text, fontSize: 12 } }; }
function _g(r, path) { var cur = r; for (var i = 0; i < path.length; i++) {
  if (!cur || typeof cur !== 'object') return null; cur = cur[path[i]]; } return cur == null ? null : cur; }

function loadCharts() {
  if (_chartsLoaded) return;
  _chartsLoaded = true;
  fetch('/api/market_position/history?limit=520').then(function (r) { return r.json(); })
    .then(function (d) { _daily = (d && d.items) || []; _drawDailyCharts(); })
    .catch(function (e) { ['mdChartPct', 'mdChartHeat'].forEach(function (id) { _chartFail(id, e); }); });
  fetch('/api/market_position/dashboard/erp_series').then(function (r) {
    // 404 = server.py 还没重启 (新端点没上桌), 要明说不许装死
    if (r.status === 404) throw new Error('图表数据端点不存在 —— server.py 需要重启一次才能认出它');
    return r.json();
  }).then(function (d) { _erp = d; _drawErp(); })
    .catch(function (e) { _chartFail('mdChartErp', e); });
}
function _chartFail(id, e) {
  var el = $(id); if (!el) return;
  el.innerHTML = '<div class="md-note" style="color:var(--danger-text);padding-top:var(--sp-6)">图表数据加载失败：'
    + esc(describeFetchError(e, '页面服务 (端口 8080)', 'server.py 没在跑，重启它')) + '</div>';
}

function _drawDailyCharts() {
  if (!_daily || !_daily.length) return;
  _drawPct(); _drawHeat();
  if (_descDrawn) _drawDescCharts();   // 折叠区已开过就顺手重画 (主题切换路径)
}

function drawGaugeRadar(snap) {
  var sc = snap && snap.scores ? snap.scores : {};
  var c = getColors();
  c.info = _token('--info');   // getColors() 无 info 位, 单行补读
  var g = echartsInit('mdGauge');
  if (g && sc.final_total != null) g.setOption({
    series: [{ type: 'gauge', startAngle: 210, endAngle: -30, min: 0, max: 10,
      radius: '95%', center: ['50%', '62%'],
      progress: { show: true, width: 16, itemStyle: { color: { type: 'linear', x: 0, y: 0, x2: 1, y2: 0,
        colorStops: [{ offset: 0, color: c.info }, { offset: .55, color: c.accent }, { offset: 1, color: c.up }] } } },
      axisLine: { lineStyle: { width: 16, color: [[1, c.border]] } },
      axisTick: { show: false }, splitLine: { show: false }, axisLabel: { show: false },
      pointer: { show: false }, anchor: { show: false },
      detail: { valueAnimation: true, fontSize: 44, fontWeight: 700, color: c.accent,
        offsetCenter: [0, '-5%'], formatter: function (v) { return v.toFixed(1); } },
      title: { offsetCenter: [0, '28%'], fontSize: 13, color: c.text2 },
      data: [{ value: sc.final_total, name: '大盘温度分（' + (sc.label || '') + '）' }] }]
  }, true);
  var r = echartsInit('mdRadar');
  var rd = radarFromDims(sc.dimensions);
  if (r && rd.indicators.length) r.setOption({
    tooltip: _tt(),
    radar: { indicator: rd.indicators, radius: '62%', splitNumber: 5,
      axisName: { color: c.text, fontSize: 12 },
      splitArea: { areaStyle: { color: ['rgba(128,128,128,.03)', 'rgba(128,128,128,.07)'] } },
      splitLine: { lineStyle: { color: c.border } }, axisLine: { lineStyle: { color: c.border } } },
    series: [{ type: 'radar', data: [{ value: rd.values, name: '当前得分',
      areaStyle: { color: hexToRgba(c.accent, .25) }, lineStyle: { color: c.accent, width: 2 },
      itemStyle: { color: c.accent }, symbolSize: 5 }] }]
  }, true);
}

function _drawErp() {
  var el = $('mdChartErp'); if (!el) return;
  if (!_erp || !_erp.success || !_erp.points || !_erp.points.length) {
    el.innerHTML = '<div class="md-note" style="padding-top:var(--sp-6)">'
      + esc((_erp && _erp.reason) || '还没有 ERP 数据') + '</div>';
    return;
  }
  var c = getColors(), ch = echartsInit('mdChartErp');
  var dates = _erp.points.map(function (p) { return p[0]; });
  var vals = _erp.points.map(function (p) { return p[1]; });
  var markData = [];
  if (_erp.median_10y != null) markData.push({ yAxis: _erp.median_10y,
    lineStyle: { color: c.text2, type: 'dashed' }, label: { formatter: '十年中位 ' + _erp.median_10y + '%', color: c.text2 } });
  if (_erp.max_10y != null) markData.push({ yAxis: _erp.max_10y,
    lineStyle: { color: c.down, type: 'dashed' }, label: { formatter: '十年最便宜 ' + _erp.max_10y + '%', color: c.down } });
  if (_erp.min_10y != null) markData.push({ yAxis: _erp.min_10y,
    lineStyle: { color: c.up, type: 'dashed' }, label: { formatter: '十年最贵 ' + _erp.min_10y + '%', color: c.up } });
  ch.setOption({
    grid: { left: 48, right: 64, top: 30, bottom: 52 },
    tooltip: Object.assign({ trigger: 'axis' }, _tt()),
    xAxis: Object.assign({ type: 'category', data: dates }, _ax(c)),
    yAxis: Object.assign({ type: 'value', name: '股债利差 %', nameTextStyle: { color: c.text2 },
      splitLine: { lineStyle: { color: c.border } } }, _ax(c)),
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18, bottom: 8,
      borderColor: c.border, textStyle: { color: c.text2 } }],
    series: [{ name: 'ERP', type: 'line', data: vals, showSymbol: false,
      lineStyle: { color: c.accent, width: 1.6 },
      areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [
        { offset: 0, color: hexToRgba(c.accent, .35) }, { offset: 1, color: hexToRgba(c.accent, .02) }] } },
      markLine: { silent: true, symbol: 'none', data: markData },
      markPoint: { symbol: 'pin', symbolSize: 44, itemStyle: { color: c.accent },
        label: { color: '#1a1d24', fontWeight: 700, fontSize: 11 },
        data: [{ coord: [dates[dates.length - 1], vals[vals.length - 1]], value: '现在' }] }
    }]
  }, true);
}

function _drawPct() {
  var el = $('mdChartPct'); if (!el || !_daily.length) return;
  var c = getColors(), ch = echartsInit('mdChartPct');
  var dates = _daily.map(function (r) { return r.date; });
  ch.setOption({
    grid: { left: 44, right: 20, top: 34, bottom: 30 },
    tooltip: Object.assign({ trigger: 'axis' }, _tt()),
    legend: { textStyle: { color: c.text2 }, top: 4 },
    xAxis: Object.assign({ type: 'category', data: dates }, _ax(c)),
    yAxis: Object.assign({ type: 'value', max: 100, name: '十年百分位 %',
      nameTextStyle: { color: c.text2 }, splitLine: { lineStyle: { color: c.border } } }, _ax(c)),
    series: [
      { name: '上证指数', type: 'line', showSymbol: false, lineStyle: { width: 1.6, color: c.accent }, itemStyle: { color: c.accent },
        data: _daily.map(function (r) { return _g(r, ['indices', 'shanghai', 'pct_10y']); }) },
      { name: '沪深300', type: 'line', showSymbol: false, lineStyle: { width: 1.6, color: c.accent2 }, itemStyle: { color: c.accent2 },
        data: _daily.map(function (r) { return _g(r, ['indices', 'hs300', 'pct_10y']); }) },
      { name: '创业板指', type: 'line', showSymbol: false, lineStyle: { width: 1.6, color: c.warn }, itemStyle: { color: c.warn },
        data: _daily.map(function (r) { return _g(r, ['indices', 'chuangyeban', 'pct_10y']); }),
        markArea: { silent: true,
          data: buildRegimeBands(dates, _daily.map(function (r) { return _g(r, ['indices', 'hs300', 'regime']); })) } }
    ]
  }, true);
}

function _drawHeat() {
  var el = $('mdChartHeat'); if (!el || !_daily.length) return;
  var c = getColors(), ch = echartsInit('mdChartHeat');
  var dates = _daily.map(function (r) { return r.date; });
  ch.setOption({
    grid: { left: 64, right: 20, top: 10, bottom: 44 },
    tooltip: _tt(),
    xAxis: Object.assign({ type: 'category', data: dates }, _ax(c)),
    yAxis: Object.assign({ type: 'category', data: ['上证指数', '沪深300', '创业板指'] }, _ax(c)),
    visualMap: { min: 0, max: 100, orient: 'horizontal', left: 'center', bottom: 0,
      textStyle: { color: c.text2 }, text: ['高(烫)', '低(冷)'],
      inRange: { color: [c.accent2, c.bg, c.up] } },
    series: [{ type: 'heatmap', progressive: 1000, itemStyle: { borderWidth: 0 },
      data: buildHeatRows(dates, [
        { name: 'sh', values: _daily.map(function (r) { return _g(r, ['indices', 'shanghai', 'pct_10y']); }) },
        { name: 'hs', values: _daily.map(function (r) { return _g(r, ['indices', 'hs300', 'pct_10y']); }) },
        { name: 'cy', values: _daily.map(function (r) { return _g(r, ['indices', 'chuangyeban', 'pct_10y']); }) }]) }]
  }, true);
}

// 「只说现状」折叠区三图 —— 首开才画 (折叠时容器 0 宽, echarts 画不出来)
function _drawDescCharts() {
  if (!_daily || !_daily.length) return;
  var c = getColors();
  var dates = _daily.map(function (r) { return r.date; });
  var amt = echartsInit('mdChartAmt');
  if (amt) amt.setOption({
    grid: { left: 64, right: 52, top: 34, bottom: 30 },
    tooltip: Object.assign({ trigger: 'axis' }, _tt()),
    legend: { textStyle: { color: c.text2 }, top: 4 },
    xAxis: Object.assign({ type: 'category', data: dates }, _ax(c)),
    yAxis: [Object.assign({ type: 'value', name: '成交额(亿元)', nameTextStyle: { color: c.text2 },
      splitLine: { lineStyle: { color: c.border } } }, _ax(c)),
      Object.assign({ type: 'value', name: '一年分位 %', max: 100,
        nameTextStyle: { color: c.text2 }, splitLine: { show: false } }, _ax(c))],
    series: [
      { name: '成交额', type: 'bar', barMaxWidth: 4, itemStyle: { color: hexToRgba(c.accent2, .55) },
        data: _daily.map(function (r) { return _g(r, ['turnover', 'amount_yi']); }) },
      { name: '一年分位', type: 'line', yAxisIndex: 1, showSymbol: false,
        lineStyle: { color: c.accent, width: 1.8 }, itemStyle: { color: c.accent },
        data: _daily.map(function (r) { return _g(r, ['turnover', 'amount_pct_1y']); }) }]
  }, true);
  var bd = echartsInit('mdChartBreadth');
  if (bd) bd.setOption({
    grid: { left: 44, right: 16, top: 34, bottom: 30 },
    tooltip: Object.assign({ trigger: 'axis' }, _tt()),
    legend: { textStyle: { color: c.text2 }, top: 4 },
    xAxis: Object.assign({ type: 'category', data: dates }, _ax(c)),
    yAxis: Object.assign({ type: 'value', max: 100, name: '占比 %',
      nameTextStyle: { color: c.text2 }, splitLine: { lineStyle: { color: c.border } } }, _ax(c)),
    series: [
      { name: '站上20日线', type: 'line', showSymbol: false, lineStyle: { color: c.accent, width: 1.8 },
        itemStyle: { color: c.accent }, areaStyle: { color: hexToRgba(c.accent, .12) },
        data: _daily.map(function (r) { return _g(r, ['breadth', 'above_ma20_pct']); }) },
      { name: '站上60日线', type: 'line', showSymbol: false, lineStyle: { color: c.accent2, width: 1.8 },
        itemStyle: { color: c.accent2 },
        data: _daily.map(function (r) { return _g(r, ['breadth', 'above_ma60_pct']); }) }]
  }, true);
  var hl = echartsInit('mdChartHL');
  if (hl) hl.setOption({
    grid: { left: 44, right: 16, top: 34, bottom: 30 },
    tooltip: Object.assign({ trigger: 'axis' }, _tt()),
    xAxis: Object.assign({ type: 'category', data: dates }, _ax(c)),
    yAxis: Object.assign({ type: 'value', name: '新高−新低 家数',
      nameTextStyle: { color: c.text2 }, splitLine: { lineStyle: { color: c.border } } }, _ax(c)),
    series: [{ name: '新高−新低', type: 'bar', barMaxWidth: 5,
      itemStyle: { color: function (p) { return p.value >= 0 ? hexToRgba(c.up, .8) : hexToRgba(c.down, .8); } },
      data: _daily.map(function (r) { return _g(r, ['breadth', 'hl_spread']); }) }]
  }, true);
  var lm = echartsInit('mdChartLimit');
  if (lm) lm.setOption({
    grid: { left: 44, right: 16, top: 34, bottom: 30 },
    tooltip: Object.assign({ trigger: 'axis' }, _tt()),
    legend: { textStyle: { color: c.text2 }, top: 4 },
    xAxis: Object.assign({ type: 'category', data: dates }, _ax(c)),
    yAxis: Object.assign({ type: 'value', name: '家数',
      nameTextStyle: { color: c.text2 }, splitLine: { lineStyle: { color: c.border } } }, _ax(c)),
    series: [
      { name: '涨停', type: 'bar', barMaxWidth: 5, itemStyle: { color: hexToRgba(c.up, .85) },
        data: _daily.map(function (r) { return _g(r, ['limit', 'up']); }) },
      { name: '跌停', type: 'bar', barMaxWidth: 5, itemStyle: { color: hexToRgba(c.down, .85) },
        data: _daily.map(function (r) { var v = _g(r, ['limit', 'down']); return v == null ? null : -v; }) }]
  }, true);
}

var _descBound = false;
function _bindDescOnly() {
  if (_descBound) return;
  _descBound = true;
  var det = document.getElementById('mdDescOnly');
  if (!det) return;
  det.addEventListener('toggle', function () {
    if (det.open && !_descDrawn) { _descDrawn = true; _drawDescCharts(); }
  });
}
