// ====== VERA 数据准备 TAB (2026-08-14) — K线缓存管理台 ======
// 零 import 依赖（recover.js 同款 seam 模式）：fetch 由浏览器提供，
// Node 测试通过 module.exports 拿纯函数。
// 生命周期钩子由 vera-ui.js switchTab 调用: window.dataPageEnter / dataPageLeave。

(function () {

// ── 纯函数（Node 可测）─────────────────────────────────

// 状态徽章: 补拉中 > 过期 > 新鲜
function statusBadge(period, refreshing) {
  if (refreshing) return { text: '补拉中…', cls: 'busy' };
  if (period.stale) return { text: '过期', cls: 'err' };
  return { text: '新鲜', cls: 'ok' };
}

// 一行状态 → 表格行 HTML（esc 调用方传入或内置简易版）
function statusRowHtml(p, refreshing, esc) {
  esc = esc || function (s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  };
  var b = statusBadge(p, refreshing);
  // W4-1: --green/--red 在 CSS 中从未定义、恒走 fallback — 归并到语义令牌。
  // 2026-09-19 UIUX: 健康度不再占用涨跌红绿 (红=涨/绿=跌是方向色) ——
  // 新鲜=--ok-text 青绿(成功语义), 过期=--danger-text(错误语义), 文字变体保浅色对比度
  var color = b.cls === 'ok' ? 'var(--ok-text)'
            : b.cls === 'err' ? 'var(--danger-text)' : 'var(--link)';
  return '<tr><td>' + esc(p.period) + '</td>'
    + '<td>' + esc(p.stocks) + '</td>'
    + '<td>' + esc(p.first_date || '—') + '</td>'
    + '<td>' + esc(p.last_date || '—') + '</td>'
    + '<td>' + esc(p.expected || '—') + '</td>'
    + '<td style="color:' + color + '">' + b.text + '</td>'
    + '<td>' + esc(p.not_intact || 0) + '</td></tr>';
}

// ── 浏览器侧（DOM + fetch）─────────────────────────────

var pollTimer = null;

function $(id) { return document.getElementById(id); }

function refreshStatus() {
  // 返回 promise（供按钮反馈链接），失败静默（页面有轮询兜底）
  return fetch('/api/data_cache/status').then(function (r) { return r.json(); })
    .then(function (d) {
      if (!d.success) { return; }
      var body = $('dcStatusBody');
      if (body) {
        body.innerHTML = d.periods.map(function (p) {
          return statusRowHtml(p, d.refreshing);
        }).join('');
      }
      var hint = $('dcRunningHint');
      if (hint) hint.style.display = d.refreshing ? '' : 'none';
    }).catch(function () {});
}

function refreshLog() {
  return fetch('/api/data_cache/log?tail=60').then(function (r) { return r.json(); })
    .then(function (d) {
      var el = $('dcLog');
      if (!el) return;
      var lines = (d && d.lines) || [];
      el.textContent = lines.length ? lines.join('\n') : '暂无日志';
      el.scrollTop = el.scrollHeight;   // 滚到底看最新进度
    }).catch(function () {});
}

// 2026-08-14: 按钮点击反馈 — 之前点"刷新"重渲染相同内容、零视觉反馈，
// 用户无法感知是否生效。点击后按钮变"刷新中…"，完成后 hint 显示时间戳。
function clickRefresh(btnId, hintId, fn) {
  var btn = $(btnId), hint = $(hintId);
  if (btn) { btn.disabled = true; btn.innerHTML = '刷新中…'; }
  Promise.resolve(fn()).then(function () {
    if (hint) hint.textContent = '已更新 ' + new Date().toLocaleTimeString('zh-CN', { hour12: false });
  }).finally(function () {
    if (btn) { btn.disabled = false; btn.innerHTML = '刷新'; }
  });
}

function submitBackfill() {
  var msg = $('dcBackfillMsg');
  var body = {
    period: $('dcPeriod').value,
    start: $('dcStart').value.trim(),
    end: $('dcEnd').value.trim(),
    universe: $('dcUniverse').value,
    limit: parseInt($('dcLimit').value, 10) || 0,
  };
  fetch('/api/data_cache/backfill', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(function (r) { return r.json(); })
    .then(function (d) {
      if (!msg) return;
      if (d.success) {
        msg.style.color = 'var(--ok-text)';
        msg.textContent = '✓ 补拉已启动（' + (d.periods || []).join(',') + '），见下方日志';
        refreshStatus(); refreshLog();
      } else {
        msg.style.color = 'var(--danger-text)';
        msg.textContent = '✗ ' + (d.error || d.reason || '启动失败');
      }
    }).catch(function (e) {
      if (msg) { msg.style.color = 'var(--danger-text)'; msg.textContent = '✗ 网络错误: ' + e.message; }
    });
}

function startPoll() {
  stopPoll();
  pollTimer = setInterval(function () { refreshStatus(); refreshLog(); }, 3000);
}
function stopPoll() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

// 生命周期钩子（vera-ui.js switchTab 调用；Node 测试环境无 window，跳过）
if (typeof window !== 'undefined') {
  window.dataPageEnter = function () { refreshStatus(); refreshLog(); startPoll(); };
  window.dataPageLeave = function () { stopPoll(); };
}

// 按钮绑定（Node 测试环境无 document，跳过）
if (typeof document !== 'undefined') {
  document.addEventListener('DOMContentLoaded', function () {
    var btn = $('dcBackfillBtn'); if (btn) btn.addEventListener('click', submitBackfill);
    var rb = $('dcRefreshBtn'); if (rb) rb.addEventListener('click', function () {
      clickRefresh('dcRefreshBtn', 'dcRefreshHint', refreshStatus);
    });
    var lb = $('dcLogBtn'); if (lb) lb.addEventListener('click', function () {
      clickRefresh('dcLogBtn', 'dcLogHint', refreshLog);
    });
  });
}

// Node 测试: CommonJS 导出纯函数
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { statusBadge, statusRowHtml };
}

})();
