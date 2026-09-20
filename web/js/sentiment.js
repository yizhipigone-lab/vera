/**
 * web/js/sentiment.js — 「舆情」页签渲染 (2026-09-20)。
 *
 * 两块：① 舆情异动台账（历史回溯）  ② 机构研究雷达（周报正文 + 近期报告元数据）
 * 数据源：/api/sentiment/*（sentiment_api.py，**只读**）。
 *
 * 口径纪律（与后端一致）：本页是**台账**（当时发生了什么），不是**指标**
 * （接下来会怎样）—— 只显示计数与原始记录，**不做评分/权重/总分**。
 * `core/market_validity.py` 已证伪宽度类指标的预测力，舆情同族。
 *
 * 两条既有约定：
 * - 同源相对路径 `fetch('/api/...')`：8080 既服务页面也服务 API。
 *   （**只有** 8081 的交易 API 需要自己拼 host —— 那是 trade.js 踩过的坑，本页不涉及。）
 * - markdown 渲染走 marked + DOMPurify（与体检报告/复盘同款），缺载退回转义纯文本。
 *
 * 生命周期：切入加载一次，**无轮询**（数据每天更新，不需要实时流），故 leave 是空操作。
 */

const $ = (id) => document.getElementById(id);

/** 当前查询状态。day 模式看单日；range 模式看区间。 */
const state = { mode: 'day', date: _iso(new Date()), start: null, end: null, rule: '', code: '' };

function _iso(d) {
  const p = (n) => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
}

function _esc(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

async function _get(url) {
  try {
    const r = await fetch(url);
    const d = await r.json();
    // 2026-09-20: FastAPI 的入参校验失败（422）返回的是 {"detail": [...]} 而**不是**
    // 业务形状 —— 若直接返回，调用方读 d.success 得到 undefined，页面只会显示
    // "读取失败：undefined"。这正是 2026-09-17 大盘箱线图 422 静默空白那类坑，
    // 故此处把 detail 提成可读错误，绝不让"入参错"看起来像"没数据"。
    if (!r.ok) {
      const detail = (d && d.detail !== undefined) ? JSON.stringify(d.detail) : ('HTTP ' + r.status);
      return { success: false, error: 'HTTP ' + r.status + '：' + detail };
    }
    return d;
  } catch (e) {
    return { success: false, error: '请求失败：' + e };
  }
}

/** markdown → 消毒后的 HTML；marked 缺载时退回转义纯文本。 */
function _renderMd(box, md) {
  if (window.marked && window.DOMPurify) {
    const html = window.veraDemoteH1 ? window.veraDemoteH1(marked.parse(md)) : marked.parse(md);
    box.innerHTML = DOMPurify.sanitize(html);
  } else {
    box.innerHTML = '<pre style="white-space:pre-wrap">' + _esc(md) + '</pre>';
  }
}

// ── ① 异动台账 ────────────────────────────────────────────────────

function _alertsUrl() {
  const p = new URLSearchParams();
  if (state.mode === 'range') {
    p.set('start', state.start);
    p.set('end', state.end);
  } else {
    p.set('date', state.date);
  }
  if (state.rule) p.set('rule', state.rule);
  if (state.code) p.set('code', state.code.trim());
  p.set('limit', '500');
  return '/api/sentiment/alerts?' + p.toString();
}

/** 极性 → 带符号文本 + 色调（看多用 up 红，看空用 down 绿，与全站一致）。 */
function _pol(polarity) {
  const v = Number(polarity) || 0;
  const cls = v > 0 ? 'st-up' : (v < 0 ? 'st-down' : 'md-dim');
  const sign = v > 0 ? '+' : '';
  return { cls: cls, text: sign + v.toFixed(2) };
}

async function loadAlerts() {
  const hint = $('stHint');
  const list = $('stList');
  const sum = $('stSummary');
  hint.textContent = '加载中…';
  const d = await _get(_alertsUrl());

  if (!d.success) {
    hint.textContent = '';
    sum.innerHTML = '<span class="st-down">读取失败：' + _esc(d.error) + '</span>';
    list.innerHTML = '';
    return;
  }

  // 规则下拉的选项来自后端下发的 rule_names（唯一真相源，前端不硬编码第二份）
  const sel = $('stRule');
  if (sel && d.rule_names && sel.options.length <= 1) {
    Object.keys(d.rule_names).forEach(function (k) {
      const o = document.createElement('option');
      o.value = k;
      o.textContent = d.rule_names[k];
      sel.appendChild(o);
    });
    sel.value = state.rule;
  }

  hint.textContent = '范围 ' + d.range + ' · ' + d.total + ' 条';
  const note = d.note ? '<div class="md-dim">' + _esc(d.note) + '</div>' : '';
  sum.innerHTML = note +
    '<b>' + d.total + '</b> 条异动（看多 <span class="st-up">' + d.bullish +
    '</span> / 看空 <span class="st-down">' + d.bearish + '</span>）' +
    '<span class="md-dim"> · 极性 = LLM 打的情绪分，强度 = 1 中 / 2 强</span>';

  if (!d.items.length) {
    list.innerHTML = '<div class="md-note" style="margin-top:10px">' +
      '这个范围没有异动记录。无异动也可能是"当天没跑"——调度器停机时不会有任何记录。' +
      '</div>';
    return;
  }

  const rows = d.items.map(function (a) {
    const p = _pol(a.polarity);
    const who = a.name ? (a.name + '(' + _esc(a.code) + ')') : _esc(a.code || '—');
    return '<div class="st-row">' +
      '<span class="st-time">' + _esc(a.time) + '</span>' +
      '<span class="st-rule">' + _esc(a.rule_name || a.rule) + '</span>' +
      '<span class="st-who">' + who + '</span>' +
      '<span class="' + p.cls + ' st-pol">' + p.text + '</span>' +
      '<span class="st-str">强度 ' + _esc(a.strength) + '</span>' +
      '<span class="st-evi">' + _esc(a.evidence) + '</span>' +
      '</div>';
  });
  list.innerHTML = '<div class="st-table">' + rows.join('') + '</div>';
}

// ── 分布（纯计数，不是评分）────────────────────────────────────────

async function loadSummary() {
  const box = $('stSummaryBox');
  if (!box) return;
  const d = await _get('/api/sentiment/summary?days=30');
  if (!d.success) {
    box.innerHTML = '<span class="st-down">读取失败：' + _esc(d.error) + '</span>';
    return;
  }
  if (!d.total) {
    box.innerHTML = '<div class="md-note">近 30 天没有记录。</div>';
    return;
  }
  const maxDay = Math.max.apply(null, d.by_day.map(function (x) { return x.count; }).concat([1]));
  const days = d.by_day.map(function (x) {
    const w = Math.max(2, Math.round(x.count / maxDay * 120));
    return '<div class="st-bar-row"><span class="st-bar-date">' + _esc(x.date) + '</span>' +
      '<span class="st-bar" style="width:' + w + 'px"></span>' +
      '<span class="st-bar-n">' + x.count + '</span></div>';
  }).join('');
  const rules = d.by_rule.map(function (x) {
    return '<div class="st-bar-row"><span class="st-bar-date">' + _esc(x.name) + '</span>' +
      '<span class="st-bar-n">' + x.count + '</span></div>';
  }).join('');
  const codes = d.top_codes.map(function (x) {
    const p = _pol(x.net);
    return '<div class="st-bar-row"><span class="st-bar-date">' +
      (x.name ? _esc(x.name) + '(' + _esc(x.code) + ')' : _esc(x.code)) + '</span>' +
      '<span class="st-bar-n">' + x.count + ' 次</span>' +
      '<span class="' + p.cls + '">净 ' + p.text + '</span></div>';
  }).join('');

  box.innerHTML =
    '<div class="md-dim" style="margin-bottom:8px">近 ' + d.days + ' 天共 <b>' + d.total +
    '</b> 条（' + d.start + ' ~ ' + d.end + '）。<b>以下都是计数，不是评分，不作预测依据。</b></div>' +
    '<div class="st-cols">' +
    '<div><h4>按日</h4>' + days + '</div>' +
    '<div><h4>按规则</h4>' + rules + '</div>' +
    '<div><h4>标的 Top10</h4>' + codes + '</div>' +
    '</div>';
}

// ── ② 机构研究雷达 ─────────────────────────────────────────────────

async function loadSgpjbg() {
  const repBox = $('stSgpjbgReports');
  const recBox = $('stSgpjbgRecent');
  repBox.innerHTML = '<span class="md-dim">加载中…</span>';
  const d = await _get('/api/sentiment/sgpjbg?days=7&limit=50');

  if (!d.success) {
    repBox.innerHTML = '<span class="st-down">读取失败：' + _esc(d.error) + '</span>';
    recBox.innerHTML = '';
    return;
  }

  if (d.reports.length) {
    repBox.innerHTML = '<div class="md-dim" style="margin-bottom:6px">周报（点开看正文）：</div>' +
      d.reports.map(function (r) {
        return '<button class="btn btn-xs st-weekly" data-weekly="' + _esc(r.name) + '">' +
          _esc(r.date) + '</button>';
      }).join(' ');
  } else {
    repBox.innerHTML = '<div class="md-note">还没有周报。' +
      '周报每周日 18:30 生成；若调度器停机则不会产出。</div>';
  }

  if (!d.recent.length) {
    recBox.innerHTML = '<div class="md-note">近 7 天无入库报告。</div>';
  } else {
    recBox.innerHTML = '<div class="st-table">' + d.recent.map(function (r) {
      return '<div class="st-row st-row-sg">' +
        '<span class="st-time">' + _esc(r.pub_date) + '</span>' +
        '<span class="st-who"><a href="' + _esc(r.url) + '" target="_blank" rel="noopener">' +
        _esc(r.title) + '</a></span>' +
        '<span class="st-str">' + _esc(r.org || '机构未标注') + '</span>' +
        '<span class="md-dim">' + _esc(r.category) + ' · ' + r.pages + '页 · 热度 ' + r.heat + '</span>' +
        '</div>';
    }).join('') + '</div>';
  }
}

async function showWeekly(name) {
  const body = $('stSgpjbgBody');
  body.innerHTML = '<span class="md-dim">加载中…</span>';
  const d = await _get('/api/sentiment/sgpjbg?name=' + encodeURIComponent(name));
  if (!d.success) {
    body.innerHTML = '<span class="st-down">读取失败：' + _esc(d.error) + '</span>';
    return;
  }
  _renderMd(body, d.markdown);
  body.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ── 交互接线 ───────────────────────────────────────────────────────

function _wire() {
  const dateEl = $('stDate');
  if (dateEl) {
    dateEl.value = state.date;
    dateEl.addEventListener('change', function () {
      state.mode = 'day';
      state.date = dateEl.value || _iso(new Date());
      loadAlerts();
    });
  }
  document.querySelectorAll('[data-st-range]').forEach(function (b) {
    b.addEventListener('click', function () {
      const k = b.getAttribute('data-st-range');
      const today = new Date();
      if (k === 'today') {
        state.mode = 'day'; state.date = _iso(today);
      } else if (k === 'yesterday') {
        const y = new Date(today.getTime() - 86400000);
        state.mode = 'day'; state.date = _iso(y);
      } else {
        const n = parseInt(k, 10);
        const s = new Date(today.getTime() - (n - 1) * 86400000);
        state.mode = 'range'; state.start = _iso(s); state.end = _iso(today);
      }
      if (dateEl && state.mode === 'day') dateEl.value = state.date;
      loadAlerts();
    });
  });
  const ruleEl = $('stRule');
  if (ruleEl) ruleEl.addEventListener('change', function () { state.rule = ruleEl.value; loadAlerts(); });
  const codeEl = $('stCode');
  if (codeEl) codeEl.addEventListener('change', function () { state.code = codeEl.value; loadAlerts(); });
  const qBtn = $('stQueryBtn');
  if (qBtn) qBtn.addEventListener('click', function () {
    state.rule = ruleEl ? ruleEl.value : '';
    state.code = codeEl ? codeEl.value : '';
    loadAlerts();
  });
  const rBtn = $('stRefreshBtn');
  if (rBtn) rBtn.addEventListener('click', function () { sentimentPageEnter(); });
  const repBox = $('stSgpjbgReports');
  if (repBox) repBox.addEventListener('click', function (ev) {
    const b = ev.target.closest('[data-weekly]');
    if (b) showWeekly(b.getAttribute('data-weekly'));
  });
}

let _wired = false;

function sentimentPageEnter() {
  if (!_wired) { _wire(); _wired = true; }
  // 补一块 30 天分布容器（HTML 里没写死，按需建 —— 避免多一处可能失配的 DOM）
  if ($('stSummary') && !$('stSummaryBox')) {
    const box = document.createElement('div');
    box.id = 'stSummaryBox';
    box.style.marginTop = '10px';
    $('stSummary').parentNode.insertBefore(box, $('stSummary').nextSibling);
  }
  loadAlerts();
  loadSummary();
  loadSgpjbg();
}

function sentimentPageLeave() { /* 无轮询，无需收尾 */ }

window.sentimentPageEnter = sentimentPageEnter;
window.sentimentPageLeave = sentimentPageLeave;
