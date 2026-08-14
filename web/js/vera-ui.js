// ====== VERA App Shell ======
// ES module entry — imports API, config, charts modules; orchestrates app logic.
import { fetchStatus, submitBacktest, stopBacktest, fetchLastResult, fetchResults, fetchResult, fetchConfigDefaults, saveConfig, fetchSavedConfig, deleteSavedConfig, fetchSectors as apiFetchSectors, fetchFactorRules as apiFetchFactorRules, submitLabJob, stopLabJob, fetchLabStatus, fetchLabHistory, fetchLabReport } from './api.js';
import { STORAGE_KEY, CONFIG_IDS, RADIO_CONFIGS, cleanNum, validateDate, validatePositive, validateNonNeg, validateLadder, loadConfig, saveAllConfig, collectConfigFromForm as cfgCollect, applyConfigDict as cfgApply, toggleEdit as cfgToggleEdit, cancelEdit as cfgCancelEdit, saveBlock as cfgSaveBlock, refreshAllSummaries as cfgRefreshSummaries } from './config.js';
import { esc, escAttr, hexToRgba, getTheme, getColors, toggleTheme, toggleSidebar, showToast, addLog, checkEngineVersion, setChartsRef, echartsInit, tweenNumber, sparkline, fillHeroSub, revealResults, fmtReasonShort, renderTradeTable, filterTrades as chartFilterTrades, renderAllCharts, sunIcon, moonIcon } from './charts.js';
import { renderDeepCharts } from './charts_deep.js?v=20260814';

// ═══════════════════════════════════════════
// Global State
// ═══════════════════════════════════════════

let lastResult = null;
let charts = {};
let allTrades = [];

const runState = { running: false, userStopped: false, controller: null };

let _savedFileExists = false;
let resizeTimer;

// Wire recover.js deps + charts module
const R = window.VeraRecover;
R.deps.addLog = addLog;
R.deps.showToast = showToast;
R.deps.renderAllCharts = renderAllCharts;
R.deps.checkEngineVersion = checkEngineVersion;
R.deps.fetchStatus = fetchStatus;
R.deps.fetchLastResult = fetchLastResult;
R.deps.fetchResults = fetchResults;
R.deps.lastResultRef = { get value() { return lastResult; }, set value(v) { lastResult = v; } };
R.deps.allTradesRef = { get value() { return allTrades; }, set value(v) { allTrades = v; } };
const { tryRecoverAbortedResult, resetRunUI, setBtnStopMode, setBtnRunMode } = R;

setChartsRef(charts);
toggleTheme._onToggle = () => { if (lastResult) renderAllCharts(lastResult); if (lastResult) renderDeepCharts(lastResult); };
toggleSidebar._onToggle = () => { setTimeout(() => Object.values(charts).forEach(c => c.resize()), 300); };

// ═══════════════════════════════════════════
// Config bridge — thin wrappers around config.js
// ═══════════════════════════════════════════

function toggleSavedButtons(exists) {
  const load = document.getElementById('btnLoadFile'), del = document.getElementById('btnDeleteFile');
  if (load) load.disabled = !exists; if (del) del.disabled = !exists;
}

function collectConfigFromForm() { return cfgCollect(_selectedSectors, collectFactorFilter, esc); }
function applyConfigDict(cfg) { cfgApply(cfg, renderSectors, updateSectorSummary, toggleUniverseDropdown, applyFactorFilterDict, _allSectors, function(v) { _selectedSectors = v; }); }
function refreshAllSummaries() { cfgRefreshSummaries(esc); }
function toggleEdit(blockId) { cfgToggleEdit(blockId, saveAllConfig, refreshAllSummaries); }
function cancelEdit(blockId) { cfgCancelEdit(blockId); }
function saveBlock(blockId) { cfgSaveBlock(blockId, saveAllConfig, refreshAllSummaries, addLog); }
function filterTrades() { chartFilterTrades(allTrades, renderTradeTable); }

// ═══════════════════════════════════════════
// Config File Ops
// ═══════════════════════════════════════════

async function saveConfigToFile() {
  if (_savedFileExists && !confirm('已存在保存的配置（config/current.yaml），确定覆盖？')) return;
  const config = collectConfigFromForm();
  try {
    const res = await saveConfig(config);
    if (res.success) { _savedFileExists = true; toggleSavedButtons(true); saveAllConfig();
      showToast('配置已保存到 config/current.yaml' + (res.warnings && res.warnings.length ? '（'+res.warnings.length+'条告警）' : ''), 'ok');
      addLog('配置已保存到 config/current.yaml', 'ok'); }
    else { showToast('保存失败: ' + (res.error || '未知错误'), 'error'); }
  } catch(e) { showToast('保存失败（网络）: ' + e.message, 'error'); }
}

async function loadConfigFromFile() {
  try {
    const res = await fetchSavedConfig();
    if (!res.success || !res.exists) { showToast(res.error || '暂无保存的配置', 'info'); return; }
    applyConfigDict(res.config); saveAllConfig(); refreshAllSummaries();
    showToast('已从 config/current.yaml 加载配置', 'ok'); addLog('已从 config/current.yaml 加载配置', 'ok');
  } catch(e) { showToast('加载失败（网络）: ' + e.message, 'error'); }
}

async function deleteSavedConfigFile() {
  if (!confirm('确定删除已保存的配置文件（config/current.yaml）？\n（当前表单值不会被清空，如需重置请点"恢复默认配置"）')) return;
  try {
    const res = await deleteSavedConfig();
    if (res.success) { _savedFileExists = false; toggleSavedButtons(false);
      showToast('已删除 config/current.yaml（表单值未变）', 'ok'); addLog('已删除 config/current.yaml', 'info'); }
    else { showToast('删除失败: ' + (res.error || '未知错误'), 'error'); }
  } catch(e) { showToast('删除失败（网络）: ' + e.message, 'error'); }
}

async function resetDefaults() {
  if (!confirm('确定恢复所有配置为默认值？')) return;
  try {
    const result = await fetchConfigDefaults();
    if (!result.success) { showToast('获取默认配置失败', 'error'); return; }
    applyConfigDict(result.config || {}); localStorage.removeItem(STORAGE_KEY);
    try { localStorage.removeItem('vera_selected_sectors'); } catch(e) {}
    refreshAllSummaries(); showToast('已恢复默认配置', 'ok');
  } catch(e) { showToast('恢复默认失败: ' + e.message, 'error'); }
}

// ═══════════════════════════════════════════
// Run Pipeline
// ═══════════════════════════════════════════

async function stopPipeline() {
  if (!runState.running) return; runState.userStopped = true; addLog('正在停止回测...', 'info');
  try { await stopBacktest(); } catch (e) {}
  if (runState.controller) runState.controller.abort();
}

function runPipeline() {
  if (runState.running) { stopPipeline(); return; }
  const startEl = document.getElementById('cfgStart'), endEl = document.getElementById('cfgEnd');
  validateDate(startEl); validateDate(endEl);
  if (startEl.classList.contains('invalid') || endEl.classList.contains('invalid')) { showToast('日期格式错误，应为 YYYYMMDD', 'error'); return; }
  if (startEl.value >= endEl.value) { showToast('起始日期必须早于结束日期', 'error'); return; }
  const ladderEl = document.getElementById('cfgLadderVal'); validateLadder(ladderEl);
  if (ladderEl.classList.contains('invalid')) { showToast('阶梯止盈格式错误', 'error'); return; }

  saveAllConfig();
  const btn = document.getElementById('btnRun'); runState.running = true; runState.userStopped = false; setBtnStopMode(btn);
  document.getElementById('progressBar').style.display = 'block';
  document.getElementById('statusDot').className = 'status-dot busy'; document.getElementById('statusText').textContent = '运行中';
  document.getElementById('progressText').textContent = ''; addLog('开始执行回测管线...', 'info');

  const config = collectConfigFromForm();
  addLog('配置: '+config.formula_name+' '+config.start_time+'~'+config.end_time, 'info');

  // 2026-07-26: 细粒度进度渲染 — 真实刻度 + 缓动逼近 (不虚构前进),
  // detail (批次/取数计数) + 已用时间 + 速率法 ETA 文字; 日志按内容去重防刷屏。
  const runT0 = Date.now();
  const _fmtDur = e => e >= 90 ? Math.floor(e/60)+'分'+String(Math.round(e%60)).padStart(2,'0')+'秒' : Math.round(e)+'s';
  let dispPct = 0, lastLogLine = '';
  let pollActive = true;
  let poll = setInterval(async () => { if (!pollActive) return;
    try { const s = await fetchStatus(); if (!pollActive) return;
      const target = s.progress || 0;
      if (target < dispPct) dispPct = target;                       // 新轮重置
      dispPct += (target - dispPct) * 0.4;                          // 缓动逼近
      if (Math.abs(target - dispPct) < 0.4) dispPct = target;
      document.getElementById('progressFill').style.width = dispPct.toFixed(1)+'%';
      const elapsed = (Date.now() - runT0) / 1000;
      let txt = s.step || '';
      if (s.detail) txt += ' · ' + s.detail;
      txt += ' · 已用 ' + _fmtDur(elapsed);
      if (s.eta_s >= 2) { const e = Math.round(s.eta_s);
        txt += ' · 预计剩余 ~' + (e >= 90 ? Math.floor(e/60)+'分'+String(e%60).padStart(2,'0')+'秒' : e+'s'); }
      document.getElementById('progressText').textContent = txt;
      document.getElementById('statusText').textContent = s.step;
      if (txt && txt !== lastLogLine) { addLog(txt + ' (' + target.toFixed(0) + '%)', 'info'); lastLogLine = txt; }
      if (!s.running && s.progress > 0 && s.has_result) { pollActive = false; clearInterval(poll); }
    } catch(e) {} }, 800);

  const controller = new AbortController(); runState.controller = controller;
  // 2026-08-14: 30 分钟 → 2 小时。5M 回测取数（594 只 × 多窗口批）实测超 30 分钟，
  // 超时 abort 后前端报错放弃但后端还在跑，用户白等（recover 探测只有 5×2 秒，
  // 探到"还在跑"也只能报超时）。拉长到 2 小时覆盖 5M 大池场景。
  const timeout = setTimeout(() => controller.abort(), 7200000);
  submitBacktest(config, controller.signal)
    .then(data => {
      clearTimeout(timeout); pollActive = false; clearInterval(poll);
      runState.running = false; runState.controller = null;
      resetRunUI(btn, '就绪', 'on');
      if (!data.success) {
        if (data.stopped || runState.userStopped) { runState.userStopped = false;
          document.getElementById('statusText').textContent = '已停止'; addLog('回测已手动停止', 'info'); showToast('回测已手动停止', 'info'); return; }
        addLog('失败: '+(data.error||'未知错误'), 'error'); showToast('回测失败: '+(data.error||'未知错误'), 'error');
        lastResult = null; document.querySelectorAll('.kpi-value').forEach(el => el.textContent = '--'); return;
      }
      addLog('回测完成: '+data.trade_count+'笔交易 (耗时 '+_fmtDur((Date.now()-runT0)/1000)+')', 'ok'); lastResult = data; allTrades = data.trades || [];
      renderAllCharts(data); renderDeepCharts(data); checkEngineVersion(data);
    }).catch(e => {
      clearTimeout(timeout); pollActive = false; clearInterval(poll); runState.running = false; runState.controller = null;
      if (runState.userStopped) { runState.userStopped = false; resetRunUI(btn, '已停止', 'on');
        addLog('回测已手动停止', 'info'); showToast('回测已手动停止', 'info'); return; }
      resetRunUI(btn, '错误', 'on'); tryRecoverAbortedResult(config, e);
    });
}

// tryRecoverAbortedResult is provided by recover.js (destructured from window.VeraRecover)

// ═══════════════════════════════════════════
// History
// ═══════════════════════════════════════════

async function loadHistory(id) {
  if (!id) return; addLog('加载历史回测: '+id, 'info');
  try { const result = await fetchResult(id); const data = result.data || result;
    if (data.success || data.trade_count != null) { lastResult = data; allTrades = data.trades || [];
      document.getElementById('tradeTableBox').style.display = '';
      document.getElementById('tradeCount').textContent = '(历史 '+data.trade_count+' 笔)';
      renderAllCharts(data); renderDeepCharts(data); checkEngineVersion(data); addLog('已加载历史回测 ('+data.trade_count+'笔)', 'ok'); }
  } catch(e) { addLog('加载失败: '+e.message, 'error'); }
}

// ═══════════════════════════════════════════
// Sectors
// ═══════════════════════════════════════════

const SECTORS_KEY = 'vera_selected_sectors';
let _allSectors = [], _selectedSectors = [];

async function loadSectors() {
  const grid = document.getElementById('sectorGrid'), empty = document.getElementById('sectorEmpty');
  try { const result = await apiFetchSectors();
    if (!result.success || !result.sectors || result.sectors.length === 0) { empty.innerHTML = '板块列表加载失败：'+(result.error||'未知错误')+'（请检查通达信客户端）<br><button class="btn btn-sm btn-secondary" id="btnRetrySectors" style="margin-top:6px">重试</button>'; empty.style.color = 'var(--up)';
      setTimeout(() => { const b = document.getElementById('btnRetrySectors'); if (b) b.addEventListener('click', loadSectors); }, 0); return; }
    _allSectors = result.sectors; } catch (e) { empty.innerHTML = '板块列表加载失败：'+e.message+'<br><button class="btn btn-sm btn-secondary" id="btnRetrySectors" style="margin-top:6px">重试</button>'; empty.style.color = 'var(--up)';
    setTimeout(() => { const b = document.getElementById('btnRetrySectors'); if (b) b.addEventListener('click', loadSectors); }, 0); return; }
  empty.style.display = 'none';
  try { _selectedSectors = JSON.parse(localStorage.getItem(SECTORS_KEY) || '[]'); } catch(e) { _selectedSectors = []; }
  renderSectors(); updateSectorSummary(); toggleUniverseDropdown();
}

function renderSectors() {
  const grid = document.getElementById('sectorGrid'), empty = document.getElementById('sectorEmpty');
  if (_allSectors.length === 0) { empty.textContent = '无板块数据'; return; } empty.style.display = 'none';
  grid.innerHTML = _allSectors.map(s => { const checked = _selectedSectors.includes(s.code);
    const codeShort = String(s.code).replace(/\.\w+$/, '');
    return '<div class="sector-item'+(checked?' checked':'')+'" data-name="'+esc(s.name)+'" data-code="'+esc(s.code)+'">'+
      '<input type="checkbox" value="'+esc(s.code)+'" '+(checked?'checked':'')+'>'+
      '<label title="'+esc(s.code)+' '+esc(s.name)+'"><span class="name">'+esc(s.name)+'</span> <span class="code">'+esc(codeShort)+'</span></label></div>'; }).join('');
  grid.querySelectorAll('input[type=checkbox]').forEach(cb => { cb.addEventListener('change', function() { onSectorToggle(this); }); });
}

function filterSectors() {
  const kw = (document.getElementById('sectorSearch').value||'').trim().toLowerCase(); let matched = 0;
  document.querySelectorAll('.sector-item').forEach(item => { const name = (item.dataset.name||'').toLowerCase(), code = (item.dataset.code||'').toLowerCase();
    const hit = !kw || name.includes(kw) || code.includes(kw); item.classList.toggle('hidden', !hit); if (hit) matched++;
    const nameEl = item.querySelector('.name'); if (nameEl) { const orig = item.dataset.name||'';
      const idx = kw && name.includes(kw) ? orig.toLowerCase().indexOf(kw) : -1;
      if (idx >= 0) { nameEl.innerHTML = esc(orig.slice(0,idx))+'<mark>'+esc(orig.slice(idx,idx+kw.length))+'</mark>'+esc(orig.slice(idx+kw.length)); }
      else { nameEl.textContent = orig; } } });
  const info = document.getElementById('sectorMatchInfo'); if (info) info.textContent = kw ? '匹配 '+matched+' / '+_allSectors.length : '';
}

function onSectorToggle(cb) { const code = cb.value;
  if (cb.checked) { if (!_selectedSectors.includes(code)) _selectedSectors.push(code); }
  else { _selectedSectors = _selectedSectors.filter(c => c !== code); }
  try { localStorage.setItem(SECTORS_KEY, JSON.stringify(_selectedSectors)); } catch(e) {}
  const item = cb.closest('.sector-item'); if (item) item.classList.toggle('checked', cb.checked); updateSectorSummary(); toggleUniverseDropdown(); }

function clearSectors() { _selectedSectors = [];
  try { localStorage.setItem(SECTORS_KEY, JSON.stringify(_selectedSectors)); } catch(e) {}
  document.querySelectorAll('#sectorGrid input[type=checkbox]').forEach(cb => { cb.checked = false;
    const item = cb.closest('.sector-item'); if (item) item.classList.remove('checked'); }); updateSectorSummary(); toggleUniverseDropdown(); }

function updateSectorSummary() { const box = document.getElementById('sectorSelected');
  if (_selectedSectors.length === 0) { box.innerHTML = ''; } else {
    box.innerHTML = _selectedSectors.map(code => { const s = _allSectors.find(x => x.code === code);
      return '<span class="sector-tag" data-code="'+escAttr(code)+'" title="点击移除">'+esc(s?s.name:code)+' <span class="x">×</span></span>'; }).join('');
    box.querySelectorAll('.sector-tag').forEach(tag => { tag.addEventListener('click', function() { removeSector(this.dataset.code); }); }); }
  updateSectorCount(); }

function removeSector(code) { _selectedSectors = _selectedSectors.filter(c => c !== code);
  try { localStorage.setItem(SECTORS_KEY, JSON.stringify(_selectedSectors)); } catch(e) {}
  const cb = document.querySelector('#sectorGrid input[value="'+code+'"]'); if (cb) { cb.checked = false;
    const item = cb.closest('.sector-item'); if (item) item.classList.remove('checked'); } updateSectorSummary(); toggleUniverseDropdown(); }

function toggleUniverseDropdown() { const sel = document.getElementById('cfgUniverse'); if (!sel) return;
  const disabled = _selectedSectors.length > 0; sel.disabled = disabled;
  sel.title = disabled ? '已选行业板块，下拉框被忽略' : ''; sel.style.opacity = disabled ? '0.5' : '1'; }

function toggleSectorPanel() { const sec = document.querySelector('.sector-section'); if (!sec) return;
  sec.classList.toggle('collapsed'); try { localStorage.setItem('vera_sector_collapsed', sec.classList.contains('collapsed')?'1':'0'); } catch(e) {} }

function updateSectorCount() { const badge = document.getElementById('sectorCountBadge'); if (!badge) return;
  const n = _selectedSectors.length; badge.textContent = n+' / '+(_allSectors.length||128); badge.classList.toggle('has', n > 0); }

// ═══════════════════════════════════════════
// Factor Filter
// ═══════════════════════════════════════════

const FACTOR_FILTER_KEY = 'vera_factor_filter';
let _factorFilter = {}; try { _factorFilter = JSON.parse(localStorage.getItem(FACTOR_FILTER_KEY)||'{}'); } catch(e) {}
let _factorRulesData = null;
function _ffFormula() { return (document.getElementById('cfgFormula').value||'').trim(); }
function _ffState() { const f = _ffFormula(); if (!_factorFilter[f]) _factorFilter[f] = { enabled: false, rules: [] }; return _factorFilter[f]; }

async function loadFactorRules() { const box = document.getElementById('factorRuleList'), formula = _ffFormula();
  if (!formula) { box.textContent = '先填公式名'; return; }
  try { _factorRulesData = await apiFetchFactorRules(formula); } catch(e) { _factorRulesData = { exists: false, rules: [] }; } renderFactorRules(); }

function renderFactorRules() { const box = document.getElementById('factorRuleList'), d = _factorRulesData||{ exists: false, rules: [] }, state = _ffState();
  document.getElementById('cfgFactorFilterEn').checked = !!state.enabled;
  if (!d.exists||!d.rules||d.rules.length===0) { box.innerHTML = '<div style="color:var(--text2)">该公式暂无体检规则 — 先跑 <code>formula_lab</code> 生成('+esc(d.hint||'')+')</div>'; return; }
  box.innerHTML = d.rules.map(rule => { const checked = state.rules.includes(rule.id)?'checked':'';
    let badgeCls, badgeText, selectable;
    if (rule.adopted) { badgeCls='ff-badge-ok'; badgeText='双窗 PASS'; selectable=true; }
    else if (rule.pending_review) { badgeCls='ff-badge-pending'; badgeText='单窗通过,待复核'; selectable=false; }
    else { badgeCls='ff-badge-fail'; badgeText='未通过'; selectable=false; }
    const disabled = selectable?'':'disabled';
    return '<div class="ff-rule-row" style="'+(selectable?'':'opacity:.5')+'">'
      +'<input type="checkbox" class="ff-rule" id="ffr_'+escAttr(rule.id.replace(/[:]/g,'_'))+'" value="'+escAttr(rule.id)+'" '+checked+' '+disabled+'>'
      +'<label class="ff-label" for="ffr_'+escAttr(rule.id.replace(/[:]/g,'_'))+'">'+esc(rule.label)+'</label>'
      +'<span class="ff-badge '+badgeCls+'">'+badgeText+'</span></div>'; }).join('');
  setTimeout(() => { box.querySelectorAll('.ff-rule').forEach(cb => { cb.addEventListener('change', saveFactorFilterState); }); }, 0); }

function saveFactorFilterState() { const state = _ffState(); state.enabled = document.getElementById('cfgFactorFilterEn').checked;
  state.rules = Array.from(document.querySelectorAll('.ff-rule:checked')).map(el => el.value);
  try { localStorage.setItem(FACTOR_FILTER_KEY, JSON.stringify(_factorFilter)); } catch(e) {} }
function collectFactorFilter() { saveFactorFilterState(); return _factorFilter; }
function applyFactorFilterDict(ff) { if (ff && typeof ff==='object') { _factorFilter = ff;
  try { localStorage.setItem(FACTOR_FILTER_KEY, JSON.stringify(_factorFilter)); } catch(e) {} renderFactorRules(); } }

let _ffLoadTimer = null;
document.getElementById('cfgFormula').addEventListener('input', () => { clearTimeout(_ffLoadTimer); _ffLoadTimer = setTimeout(loadFactorRules, 400); });
document.getElementById('cfgFormula').addEventListener('change', loadFactorRules);

// ═══════════════════════════════════════════
// Lab Page
// ═══════════════════════════════════════════

let _labPollTimer = null;
function switchTab(name) { const isLab = name==='lab', isTrade = name==='trade', isAnalysis = name==='analysis';
  // 2026-08-14: backtest 激活条件从"非lab/trade/analysis"改为显式等值 —
  // research/records/data 三个后加 TAB 会让旧判断把回测按钮也点亮 (cosmetic bug)
  document.getElementById('tabBtnBacktest').classList.toggle('active', name==='backtest');
  document.getElementById('tabBtnLab').classList.toggle('active', isLab);
  document.getElementById('tabBtnTrade').classList.toggle('active', isTrade);
  var _tabA = document.getElementById('tabBtnAnalysis'); if (_tabA) _tabA.classList.toggle('active', isAnalysis);
  document.querySelector('.app').style.display = (isLab||isTrade||isAnalysis)?'none':'';
  document.getElementById('pageLab').classList.toggle('active', isLab);
  document.getElementById('pageTrade').classList.toggle('active', isTrade);
  var _pa = document.getElementById('pageAnalysis'); if (_pa) _pa.classList.toggle('active', isAnalysis);
  if (isAnalysis && window.analysisPageEnter) window.analysisPageEnter();
  var isResearch = name==='research';
  var _tabR = document.getElementById('tabBtnResearch'); if (_tabR) _tabR.classList.toggle('active', isResearch);
  var _pr = document.getElementById('pageResearch'); if (_pr) _pr.classList.toggle('active', isResearch);
  // 2026-08-14: 数据准备 TAB (K线缓存管理台, 切入轮询/切出停止)
  var isData = name==='data';
  var _tabD = document.getElementById('tabBtnData'); if (_tabD) _tabD.classList.toggle('active', isData);
  var _pD = document.getElementById('pageData'); if (_pD) _pD.classList.toggle('active', isData);
  if (isData && window.dataPageEnter) window.dataPageEnter();
  if (!isData && window.dataPageLeave) window.dataPageLeave();
  // 2026-07-30: 交易记录 TAB (台账/查询页, 不轮询, 切入时加载)
  var isRecords = name==='records';
  var _tabRec = document.getElementById('tabBtnRecords'); if (_tabRec) _tabRec.classList.toggle('active', isRecords);
  var _pRec = document.getElementById('pageRecords'); if (_pRec) _pRec.classList.toggle('active', isRecords);
  if (isRecords && window.recordsPageEnter) window.recordsPageEnter();
  if (!isRecords && window.recordsPageLeave) window.recordsPageLeave();
  if (isLab) { refreshLabStatus(); loadLabHistory(); startLabPoll(); } else stopLabPoll();
  // 交易页轮询生命周期由 trade.js 自治 (window 钩子, 解耦两个 JS 模块)
  if (isTrade && window.tradePageEnter) window.tradePageEnter();
  if (!isTrade && window.tradePageLeave) window.tradePageLeave(); }
function startLabPoll() { stopLabPoll(); _labPollTimer = setInterval(refreshLabStatus, 2000); }
function stopLabPoll() { if (_labPollTimer) { clearInterval(_labPollTimer); _labPollTimer = null; } }

function _setLabBtn(mode) {
  // 2026-07-25: 体检运行中 → 按钮变"停止体检"; 空闲 → "开始体检"
  const btn = document.getElementById('btnLabSubmit');
  if (mode === 'stop') {
    btn.dataset.mode = 'stop';
    btn.classList.remove('btn-primary'); btn.classList.add('btn-danger');
    btn.innerHTML = '■ 停止体检';
  } else {
    btn.dataset.mode = 'run';
    btn.classList.remove('btn-danger'); btn.classList.add('btn-primary');
    btn.innerHTML = '▶ 开始体检';
  }
}

function labSubmit() { const btn = document.getElementById('btnLabSubmit');
  if (btn.dataset.mode === 'stop') {
    stopLabJob().then(d => { showToast(d.message || (d.success ? '已停止' : '停止失败'), d.success ? 'ok' : 'error');
      refreshLabStatus(); }).catch(e => showToast('停止异常: '+e, 'error'));
    return; }
  const raw = document.getElementById('labFormulas').value;
  const formulas = raw.split(/[,，]/).map(s=>s.trim()).filter(Boolean);
  if (!formulas.length) { showToast('请先填公式名', 'error'); return; } const body = { formulas };
  if (document.getElementById('labTag').value==='custom') { const s1=document.getElementById('labStart1').value.trim(), e1=document.getElementById('labEnd1').value.trim();
    if (!/^\d{8}$/.test(s1)||!/^\d{8}$/.test(e1)) { showToast('自定义短窗日期格式应为 YYYYMMDD', 'error'); return; } body.tag = s1+'_'+e1;
    const s2=document.getElementById('labStart2').value.trim(), e2=document.getElementById('labEnd2').value.trim();
    if (/^\d{8}$/.test(s2)&&/^\d{8}$/.test(e2)) body.tag2 = s2+'_'+e2; }
  else if (document.getElementById('labTag2').value==='none') { body.tag2 = ''; }
  submitLabJob(body).then(d => { if (!d.success) { showToast('提交失败: '+(d.error||''), 'error'); return; }
    showToast('已入队'+(d.queued_behind_pipeline?'(回测运行中,排队等待)':''), 'ok'); refreshLabStatus(); }).catch(e => showToast('提交异常: '+e, 'error')); }

function _labStatusBadge(t) { if (t.status==='done') return '<span class="lab-badge ok">完成</span>';
  if (t.status==='cancelled') return '<span class="lab-badge wait">已停止</span>';
  if (t.status==='failed') return '<span class="lab-badge fail">失败</span>'; if (t.status==='queued') return '<span class="lab-badge wait">排队中</span>';
  return '<span class="lab-badge run">运行中</span>'; }

function refreshLabStatus() { fetchLabStatus().then(d => { const box = document.getElementById('labQueue'), hint = document.getElementById('labHint');
    _setLabBtn(d.current ? 'stop' : 'run');
    hint.textContent = d.current ? '当前: '+d.current.formulas.join(',')+' — '+d.current.stage+'(已 '+Math.floor(d.current.elapsed_s/60)+' 分钟) · 基线股票池 '+(d.current.universe_note||'') : (d.running?'':'空闲(回测运行中提交的体检会自动排队)');
    if (!d.queue||!d.queue.length) { box.innerHTML = '<div style="color:var(--text2);font-size:12px">暂无任务</div>'; return; }
    box.innerHTML = d.queue.slice().reverse().map(t => { const mins = Math.floor(t.elapsed_s/60);
      let html = '<div class="lab-row">'+_labStatusBadge(t)+' <b>'+esc(t.formulas.join(','))+'</b><span style="color:var(--text2)">'+esc(t.stage)+(t.status!=='queued'&&mins?' · '+mins+'分钟':'')+'</span></div>';
      if (t.status==='failed'&&t.error) html += '<div class="lab-rules"><div style="color:#d05050">'+esc(t.error.slice(0,200))+'</div></div>';
      if (t.status==='done') html += '<div class="lab-rules"><div>规则已登记 — <a href="javascript:void(0)" class="lab-report-link" data-formula="'+escAttr(t.formulas[0])+'" style="color:var(--link)">查看报告</a></div></div>'; return html; }).join('');
    setTimeout(() => { box.querySelectorAll('.lab-report-link').forEach(a => { a.addEventListener('click', function(e) { e.preventDefault(); viewLabReport(this.dataset.formula); }); }); }, 0);
    if (d.running||d.queue.some(t=>t.status==='queued')) startLabPoll(); loadLabHistory(); }).catch(() => {}); }

function loadLabHistory() { fetchLabHistory().then(d => { const box = document.getElementById('labHistory');
    if (!d.items||!d.items.length) { box.innerHTML = '<div style="color:var(--text2);font-size:12px">暂无体检记录</div>'; return; }
    box.innerHTML = d.items.map(i => '<div class="lab-row"><b>'+esc(i.formula)+'</b><span style="color:var(--text2)">'+esc(i.report_date||i.generated_at||'')+'</span><span class="lab-badge '+(i.adopted?'ok':'wait')+'">'+i.rules+' 规则 / '+i.adopted+' 通过</span><a href="javascript:void(0)" class="lab-hist-link" data-formula="'+escAttr(i.formula)+'" style="color:var(--link);font-size:11px">查看</a></div>').join('');
    setTimeout(() => { box.querySelectorAll('.lab-hist-link').forEach(a => { a.addEventListener('click', function(e) { e.preventDefault(); viewLabReport(this.dataset.formula); }); }); }, 0); }).catch(() => {}); }

function viewLabReport(formula) { fetchLabReport(formula).then(d => { if (!d.success) { showToast(d.error||'无报告', 'error'); return; }
    document.getElementById('labReportCard').style.display = ''; document.getElementById('labReportTitle').textContent = '体检报告: '+d.file;
    document.getElementById('labReportBody').textContent = d.markdown; document.getElementById('labReportCard').scrollIntoView({ behavior: 'smooth' }); }); }

document.getElementById('labTag').addEventListener('change', e => { document.getElementById('labCustomDates').style.display = e.target.value==='custom'?'flex':'none'; });

// ═══════════════════════════════════════════
// Event Bindings (from index.html inline handlers)
// ═══════════════════════════════════════════

document.getElementById('tabBtnBacktest').addEventListener('click', () => switchTab('backtest'));
document.getElementById('tabBtnLab').addEventListener('click', () => switchTab('lab'));
document.getElementById('tabBtnTrade').addEventListener('click', () => switchTab('trade'));
document.getElementById('tabBtnAnalysis')?.addEventListener('click', () => switchTab('analysis'));
document.getElementById('tabBtnResearch')?.addEventListener('click', () => switchTab('research'));
document.getElementById('tabBtnRecords')?.addEventListener('click', () => switchTab('records'));
document.getElementById('tabBtnData')?.addEventListener('click', () => switchTab('data'));
document.querySelector('.theme-btn').addEventListener('click', toggleTheme);
document.querySelector('.sidebar-toggle').addEventListener('click', toggleSidebar);
document.getElementById('historySelect').addEventListener('change', function() { loadHistory(this.value); });
const sh = document.querySelector('.sector-header'); if (sh) sh.addEventListener('click', toggleSectorPanel);
document.getElementById('sectorSearch').addEventListener('input', filterSectors);
document.getElementById('btnClearSectors').addEventListener('click', clearSectors);
document.getElementById('cfgFactorFilterEn').addEventListener('change', saveFactorFilterState);

const sidebar = document.querySelector('.sidebar');
sidebar.addEventListener('input', function(e) { const el = e.target;
  if (el.id==='cfgStart'||el.id==='cfgEnd') validateDate(el); else if (el.id==='cfgCapital') validatePositive(el);
  else if (el.id==='cfgCommission'||el.id==='cfgSlippage') validateNonNeg(el); else if (el.id==='cfgLadderVal') validateLadder(el); });
sidebar.addEventListener('click', function(e) { const btn = e.target.closest('button'); if (!btn) return;
  const block = btn.closest('[id^="blk"]'); if (!block) return;
  if (btn.classList.contains('edit-btn')) toggleEdit(block.id); else if (btn.classList.contains('save-btn')) saveBlock(block.id);
  else if (btn.classList.contains('cancel-btn')) cancelEdit(block.id); });

document.getElementById('btnRun').addEventListener('click', runPipeline);
document.getElementById('btnResetDefaults').addEventListener('click', resetDefaults);
document.getElementById('btnSaveToFile').addEventListener('click', saveConfigToFile);
document.getElementById('btnLoadFile').addEventListener('click', loadConfigFromFile);
document.getElementById('btnDeleteFile').addEventListener('click', deleteSavedConfigFile);
document.getElementById('tradeSearch').addEventListener('input', filterTrades);
document.getElementById('tradeFilter').addEventListener('change', filterTrades);
document.getElementById('tradeReason').addEventListener('change', filterTrades);
document.getElementById('btnLabSubmit').addEventListener('click', labSubmit);

// ═══════════════════════════════════════════
// Init
// ═══════════════════════════════════════════

document.getElementById('statusDot').className = 'status-dot on';
const themeIcon = document.getElementById('themeIcon');
// 图标语义：显示"对面"主题（暗色→太阳=可切亮色, 亮色→月亮=可切暗色），与 toggleTheme() 一致
if (themeIcon) {
  themeIcon.innerHTML = getTheme() === 'dark' ? sunIcon : moonIcon;
}
if (localStorage.getItem('vera_sidebar')==='0') { document.querySelector('.sidebar').classList.add('collapsed');
  const svg = document.querySelector('.sidebar-toggle svg'); if (svg) svg.innerHTML = '<polyline points="15 18 9 12 15 6"/>'; }

loadConfig(); refreshAllSummaries(); loadSectors(); loadFactorRules();
fetchSavedConfig().then(res => { _savedFileExists = !!(res&&res.exists); toggleSavedButtons(_savedFileExists); }).catch(() => {});
if (localStorage.getItem('vera_sector_collapsed')==='1') { const sec = document.querySelector('.sector-section'); if (sec) sec.classList.add('collapsed'); }
addLog('前端就绪，等待执行回测', 'info');

fetchResults().then(list => { if (list&&list.length>0) { document.getElementById('historyCount').textContent = '('+list.length+'条)';
  const sel = document.getElementById('historySelect'); sel.innerHTML = '<option value="">-- 选择历史回测 --</option>';
  list.forEach(item => { const o = document.createElement('option'); o.value = item.id;
    const cumRet = (item.cumulative_return!=null&&!isNaN(item.cumulative_return))?(item.cumulative_return*100).toFixed(1)+'%':'--';
    o.textContent = item.time+' | '+item.formula+' '+item.date_range+' | '+item.trade_count+'笔 '+cumRet; sel.appendChild(o); }); } }).catch(() => {});

window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => Object.values(charts).forEach(c => c.resize()), 200); });

// Test exports are in recover.js (loadable by Node without ES module support)
