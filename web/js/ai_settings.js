// ====== VERA AI 设置 TAB (2026-09-06) — 对话大脑三档接入配置 ======
// 零 import 依赖 (data_cache.js 同款 seam 模式): fetch 由浏览器提供,
// Node 测试通过 module.exports 拿纯函数。
// 生命周期钩子由 vera-ui.js switchTab 调用: window.aiPageEnter (切入时加载)。
// 后端 ai_api.py; 配置存 config/ai.json (不入 git, Key 只回 mask)。

(function () {

// ── 纯函数 (Node 可测) ─────────────────────────────────

// 后端视图 → 各输入框回填值 (Key 用 placeholder 提示"已设置", 不回填明文)
function fillValuesFromView(view) {
  var v = view || {};
  var f = v.fast || {}, s = v.standard || {}, d = v.deep || {};
  return {
    fastBase: f.base_url || '',
    fastModel: f.model || '',
    fastKeyHint: f.key_set ? ('已设置: ' + (f.key_mask || '')) : '未设置',
    stdBase: s.base_url || '',
    stdModel: s.model || '',
    stdKeyHint: s.key_set ? ('已设置: ' + (s.key_mask || '')) : '未设置',
    deepProvider: d.provider || '',
    deepModel: d.model || '',
  };
}

// 收集表单 → save body (Key 留空 = 不修改, 由后端合并)
function collectSaveBody(getVal) {
  var body = {
    fast: { base_url: getVal('aiFastBase'), api_key: getVal('aiFastKey'),
            model: getVal('aiFastModel') },
    standard: { base_url: getVal('aiStdBase'), api_key: getVal('aiStdKey'),
                model: getVal('aiStdModel') },
    deep: { provider: getVal('aiDeepProvider'), model: getVal('aiDeepModel') },
  };
  return body;
}

// ── 浏览器侧 (DOM + fetch) ─────────────────────────────

function $(id) { return document.getElementById(id); }

function loadConfig() {
  // 切入页签时读取当前配置回填 (Key 只给 placeholder 提示)
  return fetch('/api/ai/config').then(function (r) { return r.json(); })
    .then(function (d) {
      var hint = $('aiHint');
      if (!d.success) {
        if (hint) hint.textContent = '读取失败: ' + (d.error || '');
        return;
      }
      var vals = fillValuesFromView(d.config);
      var setV = function (id, val) { var el = $(id); if (el) el.value = val; };
      setV('aiFastBase', vals.fastBase); setV('aiFastModel', vals.fastModel);
      setV('aiStdBase', vals.stdBase); setV('aiStdModel', vals.stdModel);
      setV('aiDeepProvider', vals.deepProvider); setV('aiDeepModel', vals.deepModel);
      var kf = $('aiFastKey'); if (kf) kf.placeholder = vals.fastKeyHint || 'API Key (留空不修改)';
      var ks = $('aiStdKey'); if (ks) ks.placeholder = vals.stdKeyHint || 'Auth Token / API Key (留空不修改)';
      if (hint) hint.textContent = '已加载 ' + new Date().toLocaleTimeString('zh-CN', { hour12: false });
    }).catch(function () { var hint = $('aiHint'); if (hint) hint.textContent = '读取失败: 服务不可达'; });
}

function collectBody() {
  var el = function (id) { var e = $(id); return e ? e.value.trim() : ''; };
  return collectSaveBody(el);
}

function saveAll() {
  var hint = $('aiSaveHint'), btn = $('aiSaveBtn');
  if (btn) { btn.disabled = true; btn.innerHTML = '保存中…'; }
  var body = collectBody();
  return fetch('/api/ai/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(function (r) { return r.json(); })
    .then(function (d) {
      if (hint) {
        hint.textContent = d.success ? '✓ 已保存 ' + new Date().toLocaleTimeString('zh-CN', { hour12: false })
          : '保存失败: ' + (d.error || '');
      }
      if (d.success) loadConfig();  // 刷新 placeholder (新 mask)
    }).catch(function (e) { if (hint) hint.textContent = '保存异常: ' + e; })
    .finally(function () { if (btn) { btn.disabled = false; btn.innerHTML = '保存全部'; } });
}

function testWhich(which, stateId) {
  var baseId = which === 'fast' ? 'aiFast' : 'aiStd';
  var body = {
    which: which,
    base_url: ($(baseId + 'Base') ? $(baseId + 'Base').value.trim() : ''),
    api_key: ($(baseId + 'Key') ? $(baseId + 'Key').value.trim() : ''),
    model: ($(baseId + 'Model') ? $(baseId + 'Model').value.trim() : ''),
  };
  var state = $(stateId), btn = $(which === 'fast' ? 'aiTestFast' : 'aiTestStd');
  if (btn) { btn.disabled = true; btn.innerHTML = '测试中…'; }
  if (state) state.textContent = '测试中…';
  return fetch('/api/ai/test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(function (r) { return r.json(); })
    .then(function (d) {
      if (state) {
        state.textContent = d.success
          ? '✓ ' + (d.message || '连接成功')
          : '✗ ' + (d.message || d.error || '连接失败');
      }
    }).catch(function (e) { if (state) state.textContent = '✗ 测试异常: ' + e; })
    .finally(function () { if (btn) { btn.disabled = false; btn.innerHTML = '测试连接'; } });
}

// 构造「清空某档」的 save body (纯函数可 node 测): 只提交该档 __clear__
function clearBodyFor(which) {
  var body = {};
  body[which] = { '__clear__': true };
  return body;
}

function clearSection(which, stateId) {
  var names = { fast: '快速档', standard: '标准档', deep: '深度档' };
  if (!confirm('确定清空' + (names[which] || which) + '配置?\n将回落默认 (快速档回 .env DeepSeek, 标准档回 ~/.claude, 深度档回 DSH 现状)。')) return;
  var state = $(stateId);
  var body = clearBodyFor(which);   // 单档清空, 其他档不提交 → 后端保留
  if (state) state.textContent = '清空中…';
  return fetch('/api/ai/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(function (r) { return r.json(); })
    .then(function (d) {
      if (!d.success) { if (state) state.textContent = '✗ 清空失败: ' + (d.error || ''); return; }
      // 清空成功 → 清掉本档输入框并刷新 placeholder
      var prefix = which === 'fast' ? 'aiFast' : which === 'standard' ? 'aiStd' : 'aiDeep';
      [prefix + 'Base', prefix + 'Key', prefix + 'Model', prefix + 'Provider']
        .forEach(function (id) { var el = $(id); if (el) el.value = ''; });
      if (state) state.textContent = '✓ 已清空 ' + new Date().toLocaleTimeString('zh-CN', { hour12: false });
      loadConfig();
    }).catch(function (e) { if (state) state.textContent = '✗ 清空异常: ' + e; });
}

function bind() {
  var sv = $('aiSaveBtn'); if (sv) sv.addEventListener('click', saveAll);
  var tf = $('aiTestFast'); if (tf) tf.addEventListener('click', function () { testWhich('fast', 'aiFastState'); });
  var ts = $('aiTestStd'); if (ts) ts.addEventListener('click', function () { testWhich('standard', 'aiStdState'); });
  var rb = $('aiRefreshBtn'); if (rb) rb.addEventListener('click', loadConfig);
  var cf = $('aiClearFast'); if (cf) cf.addEventListener('click', function () { clearSection('fast', 'aiFastState'); });
  var cs = $('aiClearStd'); if (cs) cs.addEventListener('click', function () { clearSection('standard', 'aiStdState'); });
  var cd = $('aiClearDeep'); if (cd) cd.addEventListener('click', function () { clearSection('deep', 'aiDeepState'); });
}

// 生命周期钩子 (vera-ui.js switchTab 调用; Node 测试环境无 window, 跳过)
if (typeof window !== 'undefined') {
  window.aiPageEnter = loadConfig;
}

// 按钮绑定 (Node 测试环境无 document, 跳过)
if (typeof document !== 'undefined') {
  document.addEventListener('DOMContentLoaded', bind);
}

// Node 测试出口 (data_cache.js 同款)
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { fillValuesFromView: fillValuesFromView, collectSaveBody: collectSaveBody,
                     clearBodyFor: clearBodyFor };
}

})();
