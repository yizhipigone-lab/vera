// ====== 交易记录页顶部两张卡 (2026-09-18) ======
// 卡片一「今天为什么动 / 没动」 —— 对应 GET /api/trade/decisions
// 卡片二「决策日历」           —— 对应 GET /api/trade/decisions/calendar
//
// 为什么单独一个文件: 交易记录页原本 4 张卡 (成交/委托/对账/审计) 已经 1387 行,
// 再把决策台账塞进 trade.js 只会让它更难维护。独立文件的另一个好处是**回滚极简**:
// 出问题删掉 index.html 里那一行 <script> 就回到今天的样子。
//
// 这个文件是 ES module (与 analysis.js 同款), 纯函数在 decision_util.mjs 里 ——
// 那部分可以在 node 下直接单测。
//
// 接线方式 (与"改 trade.js"相比更解耦): 本模块**包装**已有的
// window.recordsPageEnter / recordsPageLeave, 而不是让 trade.js 反过来调用我。
// 这样即使本文件加载失败, 原有四张卡照常工作。

import {
  actionBadge, calCellLabel, describeTradeError, dominantTone, evidenceRows,
  fetchJson, parseIso, SOURCE_LABELS, statusLine, TONE_VAR, tradeApiBase,
} from './decision_util.mjs';

const WEEK_HEADS = ['一', '二', '三', '四', '五', '六', '日'];

/** 状态: 日历当前看的月份 ('' = 缺省当月, 由后端决定)。 */
let _calMonth = '';

function esc(s) {
  return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}

// 交易 API 在 8081, 页面在 8080 —— 地址是**算出来的**, 不是猜出来的。
// (2026-09-19 修: 原来想复用 trade.js 的 get(), 但它是 IIFE 里的局部函数、没挂到
//  window 上, 于是每次都静默退化成同源 fetch 打到 8080 → 404 → 误报"8081 不可达"。)
const API_BASE = tradeApiBase(location.hostname);

function apiGet(path) {
  return fetchJson(fetch, API_BASE, path);
}

function el(id) {
  return document.getElementById(id);
}

// ── 卡片一: 今日决策 ────────────────────────────────────────────

function loadDecisions() {
  const input = el('decDate');
  const hint = el('decHint');
  if (!input || !hint) return;
  const v = input.value.trim();
  if (v && !/^\d{8}$/.test(v)) {
    hint.textContent = '日期格式应为 YYYYMMDD（8 位数字）';
    return;
  }
  hint.textContent = '';
  const btn = el('decBtn');
  if (btn) { btn.disabled = true; btn.textContent = '查询…'; }
  apiGet('/api/trade/decisions' + (v ? '?date=' + v : ''))
    .then(renderDecisions)
    .catch(function (err) {
      hint.textContent = '查询失败: ' + describeTradeError(err);
      if (el('decStatus')) el('decStatus').style.display = 'none';
      if (el('decBody')) el('decBody').innerHTML = '';
    })
    .finally(function () {
      if (btn) { btn.disabled = false; btn.textContent = '查询'; }
    });
}

function renderDecisions(data) {
  if (!data) return;
  const statusBox = el('decStatus');
  const body = el('decBody');
  if (!statusBox || !body) return;

  const st = statusLine(data) || { tone: 'muted', text: '' };
  const s = data.summary || {};
  const counts = '买入 ' + (s.buy || 0) + ' · 卖出 ' + (s.sell || 0)
    + ' · 没动 ' + (s.hold || 0) + ' · 没做成 ' + (s.fail || 0)
    + ' · 告知 ' + (s.info || 0);
  statusBox.style.display = '';
  statusBox.innerHTML =
    '<div style="font-size:var(--fs-sm);color:' + TONE_VAR[st.tone] + ';'
    + (st.tone === 'ok' ? 'font-weight:600' : '') + '">' + esc(st.text) + '</div>'
    + '<div style="font-size:var(--fs-xs);color:var(--text2);margin-top:var(--sp-1)">'
    + esc(data.date) + ' · ' + esc(counts)
    + (data.note ? ' · ' + esc(data.note) : '') + '</div>';

  const groups = data.groups || [];
  if (!groups.length) {
    body.innerHTML = '<div style="color:var(--text2);font-size:var(--fs-sm)">'
      + '这一天没有可显示的决策记录</div>';
    return;
  }
  let html = '';
  groups.forEach(function (g) {
    html += '<div class="dec-group"><div class="dec-group-h">' + esc(g.label) + '</div>';
    if (!g.rows || !g.rows.length) {
      // 空组不消失: 说明"这条策略今天没有留痕", 而且措辞保持中性 ——
      // 当时那条策略可能压根还没启用, 指名道姓说它"没跑"就是冤枉它
      html += '<div class="dec-empty">这条策略今天没有留痕'
        + '（可能程序提前退出，也可能当时还没启用它）</div>';
    } else {
      g.rows.forEach(function (r, i) {
        html += rowHtml(r, g.strategy + '-' + i);
      });
    }
    html += '</div>';
  });
  body.innerHTML = html;

  // 点一行展开"凭什么"的数字
  body.querySelectorAll('.dec-row').forEach(function (node) {
    node.addEventListener('click', function () {
      const ev = document.getElementById('dec-ev-' + node.dataset.k);
      if (ev) ev.hidden = !ev.hidden;
    });
  });
}

function rowHtml(r, key) {
  const badge = actionBadge(r.action);
  const src = SOURCE_LABELS[r.source] || '';
  const evs = evidenceRows(r.evidence);
  let evHtml = '';
  if (evs.length) {
    evHtml = '<div class="dec-ev" id="dec-ev-' + esc(key) + '" hidden>'
      + '<table class="td-table">'
      + evs.map(function (e) {
        return '<tr><td style="width:180px;color:var(--text2)">' + esc(e.label)
          + '</td><td class="dec-evv" style="text-align:left">' + esc(e.value)
          + '</td></tr>';
      }).join('')
      + '</table></div>';
  }
  return '<div class="dec-row" data-k="' + esc(key) + '" role="button" tabindex="0">'
    + '<span class="dec-badge" style="color:' + TONE_VAR[badge.tone]
    + ';border-color:' + TONE_VAR[badge.tone] + '">' + esc(badge.text) + '</span>'
    + '<span class="dec-subj">' + esc(r.subject) + '</span>'
    + '<span class="dec-txt">' + esc(r.reason_label) + '：' + esc(r.reason_text) + '</span>'
    + (src ? '<span class="dec-src" title="这条不是当场记录的，是后来翻出来的">'
        + esc(src) + '</span>' : '')
    + (evs.length ? '<span class="dec-more">明细 ▾</span>' : '')
    + '</div>' + evHtml;
}

// ── 卡片二: 决策日历 ────────────────────────────────────────────

function currentMonth() {
  const d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0');
}

function shiftMonth(delta) {
  const base = _calMonth || currentMonth();
  const p = base.split('-');
  const d = new Date(Number(p[0]), Number(p[1]) - 1 + delta, 1);
  _calMonth = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0');
  loadCalendar();
}

function loadCalendar() {
  const box = el('decCal');
  const hint = el('decCalHint');
  if (!box) return;
  apiGet('/api/trade/decisions/calendar' + (_calMonth ? '?month=' + _calMonth.replace('-', '') : ''))
    .then(function (data) {
      if (hint) hint.textContent = '';
      renderCalendar(data);
    })
    .catch(function (err) {
      if (hint) hint.textContent = '查询失败: ' + describeTradeError(err);
    });
}

function renderCalendar(data) {
  const box = el('decCal');
  const monthLabel = el('decCalMonth');
  if (!box || !data) return;

  // 月份字段畸形时直接说"数据不对", 不要硬画 —— 硬画的后果是一屏写着 "NaN"
  // 的格子 (parseIso 现在会返回 null 拦住这种情况, 见 decision_util.mjs)
  const p = parseIso(data.month + '-01');
  if (!p) {
    if (el('decCalHint')) {
      el('decCalHint').textContent = '月份数据不对：' + String(data.month);
    }
    box.innerHTML = '';
    return;
  }
  _calMonth = data.month || _calMonth;
  if (monthLabel) monthLabel.textContent = data.month || '';

  const daysInMonth = new Date(p.year, p.month, 0).getDate();
  const lead = (new Date(p.year, p.month - 1, 1).getDay() + 6) % 7;  // 周一为第一列
  const byDay = {};
  (data.days || []).forEach(function (d) { byDay[d.date] = d; });

  let html = '<table class="dec-cal"><tr>'
    + WEEK_HEADS.map(function (w) { return '<th>' + w + '</th>'; }).join('')
    + '</tr>';
  let cell = 0;
  for (let row = 0; row < 6; row++) {
    html += '<tr>';
    for (let col = 0; col < 7; col++) {
      cell++;
      const dayNum = cell - lead;
      if (dayNum < 1 || dayNum > daysInMonth) { html += '<td class="dec-blank"></td>'; continue; }
      const iso = data.month + '-' + String(dayNum).padStart(2, '0');
      const day = byDay[iso];
      const txt = calCellLabel(day);
      const tone = day && day.rows ? dominantTone(day.tones) : '';
      const bg = (tone && tone !== 'muted')
        ? 'background:color-mix(in srgb, ' + TONE_VAR[tone] + ' 14%, transparent);'
        : '';
      const cls = (day && !day.trading_day) ? 'dec-day dec-holiday'
        : (txt === '没运行' || (day && day.inferred && !day.rows)) ? 'dec-day dec-norun'
          : 'dec-day';
      html += '<td class="' + cls + '" style="' + bg + '" data-date="' + iso + '"'
        + (day && day.rows ? ' role="button" tabindex="0"' : '')
        + '><div class="dec-day-num">' + dayNum + '</div>'
        + '<div class="dec-day-txt">' + esc(txt) + '</div></td>';
    }
    html += '</tr>';
  }
  html += '</table>';
  box.innerHTML = html;

  box.querySelectorAll('.dec-day[data-date]').forEach(function (node) {
    node.addEventListener('click', function () { gotoDay(node.dataset.date); });
  });
}

/** 点日历格 → 卡片一切到那天 (输入框也同步, 用户能看到自己在看哪一天)。 */
function gotoDay(iso) {
  const input = el('decDate');
  if (!input) return;
  input.value = String(iso || '').replace(/-/g, '');
  loadDecisions();
  if (el('decTodayCard') && el('decTodayCard').scrollIntoView) {
    el('decTodayCard').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

// ── 页面生命周期钩子 (包装, 不侵入 trade.js) ────────────────────

function enterDecisions() {
  loadDecisions();
  loadCalendar();
}

function leaveDecisions() { /* 无轮询可停, 钩子对齐页面生命周期 */ }

function hookPage() {
  const prevEnter = window.recordsPageEnter;
  window.recordsPageEnter = function () {
    if (typeof prevEnter === 'function') prevEnter();
    enterDecisions();
  };
  const prevLeave = window.recordsPageLeave;
  window.recordsPageLeave = function () {
    if (typeof prevLeave === 'function') prevLeave();
    leaveDecisions();
  };
  const btn = el('decBtn');
  if (btn) btn.addEventListener('click', loadDecisions);
  const input = el('decDate');
  if (input) {
    input.addEventListener('keydown', function (e) { if (e.key === 'Enter') loadDecisions(); });
  }
  const prev = el('decPrevBtn');
  const next = el('decNextBtn');
  if (prev) prev.addEventListener('click', function () { shiftMonth(-1); });
  if (next) next.addEventListener('click', function () { shiftMonth(1); });
}

// 本文件是 module (解析完才执行), 执行时 trade.js 已经跑完并定义好钩子,
// 所以这里包装一定能包上。若万一还在解析中, 退回 DOMContentLoaded。
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', hookPage);
} else {
  hookPage();
}
