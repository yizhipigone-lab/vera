// ====== P2-2: hexToRgba 辅助函数（ECharts 兼容） ======
// H2 修正：同时处理 #hex 和 rgb(r,g,b) 两种格式（getComputedStyle 可能返回后者）
function hexToRgba(color, alpha) {
  if (color.startsWith('rgb')) {
    const m = color.match(/[\d.]+/g);
    if (m && m.length >= 3) return 'rgba(' + m[0] + ',' + m[1] + ',' + m[2] + ',' + alpha + ')';
  }
  const hex = color.replace('#', '');
  const r = parseInt(hex.slice(0,2), 16);
  const g = parseInt(hex.slice(2,4), 16);
  const b = parseInt(hex.slice(4,6), 16);
  return 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')';
}

// ====== C2 修正：XSS 防御 — innerHTML 转义函数 ======
function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}

// ====== P1-4: Toast 通知 ======
function showToast(msg, type) {
  const container = document.getElementById('toastContainer');
  const toast = document.createElement('div');
  toast.className = 'toast toast-' + (type || 'info');
  toast.textContent = msg;
  container.appendChild(toast);
  setTimeout(() => { if (toast.parentNode) toast.remove(); }, 3000);
}

// ====== U1 (审计): 检测旧版引擎结果并显示黄条警告 ======
const CURRENT_ENGINE_VERSION = 'signal-day-close';
function checkEngineVersion(data) {
  const ev = data && data.engine_version;
  const banner = document.getElementById('legacyEngineWarning');
  if (!banner) return;
  if (!ev || ev !== CURRENT_ENGINE_VERSION) {
    // 旧版本 (t+1-open) 或未标记
    banner.textContent = `⚠️ 此回测结果基于 ${ev || '旧版'} 引擎（买入价 = 次日开盘价）。当前默认采用 ${CURRENT_ENGINE_VERSION}（买入价 = 信号日收盘价）。请重跑以反映新口径。`;
    banner.style.display = 'block';
  } else {
    banner.style.display = 'none';
  }
}

// ====== P1-5: 表单即时校验 ======
function validateDate(el) {
  const v = el.value.trim();
  el.classList.toggle('invalid', v.length > 0 && !/^\d{8}$/.test(v));
}
function validatePositive(el) {
  el.classList.toggle('invalid', parseFloat(el.value) <= 0);
}
function validateNonNeg(el) {
  el.classList.toggle('invalid', parseFloat(el.value) < 0);
}
function validateLadder(el) {
  const v = el.value.trim();
  if (!v) { el.classList.remove('invalid'); return; }
  const valid = v.split(',').every(s => /^\d+(\.\d+)?\s*:\s*\d+(\.\d+)?$/.test(s.trim()));
  el.classList.toggle('invalid', !valid);
}

// ====== Theme — P3-3: 加 localStorage 持久化 ======
const themeIcon = document.getElementById('themeIcon');
const sunIcon = '<circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>';
const moonIcon = '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>';

function getTheme() { return document.documentElement.getAttribute('data-theme'); }
function getColors() {
  const s = getComputedStyle(document.documentElement);
  return { up: s.getPropertyValue('--up').trim(), down: s.getPropertyValue('--down').trim(),
    accent: s.getPropertyValue('--accent').trim(), accent2: s.getPropertyValue('--accent2').trim(),
    text: s.getPropertyValue('--text').trim(), text2: s.getPropertyValue('--text2').trim(),
    bg: s.getPropertyValue('--bg').trim(), border: s.getPropertyValue('--border').trim() };
}

function toggleTheme() {
  const next = getTheme() === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('vera_theme', next);  // P3-3: 持久化
  themeIcon.innerHTML = next === 'dark' ? sunIcon : moonIcon;
  if (lastResult) renderAllCharts(lastResult);
}
// 初始化主题图标
themeIcon.innerHTML = getTheme() === 'dark' ? sunIcon : moonIcon;

function toggleSidebar() {
  const sb = document.querySelector('.sidebar');
  const btn = document.querySelector('.sidebar-toggle svg');
  sb.classList.toggle('collapsed');
  const closed = sb.classList.contains('collapsed');
  localStorage.setItem('vera_sidebar', closed ? '0' : '1');
  btn.innerHTML = closed
    ? '<polyline points="9 18 15 12 9 6"/>'
    : '<polyline points="15 18 9 12 15 6"/>';
  setTimeout(() => Object.values(charts).forEach(c => c.resize()), 300);
}
if (localStorage.getItem('vera_sidebar') === '0') {
  document.querySelector('.sidebar').classList.add('collapsed');
  document.querySelector('.sidebar-toggle svg').innerHTML = '<polyline points="9 18 15 12 9 6"/>';
}

// ====== Config Persistence ======
const STORAGE_KEY = 'vera_all_config';
const CONFIG_IDS = [
  'cfgFormula', 'cfgFormulaArg', 'cfgUniverse', 'cfgPeriod',
  'cfgStart', 'cfgEnd', 'cfgExcludeST',
  // P-v3.4: ETF 开关
  'cfgIncludeEtf', 'cfgEtfOnly',
  'cfgCapital', 'cfgCommission', 'cfgSlippage',
  'cfgMinBuy', 'cfgMaxBuy', 'cfgLotSize', 'cfgMinLots',
  'cfgCostStopEn', 'cfgCostStopVal',
  'cfgTrailingEn', 'cfgTrailingAct', 'cfgTrailingDD',
  'cfgLadderEn', 'cfgLadderVal',
  'cfgTimeEn', 'cfgTimeVal',
  'cfgCondTimeEn', 'cfgCondTimeDays', 'cfgCondTimeProfit',
  'cfgFirstDayEn', 'cfgFirstDayTarget',
  // P-v3.4: 公式卖出 (formula_sell)
  'cfgFormulaSellEn', 'cfgFormulaSellName', 'cfgFormulaSellRatio',
];

function loadConfig() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY));
    if (saved) {
      CONFIG_IDS.forEach(id => {
        const el = document.getElementById(id);
        if (!el || !(id in saved)) return;
        let val = saved[id];
        if (el.type === 'checkbox') el.checked = val;
        else if (el.tagName === 'SELECT') { try { el.value = val; } catch(e) {} }
        else el.value = val;
      });
    }
  } catch(e) {}
  refreshAllSummaries();
}

// H5 修正：saveAllConfig 加 try/catch，localStorage 满了或隐私模式会报错
function saveAllConfig() {
  const data = {};
  CONFIG_IDS.forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    if (el.type === 'checkbox') data[id] = el.checked;
    else data[id] = el.value;
  });
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
  } catch(e) {
    showToast('配置保存失败（浏览器存储不可用）', 'error');
  }
}

// P-v3.4: 行业板块多选 — 单独 localStorage key 存代码数组
const SECTORS_KEY = 'vera_selected_sectors';
let _allSectors = [];        // [{code, name}] 全部 128 个板块
let _selectedSectors = [];   // ["881319.SH", ...] 已选代码

async function loadSectors() {
  // 从后端拉 128 个板块列表
  const grid = document.getElementById('sectorGrid');
  const empty = document.getElementById('sectorEmpty');
  try {
    const resp = await fetch('/api/sectors');
    const result = await resp.json();
    if (!result.success || !result.sectors || result.sectors.length === 0) {
      empty.innerHTML = '板块列表加载失败：' + (result.error || '未知错误') + '（请检查通达信客户端）<br><button class="btn btn-sm btn-secondary" onclick="loadSectors()" style="margin-top:6px">重试</button>';
      empty.style.color = 'var(--up)';
      return;
    }
    _allSectors = result.sectors;
  } catch (e) {
    empty.innerHTML = '板块列表加载失败：' + e.message + '<br><button class="btn btn-sm btn-secondary" onclick="loadSectors()" style="margin-top:6px">重试</button>';
    empty.style.color = 'var(--up)';
    return;
  }
  // 加载成功 — 恢复正常样式
  empty.style.display = 'none';
  // 读 localStorage 已选
  try {
    _selectedSectors = JSON.parse(localStorage.getItem(SECTORS_KEY) || '[]');
  } catch (e) { _selectedSectors = []; }
  renderSectors();
  updateSectorSummary();
  toggleUniverseDropdown();
}

function renderSectors() {
  const grid = document.getElementById('sectorGrid');
  const empty = document.getElementById('sectorEmpty');
  if (_allSectors.length === 0) { empty.textContent = '无板块数据'; return; }
  empty.style.display = 'none';
  // 构造 128 个 checkbox item
  grid.innerHTML = _allSectors.map(s => {
    const checked = _selectedSectors.includes(s.code) ? 'checked' : '';
    return '<div class="sector-item" data-name="' + esc(s.name) + '">' +
      '<input type="checkbox" value="' + esc(s.code) + '" ' + checked + ' onchange="onSectorToggle(this)">' +
      '<label title="' + esc(s.code) + '">' + esc(s.code) + ' ' + esc(s.name) + '</label>' +
      '</div>';
  }).join('');
}

function filterSectors() {
  const kw = (document.getElementById('sectorSearch').value || '').trim().toLowerCase();
  document.querySelectorAll('.sector-item').forEach(item => {
    const name = (item.dataset.name || '').toLowerCase();
    item.classList.toggle('hidden', kw && !name.includes(kw));
  });
}

function onSectorToggle(cb) {
  const code = cb.value;
  if (cb.checked) {
    if (!_selectedSectors.includes(code)) _selectedSectors.push(code);
  } else {
    _selectedSectors = _selectedSectors.filter(c => c !== code);
  }
  try { localStorage.setItem(SECTORS_KEY, JSON.stringify(_selectedSectors)); } catch(e) {}
  updateSectorSummary();
  toggleUniverseDropdown();
}

function clearSectors() {
  _selectedSectors = [];
  try { localStorage.setItem(SECTORS_KEY, JSON.stringify(_selectedSectors)); } catch(e) {}
  document.querySelectorAll('#sectorGrid input[type=checkbox]').forEach(cb => cb.checked = false);
  updateSectorSummary();
  toggleUniverseDropdown();
}

function updateSectorSummary() {
  const box = document.getElementById('sectorSelected');
  if (_selectedSectors.length === 0) { box.innerHTML = '<span style="font-size:10px;color:var(--text2)">未选板块</span>'; return; }
  // 用代码找名称
  box.innerHTML = _selectedSectors.map(code => {
    const s = _allSectors.find(x => x.code === code);
    const name = s ? s.name : code;
    return '<span class="sector-tag" onclick="removeSector(\'' + code + '\')" title="点击取消">' +
      esc(name) + ' <span class="x">×</span></span>';
  }).join('') + '<span style="font-size:10px;color:var(--text2);align-self:center;margin-left:4px">(' + _selectedSectors.length + ' 个)</span>';
}

function removeSector(code) {
  _selectedSectors = _selectedSectors.filter(c => c !== code);
  try { localStorage.setItem(SECTORS_KEY, JSON.stringify(_selectedSectors)); } catch(e) {}
  // 同步取消 checkbox
  const cb = document.querySelector('#sectorGrid input[value="' + code + '"]');
  if (cb) cb.checked = false;
  updateSectorSummary();
  toggleUniverseDropdown();
}

// 选了板块后, "股票池"下拉框变灰 (板块优先, 下拉框被忽略)
function toggleUniverseDropdown() {
  const sel = document.getElementById('cfgUniverse');
  if (!sel) return;
  const disabled = _selectedSectors.length > 0;
  sel.disabled = disabled;
  sel.title = disabled ? '已选行业板块，下拉框被忽略' : '';
  sel.style.opacity = disabled ? '0.5' : '1';
}

// C2 修正：refreshAllSummaries — 所有用户输入经 esc() 转义后再插入 innerHTML
function refreshAllSummaries() {
  const cs = esc(document.getElementById('cfgCostStopVal').value);
  document.getElementById('sumCostStop').innerHTML = '成本止损：亏损达到 <b>'+cs+'%</b> 全仓卖出 <span class="saved-badge saved">已保存</span>';
  const ta = esc(document.getElementById('cfgTrailingAct').value);
  const td = esc(document.getElementById('cfgTrailingDD').value);
  document.getElementById('sumTrailing').innerHTML = '移动止损：盈利 <b>'+ta+'%</b> 激活后，回撤 <b>'+td+'%</b> 触发全仓卖出 <span class="saved-badge saved">已保存</span>';
  const lv = esc(document.getElementById('cfgLadderVal').value.replace(/,/g, ', '));
  document.getElementById('sumLadder').innerHTML = '阶梯止盈：<b>'+lv+'</b> <span class="saved-badge saved">已保存</span>';
  const tv = esc(document.getElementById('cfgTimeVal').value);
  document.getElementById('sumTime').innerHTML = '时间止盈：持仓 <b>'+tv+'</b> 天后无条件卖出 <span class="saved-badge saved">已保存</span>';
  const cd = esc(document.getElementById('cfgCondTimeDays').value);
  const cp = esc(document.getElementById('cfgCondTimeProfit').value);
  document.getElementById('sumCondTime').innerHTML = '条件时间止盈：持仓 <b>'+cd+'</b> 天后盈利 <b>>='+cp+'%</b> 清仓 <span class="saved-badge saved">已保存</span>';
  const fd = esc(document.getElementById('cfgFirstDayTarget').value);
  document.getElementById('sumFirstDay').innerHTML = '首日未达标卖出：买入次日最高价涨幅未达 <b>'+fd+'%</b> 则收盘卖出 <span class="saved-badge saved">已保存</span>';
  // P-v3.4: 公式止损 summary
  const fsEn = document.getElementById('cfgFormulaSellEn').checked;
  const fsName = esc(document.getElementById('cfgFormulaSellName').value || '卖出XG');
  const fsRatio = esc(document.getElementById('cfgFormulaSellRatio').value);
  const fsState = fsEn ? '命中即止损' : '未启用';
  document.getElementById('sumFormulaSell').innerHTML =
    '公式止损：<b>['+fsName+']</b> '+fsState+' '+fsRatio+'% <span class="saved-badge saved">已保存</span>';
}

// P1-1: toggleEdit 加快照 guard + 编辑时隐藏编辑按钮
function toggleEdit(blockId) {
  const block = document.getElementById(blockId);
  // H2 修正：只在首次编辑时快照，防止二次编辑覆盖原始值
  block.querySelectorAll('.cfg-fields input, .cfg-fields select').forEach(el => {
    if (!el.dataset.original) {
      el.dataset.original = el.type === 'checkbox' ? String(el.checked) : el.value;
    }
  });
  block.classList.add('editing');
  block.querySelector('.cancel-btn').style.display = '';
  block.querySelector('.edit-btn').style.display = 'none';
  // P3-5: 编辑时标记徽章为"未保存"
  const badge = block.querySelector('.saved-badge');
  if (badge) { badge.textContent = '未保存'; badge.className = 'saved-badge unsaved'; }
}

// P1-1: cancelEdit 恢复原始值
function cancelEdit(blockId) {
  const block = document.getElementById(blockId);
  block.querySelectorAll('.cfg-fields input, .cfg-fields select').forEach(el => {
    if (el.dataset.original != null) {
      if (el.type === 'checkbox') el.checked = el.dataset.original === 'true';
      else el.value = el.dataset.original;
      delete el.dataset.original;
    }
  });
  block.classList.remove('editing');
  block.querySelector('.cancel-btn').style.display = 'none';
  block.querySelector('.edit-btn').style.display = '';
  // P3-5: 取消时恢复"已保存"
  const badge = block.querySelector('.saved-badge');
  if (badge) { badge.textContent = '已保存'; badge.className = 'saved-badge saved'; }
}

// P3-5: saveBlock 恢复"已保存"徽章
function saveBlock(blockId) {
  const block = document.getElementById(blockId);
  block.classList.remove('editing');
  block.querySelector('.cancel-btn').style.display = 'none';
  block.querySelector('.edit-btn').style.display = '';
  // 清除快照
  block.querySelectorAll('.cfg-fields input, .cfg-fields select').forEach(el => { delete el.dataset.original; });
  // P3-5: 保存后标记"已保存"
  const badge = block.querySelector('.saved-badge');
  if (badge) { badge.textContent = '已保存'; badge.className = 'saved-badge saved'; }
  saveAllConfig();
  refreshAllSummaries();
  addLog('全部配置已保存到浏览器', 'ok');
}

// P1-2: 恢复默认配置（调后端 API，不硬编码）
// P1-2 修正：后端 /api/config/defaults 可能不含 cond_time_stop / first_day 字段，
// 此时用 HTML 中 <input> 元素的 value 属性作为 fallback（即页面原始默认值）
async function resetDefaults() {
  if (!confirm('确定恢复所有配置为默认值？')) return;
  try {
    const resp = await fetch('/api/config/defaults');
    const result = await resp.json();
    if (!result.success) { showToast('获取默认配置失败', 'error'); return; }
    const cfg = result.config || {};
    // 后端 YAML 结构 → 前端字段映射
    const mapping = {
      cfgFormula: cfg.selection?.formula_name,
      cfgFormulaArg: cfg.selection?.formula_arg,
      cfgUniverse: cfg.selection?.universe?.type,
      cfgPeriod: cfg.backtest?.period === '5m' ? '5m' : cfg.backtest?.period === '1w' ? '1w' : '1d',
      cfgStart: cfg.time_range?.start,
      cfgEnd: cfg.time_range?.end,
      cfgCapital: cfg.backtest?.initial_capital,
      cfgCommission: cfg.backtest?.commission,
      cfgSlippage: cfg.backtest?.slippage,
      cfgMinBuy: cfg.backtest?.position_sizing?.min_buy_amount,
      cfgMaxBuy: cfg.backtest?.position_sizing?.max_buy_amount,
      cfgLotSize: cfg.backtest?.position_sizing?.lot_size,
      cfgMinLots: cfg.backtest?.position_sizing?.min_lots,
      cfgCostStopVal: cfg.stop_loss?.cost_stop?.threshold != null ? String(Math.abs(cfg.stop_loss.cost_stop.threshold * 100)) : null,
      cfgTrailingAct: cfg.stop_loss?.trailing_stop?.activation != null ? String(cfg.stop_loss.trailing_stop.activation * 100) : null,
      cfgTrailingDD: cfg.stop_loss?.trailing_stop?.drawdown != null ? String(cfg.stop_loss.trailing_stop.drawdown * 100) : null,
      cfgLadderVal: cfg.stop_loss?.ladder_tp?.levels?.map(l => Math.round(l.profit*100)+':'+Math.round(l.sell_ratio*100)).join(','),
      cfgTimeVal: cfg.stop_loss?.time_stop?.max_hold_days,
      cfgCondTimeDays: cfg.stop_loss?.cond_time_stop?.days,
      cfgCondTimeProfit: cfg.stop_loss?.cond_time_stop?.profit != null ? String(cfg.stop_loss.cond_time_stop.profit * 100) : null,
      cfgFirstDayTarget: cfg.stop_loss?.first_day?.target != null ? String(cfg.stop_loss.first_day.target * 100) : null,
    };
    const checkboxMapping = {
      cfgExcludeST: cfg.selection?.universe?.exclude_st,
      // P-v3.4: ETF 开关
      cfgIncludeEtf: cfg.selection?.universe?.include_etf,
      cfgEtfOnly: cfg.selection?.universe?.etf_only,
      cfgCostStopEn: cfg.stop_loss?.cost_stop?.enabled,
      cfgTrailingEn: cfg.stop_loss?.trailing_stop?.enabled,
      cfgLadderEn: cfg.stop_loss?.ladder_tp?.enabled,
      cfgTimeEn: cfg.stop_loss?.time_stop?.enabled,
      cfgCondTimeEn: cfg.stop_loss?.cond_time_stop?.enabled,
      cfgFirstDayEn: cfg.stop_loss?.first_day?.enabled,
    };
    for (const [id, val] of Object.entries(mapping)) {
      const el = document.getElementById(id);
      if (!el) continue;
      // P1-2 修正：API 不返回该字段时，用 HTML 元素的 defaultValue（页面原始值）做 fallback
      el.value = val != null ? String(val) : el.defaultValue;
    }
    for (const [id, val] of Object.entries(checkboxMapping)) {
      const el = document.getElementById(id);
      if (!el) continue;
      if (val != null) el.checked = val;
      // checkbox 无需 fallback，保持当前状态即可
    }
    localStorage.removeItem(STORAGE_KEY);
    // P-v3.4: 恢复默认时清空板块勾选
    try { localStorage.removeItem(SECTORS_KEY); } catch(e) {}
    _selectedSectors = [];
    if (_allSectors.length > 0) {
      document.querySelectorAll('#sectorGrid input[type=checkbox]').forEach(cb => cb.checked = false);
      updateSectorSummary();
      toggleUniverseDropdown();
    }
    refreshAllSummaries();
    showToast('已恢复默认配置', 'ok');
  } catch(e) {
    showToast('恢复默认失败: ' + e.message, 'error');
  }
}

// ====== System Log — P1-3: 加底部判断自动滚动 ======
// C2 修正：日志消息经 esc() 转义，防止服务器/用户输入含 HTML 注入
const logLines = [];
function addLog(msg, type) {
  const now = new Date().toLocaleTimeString();
  logLines.push('<span class="log-'+( type||'info')+'">['+now+'] '+esc(msg)+'</span>');
  if (logLines.length > 100) logLines.shift();
  const el = document.getElementById('logContent');
  if (el) {
    el.innerHTML = logLines.join('<br>');
    // L1 修正：只有用户在底部时才自动滚动，翻看历史时不打断
    const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
    if (atBottom) el.scrollTop = el.scrollHeight;
  }
}

let lastResult = null;
let charts = {};
let allTrades = [];  // P4-1: 保存全量交易数据用于筛选

function echartsInit(id) {
  const dom = document.getElementById(id);
  if (!dom) return null;
  if (charts[id]) charts[id].dispose();
  const c = echarts.init(dom);
  charts[id] = c;
  c.resize();
  return c;
}

function runPipeline() {
  // P1-5: 执行前校验
  const startEl = document.getElementById('cfgStart');
  const endEl = document.getElementById('cfgEnd');
  validateDate(startEl); validateDate(endEl);
  if (startEl.classList.contains('invalid') || endEl.classList.contains('invalid')) {
    showToast('日期格式错误，应为 YYYYMMDD', 'error'); return;
  }
  if (startEl.value >= endEl.value) {
    showToast('起始日期必须早于结束日期', 'error'); return;
  }
  const ladderEl = document.getElementById('cfgLadderVal');
  validateLadder(ladderEl);
  if (ladderEl.classList.contains('invalid')) {
    showToast('阶梯止盈格式错误', 'error'); return;
  }

  saveAllConfig();
  const btn = document.getElementById('btnRun');
  btn.disabled = true; btn.innerHTML = '运行中...';
  document.getElementById('progressBar').style.display = 'block';
  document.getElementById('statusDot').className = 'status-dot busy';
  document.getElementById('statusText').textContent = '运行中';
  document.getElementById('progressText').textContent = '';
  addLog('开始执行回测管线...', 'info');

  const getVal = id => document.getElementById(id).value;

  const pct = (id) => parseFloat(getVal(id)) / 100;
  // C1 修正：安全解析数字，空字符串/非法值返回 0（而非 NaN 直通后端）
  const safeFloat = (id) => { const v = parseFloat(getVal(id)); return isNaN(v) ? 0 : v; };
  const safeInt = (id) => { const v = parseInt(getVal(id)); return isNaN(v) ? 0 : v; };
  const ladderRaw = getVal('cfgLadderVal');
  const ladderParts = ladderRaw.split(',').map(s => {
    const [profit, ratio] = s.trim().split(':');
    return (parseFloat(profit)/100).toFixed(2) + ':' + (parseFloat(ratio)/100).toFixed(2);
  }).join(',');

  const config = {
    strategy_name: '',
    formula_name: getVal('cfgFormula'), formula_arg: getVal('cfgFormulaArg'),
    universe_type: getVal('cfgUniverse'), exclude_st: document.getElementById('cfgExcludeST').checked,
    // P-v3.4: ETF 开关 — 仅ETF 优先于 包含ETF (后端 selector 同款逻辑)
    include_etf: document.getElementById('cfgIncludeEtf').checked,
    etf_only: document.getElementById('cfgEtfOnly').checked,
    // P-v3.4: 行业板块代码 (逗号分隔字符串, 跟 ladder_levels 同风格)
    sectors: _selectedSectors.join(','),
    start_time: getVal('cfgStart'), end_time: getVal('cfgEnd'),
    period: getVal('cfgPeriod'), dividend_type: 1,
    initial_capital: safeFloat('cfgCapital'),
    commission: safeFloat('cfgCommission'),
    slippage: safeFloat('cfgSlippage'),
    max_positions: 999,
    max_position_pct: 1.0,
    min_buy_amount: safeFloat('cfgMinBuy'),
    max_buy_amount: safeFloat('cfgMaxBuy'),
    lot_size: safeInt('cfgLotSize'),
    min_lots: safeInt('cfgMinLots'),
    // H3 修正：成本止损阈值必须为负值，用 -Math.abs 确保即使用户输入正值也不会变止盈
    cost_stop_enabled: document.getElementById('cfgCostStopEn').checked, cost_stop_threshold: -Math.abs(pct('cfgCostStopVal')),
    trailing_enabled: document.getElementById('cfgTrailingEn').checked, trailing_activation: pct('cfgTrailingAct'), trailing_drawdown: pct('cfgTrailingDD'),
    ladder_enabled: document.getElementById('cfgLadderEn').checked, ladder_levels: ladderParts,
    time_enabled: document.getElementById('cfgTimeEn').checked, max_hold_days: safeInt('cfgTimeVal'),
    cond_time_enabled: document.getElementById('cfgCondTimeEn').checked,
    cond_time_days: safeInt('cfgCondTimeDays'),
    cond_time_profit: safeFloat('cfgCondTimeProfit') / 100,
    first_day_enabled: document.getElementById('cfgFirstDayEn').checked,
    first_day_target: safeFloat('cfgFirstDayTarget') / 100,
    // P-v3.4: 公式卖出 — 钳位 [0, 100] 防呆
    formula_sell_enabled: document.getElementById('cfgFormulaSellEn').checked,
    formula_sell_name: document.getElementById('cfgFormulaSellName').value.trim(),
    formula_sell_ratio: Math.max(0, Math.min(100, safeFloat('cfgFormulaSellRatio'))) / 100,
    benchmark_indices: 'shanghai,chuangyeban,kechuang50,zhongzhengA500',
  };

  addLog('配置: '+config.formula_name+' '+config.start_time+'~'+config.end_time, 'info');

  let pollActive = true;
  let poll = setInterval(async () => {
    if (!pollActive) return;
    try {
      const r = await fetch('/api/status'); const s = await r.json();
      if (!pollActive) return;
      document.getElementById('progressFill').style.width = s.progress + '%';
      document.getElementById('progressText').textContent = s.step;
      document.getElementById('statusText').textContent = s.step;
      if (s.step) addLog(s.step + ' ('+s.progress+'%)', 'info');
      if (!s.running && s.progress > 0 && s.has_result) { pollActive = false; clearInterval(poll); }
    } catch(e) {}
  }, 800);

  // H8 修正：加 AbortController + 5min 超时，防止网络挂死永远转圈
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 300000);
  fetch('/api/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(config), signal: controller.signal })
    .then(r => r.json())
    .then(data => {
      clearTimeout(timeout);
      pollActive = false; clearInterval(poll);
      document.getElementById('progressBar').style.display = 'none';
      document.getElementById('progressText').textContent = '';
      document.getElementById('statusDot').className = 'status-dot on';
      document.getElementById('statusText').textContent = '就绪';
      btn.disabled = false; btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg> 执行回测';
      if (!data.success) {
        addLog('失败: ' + (data.error || '未知错误'), 'error');
        showToast('回测失败: ' + (data.error || '未知错误'), 'error');  // P1-4: alert → toast
        lastResult = null;
        document.querySelectorAll('.kpi-value').forEach(el => el.textContent = '--');
        return;
      }
      addLog('回测完成: '+data.trade_count+'笔交易', 'ok');
      lastResult = data;
      renderAllCharts(data);
      checkEngineVersion(data);
    })
    .catch(e => {
      clearTimeout(timeout);
      pollActive = false; clearInterval(poll); btn.disabled = false;
      btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg> 执行回测';
      document.getElementById('statusDot').className = 'status-dot on';
      document.getElementById('statusText').textContent = '错误';
      const msg = e.name === 'AbortError' ? '请求超时（5分钟），请缩小回测区间' : e.message;
      addLog('网络错误: '+msg, 'error');
      showToast('网络错误: '+msg, 'error');  // P1-4: 加 toast
    });
}

function renderAllCharts(data) {
  const c = getColors();
  const m = data.metrics || {};

  // KPI cards
  const setKpi = (id, val, fmt) => {
    const el = document.getElementById(id);
    el.textContent = fmt(val); el.className = 'kpi-value';
    if (typeof val === 'number') { if (val > 0) el.classList.add('pos'); else if (val < 0) el.classList.add('neg'); }
  };
  setKpi('kpiCumRet', m.cumulative_return, v => v != null ? (v*100).toFixed(2)+'%' : '--');
  setKpi('kpiAnnRet', m.annualized_return, v => v != null ? (v*100).toFixed(2)+'%' : '--');
  setKpi('kpiMaxDD', m.max_drawdown, v => v != null ? (v*100).toFixed(2)+'%' : '--');
  setKpi('kpiSharpe', m.sharpe_ratio, v => v != null ? v.toFixed(2) : '--');
  setKpi('kpiWinRate', m.win_rate, v => v != null ? (v*100).toFixed(1)+'%' : '--');
  setKpi('kpiPLR', m.profit_loss_ratio, v => v != null ? v.toFixed(2) : '--');
  setKpi('kpiTrades', m.total_trades, v => v != null ? v : '--');
  setKpi('kpiCalmar', m.calmar_ratio, v => v != null ? v.toFixed(2) : '--');
  setKpi('kpiProfitF', m.profit_factor, v => v != null ? v.toFixed(2) : '--');
  setKpi('kpiBest', m.max_single_gain, v => v != null ? (v*100).toFixed(2)+'%' : '--');

  // P2-3: 更新图表标题
  const formula = data.formula_name || (lastResult && lastResult.formula_name) || '';
  const dateRange = (document.getElementById('cfgStart').value||'') + '~' + (document.getElementById('cfgEnd').value||'');
  const hdr = document.getElementById('equityChartHeader');
  if (hdr && formula) hdr.textContent = '权益曲线 & 基准对比 — ' + formula + ' ' + dateRange;

  // === ECharts: Equity curve ===
  if (data.equity && data.equity.length > 0) {
    const chart = echartsInit('chartEquity');
    const dates = data.equity.map(r => r.date.slice(0,10));
    // H1 修正：用 ?? 1 替代 || 1，当 equity 为 0 时不应 fallback
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
    // H4 修正：基准线从首个有效数据点重归一化，消除与策略起始日的视觉偏移
    if (data.benchmarks) {
      const bmNames = { shanghai: '上证', chuangyeban: '创业板', kechuang50: '科创50', zhongzhengA500: '中证A500' };
      const bmColors = [c.warn, c.accent2, c.accent, c.down];
      let ci = 0;
      for (const [name, bm] of Object.entries(data.benchmarks)) {
        if (!bm || !bm.length) continue;
        // 构建 date → index_close 映射
        const bmMap = {};
        bm.forEach(r => {
          const d = String(r.date || '').slice(0, 10);
          if (r.index_close != null) bmMap[d] = r.index_close;
        });
        // 找基准在策略日期范围内的首个有效点，以此为归一化基准
        let bmStart = null;
        for (const d of dates) {
          if (bmMap[d] != null) { bmStart = bmMap[d]; break; }
        }
        if (bmStart == null) continue;
        // 从首点重归一化：(close / close_首点 - 1) * 100
        const bmVals = dates.map(d => bmMap[d] != null ? (bmMap[d] / bmStart - 1) * 100 : null);
        if (bmVals.every(v => v === null)) continue;
        series.push({ name: bmNames[name]||name, type: 'line', data: bmVals,
          lineStyle: { color: bmColors[ci % 4], width: 1, type: 'dashed' }, symbol: 'none',
          connectNulls: true });  // connectNulls: true 跳过 null 段后连线
        ci++;
      }
    }
    chart.setOption({
      tooltip: { trigger: 'axis', formatter: function(params) {
        let s = params[0].axisValue + '<br/>';
        params.forEach(p => {
          s += '<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:'+p.color+';margin-right:5px"></span>';
          s += p.seriesName + ': <b>' + (p.value != null ? p.value.toFixed(2)+'%' : '-') + '</b><br/>';
        });
        return s;
      }},
      legend: { top: 0, textStyle: { color: c.text, fontSize: 10 } },
      // P2-1: 加 dataZoom 缩放
      dataZoom: [
        { type: 'inside', xAxisIndex: 0, start: 0, end: 100 },
        { type: 'slider', xAxisIndex: 0, bottom: 10, height: 20,
          borderColor: c.border, fillerColor: hexToRgba(c.accent, 0.13),
          textStyle: { color: c.text2, fontSize: 9 } },
      ],
      grid: { left: 60, right: 70, top: 35, bottom: 60 },
      xAxis: { type: 'category', data: dates, axisLine: { lineStyle: { color: c.border } }, axisLabel: { color: c.text2, fontSize: 9 } },
      yAxis: [
        { type: 'value', name: '累计收益 %', nameTextStyle: { color: c.text2, fontSize: 10 },
          axisLabel: { color: c.text2, fontSize: 9, formatter: '{value}%' }, splitLine: { lineStyle: { color: c.border } } },
        { type: 'value', name: '回撤 %', nameTextStyle: { color: c.text2, fontSize: 10 },
          axisLabel: { color: c.text2, fontSize: 9, formatter: '{value}%' }, splitLine: { show: false } },
      ],
      // P4-3: 图表导出工具
      toolbox: { right: 10, top: 0, feature: {
        saveAsImage: { title: '保存图片', pixelRatio: 2 },
        dataZoom: { title: { zoom: '区域缩放', back: '还原' } },
        restore: { title: '还原' },
      }, iconStyle: { borderColor: c.text2 } },
      series: series,
    }, true);
  }

  // P0-1 修正：月度收益改用复合算法
  if (data.equity && data.equity.length > 1) {
    const chart = echartsInit('chartMonthly');
    const monthly = {};
    for (let i = 1; i < data.equity.length; i++) {
      if (data.equity[i-1].equity > 0) {
        const d = new Date(data.equity[i].date);
        const k = d.getFullYear()+'年'+String(d.getMonth()+1).padStart(2,'0')+'月';
        const r = (data.equity[i].equity - data.equity[i-1].equity) / data.equity[i-1].equity;
        if (!monthly[k]) monthly[k] = []; monthly[k].push(r);
      }
    }
    const months = Object.keys(monthly).sort();
    // P0-1: 复合收益 (1+r1)(1+r2)...(1+rn) - 1，替代简单加总
    const mData = months.map(m => {
      const product = monthly[m].reduce((acc, r) => acc * (1 + r), 1);
      return { name: m, value: ((product - 1) * 100).toFixed(2) };
    });
    chart.setOption({
      tooltip: { trigger: 'axis', formatter: p => p[0].name+'<br/>月收益: <b>'+p[0].value+'%</b>' },
      grid: { left: 50, right: 20, top: 10, bottom: 60 },
      xAxis: { type: 'category', data: months, axisLabel: { color: c.text2, fontSize: 9, rotate: 45 }, axisLine: { lineStyle: { color: c.border } } },
      yAxis: { type: 'value', name: '月收益 %', nameTextStyle: { color: c.text2, fontSize: 10 }, axisLabel: { color: c.text2, fontSize: 9, formatter: '{value}%' }, splitLine: { lineStyle: { color: c.border } } },
      series: [{ type: 'bar', data: mData.map(d => ({ value: parseFloat(d.value), itemStyle: { color: parseFloat(d.value) >= 0 ? c.up : c.down } })) }],
    }, true);
  }

  // === Trade Analysis Charts — P2-2: 颜色跟随主题 ===
  if (data.trades && data.trades.length > 0) {
    // P&L 分布 — P2-2: 用 hexToRgba 动态生成颜色
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
      tooltip: { trigger: 'axis', formatter: p => p[0].name+'<br/>交易笔数: <b>'+p[0].value+'</b>' },
      grid: { left: 50, right: 20, top: 10, bottom: 40 },
      xAxis: { type: 'category', data: rangeCount.map(d=>d.name), axisLabel: { color: c.text2, fontSize: 9, rotate: 30 }, axisLine: { lineStyle: { color: c.border } } },
      yAxis: { type: 'value', name: '笔数', nameTextStyle: { color: c.text2, fontSize: 10 }, axisLabel: { color: c.text2, fontSize: 9 }, splitLine: { lineStyle: { color: c.border } } },
      series: [{ type: 'bar', data: rangeCount }],
    }, true);

    // 退出原因分布 — P2-2: 饼图颜色跟随主题
    const chart2 = echartsInit('chartExit');
    const reasonMap = { '成本止损': '成本止损', '移动止损': '移动止损', '移动止盈': '移动止盈', '阶梯止盈': '阶梯止盈', '时间止损': '时间止损', '时间止盈': '时间止盈', cond_time_stop: '条件时间止盈', '换股卖出': '换股卖出', '首日未达标': '首日未达标', 'formula_sell': '公式止损', '退市': '退市' };
    const reasonCount = {};
    data.trades.forEach(t => {
      const reasons = (t.exit_reason || '换股卖出').split('+');
      reasons.forEach(r => {
        const label = reasonMap[r] || r;
        reasonCount[label] = (reasonCount[label] || 0) + 1;
      });
    });
    const pieData = Object.entries(reasonCount).map(([name, value]) => ({ name, value }));
    // P-v3.4 修复: 每种 reason 固定颜色 (不再按 index 循环), 移动止损用橙色避免撞背景
    const reasonColorMap = {
      '成本止损': c.up,        // 红 (亏损)
      '移动止损': c.warn,      // 橙 (亏损) — 原来撞背景, 改醒目橙
      '移动止盈': c.down,      // 绿 (盈利)
      '阶梯止盈': c.accent,    // 蓝 (盈利)
      '时间止损': c.accent2,   // 紫 (亏损)
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
      series: [{ type: 'pie', radius: ['35%','60%'], center: ['50%','45%'], data: pieData.map(d => ({
        ...d,
        itemStyle: { color: reasonColorMap[d.name] || c.text2, borderColor: c.bg, borderWidth: 2 },
      })),
        label: { color: c.text, fontSize: 10, formatter: '{b}\n{d}%' } }],
    }, true);

    // 持仓天数分布
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
      tooltip: { trigger: 'axis', formatter: p => '持仓'+p[0].name+'<br/>笔数: <b>'+p[0].value+'</b>' },
      grid: { left: 50, right: 20, top: 10, bottom: 30 },
      xAxis: { type: 'category', data: holdData.map(d=>d.name), axisLabel: { color: c.text2, fontSize: 9 }, axisLine: { lineStyle: { color: c.border } } },
      yAxis: { type: 'value', name: '笔数', nameTextStyle: { color: c.text2, fontSize: 10 }, axisLabel: { color: c.text2, fontSize: 9 }, splitLine: { lineStyle: { color: c.border } } },
      series: [{ type: 'bar', data: holdData, itemStyle: { color: c.accent } }],
    }, true);

    // P4-1: 保存全量交易数据 + 渲染交易表
    allTrades = data.trades;
    renderTradeTable(allTrades);

    // P4-1: 填充退出原因筛选下拉
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
    // M8 修正：0 笔交易时清空交易表和全量数据，避免残留上次结果
    allTrades = [];
    const tbody = document.getElementById('tradeTableBody');
    if (tbody) tbody.innerHTML = '';
    document.getElementById('tradeFiltered').textContent = '显示 0 / 0 笔';
  }

  if (data.stop_config_summary) {
    document.getElementById('summaryBox').style.display = '';
    document.getElementById('summaryContent').textContent = data.stop_config_summary;
  }
}

// P4-1: 交易表渲染（支持筛选）
const reasonDetail = {
  '成本止损': '成本止损 — 亏损触及止损线，全仓卖出',
  '移动止损': '移动止损 — 从最高点回撤触及阈值，亏损清仓',
  '移动止盈': '移动止盈 — 从最高点回撤触及阈值，盈利清仓',
  '阶梯止盈': '阶梯止盈 — 盈利达到目标档位，分批卖出',
  '时间止损': '时间止损 — 持仓天数达到上限，亏损清仓',
  '时间止盈': '时间止盈 — 持仓天数达到上限，盈利清仓',
  'cond_time_stop': '条件时间止盈 — 持仓N天后盈利达标，全仓卖出',
  '首日未达标': '首日未达标 — 买入次日最高价涨幅未达目标，收盘强制卖出',
  '换股卖出': '换股卖出 — 同一股票出现新买入信号，替换旧持仓',
  // P-v3.4: 公式卖出
  'formula_sell': '公式止损 — TDX 公式信号命中，按比例止损（不看盈亏，最高优先级）',
};
const reasonShortMap = { '成本止损': '成本止损', '移动止损': '移动止损', '移动止盈': '移动止盈', '阶梯止盈': '阶梯止盈', '时间止损': '时间止损', '时间止盈': '时间止盈', cond_time_stop: '条件时间止盈', '换股卖出': '换股卖出', '首日未达标': '首日未达标', 'formula_sell': '公式止损' };

function fmtReasonShort(r) {
  if (!r) return '—';
  const parts = r.split('+').map(s => reasonShortMap[s] || s);
  return parts.join('+');
}

function renderTradeTable(trades) {
  document.getElementById('tradeTableBox').style.display = '';
  const minBuy = document.getElementById('cfgMinBuy').value || 2000;
  const maxBuy = document.getElementById('cfgMaxBuy').value || 10000;
  const lot = document.getElementById('cfgLotSize').value || 100;
  document.getElementById('tradeCount').textContent =
    '(本次回测: '+trades.length+' 笔 | 每笔'+minBuy+'~'+maxBuy+'元, '+lot+'股/手)';
  const tbody = document.getElementById('tradeTableBody');
  const totalTrades = trades.length;
  // C2 修正：所有服务器/用户数据经 esc() 转义后再插入 innerHTML
  tbody.innerHTML = trades.slice().reverse().map((t, i) => {
    const pnl = (t.profit_pct || t.return || 0) * 100;
    const cls = pnl > 0 ? 'td-up' : pnl < 0 ? 'td-down' : '';
    const eDate = esc(String(t.entry_date||'').slice(0,10));
    const xDate = esc(String(t.exit_date||'').slice(0,10));
    const name = esc(t.stock_name || t.stock_code || '');
    const code = esc(t.stock_code || '');
    const shares = t.shares || 0;
    const ep = t.entry_price || 0;
    const xp = t.exit_price || 0;
    const holdDays = t.hold_days != null ? t.hold_days : '';
    const reasonShort = esc(fmtReasonShort(t.exit_reason));
    const reasonFull = esc((t.exit_reason||'').split('+').map(s => reasonDetail[s]||s).join('；'));
    return '<tr>'+
      '<td style="color:var(--text2);font-size:10px">'+(totalTrades - i)+'</td>'+
      '<td style="font-family:var(--mono);font-size:10px">'+code+'</td>'+
      '<td title="'+code+'">'+name+'</td>'+
      '<td>'+eDate+'</td>'+
      '<td>'+ep+'</td>'+
      '<td>'+shares+' 股</td>'+
      '<td>'+xDate+'</td>'+
      '<td>'+xp+'</td>'+
      '<td>'+shares+' 股</td>'+
      '<td>'+holdDays+'</td>'+
      '<td class="'+cls+'">'+pnl.toFixed(2)+'%</td>'+
      '<td style="font-size:10px;max-width:120px" title="'+reasonFull+'">'+reasonShort+'</td></tr>';
  }).join('');
  document.getElementById('tradeFiltered').textContent = '显示 ' + trades.length + ' / ' + allTrades.length + ' 笔';
  // P-v3.4: 动态补 "退出原因" 下拉选项 — 让 formula_sell 等新 reason 可被筛出
  const reasonSelect = document.getElementById('tradeReason');
  if (reasonSelect) {
    const existing = Array.from(reasonSelect.options).map(o => o.value);
    const seenReasons = new Set();
    allTrades.forEach(t => {
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

// P4-1: 筛选逻辑
function filterTrades() {
  if (!allTrades.length) return;
  const search = (document.getElementById('tradeSearch').value || '').toLowerCase();
  const filter = document.getElementById('tradeFilter').value;
  const reason = document.getElementById('tradeReason').value;
  const reasonMap = { '成本止损': '成本止损', '移动止损': '移动止损', '移动止盈': '移动止盈', '阶梯止盈': '阶梯止盈', '时间止损': '时间止损', '时间止盈': '时间止盈', cond_time_stop: '条件时间止盈', '换股卖出': '换股卖出', '首日未达标': '首日未达标', 'formula_sell': '公式止损' };
  const filtered = allTrades.filter(t => {
    const pnl = (t.profit_pct || t.return || 0) * 100;
    if (search) {
      const code = (t.stock_code||'').toLowerCase();
      const name = (t.stock_name||'').toLowerCase();
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
  renderTradeTable(filtered);
}

// ====== Init ======
document.getElementById('statusDot').className = 'status-dot on';
loadConfig();
loadSectors();   // P-v3.4: 加载行业板块列表
addLog('前端就绪，等待执行回测', 'info');

// 加载历史回测列表
fetch('/api/results').then(r => r.json()).then(list => {
  if (list && list.length > 0) {
    document.getElementById('historyCount').textContent = '('+list.length+'条)';
    const sel = document.getElementById('historySelect');
    sel.innerHTML = '<option value="">-- 选择历史回测 --</option>';
    list.forEach(item => {
      const o = document.createElement('option');
      o.value = item.id;
      const cumRet = (item.cumulative_return != null && !isNaN(item.cumulative_return))
        ? (item.cumulative_return*100).toFixed(1)+'%' : '--';
      o.textContent = item.time + ' | '+item.formula+' '+item.date_range+' | '+item.trade_count+'笔 '+cumRet;
      sel.appendChild(o);
    });
  }
}).catch(() => {});

function loadHistory(id) {
  if (!id) return;
  addLog('加载历史回测: '+id, 'info');
  fetch('/api/results/'+id).then(r => r.json()).then(result => {
    const data = result.data || result;
    if (data.success || data.trade_count != null) {
      lastResult = data;
      document.getElementById('tradeTableBox').style.display = '';
      document.getElementById('tradeCount').textContent = '(历史 '+data.trade_count+' 笔)';
      renderAllCharts(data);
      checkEngineVersion(data);
      addLog('已加载历史回测 ('+data.trade_count+'笔)', 'ok');
    }
  }).catch(e => addLog('加载失败: '+e.message, 'error'));
}

// M6 修正：resize 加 200ms 防抖，避免拖拽窗口时频繁重绘
let resizeTimer;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => Object.values(charts).forEach(c => c.resize()), 200);
});
