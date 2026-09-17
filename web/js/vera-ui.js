// ====== VERA App Shell ======
// ES module entry — imports API, config, charts modules; orchestrates app logic.
import { fetchStatus, submitBacktest, stopBacktest, fetchLastResult, fetchResults, fetchResult, fetchConfigDefaults, saveConfig, fetchSavedConfig, deleteSavedConfig, fetchSectors as apiFetchSectors, fetchFactorRules as apiFetchFactorRules, submitLabJob, stopLabJob, fetchLabStatus, fetchLabHistory, fetchLabReport, fetchFarmStatus, farmCheck, farmOnboard, farmVerify, farmBacktest, farmStop, fetchFarmReports, fetchFarmReport, fetchFarmLog, fetchFarmPrefill } from './api.js?v=20260916b';
import { STORAGE_KEY, CONFIG_IDS, RADIO_CONFIGS, cleanNum, validateDate, validatePositive, validateNonNeg, validateLadder, loadConfig, saveAllConfig, collectConfigFromForm as cfgCollect, applyConfigDict as cfgApply, toggleEdit as cfgToggleEdit, cancelEdit as cfgCancelEdit, saveBlock as cfgSaveBlock, refreshAllSummaries as cfgRefreshSummaries, notifyFormulaChanged } from './config.js?v=20260916f';
import { esc, escAttr, hexToRgba, getTheme, getColors, toggleTheme, toggleSidebar, showToast, addLog, checkEngineVersion, setChartsRef, echartsInit, tweenNumber, sparkline, fillHeroSub, revealResults, fmtReasonShort, renderTradeTable, filterTrades as chartFilterTrades, renderAllCharts, sunIcon, moonIcon } from './charts.js?v=20260906d';
import { renderDeepCharts } from './charts_deep.js?v=20260906c';

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
toggleSidebar._onToggle = () => { setTimeout(() => Object.values(charts).forEach(c => c.resize()), 300);
  // W1-2: 侧栏折叠钮(div→button)后同步 aria-expanded
  const st = document.querySelector('.sidebar-toggle');
  if (st) st.setAttribute('aria-expanded', String(!document.querySelector('.sidebar').classList.contains('collapsed'))); };

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

// W3-4: 搜索防抖 — 9 万笔回测下旧实现每敲一键全量 filter+重建 200 行 DOM;
// 250ms 内连打只触发一次, 回车立即执行不等防抖
function debounce(fn, ms) {
  let t = null;
  const d = (...a) => { clearTimeout(t); t = setTimeout(() => fn.apply(null, a), ms); };
  d.cancel = () => clearTimeout(t);
  return d;
}
const filterTradesDebounced = debounce(filterTrades, 250);

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
      // W1-7: 进度条语义化, 读屏可感知进度
      const pb = document.getElementById('progressBar'); if (pb) pb.setAttribute('aria-valuenow', dispPct.toFixed(0));
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

// sectorEmpty 原本是 #sectorGrid 的子节点, 而 renderSectors 会 `grid.innerHTML=` 把它
// 一并抹掉 → 之后任何 getElementById('sectorEmpty') 都返回 null。统一走这个"按需重建"
// 入口 (2026-09-17 修复: 同一根因在 renderSectors 与 loadSectors 两处都踩过, 不写第二份)。
function _sectorEmptyBox(grid) {
  let e = document.getElementById('sectorEmpty');
  if (!e) { e = document.createElement('div');
    e.className = 'sector-empty'; e.id = 'sectorEmpty'; grid.appendChild(e); }
  return e;
}

async function loadSectors() {
  const grid = document.getElementById('sectorGrid'); if (!grid) return;
  // 失败提示收口一份 (原两处近乎逐行重复, 含重试按钮挂载)
  const fail = (msg) => {
    grid.innerHTML = '';                      // 清掉上一轮残留, 让错误提示看得见
    const e = _sectorEmptyBox(grid);
    e.innerHTML = msg + '<br><button class="btn btn-sm btn-secondary" id="btnRetrySectors" style="margin-top:var(--sp-2)">重试</button>';
    e.style.color = 'var(--up)';
    setTimeout(() => { const b = document.getElementById('btnRetrySectors'); if (b) b.addEventListener('click', loadSectors); }, 0);
  };
  try { const result = await apiFetchSectors();
    if (!result.success || !result.sectors || result.sectors.length === 0) {
      fail('板块列表加载失败：' + (result.error || '未知错误') + '（请检查通达信客户端）');
      return; }
    _allSectors = result.sectors;
  } catch (e) { fail('板块列表加载失败：' + e.message); return; }
  _sectorEmptyBox(grid).style.display = 'none';
  try { _selectedSectors = JSON.parse(localStorage.getItem(SECTORS_KEY) || '[]'); } catch(e) { _selectedSectors = []; }
  renderSectors(); updateSectorSummary(); toggleUniverseDropdown();
}

function renderSectors() {
  const grid = document.getElementById('sectorGrid'); if (!grid) return;
  if (_allSectors.length === 0) {
    // 2026-09-17 修复(真事故): sectorEmpty 是 #sectorGrid 的子节点, 下面的
    // grid.innerHTML= 会把它一并抹掉 → 从第二次调用起 getElementById 返回 null,
    // 旧代码 `empty.style.display` 抛 "Cannot read properties of null (reading 'style')"。
    // 农场「→ 回测页」会先清板块, 异常点恰在参数回填之前 → 区间/本金/止损全没填上,
    // 页面却看着"填好了"(静默换口径)。这里改成按需重建占位元素 (自愈)。
    let empty = document.getElementById('sectorEmpty');
    if (!empty) { empty = document.createElement('div');
      empty.className = 'sector-empty'; empty.id = 'sectorEmpty'; }
    empty.textContent = '无板块数据';
    grid.innerHTML = '';           // 清掉上一轮残留, 再显示占位
    grid.appendChild(empty);
    return;
  }
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

function clearSectors() { setSectors([]); }   // 2026-09-16 收口: 唯一写入口 = setSectors

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
  // 2026-09-17: 大盘位置 TAB (固定浮层; 切入时加载一次, 无轮询 —— 每天只更新一次)
  var isMarket = name==='market';
  var _tabM = document.getElementById('tabBtnMarket'); if (_tabM) _tabM.classList.toggle('active', isMarket);
  var _pM = document.getElementById('pageMarket'); if (_pM) _pM.classList.toggle('active', isMarket);
  if (isMarket && window.marketPageEnter) window.marketPageEnter();
  document.querySelector('.app').style.display = (isLab||isTrade||isAnalysis||isMarket)?'none':'';
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
  // 2026-09-06: 公式农场 TAB (三段闸门, 切入轮询/切出停止)
  var isFarm = name==='farm';
  var _tabF = document.getElementById('tabBtnFarm'); if (_tabF) _tabF.classList.toggle('active', isFarm);
  var _pF = document.getElementById('pageFarm'); if (_pF) _pF.classList.toggle('active', isFarm);
  if (isFarm) { refreshFarmStatus(); loadFarmReports(); startFarmPoll(); } else stopFarmPoll();
  // 2026-09-06: AI 设置 TAB (对话大脑三档接入配置; 切入加载, 无轮询)
  var isAi = name==='ai';
  var _tabAi = document.getElementById('tabBtnAi'); if (_tabAi) _tabAi.classList.toggle('active', isAi);
  var _pAi = document.getElementById('pageAi'); if (_pAi) _pAi.classList.toggle('active', isAi);
  if (isAi && window.aiPageEnter) window.aiPageEnter();
  if (isLab) { refreshLabStatus(); loadLabHistory(); startLabPoll(); } else stopLabPoll();
  // 交易页轮询生命周期由 trade.js 自治 (window 钩子, 解耦两个 JS 模块)
  if (isTrade && window.tradePageEnter) window.tradePageEnter();
  if (!isTrade && window.tradePageLeave) window.tradePageLeave();
  // W1-4: 页签写入 URL hash — 刷新保持页签、可把"交易页"地址发给别人
  // (值相同不重复写, 防止与 hashchange 监听互相触发成环)
  if (location.hash.slice(1) !== name) location.hash = name; }
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
    if (!d.queue||!d.queue.length) { box.innerHTML = '<div style="color:var(--text2);font-size:var(--fs-sm)">暂无任务</div>'; return; }
    box.innerHTML = d.queue.slice().reverse().map(t => { const mins = Math.floor(t.elapsed_s/60);
      let html = '<div class="lab-row">'+_labStatusBadge(t)+' <b>'+esc(t.formulas.join(','))+'</b><span style="color:var(--text2)">'+esc(t.stage)+(t.status!=='queued'&&mins?' · '+mins+'分钟':'')+'</span></div>';
      if (t.status==='failed'&&t.error) html += '<div class="lab-rules"><div style="color:var(--up)">'+esc(t.error.slice(0,200))+'</div></div>';
      if (t.status==='done') html += '<div class="lab-rules"><div>规则已登记 — <button type="button" class="lab-report-link" data-formula="'+escAttr(t.formulas[0])+'" style="color:var(--link);background:none;border:none;padding:0;cursor:pointer;font-size:var(--fs-xs);text-decoration:underline">查看报告</button></div></div>'; return html; }).join('');
    setTimeout(() => { box.querySelectorAll('.lab-report-link').forEach(a => { a.addEventListener('click', function(e) { e.preventDefault(); viewLabReport(this.dataset.formula); }); }); }, 0);
    if (d.running||d.queue.some(t=>t.status==='queued')) startLabPoll(); loadLabHistory(); }).catch(() => {}); }

function loadLabHistory() { fetchLabHistory().then(d => { const box = document.getElementById('labHistory');
    if (!d.items||!d.items.length) { box.innerHTML = '<div style="color:var(--text2);font-size:var(--fs-sm)">暂无体检记录</div>'; return; }
    box.innerHTML = d.items.map(i => '<div class="lab-row"><b>'+esc(i.formula)+'</b><span style="color:var(--text2)">'+esc(i.report_date||i.generated_at||'')+'</span><span class="lab-badge '+(i.adopted?'ok':'wait')+'">'+i.rules+' 规则 / '+i.adopted+' 通过</span><button type="button" class="lab-hist-link" data-formula="'+escAttr(i.formula)+'" style="color:var(--link);background:none;border:none;padding:0;cursor:pointer;font-size:var(--fs-xs);text-decoration:underline">查看</button></div>').join('');
    setTimeout(() => { box.querySelectorAll('.lab-hist-link').forEach(a => { a.addEventListener('click', function(e) { e.preventDefault(); viewLabReport(this.dataset.formula); }); }); }, 0); }).catch(() => {}); }

function viewLabReport(formula) { fetchLabReport(formula).then(d => { if (!d.success) { showToast(d.error||'无报告', 'error'); return; }
    document.getElementById('labReportCard').style.display = ''; document.getElementById('labReportTitle').textContent = '体检报告: '+d.file;
    const body = document.getElementById('labReportBody');
    // 2026-08-19: markdown 渲染 (同 brain_chat 姿势), DOMPurify 消毒防注入; marked 缺载时退回转义文本
    // W4-6: h1 降级 h2 (与 brain 渲染路径共用 window.veraDemoteH1, 页内已有 <h1>VERA</h1>)
    body.innerHTML = (window.marked && window.DOMPurify)
      ? DOMPurify.sanitize(window.veraDemoteH1 ? window.veraDemoteH1(marked.parse(d.markdown)) : marked.parse(d.markdown))
      : '<pre>'+esc(d.markdown)+'</pre>';
    document.getElementById('labReportCard').scrollIntoView({ behavior: 'smooth' }); }); }

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
document.getElementById('tabBtnFarm')?.addEventListener('click', () => switchTab('farm'));
document.getElementById('tabBtnAi')?.addEventListener('click', () => switchTab('ai'));
document.getElementById('btnFarmCheck')?.addEventListener('click', () => farmGate(farmCheck, '检查增量'));
document.getElementById('btnFarmOnboard')?.addEventListener('click', () => farmGate(farmOnboard, '一键入库'));
document.getElementById('btnFarmVerify')?.addEventListener('click', () => farmGate(farmVerify, '定量复核'));
document.getElementById('btnFarmBacktest')?.addEventListener('click', () => farmGate(farmBacktest, '开始粗扫'));
document.getElementById('btnFarmStop')?.addEventListener('click', () => farmStop().then(() => showToast('已请求停止')).catch(() => {}));

// ═══════════════════════════════════════════
// Farm Page (公式农场三段闸门, 2026-09-06)
// ═══════════════════════════════════════════

let _farmPollTimer = null;
function startFarmPoll() { stopFarmPoll(); _farmPollTimer = setInterval(refreshFarmStatus, 2000); }
function stopFarmPoll() { if (_farmPollTimer) { clearInterval(_farmPollTimer); _farmPollTimer = null; } }

// 2026-09-16: 状态三态 (done/stopped/failed) —— 人工停止与真失败分开显示
function _farmStatusMark(status, withWord) {
  if (status === 'done') return withWord ? '✅ 完成 ' : '✅';
  if (status === 'stopped') return withWord ? '⏹ 已人工停止 ' : '⏹';
  return withWord ? '❌ 失败 ' : '❌';
}

function refreshFarmStatus() {
  fetchFarmStatus().then(d => {
    const cur = d.current, last = d.last || {};
    const setS = (id, gate) => { const el = document.getElementById(id); if (!el) return;
      if (cur && cur.gate === gate && cur.status === 'running') { el.textContent = '⏳ 运行中: ' + (cur.stage || ''); return; }
      if (cur && cur.gate === gate && cur.status !== 'running') { el.textContent = _farmStatusMark(cur.status, true) + (cur.finished_at || '') + (cur.error ? ' — ' + cur.error : ''); return; }
      const l = last[gate];
      el.textContent = l ? ('上次: ' + _farmStatusMark(l.status, false) + ' ' + (l.finished_at || '')) : '未运行'; };
    setS('farmCheckStatus', 'check'); setS('farmOnboardStatus', 'onboard');
    setS('farmVerifyStatus', 'verify'); setS('farmBacktestStatus', 'backtest');
    // 2026-09-16: 「查看完整日志」链接 —— 该闸门有落盘日志才显示
    const setLog = (id, gate) => { const a = document.getElementById(id); if (!a) return;
      const has = !!((cur && cur.gate === gate && cur.log_file) || (last[gate] && last[gate].log_file));
      a.style.display = has ? '' : 'none';
      a.onclick = has ? () => showFarmLog(gate, (d.gates || {})[gate] || gate) : null; };
    setLog('farmLogCheck', 'check'); setLog('farmLogOnboard', 'onboard');
    setLog('farmLogVerify', 'verify'); setLog('farmLogBacktest', 'backtest');
    renderFarmSummary(d); renderFarmOverview(d);
    const box = document.getElementById('farmNewList');
    if (box && cur && cur.status === 'running') {
      box.innerHTML = '<pre style="font-size:var(--fs-xs);max-height:200px;overflow:auto;background:var(--bg);padding:var(--sp-2);border-radius:6px;margin-top:var(--sp-2)">'
        + esc((cur.log_tail || []).slice(-30).join('\n')) + '</pre>';
    }
  }).catch(() => {});
}

function farmGate(fn, label) {
  fn().then(() => { showToast(label + '已启动'); refreshFarmStatus(); })
    .catch(e => showToast(e.message || '启动失败', 'error'));
}

// ── 2026-09-16 看板计划书阶段 3: 流水线卡片(状态灯/成绩单/置灰) + 总览看板 ──
const _FARM_GATES = [['Check', 'check', 'btnFarmCheck'], ['Onboard', 'onboard', 'btnFarmOnboard'],
                     ['Verify', 'verify', 'btnFarmVerify'], ['Backtest', 'backtest', 'btnFarmBacktest']];

function _farmDotState(cur, last, gate) {
  if (cur && cur.gate === gate) {
    if (cur.status === 'running') return 'run';
    return cur.status === 'done' ? 'ok' : (cur.status === 'stopped' ? 'stop' : 'bad');
  }
  const l = (last || {})[gate];
  if (!l) return '';
  return l.status === 'done' ? 'ok' : (l.status === 'stopped' ? 'stop' : 'bad');
}

function renderFarmSummary(d) {
  const sum = d.summary || {};
  _FARM_GATES.forEach(function (g) {
    const suffix = g[0], gate = g[1], btnId = g[2];
    const info = sum[gate] || {};
    const sumEl = document.getElementById('farmSum' + suffix);
    if (sumEl) sumEl.textContent = info.text || '';
    const btn = document.getElementById(btnId);
    if (btn) { btn.disabled = info.ready === false; btn.title = info.ready === false ? (info.hint || '') : ''; }
    const noteEl = document.getElementById('farmNote' + suffix);
    if (noteEl) noteEl.textContent = info.note || '';
    const dot = document.getElementById('farmDot' + suffix);
    if (dot) dot.className = 'farm-dot ' + _farmDotState(d.current, d.last, gate);
  });
}

let _farmOvSig = '';
function _pct(v, digits) { return v == null ? '—' : (v * 100).toFixed(digits == null ? 1 : digits) + '%'; }
function renderFarmOverview(d) {
  const ov = d.overview; if (!ov) return;
  const sig = JSON.stringify(ov);
  if (sig === _farmOvSig) return;   // 签名没变不重绘, 防 2 秒轮询闪屏
  _farmOvSig = sig;
  const empty = document.getElementById('farmOvEmpty');
  if (empty) empty.style.display = ov.empty ? '' : 'none';
  const funnelBox = document.getElementById('farmFunnel');
  if (funnelBox) {
    const max = Math.max(1, ...(ov.funnel || []).map(f => f.count || 0));
    funnelBox.innerHTML = (ov.funnel || []).map(f =>
      '<div class="farm-funnel-row"><span class="farm-funnel-label">' + esc(f.label) + '</span>'
      + '<span class="farm-funnel-bar" style="width:' + Math.max(2, Math.round((f.count || 0) / max * 100)) + '%"></span>'
      + '<span class="farm-funnel-num">' + (f.count || 0) + (f.pass != null ? ' · 过 ' + f.pass : '') + '</span></div>').join('');
  }
  const tr = document.getElementById('farmTopReasons');
  if (tr) tr.textContent = (ov.top_reasons && ov.top_reasons.length)
    ? '这批货主要死在: ' + ov.top_reasons.map(r => r[0] + ' ' + r[1] + ' 条').join(' · ') : '';
  const board = document.getElementById('farmBoard');
  if (board) {
    const rowHtml = (r, withBtn) => '<tr><td>' + esc(r.gs) + '</td><td>' + esc((r.file || '').replace(/\.md$/, '').slice(0, 20)) + '</td>'
      + '<td>' + esc(r.key || '—') + '</td><td class="num">' + _pct(r.annret) + '</td>'
      + '<td class="num">' + (r.calmar == null ? '—' : Number(r.calmar).toFixed(2)) + '</td>'
      + '<td class="num">' + _pct(r.maxdd) + '</td><td class="num">' + _pct(r.winrate, 0) + '</td>'
      + '<td class="num">' + (r.trades == null ? '—' : esc(String(r.trades))) + '</td>'
      + '<td>' + esc(r.onboard_date || '') + '</td>'
      // 2026-09-16 审计 L5: url 源自股旁网抓取 HTML, 只放行 http(s) —— escAttr
      // 不拦 javascript: 协议, 源站被挂马时可落成存储型 XSS (需点击, LOW 但便宜)
      + '<td>' + (/^https?:\/\//i.test(r.url || '') ? '<a href="' + escAttr(r.url) + '" target="_blank" rel="noopener" style="color:var(--link)">来源</a>' : '') + '</td>'
      // 2026-09-16 回填回测页计划书: 按钮只放达标组 (样本不足/未达标不放, 防误导)
      + (withBtn ? '<td><button class="btn farm-to-bt" data-gs="' + escAttr(r.gs) + '" style="font-size:var(--fs-xs);padding:1px 8px">→ 回测页</button></td>' : '') + '</tr>';
    const table = (rows, withBtn) => '<table><thead><tr><th>GS</th><th>公式</th><th>最优组合</th><th>年化</th><th>卡玛</th><th>最大回撤</th><th>胜率</th><th>笔数</th><th>入库日</th><th>来源</th>' + (withBtn ? '<th>操作</th>' : '') + '</tr></thead><tbody>'
      + rows.map(r => rowHtml(r, withBtn)).join('') + '</tbody></table>';
    const b = ov.board || { pass: [], insufficient: [], fail: [] };
    const totals = ov.board_totals || { pass: b.pass.length, insufficient: b.insufficient.length, fail: b.fail.length };
    const capNote = (rows, total) => rows.length < total ? ' (仅列前 ' + rows.length + ' 条)' : '';
    let html = '';
    if (!ov.board_total) html = '<div style="color:var(--text2);font-size:var(--fs-sm)">粗扫还没跑出结果——点下方「④ 开始粗扫」试第一批。</div>';
    if (b.pass.length) html += '<div class="farm-group-title farm-group-pass">✅ 达标 ' + totals.pass + ' 条' + capNote(b.pass, totals.pass) + '</div>' + table(b.pass, true);
    if (b.insufficient.length) html += '<div class="farm-group-title farm-group-thin">🟡 样本不足 ' + totals.insufficient + ' 条 (数字好看但笔数不足 20, 不作数)' + capNote(b.insufficient, totals.insufficient) + '</div>' + table(b.insufficient);
    if (b.fail.length) html += '<details style="margin-top:8px"><summary class="farm-group-title farm-group-fail" style="cursor:pointer">未达标 / 无有效组合 ' + totals.fail + ' 条 (点击展开' + capNote(b.fail, totals.fail) + ')</summary>' + table(b.fail) + '</details>';
    // 2026-09-17 作废组: 入库后才发现踩未来函数黑名单的公式, 成绩不计入达标
    if (b.void && b.void.length) {
      const reasons = {};
      b.void.forEach(r => { const k = (r.void_reason || '未注明').split('(')[0]; reasons[k] = (reasons[k] || 0) + 1; });
      const rtxt = Object.keys(reasons).map(k => k + ' ×' + reasons[k]).join(' · ');
      html += '<details style="margin-top:8px"><summary class="farm-group-title farm-group-fail" style="cursor:pointer">⛔ 作废 ' + totals.void + ' 条 (踩黑名单, 成绩不计入: ' + esc(rtxt) + '; 点击展开' + capNote(b.void, totals.void) + ')</summary>' + table(b.void) + '</details>';
    }
    board.innerHTML = html;
  }
}

// 2026-09-16: 查看闸门完整日志 (失败病因结构化配套)
function showFarmLog(gate, label) {
  fetchFarmLog(gate).then(d => {
    const card = document.getElementById('farmLogCard'); if (!card) return;
    card.style.display = '';
    document.getElementById('farmLogTitle').textContent =
      '运行日志 · ' + label + ' · ' + (d.file || '') + (d.truncated ? ' (过长已截尾)' : '');
    document.getElementById('farmLogBody').textContent = d.log || '(空)';
    card.scrollIntoView({ behavior: 'smooth' });
  }).catch(e => showToast(e.message || '日志读取失败', 'error'));
}

// ── 2026-09-16 达标榜 → 回测页回填 (计划书: 方案 A 只回填不代跑) ──
// 快照与直写同一字段集合; 佣金/滑点/整手等**个人成本设置**一律不碰。
// 审计 HIGH-2: 会改结果的池子/止损语义开关必须一并回填并快照 ——
// ① 板块选择非空时股票池下拉框会被 silently 忽略 (sector 并集优先),
//    ② 引擎默认移动止盈确认=盘中触线而页面默认条件单语义 (HIGH-1)。
// 板块另有单独清理 (clearSectors, 不在字段表内)。
const _PREFILL_FIELDS = ['cfgFormula', 'cfgFormulaArg', 'cfgUniverse', 'cfgPeriod',
  'cfgEntryPriceMode', 'cfgStart', 'cfgEnd', 'cfgCapital', 'cfgMinBuy', 'cfgMaxBuy',
  'cfgExcludeST', 'cfgIncludeEtf', 'cfgEtfOnly',
  'cfgCostStopEn', 'cfgCostStopVal', 'cfgTrailingEn', 'cfgTrailingAct', 'cfgTrailingDD',
  'cfgTrailingConfirm',
  'cfgLadderEn', 'cfgTimeEn', 'cfgTimeVal', 'cfgCondTimeEn', 'cfgCondTimeDays', 'cfgCondTimeProfit',
  'cfgFirstDayEn', 'cfgFormulaSellEn', 'cfgFactorFilterEn'];
let _farmPrefillSnapshot = null;
let _farmPrefillSectorsSnap = null;

function _farmToBacktest(gs) {
  fetchFarmPrefill(gs).then(d => {
    const banner = document.getElementById('farmPrefillBanner');
    const bannerVisible = !!banner && banner.style.display !== 'none';
    // ① 快照 (横幅「恢复原配置」用) —— 审计 MEDIUM-3: 连续点两条公式时,
    //    只有第一次快照 (用户原配置); 后续点击不覆盖, 否则恢复的是上一次回填值
    if (!bannerVisible) {
      const snap = { radio: (document.querySelector('input[name="cfgPriority"]:checked') || {}).value || '' };
      _PREFILL_FIELDS.forEach(id => { const el = document.getElementById(id);
        if (el) snap[id] = el.type === 'checkbox' ? el.checked : el.value; });
      _farmPrefillSnapshot = snap;
      // 审计第二轮 MEDIUM-B: 板块快照必须读**磁盘**而不是内存 —— 通达信没开时
      // loadSectors() 提前 return (_selectedSectors 停在 []), 从内存快照会把
      // 用户真实选择记成空, 恢复时用空覆盖, 设置不可逆丢失
      let raw = [];
      try { raw = JSON.parse(localStorage.getItem(SECTORS_KEY) || '[]'); } catch (e) { raw = []; }
      _farmPrefillSectorsSnap = Array.isArray(raw) ? raw.slice() : [];
    }
    // ② 逐字段直写 (不走 applyConfigDict —— 它会把缺失字段重置成默认值,
    //    冲掉用户的佣金/滑点设置, 计划书侦察结论 4)
    const p = d.params, cal = d.caliber;
    const set = (id, v) => { const el = document.getElementById(id);
      if (el) { if (el.type === 'checkbox') el.checked = !!v; else el.value = String(v); } };
    set('cfgFormula', d.gs); set('cfgFormulaArg', '');
    notifyFormulaChanged();   // 审计 MEDIUM-E: 程序化改公式后刷因子规则面板
    set('cfgUniverse', cal.universe_type); set('cfgPeriod', cal.period);
    set('cfgEntryPriceMode', cal.entry_price_mode);
    // 池子/语义开关按粗扫口径复位 (HIGH-2 / HIGH-1)
    set('cfgExcludeST', true); set('cfgIncludeEtf', false); set('cfgEtfOnly', false);
    set('cfgTrailingConfirm', cal.trailing_confirm);
    set('cfgFirstDayEn', false); set('cfgFormulaSellEn', false);
    set('cfgFactorFilterEn', false);
    // 审计第二轮 MEDIUM-B: 板块 UI 未加载成功 (_allSectors 空) 时**只清内存不落盘**
    // —— 否则 clearSectors() 会把用户磁盘上的真实选择立刻抹成 [] 且恢复不回来;
    // 结果口径不受影响 (提交走内存 _selectedSectors, 已是空)
    // 2026-09-17: 整段加 try/catch —— 清板块是**附带动作**, 绝不能让它的异常打断
    // 下面的参数回填。半截配置比报错危险得多: 页面看着"填好了", 实际跑另一套口径
    // (真事故: renderSectors 抛 null.style, 本金停在 100万默认值而非粗扫的 300万)。
    try {
      if (_allSectors && _allSectors.length > 0) {
        if (typeof clearSectors === 'function') clearSectors();
      } else {
        _selectedSectors = [];
      }
    } catch (e) { console.warn('清板块失败(继续回填参数)', e); }
    if (d.window && d.window[0]) set('cfgStart', String(d.window[0]).replace(/-/g, ''));
    if (d.window && d.window[1]) set('cfgEnd', String(d.window[1]).replace(/-/g, ''));
    set('cfgCapital', cal.capital); set('cfgMaxBuy', cal.max_buy);
    set('cfgMinBuy', cal.min_buy);   // 审计 LOW-G: 硬闸参数, 不同值会让复跑一笔不开
    // 自审修复: JS 浮点直乘会出 "20.000000000000004" 填进输入框 —— 先修约再写
    const pct100 = v => String(Number((v * 100).toFixed(6)));
    set('cfgCostStopEn', true); set('cfgCostStopVal', pct100(Math.abs(p.cost)));
    set('cfgTrailingEn', true); set('cfgTrailingAct', pct100(p.act));
    set('cfgTrailingDD', pct100(p.dd));
    set('cfgLadderEn', false);                       // 当前数据全部不用阶梯止盈
    set('cfgTimeEn', true); set('cfgTimeVal', p.time_days);
    const condOn = (p.cond_days || 0) > 0;
    set('cfgCondTimeEn', condOn);
    if (condOn) { set('cfgCondTimeDays', p.cond_days);
      set('cfgCondTimeProfit', pct100(p.cond_profit || 0)); }
    const radio = document.querySelector('input[name="cfgPriority"][value="' + cal.priority_value + '"]');
    if (radio) radio.checked = true;
    // ③ 横幅 + 切页 (人工核对后自己点开始回测 —— 两口径铁律的最后一道闸)
    const name = (d.file || '').replace(/\.md$/, '');
    const win = (d.window && d.window[0] && d.window[1])
      ? ' · 区间 ' + d.window[0] + '~' + d.window[1] : '';
    let txt = '已从公式农场回填: ' + d.gs + (name ? '(' + name + ')' : '')
      + ' · 最优组合 ' + d.combo_text
      + ' · 口径: ' + (d.caliber_text || '')          // 文案后端唯一生成 (LOW-8)
      + win
      + ' · 已复位池子/语义开关'
      + ' · 请核对后手动点「开始回测」';
    if (d.ladder_note) txt += ' ⚠ ' + d.ladder_note;
    if (banner) { document.getElementById('farmPrefillText').textContent = txt;
      banner.style.display = ''; }
    switchTab('backtest');
    // 审计第二轮 LOW-I: 摘要刷新是装饰性动作, 必须排在"横幅已显示+已切页"之后
    // 且自吞异常 —— 原顺序(刷新→横幅)一旦抛错, 表单已改却弹"回填失败"、无横幅
    // 不切页, 直接违背"人工过目是最后一道闸"的前提
    try { refreshAllSummaries(); } catch (e) { console.warn('摘要刷新失败', e); }
  }).catch(e => showToast(e.message || '回填失败', 'error'));
}

// 板块状态唯一写入口 (审计第二轮 MEDIUM-C: 「变更板块后必须同步股票池下拉框」
// 原来手写在 onSectorToggle/clearSectors/removeSector 三处, 恢复分支漏调 → 收口)
function setSectors(list) {
  _selectedSectors = (list || []).slice();
  try { localStorage.setItem(SECTORS_KEY, JSON.stringify(_selectedSectors)); } catch (e) {}
  renderSectors();
  updateSectorSummary();
  toggleUniverseDropdown();
}

function _farmRestorePrefill() {
  const snap = _farmPrefillSnapshot;
  if (snap) {
    _PREFILL_FIELDS.forEach(id => { const el = document.getElementById(id);
      if (el && id in snap) { if (el.type === 'checkbox') el.checked = snap[id]; else el.value = snap[id]; } });
    if (snap.radio) { const r = document.querySelector('input[name="cfgPriority"][value="' + snap.radio + '"]');
      if (r) r.checked = true; }
    // 板块选择恢复: 走 setSectors (含 toggleUniverseDropdown, 修 MEDIUM-C)
    if (_farmPrefillSectorsSnap) setSectors(_farmPrefillSectorsSnap);
    try { refreshAllSummaries(); } catch (e) { console.warn('摘要刷新失败', e); }
  }
  _farmPrefillSnapshot = null;                   // MEDIUM-3: 恢复后允许重新快照
  _farmPrefillSectorsSnap = null;
  document.getElementById('farmPrefillBanner').style.display = 'none';
}

document.getElementById('farmBoard')?.addEventListener('click', function (ev) {
  const btn = ev.target.closest('.farm-to-bt');
  if (btn) _farmToBacktest(btn.dataset.gs);
});
document.getElementById('farmPrefillRestore')?.addEventListener('click', _farmRestorePrefill);
document.getElementById('farmPrefillDismiss')?.addEventListener('click', () => {
  // 审计第二轮 MEDIUM-D: 「知道了」只隐藏横幅会让下次点击重新快照(覆盖原配置) ——
  // 必须一并清快照, 与「恢复原配置」保持同一生命周期语义
  _farmPrefillSnapshot = null;
  _farmPrefillSectorsSnap = null;
  document.getElementById('farmPrefillBanner').style.display = 'none';
});

function loadFarmReports() {
  fetchFarmReports().then(d => {
    const box = document.getElementById('farmReports'); if (!box) return;
    if (!d.items || !d.items.length) { box.innerHTML = '<div style="color:var(--text2)">暂无报告</div>'; return; }
    box.innerHTML = d.items.map(i =>
      '<div class="lab-row"><b>' + esc(i.file) + '</b><span style="color:var(--text2);font-size:var(--fs-xs)"> ' + esc(i.mtime) + '</span> '
      + '<a href="javascript:void(0)" class="farm-rpt" data-f="' + escAttr(i.file) + '" style="color:var(--link)">查看</a></div>').join('');
    box.querySelectorAll('.farm-rpt').forEach(a => a.addEventListener('click', function () {
      fetchFarmReport(this.dataset.f).then(d2 => {
        const b = document.getElementById('farmReportBody');
        // 2026-09-06 审查修复: 补上与 lab 报告同款的 marked 守卫 + demoteH1 (原裸调, marked 缺载即抛错)
        b.innerHTML = (window.marked && window.DOMPurify)
          ? DOMPurify.sanitize(window.veraDemoteH1 ? window.veraDemoteH1(marked.parse(d2.markdown)) : marked.parse(d2.markdown))
          : '<pre style="white-space:pre-wrap;margin:0">' + esc(d2.markdown) + '</pre>';
        b.scrollIntoView({ behavior: 'smooth' });
      }).catch(e => showToast(e.message, 'error'));
    }));
  }).catch(() => {});
}
document.querySelector('.theme-btn').addEventListener('click', toggleTheme);
document.querySelector('.sidebar-toggle').addEventListener('click', toggleSidebar);
document.getElementById('historySelect').addEventListener('change', function() { loadHistory(this.value); });
const sh = document.querySelector('.sector-header'); if (sh) { sh.addEventListener('click', toggleSectorPanel);
  // W1-2: 板块折叠头(div→button)后同步 aria-expanded (toggleSectorPanel 切 class, 本监听在其后同步态)
  sh.addEventListener('click', () => sh.setAttribute('aria-expanded', String(!document.querySelector('.sector-section').classList.contains('collapsed')))); }
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
document.getElementById('tradeSearch').addEventListener('input', filterTradesDebounced);
document.getElementById('tradeSearch').addEventListener('keydown', e => { if (e.key === 'Enter') { filterTradesDebounced.cancel(); filterTrades(); } });
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
// W1-2: 侧栏折叠钮初始 aria-expanded 同步
(function syncSidebarAria() {
  const st = document.querySelector('.sidebar-toggle');
  if (st) st.setAttribute('aria-expanded', String(!document.querySelector('.sidebar').classList.contains('collapsed')));
})();

loadConfig(); refreshAllSummaries(); loadSectors(); loadFactorRules();
// W4-6: lab 自定义日期 placeholder 动态生成 (原写死 20250719/20260718 已过期)
(function labDatePlaceholders() {
  const f = d => d.getFullYear() + String(d.getMonth()+1).padStart(2,'0') + String(d.getDate()).padStart(2,'0');
  const now = new Date();
  const y1 = new Date(now); y1.setFullYear(now.getFullYear()-1);
  const y3 = new Date(now); y3.setFullYear(now.getFullYear()-3);
  const s1 = document.getElementById('labStart1'), e1 = document.getElementById('labEnd1');
  const s2 = document.getElementById('labStart2'), e2 = document.getElementById('labEnd2');
  if (s1) s1.placeholder = f(y1); if (e1) e1.placeholder = f(now);
  if (s2) s2.placeholder = f(y3); if (e2) e2.placeholder = f(now);
})();
fetchSavedConfig().then(res => { _savedFileExists = !!(res&&res.exists); toggleSavedButtons(_savedFileExists); }).catch(() => {});
if (localStorage.getItem('vera_sector_collapsed')==='1') { const sec = document.querySelector('.sector-section'); if (sec) sec.classList.add('collapsed'); }
// W1-2: 板块折叠头初始 aria-expanded 同步 (须在 localStorage 折叠恢复之后)
(function syncSectorAria() {
  const sh2 = document.querySelector('.sector-header');
  if (sh2) sh2.setAttribute('aria-expanded', String(!document.querySelector('.sector-section').classList.contains('collapsed')));
})();
addLog('前端就绪，等待执行回测', 'info');

fetchResults().then(list => { if (list&&list.length>0) { document.getElementById('historyCount').textContent = '('+list.length+'条)';
  const sel = document.getElementById('historySelect'); sel.innerHTML = '<option value="">-- 选择历史回测 --</option>';
  list.forEach(item => { const o = document.createElement('option'); o.value = item.id;
    const cumRet = (item.cumulative_return!=null&&!isNaN(item.cumulative_return))?(item.cumulative_return*100).toFixed(1)+'%':'--';
    o.textContent = item.time+' | '+item.formula+' '+item.date_range+' | '+item.trade_count+'笔 '+cumRet; sel.appendChild(o); }); } }).catch(() => {});

window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => Object.values(charts).forEach(c => c.resize()), 200); });

// W1-4: 初始 hash 恢复页签 + 浏览器前进后退联动 (白名单与 switchTab 实际入参一致)
const _validTabs = ['backtest','lab','trade','analysis','research','records','data','farm','ai','market'];
function _applyHashTab() { const h = location.hash.slice(1); if (_validTabs.includes(h)) switchTab(h); }
_applyHashTab();
window.addEventListener('hashchange', _applyHashTab);

// Test exports are in recover.js (loadable by Node without ES module support)
