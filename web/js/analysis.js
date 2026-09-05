// ====== VERA Analysis Tab — 交易日历 + 实盘图表 ======
import { getColors, hexToRgba, echartsInit, tweenNumber, renderEquityCurve, esc } from './charts.js';
import { renderUnderwater, renderRolling, renderAttribution, renderAttributionStocks } from './charts_deep.js?v=20260814';
import { bucketReason, isKnownReason } from './reason_util.mjs';
// 2026-09-05 (治理III W4-e): 同源取数收敛到统一 client
import { fetchBenchmarkHistory, fetchCalendar } from './api.js';

const API_BASE = 'http://' + location.hostname + ':8081/api/trade/analysis';
const TRADE_API = 'http://' + location.hostname + ':8081/api/trade';
const DIR_BUY = 23, DIR_SELL = 24;

// 2026-08-26: 股票代码/简称 → 同花顺标的首页链接 (web/js/stock_link.js 提供 window.stockLink)
function stockLink(code, text) {
  return (typeof window !== 'undefined' && window.stockLink)
    ? window.stockLink(code, text)
    : esc(text != null && text !== '' ? text : (code || ''));
}

let _calendarYear, _calendarMonth;
let _selectedDate = '';
let _dailyReportSeq = 0;  // 2026-08-07 审计 HIGH#2: showDailyReport 请求序号, 防快连点慢响应覆盖快响应
let _calLoadSeq = 0;      // 2026-09-04 对手审计: 日历月度数据请求序号, 防切月/进页竞态 (慢的旧月响应后到覆盖新月渲染)
let _allDeals = [];
let _dealPage = 0;
const DEAL_PAGE_SIZE = 200;

// ── Entry (called by vera-ui.js switchTab) ──
window.analysisPageEnter = async function() {
  const now = new Date();
  _calendarYear = now.getFullYear();
  _calendarMonth = now.getMonth() + 1;
  await loadAnalysisData();
};

// ── Data loading ──
async function loadAnalysisData() {
  const emptyEl = document.getElementById('analysisEmpty');
  const contentEl = document.getElementById('analysisContent');
  const calSeq = ++_calLoadSeq;  // 进页加载也占一个序号: 与切月路径互斥, 谁最后发起谁有渲染权
  let fetchError = false;
  try {
    const results = await Promise.allSettled([
      fetch(API_BASE + '/summary').then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }),
      fetch(API_BASE + '/equity').then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }),
      fetch(API_BASE + '/daily_pnl?year=' + _calendarYear + '&month=' + _calendarMonth).then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }),
    ]);
    const summary = results[0].status === 'fulfilled' ? results[0].value : null;
    const equity = results[1].status === 'fulfilled' ? results[1].value : null;
    const dailyPnl = results[2].status === 'fulfilled' ? results[2].value : null;
    if (results.some(r => r.status === 'rejected')) fetchError = true;

    if (!summary || (!fetchError && summary.total_trades === 0 && (!equity || !equity.equity || equity.equity.length === 0))) {
      if (emptyEl) { emptyEl.style.display = ''; emptyEl.querySelector('p').textContent = fetchError ? '数据加载失败，请确认 trade 服务运行中' : '实盘开始后这里会出现业绩分析'; }
      if (contentEl) contentEl.style.display = 'none';
      return;
    }
    if (emptyEl) emptyEl.style.display = 'none';
    if (contentEl) contentEl.style.display = '';

    // Load benchmarks & calendar in parallel
    const startDate = summary.start_date || '';
    const endDate = summary.end_date || '';
    const [benchData, calData] = await Promise.all([
      startDate ? fetchBenchmarkHistory({
        indices: 'shanghai,hs300,chuangyeban,kechuang50,zhongzhengA500',
        start: startDate, end: endDate,
      }).catch(() => null) : null,
      fetchCalendar(_calendarYear, _calendarMonth).catch(() => null),
    ]);

    // Render KPI
    renderKpiCards(summary);

    // Render calendar
    // 序号守卫: 加载期间用户已切月 (序号变) → 本次渲染放弃, 避免旧月数据覆盖新月视图
    if (calSeq === _calLoadSeq) renderCalendar(calData, dailyPnl);

    // Render equity curve
    if (equity && equity.equity && equity.equity.length > 0) {
      const eqData = { equity: equity.equity, benchmarks: {} };
      if (benchData) {
        for (const [k, v] of Object.entries(benchData)) {
          eqData.benchmarks[k] = (v || []).map(r => ({ date: r.date, index_close: r.close, close: r.close }));
        }
      }
      renderEquityCurve('chartAnalysisEquity', eqData, '实盘', getColors());
      // 深挖包 Phase 1: 水下曲线 (回撤)。eqData.equity 为日级 {date, equity, drawdown} 数组;
      // 独立 try/catch, 失败只隐藏该图, 不拖垮整个分析 Tab (外层 catch 会隐藏 analysisContent)
      try { renderUnderwater('chartAnalysisUnderwater', eqData.equity, getColors()); }
      catch (e) { console.warn('renderUnderwater(实盘) 失败:', e); }
    }

    // 深挖包 Phase 2: 滚动指标 (equity 响应追加的 rolling key, 形状同回测 rolling_metrics)。
    // key 不存在 (后端未就绪/老数据) → renderRolling 内部静默隐藏容器; 独立 try/catch 不拖垮其他图
    try { renderRolling('chartAnalysisRolling', equity && equity.rolling, getColors()); }
    catch (e) { console.warn('renderRolling(实盘) 失败:', e); }

    // 深挖包 Phase 2: 实盘业绩归因 (板块 + 个股 Top10)。
    // 端点未就绪/失败 → 隐藏两个容器; meta.skipped_no_pnl > 0 由渲染层出副标题提示
    try {
      const attr = await fetch(API_BASE + '/attribution').then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); });
      renderAttribution('chartAnalysisAttrSector', attr, getColors());
      renderAttributionStocks('chartAnalysisAttrStock', attr, getColors());
    } catch (e) {
      console.warn('实盘归因加载失败:', e);
      try { renderAttribution('chartAnalysisAttrSector', null, getColors()); } catch (_) { /* 忽略 */ }
      try { renderAttributionStocks('chartAnalysisAttrStock', null, getColors()); } catch (_) { /* 忽略 */ }
    }

    // Render heatmap from equity data
    if (equity && equity.equity && equity.equity.length > 1) {
      renderHeatmap(equity.equity);
    }

    // Load deals for this month
    await loadDeals();

    // Render distribution charts
    renderDistribution(_allDeals);
    renderExitPie(_allDeals);

    // Wire calendar nav
    wireCalendarNav();
  } catch (e) {
    console.error('Analysis load error:', e);
    if (emptyEl) { emptyEl.style.display = ''; emptyEl.querySelector('p').textContent = '数据加载失败，请重试'; }
    if (contentEl) contentEl.style.display = 'none';
  }
}

// ── KPI Cards ──
function renderKpiCards(s) {
  if (!s) return;
  const grid = document.getElementById('analysisKpiGrid');
  if (!grid) return;
  const fmtPct = v => v != null ? (v * 100).toFixed(2) + '%' : '--';
  const fmtNum = v => v != null ? (v >= 0 ? '+' : '') + v.toFixed(2) : '--';
  const fmtInt = v => v != null ? Math.round(v) : '--';
  // 2026-08-13 (用户要求): 每个指标带大白话说明 (悬停 ? 图标可见, 含好坏参照线)
  const cards = [
    { label: '累计收益', id: 'akpi1', val: s.cumulative_return, fmt: v => fmtPct(v), cls: s.cumulative_return > 0 ? 'pos' : 'neg',
      tip: '从期初到现在总共赚/亏的百分比。正数=赚钱, 负数=亏钱。' },
    { label: '年化收益', id: 'akpi2', val: s.annualized_return, fmt: v => fmtPct(v), cls: s.annualized_return > 0 ? 'pos' : 'neg',
      tip: '把收益折算成一年的速度, 方便和理财/指数比较。例: 年化 10% ≈ 每 100 元一年赚 10 元。参照: 银行定存约 1.5-2%, 沪深300长期约 5-8%。' },
    { label: '最大回撤', id: 'akpi3', val: s.max_drawdown, fmt: v => fmtPct(v), cls: s.max_drawdown < 0 ? 'pos' : 'neg',
      tip: '净值从最高点跌到最低点的最大幅度, 即"最惨的时候账面亏多少"。越小越好; 超过 -20% 说明波动很煎熬。' },
    { label: '夏普比率', id: 'akpi4', val: s.sharpe_ratio, fmt: v => fmtNum(v), cls: '',
      tip: '每承担一份上下波动, 换来多少收益 (已扣无风险利率)。>1 算不错, >2 优秀, <0 说明波动白挨了。' },
    { label: '胜率', id: 'akpi5', val: s.win_rate, fmt: v => v != null ? (v * 100).toFixed(1) + '%' : '--', cls: '',
      tip: '赚钱的交易占总交易的比例。注意: 胜率高≠赚钱 —— 常赚小钱亏一次大的照样亏, 要结合盈亏比看。' },
    { label: '盈亏比', id: 'akpi6', val: s.profit_loss_ratio, fmt: v => fmtNum(v), cls: '',
      tip: '平均每笔盈利 ÷ 平均每笔亏损。>1 表示赚的时候比亏的时候多; 胜率×盈亏比 联立才决定长期赚不赚钱。' },
    { label: '交易笔数', id: 'akpi7', val: s.total_trades, fmt: v => fmtInt(v), cls: '',
      tip: '统计期内完成的买卖笔数。笔数太少 (如 <30) 时, 其他指标偶然性大, 别太当真。' },
    { label: '卡玛比率', id: 'akpi8', val: s.calmar_ratio, fmt: v => fmtNum(v), cls: '',
      tip: '年化收益 ÷ 最大回撤, 衡量"用多大痛苦换收益"。>1 不错, >3 优秀; <1 说明受的苦比赚的还多。' },
    { label: 'Sortino', id: 'akpi9', val: s.sortino_ratio, fmt: v => fmtNum(v), cls: '',
      tip: '索提诺比率: 类似夏普, 但只把"下跌"算作风险 (上涨不惩罚)。>1 不错, >2 优秀。一般比夏普略高。' },
    { label: '盈利因子', id: 'akpi10', val: s.profit_factor, fmt: v => fmtNum(v), cls: '',
      tip: '总盈利 ÷ 总亏损。<1 必亏, 1.0-1.5 勉强, >1.5 较好, >2 优秀。' },
  ];
  const warnHtml = s.reconciliation_warning
    ? '<span style="color:var(--warn);font-size:11px;margin-left:4px" title="净值推算与QMT资产偏差>1%">⚠</span>' : '';
  const labelHtml = c => c.tip
    ? c.label + ' <span style="cursor:help;color:var(--text2);font-size:10px" title="' + c.tip + '">?</span>'
    : c.label;
  grid.innerHTML = '<div class="kpi-row-primary" style="display:grid;grid-template-columns:repeat(5,1fr);gap:10px">' +
    cards.slice(0, 5).map(c => '<div class="kpi-card"><div class="kpi-label">' + labelHtml(c) + warnHtml + '</div><div class="kpi-value ' + c.cls + '" id="' + c.id + '">' + c.fmt(c.val) + '</div></div>').join('') +
    '</div><div class="kpi-row-secondary" style="display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-top:10px">' +
    cards.slice(5).map(c => '<div class="kpi-card"><div class="kpi-label">' + labelHtml(c) + '</div><div class="kpi-value ' + c.cls + '" id="' + c.id + '" style="font-size:18px">' + c.fmt(c.val) + '</div></div>').join('') +
    '</div>';
}

// ── Calendar ──
function renderCalendar(calData, dailyPnl) {
  const grid = document.getElementById('analysisCalendarGrid');
  const title = document.getElementById('calTitle');
  if (!grid) return;
  title.textContent = _calendarYear + '年' + _calendarMonth + '月';
  // 2026-09-04: 月度收益行随日历一起渲染 — 首载/切月/今天按钮三条路径都走这里
  renderMonthSummary(dailyPnl);

  // Get days in month
  const daysInMonth = new Date(_calendarYear, _calendarMonth, 0).getDate();
  const firstDow = new Date(_calendarYear, _calendarMonth - 1, 1).getDay(); // 0=Sun
  const today = new Date();
  const todayStr = today.getFullYear() + '-' + String(today.getMonth() + 1).padStart(2, '0') + '-' + String(today.getDate()).padStart(2, '0');

  const cells = [];
  // Leading blanks
  for (let i = 0; i < firstDow; i++) {
    cells.push('<div class="cal-cell cal-empty"></div>');
  }
  // Month days
  for (let d = 1; d <= daysInMonth; d++) {
    const dateStr = _calendarYear + '-' + String(_calendarMonth).padStart(2, '0') + '-' + String(d).padStart(2, '0');
    const calInfo = calData && calData.trading_calendar ? calData.trading_calendar[dateStr] : null;
    const isTrading = calInfo ? calInfo.is_trading : (new Date(_calendarYear, _calendarMonth - 1, d).getDay() !== 0 && new Date(_calendarYear, _calendarMonth - 1, d).getDay() !== 6);
    const pnl = dailyPnl && dailyPnl[dateStr];
    const isToday = dateStr === todayStr;
    const isSel = dateStr === _selectedDate;

    let bgStyle = '';
    let borderStyle = '';
    if (isToday) borderStyle = 'border:2px solid var(--accent);';
    else if (isSel) borderStyle = 'border:2px solid var(--accent2);';

    if (!isTrading) {
      bgStyle = 'background:var(--surface);opacity:0.6';
    } else if (pnl && pnl.pnl_rate !== 0) {
      const color = pnl.pnl_rate > 0 ? 'var(--up)' : 'var(--down)';
      bgStyle = 'background:radial-gradient(circle at 50% 50%, color-mix(in srgb, ' + color + ' 22%, transparent), color-mix(in srgb, ' + color + ' 6%, var(--surface)))';
    }

    const pnlRateStr = pnl ? (pnl.pnl_rate >= 0 ? '+' : '') + (pnl.pnl_rate * 100).toFixed(2) + '%' : '—';
    const pnlAmtStr = pnl ? ((pnl.pnl_amount >= 0 ? '+¥' : '-¥') + Math.abs(pnl.pnl_amount).toLocaleString('zh-CN', {maximumFractionDigits: 0})) : '';
    const pnlCls = pnl && pnl.pnl_rate > 0 ? 'cal-up' : pnl && pnl.pnl_rate < 0 ? 'cal-down' : '';
    const tradeInfo = pnl ? ('买' + pnl.buy_count + ' 卖' + pnl.sell_count) : isTrading ? '无成交' : '休市';

    const dataAttrs = isTrading && pnl ? ' data-date="' + dateStr + '"' : '';
    cells.push('<div class="cal-cell' + (isTrading && pnl ? ' cal-clickable' : '') + '" style="' + bgStyle + ';' + borderStyle + '"' + dataAttrs +
      ' role="button" tabindex="' + (isTrading ? '0' : '-1') + '" aria-label="' + (d + '日 ' + tradeInfo + ' ' + pnlRateStr) + '">' +
      '<span class="cal-day">' + d + '</span>' +
      '<span class="cal-pnl ' + pnlCls + '">' + pnlRateStr + '</span>' +
      (pnlAmtStr ? '<span class="cal-amt ' + pnlCls + '">' + pnlAmtStr + '</span>' : '') +
      '<span class="cal-trades">' + tradeInfo + '</span></div>');
  }
  grid.innerHTML = cells.join('');

  // Click handlers (2026-08-07: 选中日 → 拉盘后日报详情; 再点同一格取消)
  grid.querySelectorAll('.cal-clickable').forEach(el => {
    el.addEventListener('click', function() {
      const ds = this.dataset.date;
      if (_selectedDate === ds) { _selectedDate = ''; hideDailyReport(); }
      else { _selectedDate = ds; showDailyReport(ds); }
      renderCalendar(calData, dailyPnl);
      filterDealsByDate(_selectedDate);
    });
  });
}

// ── 月度收益汇总行 (2026-09-04): 随月份导航动态刷新 ──
// 数据源 = daily_pnl 响应的 "_month" 键 (后端以「上月最后交易日总资产」为
// 基准一次除法算出, 与逐日链复利合成等价、无连乘舍入)。老响应无该键
// (trade 服务未重启) 或请求失败 → 显示"该月无交易数据", 不报错。
function renderMonthSummary(dailyPnl) {
  const el = document.getElementById('calMonthSummary');
  if (!el) return;
  const m = dailyPnl ? dailyPnl['_month'] : null;
  if (!m || !m.trading_days) {
    el.innerHTML = '<span style="color:var(--text2)">该月无交易数据</span>';
    return;
  }
  const cls = m.pnl_rate > 0 ? 'var(--up)' : m.pnl_rate < 0 ? 'var(--down)' : 'var(--text)';
  const rateStr = (m.pnl_rate >= 0 ? '+' : '') + (m.pnl_rate * 100).toFixed(2) + '%';
  const amtStr = (m.pnl_amount >= 0 ? '+¥' : '-¥') + Math.abs(m.pnl_amount).toLocaleString('zh-CN', { maximumFractionDigits: 0 });
  const baseTxt = m.baseline_is_prev_month
    ? '基准 = 上月最后一个交易日 (' + esc(m.baseline_date || '') + ') 的日终总资产'
    : '账户首月无上月基准, 自当月首个交易日 (' + esc(m.baseline_date || '') + ') 起算';
  const tip = '本月所有交易日涨跌合成后的总收益率 (复利口径, 不是每日百分比简单相加)。' + baseTxt +
    ', 截止本月最后一个有数据的交易日 (当月未过完 = 月至今)。' +
    '注意: 月内入金/出金会被当作盈亏计入, 与日历格子、权益曲线同口径。';
  const sep = '<span style="color:var(--border)">|</span>';
  el.innerHTML =
    '<span style="color:var(--text2)">本月收益 <span style="cursor:help;color:var(--text2);font-size:10px" title="' + tip + '">?</span></span>' +
    '<span style="font-size:18px;font-weight:700;color:' + cls + ';font-family:var(--mono)">' + rateStr + '</span>' +
    '<span style="font-weight:600;color:' + cls + '">' + amtStr + '</span>' +
    sep +
    '<span>交易日 <b>' + m.trading_days + '</b> 天 · 盈 <span style="color:var(--up)">' + m.win_days + '</span> · 亏 <span style="color:var(--down)">' + m.loss_days + '</span></span>' +
    sep +
    '<span>买 <b>' + m.buy_count + '</b> · 卖 <b>' + m.sell_count + '</b></span>';
}

// ── 盘后日报详情 (2026-08-07): 点击日历格子展示当日全明细 ──
// 独立 HTML 渲染, 不复用飞书 lark_md; 字段对齐 trade_main._notify_daily payload
function _fmtMoney(v) {
  if (v == null) return '—';
  const n = Number(v);
  return (n < 0 ? '-' : '') + '¥' + Math.abs(n).toLocaleString('zh-CN', { maximumFractionDigits: 2, minimumFractionDigits: 2 });
}
function _fmtSigned(v) {
  if (v == null) return '—';
  const n = Number(v);
  return (n >= 0 ? '+' : '') + n.toLocaleString('zh-CN', { maximumFractionDigits: 2, minimumFractionDigits: 2 });
}
function _pnlColor(v) { return 'var(--' + (Number(v) >= 0 ? 'up' : 'down') + ')'; }

// 单个 section 卡片 (label-value 两列)
function _drSection(title, rows) {
  const rowsHtml = rows.map(r =>
    '<div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px;padding:3px 0;font-size:12px">' +
    '<span style="color:var(--text2)">' + r[0] + '</span>' +
    '<span style="color:var(--text);text-align:right">' + r[1] + '</span></div>'
  ).join('');
  return '<div style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:10px 12px">' +
    '<div style="font-size:11px;font-weight:600;color:var(--accent);margin-bottom:6px;letter-spacing:0.5px">' + title + '</div>' +
    rowsHtml + '</div>';
}

function renderDailyReport(payload, dateStr) {
  const box = document.getElementById('analysisDailyReportBox');
  const body = document.getElementById('analysisDailyReport');
  const title = document.getElementById('analysisDailyReportTitle');
  if (!box || !body || !title) return;

  // header: 日期 + 当日盈亏 (红绿)
  let pnlHtml = '';
  if (payload && payload.day_pnl != null) {
    const dp = payload.day_pnl;
    pnlHtml = ' <span style="color:' + _pnlColor(dp) + ';font-weight:600">' + _fmtSigned(dp) +
      (payload.day_pnl_pct != null ? ' (' + _fmtSigned(payload.day_pnl_pct) + '%)' : '') + '</span>';
  }
  title.innerHTML = '盘后日报 · ' + esc(dateStr) + pnlHtml;

  if (!payload) {
    body.innerHTML = '<div style="padding:24px;text-align:center;color:var(--text2);font-size:12px">该日无盘后日报记录</div>';
    box.style.display = '';
    return;
  }

  const d = payload;
  const totalAsset = Number(d.total_asset || 0);
  const cash = Number(d.cash || 0);
  const marketValue = (d.market_value != null ? Number(d.market_value) : totalAsset - cash);
  const sections = [];

  // 资产
  const assetRows = [
    ['总资产', _fmtMoney(totalAsset)],
    ['市值', _fmtMoney(marketValue)],
    ['现金', _fmtMoney(cash)],
  ];
  if (d.position_count != null) assetRows.push(['持仓', d.position_count + ' 只']);
  if (d.floating_pnl != null) assetRows.push(['浮盈', '<span style="color:' + _pnlColor(d.floating_pnl) + '">' + _fmtSigned(d.floating_pnl) + '</span>']);
  sections.push(_drSection('资产', assetRows));

  // 交易摘要
  if (d.buy_count != null || d.sell_count != null) {
    const sumRows = [['买卖', '买 ' + (d.buy_count || 0) + ' · 卖 ' + (d.sell_count || 0)]];
    if (d.turnover != null) sumRows.push(['成交额', _fmtMoney(d.turnover)]);
    if (d.realized_pnl != null) {
      let cell = '<span style="color:' + _pnlColor(d.realized_pnl) + '">' + _fmtSigned(d.realized_pnl) + '</span>';
      if (d.win_rate != null) cell += ' <span style="color:var(--text2);font-size:11px">胜率 ' + (Number(d.win_rate) * 100).toFixed(0) + '%</span>';
      sumRows.push(['已实现盈亏', cell]);
    }
    sections.push(_drSection('交易摘要', sumRows));
  }

  // 仓位变动
  const changes = d.position_changes || {};
  const changeRows = [];
  for (const [key, label] of [['new', '新进'], ['closed', '清仓'], ['added', '加仓'], ['reduced', '减仓']]) {
    const items = changes[key] || [];
    if (items.length) {
      const txt = items.map(it => {
        const delta = Number(it.delta || 0);
        return esc(it.code || '') + '(<span style="color:' + _pnlColor(delta) + '">' + (delta >= 0 ? '+' : '') + delta + '</span>)';
      }).join(' · ');
      changeRows.push([label, txt]);
    }
  }
  if (changeRows.length) sections.push(_drSection('仓位变动', changeRows));

  // 卖出明细
  const sells = d.sell_details || [];
  if (sells.length) {
    const sellRows = sells.map(s => {
      const pa = Number(s.pnl_amount || 0);
      let cell = '<span style="color:' + _pnlColor(pa) + '">' + _fmtSigned(pa) + '</span>';
      if (s.pnl_pct) {
        const pp = Number(s.pnl_pct);
        cell += ' <span style="color:var(--text2);font-size:11px">(' + (pp >= 0 ? '+' : '') + pp.toFixed(2) + '%)</span>';
      }
      if (s.reason) cell += ' <span style="color:var(--text2);font-size:11px">· ' + esc(s.reason) + '</span>';
      return [esc(s.code || ''), cell];
    });
    if (d.sell_details_folded) {
      const fc = Number(d.sell_details_folded.count || 0);
      const fs = Number(d.sell_details_folded.sum_pnl_amount || 0);
      sellRows.push(['另 ' + fc + ' 笔', '<span style="color:' + _pnlColor(fs) + '">合计 ' + _fmtSigned(fs) + '</span>']);
    }
    sections.push(_drSection('卖出明细', sellRows));
  }

  body.innerHTML = '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px">' + sections.join('') + '</div>';
  box.style.display = '';
}

async function showDailyReport(dateStr) {
  const seq = ++_dailyReportSeq;  // 序号守卫: 丢弃被更新点击取代的旧响应
  const box = document.getElementById('analysisDailyReportBox');
  const title = document.getElementById('analysisDailyReportTitle');
  const body = document.getElementById('analysisDailyReport');
  if (title) title.textContent = '盘后日报 · ' + dateStr;
  if (body) body.innerHTML = '<div style="padding:24px;text-align:center;color:var(--text2);font-size:12px">加载中...</div>';
  if (box) box.style.display = '';
  try {
    const resp = await fetch(API_BASE + '/daily_report?date=' + dateStr).then(r => r.json());
    if (seq !== _dailyReportSeq) return;  // 已被更新的点击取代, 丢弃陈旧响应
    renderDailyReport(resp.report, dateStr);
  } catch (e) {
    if (seq === _dailyReportSeq && body) body.innerHTML = '<div style="padding:24px;text-align:center;color:var(--text2);font-size:12px">加载失败, 请确认 trade 服务运行中</div>';
  }
}

function hideDailyReport() {
  const box = document.getElementById('analysisDailyReportBox');
  if (box) box.style.display = 'none';
}

function wireCalendarNav() {
  document.getElementById('calPrevYear').onclick = () => { _calendarYear--; refreshCalendarMonth(); };
  document.getElementById('calPrevMonth').onclick = () => { _calendarMonth--; if (_calendarMonth < 1) { _calendarMonth = 12; _calendarYear--; } refreshCalendarMonth(); };
  document.getElementById('calNextMonth').onclick = () => { _calendarMonth++; if (_calendarMonth > 12) { _calendarMonth = 1; _calendarYear++; } refreshCalendarMonth(); };
  document.getElementById('calNextYear').onclick = () => { _calendarYear++; refreshCalendarMonth(); };
  document.getElementById('calToday').onclick = () => {
    const n = new Date(); _calendarYear = n.getFullYear(); _calendarMonth = n.getMonth() + 1;
    _selectedDate = _calendarYear + '-' + String(_calendarMonth).padStart(2, '0') + '-' + String(n.getDate()).padStart(2, '0');
    refreshCalendarMonth();
  };
  // 2026-08-07: 日报详情"收起"按钮 — 隐藏卡片 + 清选中 + 去日历高亮 + 还原全量成交
  const drClose = document.getElementById('analysisDailyReportClose');
  if (drClose) drClose.onclick = () => {
    _selectedDate = '';
    hideDailyReport();
    refreshCalendarMonth();
    filterDealsByDate('');
  };
}

async function refreshCalendarMonth() {
  // 2026-09-04 对手审计: 请求序号守卫 (同 showDailyReport 的 _dailyReportSeq
  // 模式) — 快速连点切月时, 慢的旧月响应后到必须丢弃, 否则渲染成
  // "新月标题 + 旧月日历和月度收益行" 的错配视图
  const seq = ++_calLoadSeq;
  _dailyReportSeq++;  // 2026-09-04 复验遗留③: 切月作废在途日报请求 —— 否则慢响应回来 renderDailyReport 会把刚 hideDailyReport 的旧月卡片复活 (2026-08-07 起的相邻既有缺陷)
  // 2026-08-07 自检: 切月后若选中日不在新视图月份, 清选中 + 隐藏日报卡片,
  // 防止 8 月选 8/7 看完日报切到 9 月时, 卡片仍停留 8/7 陈旧数据误导用户。
  // "今天"按钮(选中日=当月)与"收起"按钮(已清空)不受误伤。
  const _viewingMonth = _calendarYear + '-' + String(_calendarMonth).padStart(2, '0');
  if (_selectedDate && _selectedDate.slice(0, 7) !== _viewingMonth) {
    _selectedDate = '';
    hideDailyReport();
  }
  const calData = await fetchCalendar(_calendarYear, _calendarMonth).catch(() => null);
  const dailyPnl = await fetch(API_BASE + '/daily_pnl?year=' + _calendarYear + '&month=' + _calendarMonth).then(r => r.json()).catch(() => null);
  if (seq !== _calLoadSeq) return;  // 已有更新的切月请求, 丢弃本次陈旧响应
  renderCalendar(calData, dailyPnl);
}

// ── Heatmap ──
function renderHeatmap(equity) {
  const dom = document.getElementById('chartAnalysisHeat');
  if (!dom || equity.length < 2) return;
  const c = getColors();
  const chart = echartsInit('chartAnalysisHeat');
  if (!chart) return;

  // Aggregate monthly from daily equity
  const monthly = {};
  for (let i = 1; i < equity.length; i++) {
    if (equity[i - 1].equity > 0) {
      const d = new Date(equity[i].date);
      const y = d.getFullYear();
      const m = d.getMonth() + 1;
      const key = y + '-' + String(m).padStart(2, '0');
      const ret = (equity[i].equity - equity[i - 1].equity) / equity[i - 1].equity;
      if (!monthly[key]) monthly[key] = [];
      monthly[key].push(ret);
    }
  }
  const keys = Object.keys(monthly).sort();
  const years = [...new Set(keys.map(k => parseInt(k.split('-')[0])))].sort();
  const months = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12];
  const data = [];
  const yLabels = years.map(String);
  const xLabels = months.map(m => m + '月');

  years.forEach(y => {
    months.forEach(m => {
      const key = y + '-' + String(m).padStart(2, '0');
      const rets = monthly[key];
      if (rets && rets.length > 0) {
        const product = rets.reduce((acc, r) => acc * (1 + r), 1);
        // 2026-08-13 修复: echarts heatmap 数据格式是 [x下标, y下标, 值],
        // 旧代码写成 [年, 月] 把格子画到不存在的行, 热力图永远空白
        data.push([m - 1, yLabels.indexOf(String(y)), Number(((product - 1) * 100).toFixed(2))]);
      }
    });
  });

  chart.setOption({
    tooltip: {
      formatter: p => p.value[0] !== undefined
        ? years[p.value[1]] + '年' + months[p.value[0]] + '月: <b>' + (p.value[2] >= 0 ? '+' : '') + p.value[2].toFixed(2) + '%</b>'
        : ''
    },
    grid: { left: 50, right: 30, top: 10, bottom: 30 },
    xAxis: { type: 'category', data: xLabels, axisLabel: { color: c.text2, fontSize: 10 } },
    yAxis: { type: 'category', data: yLabels, axisLabel: { color: c.text2, fontSize: 10 } },
    visualMap: {
      min: -15, max: 15, calculable: true, orient: 'horizontal', left: 'center', bottom: 0,
      inRange: { color: [c.down, hexToRgba(c.down, 0.4), 'transparent', hexToRgba(c.up, 0.4), c.up] },
      textStyle: { color: c.text2, fontSize: 9 }
    },
    series: [{
      type: 'heatmap', data: data,
      label: { show: true, color: c.text, fontSize: 10, formatter: p => p.value[2] >= 0 ? '+' + p.value[2].toFixed(1) + '%' : p.value[2].toFixed(1) + '%' },
      emphasis: { itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.5)' } }
    }]
  }, true);
}

// ── Distribution charts ──
function renderDistribution(deals) {
  const dom = document.getElementById('chartAnalysisDist');
  if (!dom || !deals || !deals.length) return;
  const c = getColors();
  const chart = echartsInit('chartAnalysisDist');
  if (!chart) return;
  // Group by direction-matched P&L
  const buyMap = {}, sellMap = {};
  deals.forEach(t => {
    const code = t.code;
    if (t.direction === DIR_BUY) {
      if (!buyMap[code]) buyMap[code] = { total: 0, qty: 0 };
      buyMap[code].total += t.price * t.qty;
      buyMap[code].qty += t.qty;
    } else if (t.direction === DIR_SELL) {
      if (!sellMap[code]) sellMap[code] = { total: 0, qty: 0 };
      sellMap[code].total += t.price * t.qty;
      sellMap[code].qty += t.qty;
    }
  });
  const pnls = [];
  for (const code of Object.keys(sellMap)) {
    if (buyMap[code] && buyMap[code].qty > 0) {
      const buyAvg = buyMap[code].total / buyMap[code].qty;
      const sellAvg = sellMap[code].total / sellMap[code].qty;
      pnls.push((sellAvg - buyAvg) / buyAvg * 100);
    }
  }
  if (!pnls.length) return;
  const ranges = [
    { label: '< -10%', min: -Infinity, max: -10 },
    { label: '-10~-5%', min: -10, max: -5 },
    { label: '-5~0%', min: -5, max: 0 },
    { label: '0~5%', min: 0, max: 5 },
    { label: '5~10%', min: 5, max: 10 },
    { label: '10~20%', min: 10, max: 20 },
    { label: '> 20%', min: 20, max: Infinity },
  ];
  const downShades = [hexToRgba(c.down, 0.7), hexToRgba(c.down, 0.9), c.down];
  const upShades = [c.up, hexToRgba(c.up, 0.8), hexToRgba(c.up, 0.5), hexToRgba(c.up, 0.35)];
  const colors = [...downShades, ...upShades];
  const counts = ranges.map((r, i) => ({
    name: r.label, value: pnls.filter(v => v >= r.min && v < r.max).length, itemStyle: { color: colors[i] }
  }));
  chart.setOption({
    tooltip: { trigger: 'axis', formatter: p => p[0].name + '<br/>交易: <b>' + p[0].value + '</b>' },
    grid: { left: 40, right: 10, top: 10, bottom: 40 },
    xAxis: { type: 'category', data: counts.map(d => d.name), axisLabel: { color: c.text2, fontSize: 9, rotate: 30 } },
    yAxis: { type: 'value', name: '笔数', axisLabel: { color: c.text2, fontSize: 9 }, splitLine: { lineStyle: { color: c.border } } },
    series: [{ type: 'bar', data: counts }],
  }, true);
}

function renderExitPie(deals) {
  const dom = document.getElementById('chartAnalysisExit');
  if (!dom || !deals || !deals.length) return;
  const c = getColors();
  const chart = echartsInit('chartAnalysisExit');
  if (!chart) return;
  // 2026-08-07: 卖出原因上色修复。
  // 病根: 实盘 trades.reason 是带数字描述的长句子 (trade_main._reason_from_ctx),
  // 如 "移动止盈: 最高 12.00 (峰值涨幅 +20%...) 触发" / "阶梯止盈·档1" / "硬止损: ...",
  // 旧代码用"精确相等查表" + slice(0,12) 截断, 几乎每笔都对不上 reasonMap 的纯净词,
  // 全部 fallback 到 c.text2 (灰) → 所有扇区都灰。
  // 修法: 先按分隔符取策略名前缀归一成类目, 再按类目聚合计数与上色;
  // 类目表对齐实盘真实用词 (executor._REASON_LABELS), 表外新类目走调色板兜底, 绝不再灰。
  const reasonColor = {
    '硬止损': c.up,                      // 旧词"成本止损"已按术语规范改"硬止损" (executor.py:40)
    '移动止盈': c.down,
    '阶梯止盈': c.accent,
    '时间止损': c.accent2,
    '条件时间止盈': hexToRgba(c.accent2, 0.55),
    '首日不达标': hexToRgba(c.down, 0.5),
    '换股卖出': hexToRgba(c.up, 0.55),
    '公式止损': hexToRgba(c.accent, 0.55),
    '退市': hexToRgba(c.text2, 0.6),
    '人工卖出': hexToRgba(c.accent, 0.4),
    '未标注': hexToRgba(c.text2, 0.35),
    '系统卖出': hexToRgba(c.text2, 0.5),
  };
  // 表外全新类目的兜底调色板 (确保任何 reason 都能上色, 不再退回灰)
  const fallbackPalette = [
    c.accent, c.down, c.up, c.accent2,
    hexToRgba(c.accent, 0.6), hexToRgba(c.down, 0.6),
    hexToRgba(c.up, 0.6), hexToRgba(c.accent2, 0.6),
  ];
  // 实盘成交原因 → 策略类目 (图例名 = 上色 key)。bucketReason 已提纯到 reason_util.js (可 node 测试)。
  const reasonCount = {};
  const reasonSample = {};  // 类目 → 首条原始 reason 全文 (供 tooltip 显示 detail, 补回归一丢失的数字)
  deals.filter(t => t.direction === DIR_SELL).forEach(t => {
    const label = bucketReason(t.reason, t.source);
    reasonCount[label] = (reasonCount[label] || 0) + 1;
    if (!reasonSample[label] && t.reason) reasonSample[label] = t.reason;
  });
  // 按笔数降序 (多的扇区在前, 图例更聚焦)
  const pieData = Object.entries(reasonCount)
    .map(([name, value]) => ({ name, value, detail: reasonSample[name] || '' }))
    .sort((a, b) => b.value - a.value);
  let fbIdx = 0;
  chart.setOption({
    // tooltip 除"类目: N 次 (P%)"外, 附该类目首条原始 reason 全文 (截断 60 字, esc 转义防注入)
    tooltip: {
      trigger: 'item',
      formatter: (p) => {
        const d = p.data;
        let s = `${d.name}: ${d.value} 次 (${p.percent}%)`;
        if (d.detail) {
          const dtl = d.detail.length > 60 ? d.detail.slice(0, 60) + '…' : d.detail;
          s += `<br/><span style="color:${c.text2};font-size:11px">${esc(dtl)}</span>`;
        }
        return s;
      },
    },
    legend: { bottom: 0, textStyle: { color: c.text, fontSize: 9 } },
    series: [{
      type: 'pie', radius: ['30%', '55%'], center: ['50%', '45%'],
      data: pieData.map(d => {
        // 表外新类目走调色板兜底 (可观测性: 便于排查再次掉灰)
        if (!isKnownReason(d.name)) console.debug('[renderExitPie] 表外类目走调色板兜底:', d.name);
        return {
          ...d,
          itemStyle: {
            color: reasonColor[d.name] || fallbackPalette[fbIdx++ % fallbackPalette.length],
            borderColor: c.bg, borderWidth: 1,
          },
        };
      }),
      label: { color: c.text, fontSize: 9, formatter: '{b}\n{d}%' }
    }],
  }, true);
}

// ── Deals table ──
async function loadDeals() {
  try {
    // 2026-08-13 修复: 旧调用不带日期 → 后端只给当日成交, 盈亏分布/卖出原因
    // 永远凑不出完整买卖对。改为拉全量历史 (2020 起, 上限 5000 笔),
    // 表格的日期过滤仍由 filterDealsByDate 在前端做。
    const today = new Date();
    const end = today.getFullYear() + String(today.getMonth() + 1).padStart(2, '0') + String(today.getDate()).padStart(2, '0');
    const resp = await fetch(TRADE_API + '/deals?start=20200101&end=' + end + '&limit=5000').then(r => r.json()).catch(() => null);
    _allDeals = (resp && resp.deals) ? resp.deals : [];
    renderDealTable();
  } catch (e) {
    _allDeals = [];
    renderDealTable();
  }
}

function filterDealsByDate(dateStr) {
  _dealPage = 0;
  document.getElementById('analysisTradeSearch').value = '';
  document.getElementById('analysisTradeFilter').value = '';
  document.getElementById('analysisTradeDir').value = '';
  renderDealTable(dateStr);
}

function renderDealTable(forceDate) {
  const searchStr = (document.getElementById('analysisTradeSearch').value || '').toLowerCase();
  const pnlFilter = document.getElementById('analysisTradeFilter').value;
  const dirFilter = document.getElementById('analysisTradeDir').value;
  let rows = _allDeals.slice();
  // Filter
  if (searchStr) rows = rows.filter(t => (t.code || '').toLowerCase().includes(searchStr) || (t.name || '').toLowerCase().includes(searchStr));
  if (forceDate) {
    const d = forceDate;
    rows = rows.filter(t => {
      const ts = new Date(t.ts * 1000);
      const ds = ts.getFullYear() + '-' + String(ts.getMonth() + 1).padStart(2, '0') + '-' + String(ts.getDate()).padStart(2, '0');
      return ds === d;
    });
  }
  if (dirFilter === 'buy') rows = rows.filter(t => t.direction === DIR_BUY);
  if (dirFilter === 'sell') rows = rows.filter(t => t.direction === DIR_SELL);
  // 盈亏筛选 (analysisTradeFilter): 依据后端 trades.pnl_amount (卖出盈亏,
  // book 成本法; 买入行/历史行 = null)。选盈利/亏损时买入行自然排除
  // (买入本无盈亏, 混入会干扰)。2026-08-10 修复: 原代码读了 pnlFilter
  // 变量却从未用于过滤, 下拉"动而无效"。
  if (pnlFilter === 'win') rows = rows.filter(t => t.pnl_amount != null && t.pnl_amount > 0);
  if (pnlFilter === 'loss') rows = rows.filter(t => t.pnl_amount != null && t.pnl_amount < 0);
  // Paginate
  const total = rows.length;
  const nPages = Math.max(1, Math.ceil(total / DEAL_PAGE_SIZE));
  if (_dealPage >= nPages) _dealPage = nPages - 1;
  const pageRows = rows.slice().reverse().slice(_dealPage * DEAL_PAGE_SIZE, (_dealPage + 1) * DEAL_PAGE_SIZE);
  const tbody = document.getElementById('analysisTradeBody');
  tbody.innerHTML = pageRows.map((t, i) => {
    const ts = new Date(t.ts * 1000);
    const ds = ts.getFullYear() + '-' + String(ts.getMonth() + 1).padStart(2, '0') + '-' + String(ts.getDate()).padStart(2, '0');
    const dir = t.direction === DIR_BUY ? '买' : '卖';
    const cls = t.direction === DIR_BUY ? 'td-down' : 'td-up';
    return '<tr><td>' + (total - (_dealPage * DEAL_PAGE_SIZE + i)) + '</td>' +
      '<td>' + stockLink(t.code) + '</td>' +
      '<td>' + (t.name ? stockLink(t.code, t.name) : '') + '</td>' +
      '<td>' + ds + '</td>' +
      '<td class="' + cls + '">' + dir + '</td>' +
      '<td>' + (t.price || 0).toFixed(2) + '</td>' +
      '<td>' + (t.qty || 0) + '</td>' +
      '<td>' + (t.amount || (t.price * t.qty) || 0).toLocaleString('zh-CN') + '</td>' +
      '<td style="font-size:10px;max-width:100px">' + esc(t.reason || (t.source === 'manual' ? '人工' : '')) + '</td></tr>';
  }).join('');
  document.getElementById('analysisTradeCount').textContent = '共 ' + total + ' 笔';
  // Pager
  const pager = document.getElementById('analysisTradePager');
  if (nPages <= 1) { pager.innerHTML = ''; }
  else {
    pager.innerHTML = '<button class="btn btn-sm" ' + (_dealPage === 0 ? 'disabled' : '') + ' id="adpPrev">‹ 上一页</button>' +
      '<span style="margin:0 8px">第 ' + (_dealPage + 1) + ' / ' + nPages + ' 页</span>' +
      '<button class="btn btn-sm" ' + (_dealPage >= nPages - 1 ? 'disabled' : '') + ' id="adpNext">下一页 ›</button>';
    document.getElementById('adpPrev').onclick = () => { _dealPage--; renderDealTable(forceDate); };
    document.getElementById('adpNext').onclick = () => { _dealPage++; renderDealTable(forceDate); };
  }
}

// Wire deal table toolbar
(function wireDealToolbar() {
  // Deferred until DOM ready
  setTimeout(() => {
    const search = document.getElementById('analysisTradeSearch');
    const filt = document.getElementById('analysisTradeFilter');
    const dirF = document.getElementById('analysisTradeDir');
    const reset = document.getElementById('analysisTradeReset');
    const goTrade = document.getElementById('analysisGoTradeBtn');
    if (search) search.addEventListener('input', () => { _dealPage = 0; renderDealTable(); });
    if (filt) filt.addEventListener('change', () => { _dealPage = 0; renderDealTable(); });
    if (dirF) dirF.addEventListener('change', () => { _dealPage = 0; renderDealTable(); });
    if (reset) reset.addEventListener('click', () => { _dealPage = 0; _selectedDate = ''; document.getElementById('analysisTradeSearch').value = ''; document.getElementById('analysisTradeFilter').value = ''; document.getElementById('analysisTradeDir').value = ''; renderDealTable(); });
    if (goTrade) goTrade.addEventListener('click', () => { document.getElementById('tabBtnTrade')?.click(); });
  }, 100);
})();
