// ====== VERA Chart Renderer ======
// 所有可视化渲染。接收数据，产出 DOM/ECharts。不发起网络请求。

// ── XSS 防御 ──

export function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
export const escAttr = esc;

// ── 颜色工具 ──

export function hexToRgba(color, alpha) {
  if (color.startsWith('rgb')) {
    const m = color.match(/[\d.]+/g);
    if (m && m.length >= 3) return 'rgba(' + m[0] + ',' + m[1] + ',' + m[2] + ',' + alpha + ')';
  }
  const hex = color.replace('#', '');
  const r = parseInt(hex.slice(0, 2), 16);
  const g = parseInt(hex.slice(2, 4), 16);
  const b = parseInt(hex.slice(4, 6), 16);
  return 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')';
}

// ── Theme ──

const themeIcon = document.getElementById('themeIcon');
const sunIcon = '<circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>';
const moonIcon = '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>';

export function getTheme() { return document.documentElement.getAttribute('data-theme'); }

export function getColors() {
  const s = getComputedStyle(document.documentElement);
  return {
    up: s.getPropertyValue('--up').trim(), down: s.getPropertyValue('--down').trim(),
    accent: s.getPropertyValue('--accent').trim(), accent2: s.getPropertyValue('--accent2').trim(),
    text: s.getPropertyValue('--text').trim(), text2: s.getPropertyValue('--text2').trim(),
    bg: s.getPropertyValue('--bg').trim(), border: s.getPropertyValue('--border').trim(),
    bm1: s.getPropertyValue('--bm-1').trim(), bm2: s.getPropertyValue('--bm-2').trim(),
    bm3: s.getPropertyValue('--bm-3').trim(), bm4: s.getPropertyValue('--bm-4').trim(), bm5: s.getPropertyValue('--bm-5').trim()
  };
}

export function toggleTheme() {
  const next = getTheme() === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('vera_theme', next);
  if (themeIcon) themeIcon.innerHTML = next === 'dark' ? sunIcon : moonIcon;
  // lastResult/renderAllCharts is injected by vera-ui.js
  if (toggleTheme._onToggle) toggleTheme._onToggle();
}

export function toggleSidebar() {
  const sb = document.querySelector('.sidebar');
  const btn = document.querySelector('.sidebar-toggle svg');
  sb.classList.toggle('collapsed');
  const closed = sb.classList.contains('collapsed');
  localStorage.setItem('vera_sidebar', closed ? '0' : '1');
  if (btn) btn.innerHTML = closed
    ? '<polyline points="9 18 15 12 9 6"/>'
    : '<polyline points="15 18 9 12 15 6"/>';
  // charts is injected by vera-ui.js
  if (toggleSidebar._onToggle) toggleSidebar._onToggle();
}

// ── Toast ──

export function showToast(msg, type) {
  const container = document.getElementById('toastContainer');
  const toast = document.createElement('div');
  toast.className = 'toast toast-' + (type || 'info');
  toast.textContent = msg;
  container.appendChild(toast);
  setTimeout(() => { if (toast.parentNode) toast.remove(); }, 3000);
}

// ── System Log ──

const logLines = [];

export function addLog(msg, type) {
  const now = new Date().toLocaleTimeString();
  logLines.push('<span class="log-' + (type || 'info') + '">[' + now + '] ' + esc(msg) + '</span>');
  if (logLines.length > 100) logLines.shift();
  const el = document.getElementById('logContent');
  if (el) {
    el.innerHTML = logLines.join('<br>');
    const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
    if (atBottom) el.scrollTop = el.scrollHeight;
  }
}

// ── Engine Version Check ──

const CURRENT_ENGINE_VERSION = 'signal-day-close';

export function checkEngineVersion(data) {
  const ev = data && data.engine_version;
  const banner = document.getElementById('legacyEngineWarning');
  if (!banner) return;
  if (!ev || ev !== CURRENT_ENGINE_VERSION) {
    banner.textContent = '⚠️ 此回测结果基于 ' + (ev || '旧版') + ' 引擎（买入价 = 次日开盘价）。当前默认采用 ' + CURRENT_ENGINE_VERSION + '（买入价 = 信号日收盘价）。请重跑以反映新口径。';
    banner.style.display = 'block';
  } else {
    banner.style.display = 'none';
  }
}

// ── ECharts 管理 ──

// charts dict is injected by vera-ui.js
export let _charts = {};

export function setChartsRef(ref) { _charts = ref; }

export function echartsInit(id) {
  const dom = document.getElementById(id);
  if (!dom) return null;
  if (_charts[id]) _charts[id].dispose();
  const c = echarts.init(dom);
  _charts[id] = c;
  c.resize();
  return c;
}

// ── KPI Tween ──

export function tweenNumber(el, target, formatter, duration) {
  duration = duration || 600;
  if (typeof target !== 'number' || isNaN(target)) { el.textContent = '--'; return; }
  const t0 = performance.now();
  function step(now) {
    const p = Math.min(1, (now - t0) / duration);
    const eased = 1 - Math.pow(1 - p, 3);
    el.textContent = formatter(target * eased);
    if (p < 1) requestAnimationFrame(step); else el.textContent = formatter(target);
  }
  requestAnimationFrame(step);
}

// ── SVG Sparkline ──

export function sparkline(values, color, fillAlpha) {
  if (!values || values.length < 2) return '';
  const w = 100, h = 32, pad = 2;
  let min = Infinity, max = -Infinity;
  for (const v of values) { if (v < min) min = v; if (v > max) max = v; }
  const range = max - min || 1;
  const n = values.length;
  const pts = values.map((v, i) => {
    const x = pad + (i / (n - 1)) * (w - pad * 2);
    const y = pad + (1 - (v - min) / range) * (h - pad * 2);
    return x.toFixed(1) + ',' + y.toFixed(1);
  }).join(' ');
  const areaPts = pad + ',' + (h - pad) + ' ' + pts + ' ' + (w - pad) + ',' + (h - pad);
  return '<svg viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none">' +
    '<polygon points="' + areaPts + '" fill="' + color + '" fill-opacity="' + (fillAlpha == null ? 0.15 : fillAlpha) + '"/>' +
    '<polyline points="' + pts + '" fill="none" stroke="' + color + '" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>' +
    '</svg>';
}

// ── Hero Sub-Metrics ──

export function fillHeroSub(m, data, c) {
  const sh = data.benchmarks && data.benchmarks.shanghai;
  let shRet = null;
  if (sh && sh.length > 1) {
    const first = sh.find(r => r.index_close != null);
    const last = sh.slice().reverse().find(r => r.index_close != null);
    if (first && last && first.index_close) shRet = last.index_close / first.index_close - 1;
  }
  const cumSub = document.getElementById('kpiCumRetSub');
  if (cumSub) {
    if (shRet != null && m.cumulative_return != null) {
      const win = m.cumulative_return >= shRet;
      cumSub.innerHTML = (win ? '▲' : '▼') + ' <span class="' + (win ? 'delta-up' : 'delta-down') + '">vs 上证 ' + (shRet * 100).toFixed(2) + '%</span>';
    } else cumSub.textContent = '';
  }
  const ddSub = document.getElementById('kpiMaxDDSub');
  if (ddSub) {
    if (m.max_drawdown != null) {
      let rec = '';
      if (m.max_dd_recovery_days != null) {
        rec = m.max_dd_recovered === false ? ' · ' + m.max_dd_recovery_days + '天仍未修复' : ' · 修复用' + m.max_dd_recovery_days + '天';
      }
      ddSub.innerHTML = '<span style="color:var(--text2)">峰值到谷值最大跌幅' + rec + '</span>';
    } else ddSub.textContent = '';
  }
  const shSub = document.getElementById('kpiSharpeSub');
  if (shSub) {
    if (m.sharpe_ratio != null) {
      let grade, cls;
      if (m.sharpe_ratio >= 2) { grade = '优秀'; cls = 'delta-up'; }
      else if (m.sharpe_ratio >= 1) { grade = '良好'; cls = 'delta-up'; }
      else if (m.sharpe_ratio >= 0) { grade = '一般'; cls = ''; }
      else { grade = '不佳'; cls = 'delta-down'; }
      const srt = m.sortino_ratio != null ? '<span style="color:var(--text2)"> · Sortino ' + Number(m.sortino_ratio).toFixed(2) + '</span>' : '';
      shSub.innerHTML = '<span class="' + cls + '">' + grade + '</span>' + srt;
    } else shSub.textContent = '';
  }
  const cumSpark = document.getElementById('kpiCumRetSpark');
  if (cumSpark) {
    if (data.equity && data.equity.length > 1) {
      const eq0 = data.equity[0].equity || 1;
      const eqPct = data.equity.map(r => (r.equity / eq0 - 1) * 100);
      cumSpark.innerHTML = sparkline(eqPct, c.accent, 0.16);
    } else cumSpark.innerHTML = '';
  }
  const ddSpark = document.getElementById('kpiMaxDDSpark');
  if (ddSpark) {
    if (data.equity && data.equity.length > 1) {
      const dd = data.equity.map(r => (r.drawdown || 0) * 100);
      ddSpark.innerHTML = sparkline(dd, c.up, 0.2);
    } else ddSpark.innerHTML = '';
  }
}

// ── Stagger Reveal ──

export function revealResults() {
  const cards = document.querySelectorAll('.kpi-card, .chart-box');
  cards.forEach(c => { c.classList.remove('reveal'); c.style.animationDelay = ''; });
  const grid = document.getElementById('kpiGrid');
  if (grid) void grid.offsetWidth;
  cards.forEach((c, i) => {
    c.style.animationDelay = (i * 40) + 'ms';
    c.classList.add('reveal');
  });
}

// ── Trade Reason Mappings ──

const reasonDetail = {
  '成本止损': '成本止损 — 亏损触及止损线，全仓卖出',
  '移动止盈': '移动止盈 — 从最高点回撤触及阈值，按回撤线价清仓',
  '阶梯止盈': '阶梯止盈 — 盈利达到目标档位，分批卖出',
  '时间止损': '时间止损 — 持仓天数达到上限，亏损清仓',
  '时间止盈': '时间止盈 — 持仓天数达到上限，盈利清仓',
  'cond_time_stop': '条件时间止盈 — 持仓N天后盈利达标，全仓卖出',
  '首日未达标': '首日未达标 — 买入次日最高价涨幅未达目标，收盘强制卖出',
  '换股卖出': '换股卖出 — 同一股票出现新买入信号，替换旧持仓',
  'formula_sell': '公式止损 — TDX 公式信号命中，按比例止损（不看盈亏，最高优先级）',
};

const reasonShortMap = {
  '成本止损': '成本止损', '移动止盈': '移动止盈',
  '阶梯止盈': '阶梯止盈', '时间止损': '时间止损',
  '时间止盈': '时间止盈', cond_time_stop: '条件时间止盈',
  trailing_stop: '移动止盈', '换股卖出': '换股卖出',
  '首日未达标': '首日未达标', 'formula_sell': '公式止损'
};

export function fmtReasonShort(r) {
  if (!r) return '—';
  const parts = r.split('+').map(s => reasonShortMap[s] || s);
  return parts.join('+');
}

// ── Trade Table ──

export function renderTradeTable(trades, allTradesCount) {
  document.getElementById('tradeTableBox').style.display = '';
  const minBuy = document.getElementById('cfgMinBuy').value || 2000;
  const maxBuy = document.getElementById('cfgMaxBuy').value || 10000;
  const lot = document.getElementById('cfgLotSize').value || 100;
  document.getElementById('tradeCount').textContent =
    '(本次回测: ' + trades.length + ' 笔 | 每笔' + minBuy + '~' + maxBuy + '元, ' + lot + '股/手)';
  const tbody = document.getElementById('tradeTableBody');
  const totalTrades = trades.length;
  tbody.innerHTML = trades.slice().reverse().map((t, i) => {
    const pnl = (t.profit_pct || t.return || 0) * 100;
    const cls = pnl > 0 ? 'td-up' : pnl < 0 ? 'td-down' : '';
    const eDate = esc(String(t.entry_date || '').slice(0, 10));
    const xDate = esc(String(t.exit_date || '').slice(0, 10));
    const name = esc(t.stock_name || t.stock_code || '');
    const code = esc(t.stock_code || '');
    const shares = t.shares || 0;
    const ep = t.entry_price || 0;
    const xp = t.exit_price || 0;
    const holdDays = t.hold_days != null ? t.hold_days : '';
    const reasonShort = esc(fmtReasonShort(t.exit_reason));
    const reasonFull = esc((t.exit_reason || '').split('+').map(s => reasonDetail[s] || s).join('；'));
    return '<tr>' +
      '<td style="color:var(--text2);font-size:10px">' + (totalTrades - i) + '</td>' +
      '<td style="font-family:var(--mono);font-size:10px">' + code + '</td>' +
      '<td title="' + code + '">' + name + '</td>' +
      '<td>' + eDate + '</td>' +
      '<td>' + ep + '</td>' +
      '<td>' + shares + ' 股</td>' +
      '<td>' + xDate + '</td>' +
      '<td>' + xp + '</td>' +
      '<td>' + shares + ' 股</td>' +
      '<td>' + holdDays + '</td>' +
      '<td class="' + cls + '">' + pnl.toFixed(2) + '%</td>' +
      '<td style="font-size:10px;max-width:120px" title="' + reasonFull + '">' + reasonShort + '</td></tr>';
  }).join('');
  document.getElementById('tradeFiltered').textContent = '显示 ' + trades.length + ' / ' + (allTradesCount || trades.length) + ' 笔';
  // dynamic reason dropdown population
  const reasonSelect = document.getElementById('tradeReason');
  if (reasonSelect) {
    const existing = Array.from(reasonSelect.options).map(o => o.value);
    const seenReasons = new Set();
    (allTradesCount ? trades : trades).forEach(t => {
      if (!t.exit_reason) return;
      t.exit_reason.split('+').forEach(r => { if (r) seenReasons.add(r); });
    });
    seenReasons.forEach(r => {
      if (!existing.includes(r)) {
        const opt = document.createElement('option');
        opt.value = r;
        opt.textContent = reasonShortMap[r] || r;
        reasonSelect.appendChild(opt);
      }
    });
  }
}

// ── Trade Filter ──

export function filterTrades(allTrades, renderFn) {
  if (!allTrades || !allTrades.length) return;
  const search = (document.getElementById('tradeSearch').value || '').toLowerCase();
  const filter = document.getElementById('tradeFilter').value;
  const reason = document.getElementById('tradeReason').value;
  const reasonMap = {
    '成本止损': '成本止损', '移动止盈': '移动止盈',
    '阶梯止盈': '阶梯止盈', '时间止损': '时间止损',
    '时间止盈': '时间止盈', cond_time_stop: '条件时间止盈',
    trailing_stop: '移动止盈', '换股卖出': '换股卖出',
    '首日未达标': '首日未达标', 'formula_sell': '公式止损'
  };
  const filtered = allTrades.filter(t => {
    const pnl = (t.profit_pct || t.return || 0) * 100;
    if (search) {
      const code = (t.stock_code || '').toLowerCase();
      const name = (t.stock_name || '').toLowerCase();
      if (!code.includes(search) && !name.includes(search)) return false;
    }
    if (filter === 'win' && pnl <= 0) return false;
    if (filter === 'loss' && pnl >= 0) return false;
    if (reason) {
      const reasons = (t.exit_reason || '换股卖出').split('+').map(r => reasonMap[r] || r);
      if (!reasons.includes(reason)) return false;
    }
    return true;
  });
  if (renderFn) renderFn(filtered, allTrades.length);
}

// ── Main Render Entry ──

export function renderAllCharts(data) {
  const c = getColors();
  const m = data.metrics || {};

  // KPI cards
  const setKpi = (id, val, fmt) => {
    const el = document.getElementById(id);
    el.className = 'kpi-value';
    if (typeof val === 'number') { if (val > 0) el.classList.add('pos'); else if (val < 0) el.classList.add('neg'); }
    tweenNumber(el, val, fmt);
  };
  setKpi('kpiCumRet', m.cumulative_return, v => v != null ? (v * 100).toFixed(2) + '%' : '--');
  setKpi('kpiAnnRet', m.annualized_return, v => v != null ? (v * 100).toFixed(2) + '%' : '--');
  setKpi('kpiMaxDD', m.max_drawdown, v => v != null ? (v * 100).toFixed(2) + '%' : '--');
  setKpi('kpiSharpe', m.sharpe_ratio, v => v != null ? v.toFixed(2) : '--');
  setKpi('kpiWinRate', m.win_rate, v => v != null ? (v * 100).toFixed(1) + '%' : '--');
  setKpi('kpiPLR', m.profit_loss_ratio, v => v != null ? v.toFixed(2) : '--');
  setKpi('kpiTrades', m.total_trades, v => v != null ? Math.round(v) : '--');
  setKpi('kpiCalmar', m.calmar_ratio, v => v != null ? v.toFixed(2) : '--');
  setKpi('kpiProfitF', m.profit_factor, v => v != null ? v.toFixed(2) : '--');
  setKpi('kpiBest', m.max_single_gain, v => v != null ? (v * 100).toFixed(2) + '%' : '--');

  // Excess return cards
  const bs = data.benchmark_stats || {};
  const fillExcess = (id, subId, st) => {
    setKpi(id, st && st.total_excess, v => v != null ? (v >= 0 ? '+' : '') + (v * 100).toFixed(2) + '%' : '--');
    const sub = document.getElementById(subId);
    if (!sub) return;
    if (st && st.annual_excess != null) {
      const ir = st.information_ratio != null ? ' · IR ' + Number(st.information_ratio).toFixed(2) : '';
      const mw = st.excess_monthly_win_rate != null ? ' · 月胜率 ' + (st.excess_monthly_win_rate * 100).toFixed(0) + '%' : '';
      sub.innerHTML = '<span style="color:var(--text2)">年化 ' + (st.annual_excess >= 0 ? '+' : '') + (st.annual_excess * 100).toFixed(2) + '%' + ir + mw + '</span>';
    } else sub.textContent = '';
  };
  fillExcess('kpiExcessHS300', 'kpiExcessHS300Sub', bs.hs300);
  fillExcess('kpiExcessCYB', 'kpiExcessCYBSub', bs.chuangyeban);

  // Chart header
  const formula = data.formula_name || '';
  const dateRange = (document.getElementById('cfgStart').value || '') + '~' + (document.getElementById('cfgEnd').value || '');
  const hdr = document.getElementById('equityChartHeader');
  if (hdr && formula) hdr.textContent = '权益曲线 & 基准对比 — ' + formula + ' ' + dateRange;

  // ECharts: Equity curve
  if (data.equity && data.equity.length > 0) {
    const chart = echartsInit('chartEquity');
    const dates = data.equity.map(r => r.date.slice(0, 10));
    const eq0 = data.equity[0].equity ?? 1;
    const eqPct = data.equity.map(r => (r.equity / eq0 - 1) * 100);
    const dd = data.equity.map(r => (r.drawdown || 0) * 100);
    const series = [
      { name: '策略', type: 'line', data: eqPct, smooth: true,
        lineStyle: { color: c.accent, width: 2.5 }, symbol: 'none',
        markLine: { silent: true, data: [{ yAxis: 0, lineStyle: { color: c.text2, type: 'dashed', width: 1 } }] } },
      { name: '回撤', type: 'line', yAxisIndex: 1, data: dd,
        lineStyle: { color: c.down, width: 1 }, areaStyle: { color: hexToRgba(c.down, 0.13) },
        symbol: 'none' },
    ];
    if (data.benchmarks) {
      const bmNames = { shanghai: '上证', hs300: '沪深300', chuangyeban: '创业板', kechuang50: '科创50', zhongzhengA500: '中证A500' };
      const bmColors = [c.bm1, c.bm2, c.bm3, c.bm4];
      let ci = 0;
      for (const [name, bm] of Object.entries(data.benchmarks)) {
        if (!bm || !bm.length) continue;
        const bmMap = {};
        bm.forEach(r => {
          const d = String(r.date || '').slice(0, 10);
          if (r.index_close != null) bmMap[d] = r.index_close;
        });
        let bmStart = null;
        for (const d of dates) {
          if (bmMap[d] != null) { bmStart = bmMap[d]; break; }
        }
        if (bmStart == null) continue;
        const bmVals = dates.map(d => bmMap[d] != null ? (bmMap[d] / bmStart - 1) * 100 : null);
        if (bmVals.every(v => v === null)) continue;
        series.push({ name: bmNames[name] || name, type: 'line', data: bmVals,
          lineStyle: { color: bmColors[ci % 4], width: 1, type: 'dashed' }, symbol: 'none',
          connectNulls: true });
        ci++;
      }
    }
    chart.setOption({
      tooltip: { trigger: 'axis', formatter: function(params) {
        let s = params[0].axisValue + '<br/>';
        params.forEach(p => {
          s += '<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:' + p.color + ';margin-right:5px"></span>';
          s += p.seriesName + ': <b>' + (p.value != null ? p.value.toFixed(2) + '%' : '-') + '</b><br/>';
        });
        return s;
      }},
      legend: { top: 0, textStyle: { color: c.text, fontSize: 10 } },
      dataZoom: [
        { type: 'inside', xAxisIndex: 0, start: 0, end: 100 },
        { type: 'slider', xAxisIndex: 0, bottom: 10, height: 20,
          borderColor: c.border, fillerColor: hexToRgba(c.accent, 0.13),
          textStyle: { color: c.text2, fontSize: 9 } },
      ],
      grid: { left: 60, right: 50, top: 35, bottom: 60 },
      xAxis: { type: 'category', data: dates, axisLine: { lineStyle: { color: c.border } }, axisLabel: { color: c.text2, fontSize: 9 } },
      yAxis: [
        { type: 'value', name: '累计收益 %', nameTextStyle: { color: c.text2, fontSize: 10 },
          axisLabel: { color: c.text2, fontSize: 9, formatter: '{value}%' }, splitLine: { lineStyle: { color: c.border } } },
        { type: 'value', name: '回撤 %', nameTextStyle: { color: c.text2, fontSize: 10 },
          axisLabel: { color: c.text2, fontSize: 9, formatter: '{value}%' }, splitLine: { show: false } },
      ],
      toolbox: { right: 800, top: 0, feature: {
        saveAsImage: { title: '保存图片', pixelRatio: 2 },
        dataZoom: { title: { zoom: '区域缩放', back: '还原' } },
        restore: { title: '刷新' },
      }, iconStyle: { borderColor: c.text2 } },
      series: series,
    }, true);
  }

  // Monthly returns
  if (data.equity && data.equity.length > 1) {
    const chart = echartsInit('chartMonthly');
    const monthly = {};
    for (let i = 1; i < data.equity.length; i++) {
      if (data.equity[i - 1].equity > 0) {
        const d = new Date(data.equity[i].date);
        const k = d.getFullYear() + '年' + String(d.getMonth() + 1).padStart(2, '0') + '月';
        const r = (data.equity[i].equity - data.equity[i - 1].equity) / data.equity[i - 1].equity;
        if (!monthly[k]) monthly[k] = []; monthly[k].push(r);
      }
    }
    const months = Object.keys(monthly).sort();
    const mData = months.map(m => {
      const product = monthly[m].reduce((acc, r) => acc * (1 + r), 1);
      return { name: m, value: ((product - 1) * 100).toFixed(2) };
    });
    chart.setOption({
      tooltip: { trigger: 'axis', formatter: p => p[0].name + '<br/>月收益: <b>' + p[0].value + '%</b>' },
      grid: { left: 50, right: 20, top: 10, bottom: 60 },
      xAxis: { type: 'category', data: months, axisLabel: { color: c.text2, fontSize: 9, rotate: 45 }, axisLine: { lineStyle: { color: c.border } } },
      yAxis: { type: 'value', name: '月收益 %', nameTextStyle: { color: c.text2, fontSize: 10 }, axisLabel: { color: c.text2, fontSize: 9, formatter: '{value}%' }, splitLine: { lineStyle: { color: c.border } } },
      series: [{ type: 'bar', data: mData.map(d => ({ value: parseFloat(d.value), itemStyle: { color: parseFloat(d.value) >= 0 ? c.up : c.down } })) }],
    }, true);
  }

  // Trade analysis charts
  if (data.trades && data.trades.length > 0) {
    // P&L distribution
    const chart1 = echartsInit('chartTrade');
    const ranges = [
      { label: '< -10%', min: -Infinity, max: -10 },
      { label: '-10%~-5%', min: -10, max: -5 },
      { label: '-5%~0%', min: -5, max: 0 },
      { label: '0%~5%', min: 0, max: 5 },
      { label: '5%~10%', min: 5, max: 10 },
      { label: '10%~20%', min: 10, max: 20 },
      { label: '> 20%', min: 20, max: Infinity },
    ];
    const downShades = [hexToRgba(c.down, 0.5), hexToRgba(c.down, 0.7), c.down];
    const upShades = [c.up, hexToRgba(c.up, 0.8), hexToRgba(c.up, 0.5), hexToRgba(c.up, 0.35)];
    const rangeColors = [...downShades, ...upShades];
    const rangeCount = ranges.map((r, i) => {
      const cnt = data.trades.filter(t => {
        const v = (t.profit_pct || t.return || 0) * 100;
        return v >= r.min && v < r.max;
      }).length;
      return { name: r.label, value: cnt, itemStyle: { color: rangeColors[i] } };
    });
    chart1.setOption({
      tooltip: { trigger: 'axis', formatter: p => p[0].name + '<br/>交易笔数: <b>' + p[0].value + '</b>' },
      grid: { left: 50, right: 20, top: 10, bottom: 40 },
      xAxis: { type: 'category', data: rangeCount.map(d => d.name), axisLabel: { color: c.text2, fontSize: 9, rotate: 30 }, axisLine: { lineStyle: { color: c.border } } },
      yAxis: { type: 'value', name: '笔数', nameTextStyle: { color: c.text2, fontSize: 10 }, axisLabel: { color: c.text2, fontSize: 9 }, splitLine: { lineStyle: { color: c.border } } },
      series: [{ type: 'bar', data: rangeCount }],
    }, true);

    // Exit reason pie
    const chart2 = echartsInit('chartExit');
    const reasonMap = { '成本止损': '成本止损', '移动止盈': '移动止盈', '阶梯止盈': '阶梯止盈', '时间止损': '时间止损', '时间止盈': '时间止盈', cond_time_stop: '条件时间止盈', trailing_stop: '移动止盈', '换股卖出': '换股卖出', '首日未达标': '首日未达标', 'formula_sell': '公式止损', '退市': '退市' };
    const reasonCount = {};
    data.trades.forEach(t => {
      const reasons = (t.exit_reason || '换股卖出').split('+');
      reasons.forEach(r => {
        const label = reasonMap[r] || r;
        reasonCount[label] = (reasonCount[label] || 0) + 1;
      });
    });
    const pieData = Object.entries(reasonCount).map(([name, value]) => ({ name, value }));
    const reasonColorMap = {
      '成本止损': c.up,
      '移动止盈': c.down,
      '阶梯止盈': c.accent,
      '时间止损': c.accent2,
      '时间止盈': 'color-mix(in srgb, ' + c.down + ' 50%, ' + c.accent + ')',
      '条件时间止盈': 'color-mix(in srgb, ' + c.accent2 + ' 50%, ' + c.warn + ')',
      '换股卖出': 'color-mix(in srgb, ' + c.up + ' 50%, ' + c.accent2 + ')',
      '首日未达标': 'color-mix(in srgb, ' + c.warn + ' 50%, ' + c.text + ')',
      '公式止损': 'color-mix(in srgb, ' + c.accent + ' 50%, ' + c.up + ')',
      '退市': c.text2,
    };
    chart2.setOption({
      tooltip: { trigger: 'item', formatter: '{b}: {c} 次 ({d}%)' },
      legend: { bottom: 0, textStyle: { color: c.text, fontSize: 10 } },
      series: [{ type: 'pie', radius: ['35%', '60%'], center: ['50%', '45%'], data: pieData.map(d => ({
        ...d,
        itemStyle: { color: reasonColorMap[d.name] || c.text2, borderColor: c.bg, borderWidth: 2 },
      })),
        label: { color: c.text, fontSize: 10, formatter: '{b}\n{d}%' } }],
    }, true);

    // Hold days distribution
    const chart3 = echartsInit('chartHold');
    const holdDays = data.trades.map(t => t.hold_days).filter(v => v != null && v >= 0);
    const holdRanges = [
      { label: '1天', min: 0, max: 2 },
      { label: '2-3天', min: 2, max: 4 },
      { label: '4-7天', min: 4, max: 8 },
      { label: '8-14天', min: 8, max: 15 },
      { label: '15-20天', min: 15, max: 21 },
      { label: '20天+', min: 21, max: Infinity },
    ];
    const holdData = holdRanges.map(r => ({
      name: r.label, value: holdDays.filter(d => d >= r.min && d < r.max).length
    }));
    chart3.setOption({
      tooltip: { trigger: 'axis', formatter: p => '持仓' + p[0].name + '<br/>笔数: <b>' + p[0].value + '</b>' },
      grid: { left: 50, right: 20, top: 10, bottom: 30 },
      xAxis: { type: 'category', data: holdData.map(d => d.name), axisLabel: { color: c.text2, fontSize: 9 }, axisLine: { lineStyle: { color: c.border } } },
      yAxis: { type: 'value', name: '笔数', nameTextStyle: { color: c.text2, fontSize: 10 }, axisLabel: { color: c.text2, fontSize: 9 }, splitLine: { lineStyle: { color: c.border } } },
      series: [{ type: 'bar', data: holdData, itemStyle: { color: c.accent } }],
    }, true);

    // Trade table
    renderTradeTable(data.trades, data.trades.length);

    // Populate reason dropdown
    const reasonSelect = document.getElementById('tradeReason');
    const existingReasons = new Set();
    data.trades.forEach(t => {
      const reasons = (t.exit_reason || '换股卖出').split('+');
      reasons.forEach(r => { existingReasons.add(reasonMap[r] || r); });
    });
    reasonSelect.innerHTML = '<option value="">所有退出原因</option>';
    [...existingReasons].sort().forEach(r => {
      const o = document.createElement('option');
      o.value = r; o.textContent = r;
      reasonSelect.appendChild(o);
    });
  } else {
    document.getElementById('tradeFiltered').textContent = '显示 0 / 0 笔';
  }

  // Stop config summary
  if (data.stop_config_summary) {
    document.getElementById('summaryBox').style.display = '';
    document.getElementById('summaryContent').textContent = data.stop_config_summary;
  }

  // Degradation card
  const degrBox = document.getElementById('degradationBox');
  if (data.degradation && data.degradation.enabled) {
    const dg = data.degradation;
    const fmtAmt = v => (v == null ? '—' : (v >= 0 ? '+' : '') + Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 0 }) + ' 元');
    const fmtPct = v => (v == null ? '—' : (v * 100).toFixed(2) + '%');
    const lines = [];
    lines.push('降级持仓: ' + dg.degraded_trades + ' / ' + dg.total_trades + ' 笔 (' + fmtPct(dg.degraded_pct) + ')'
      + ' | 降级股-天: ' + (dg.n_stock_days || 0)
      + (dg.rejected_limit_up ? ' | 1d 涨停拒绝: ' + dg.rejected_limit_up : ''));
    if (dg.degraded_days && Object.keys(dg.degraded_days).length) {
      lines.push('降级日分布: ' + Object.entries(dg.degraded_days).map(([d, n]) => d + ' × ' + n).join(', '));
    }
    if (dg.impact_amount) {
      lines.push('影响金额区间: [' + fmtAmt(dg.impact_amount.pessimistic) + ', ' + fmtAmt(dg.impact_amount.optimistic) + ']'
        + ' (模糊日 ' + (dg.ambiguous_trades || 0) + ' 笔: 同日止盈止损先后未知)');
    }
    if (dg.return_range) {
      lines.push('收益率影响: [' + fmtPct(dg.return_range.pessimistic) + ', ' + fmtPct(dg.return_range.optimistic) + ']');
    }
    if (dg.close_based_stop_bias && dg.close_based_stop_bias.count) {
      lines.push('收盘价策略偏差: ' + dg.close_based_stop_bias.count + ' 笔, ['
        + fmtAmt(dg.close_based_stop_bias.amount_pessimistic) + ', '
        + fmtAmt(dg.close_based_stop_bias.amount_optimistic) + ']'
        + ' (时间止损/条件时间止盈/首日按 1d 收盘成交, 真 5m 价不可观测)');
    }
    if (dg.sharpe_range && dg.max_drawdown_range) {
      lines.push('夏普区间: [' + Number(dg.sharpe_range.pessimistic).toFixed(2) + ', ' + Number(dg.sharpe_range.optimistic).toFixed(2) + ']'
        + ' | 最大回撤区间: [' + fmtPct(dg.max_drawdown_range.pessimistic) + ', ' + fmtPct(dg.max_drawdown_range.optimistic) + ']');
    }
    if (dg.adjust_mismatches) {
      lines.push('⚠ 复权一致性违例: ' + dg.adjust_mismatches + ' 处 (5m 日聚合 ≠ 1d, 查复权口径)');
    }
    document.getElementById('degradationContent').textContent = lines.join('\n');
    degrBox.style.display = '';
  } else {
    degrBox.style.display = 'none';
  }

  // Open positions
  const opBox = document.getElementById('openPosBox');
  if (data.open_positions && data.open_positions.length > 0) {
    const ops = data.open_positions;
    const mvSum = ops.reduce((s, p) => s + (p.market_value || 0), 0);
    document.getElementById('openPosCount').textContent =
      '(' + ops.length + ' 笔, 市值合计 ' + mvSum.toLocaleString('zh-CN', { maximumFractionDigits: 0 }) + ' 元, 已按市值计入权益)';
    document.getElementById('openPosTableBody').innerHTML = ops.map((p, i) => {
      const pct = (p.unrealized_pct || 0) * 100;
      const cls = pct > 0 ? 'td-up' : pct < 0 ? 'td-down' : '';
      return '<tr><td>' + (i + 1) + '</td>'
        + '<td>' + esc(p.stock_code || '') + '</td>'
        + '<td>' + esc(p.stock_name || '') + '</td>'
        + '<td>' + esc(String(p.entry_date || '').slice(0, 10)) + '</td>'
        + '<td>' + (p.entry_price != null ? p.entry_price : '—') + '</td>'
        + '<td>' + (p.shares != null ? p.shares : '—') + '</td>'
        + '<td>' + (p.last_price != null ? p.last_price : '—') + '</td>'
        + '<td>' + (p.market_value != null ? Number(p.market_value).toLocaleString('zh-CN', { maximumFractionDigits: 0 }) : '—') + '</td>'
        + '<td class="' + cls + '">' + pct.toFixed(2) + '%</td></tr>';
    }).join('');
    opBox.style.display = '';
  } else {
    opBox.style.display = 'none';
  }

  // Hero sub-metrics + reveal
  fillHeroSub(m, data, c);
  revealResults();
}
