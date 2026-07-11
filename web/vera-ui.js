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

// ====== 2026-07-05: 回测超时/断网后的兜底探测参数 ======
const RECOVER = {
  MAX_RETRY: 5,            // 最多探测次数
  INTERVAL_MS: 2000,       // 每次间隔（ms）
  MAX_WAIT_MS: 10000,      // = MAX_RETRY × INTERVAL_MS, 用户可见的"自动恢复中..."最长等待
};
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

// ====== 2026-07-09: 阶梯止盈小数精度 — 去浮点尾巴 ======
// 替代旧的 toFixed(2)/Math.round 强制取整（会把 6.5% → 7%、2.7% → 3%）。
// toPrecision(12) 保留足够精度同时干掉浮点尾巴，如：
//   0.011000000000000002 → 0.011,  6.500000000000001 → 6.5,  0.06*100 → 6
// 用于阶梯止盈的写入路径与回填路径，让档位盈利/卖出比例支持任意小数百分比。
function cleanNum(x) {
  const n = Number(x);
  return isNaN(n) ? 0 : parseFloat(n.toPrecision(12));
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

// 2026-07-05: radio 组定义 — saveAllConfig 收集 / loadConfig 恢复 / applyConfig 应用
//   name:   radio 的 name 属性 (querySelector key)
//   allow:  localStorage 持久化的 key (也是后端 YAML stop_loss 字段名)
//   新增 radio 只需在下面加一条即可, 无需改 applyConfig/loadConfig/saveAllConfig
const RADIO_CONFIGS = [
  { name: 'cfgPriority', allow: 'cfgPriority', fallback: 'trailing_first' },
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
      // 2026-07-05: radio 组 — 用 saved[rc.allow] 找 checked 的 input
      RADIO_CONFIGS.forEach(rc => {
        if (!(rc.allow in saved)) return;
        const target = document.querySelector(`input[name="${rc.name}"][value="${saved[rc.allow]}"]`);
        if (target) {
          target.checked = true;
        } else {
          // L8: 找不到目标 radio 时警告 (旧 localStorage 存了已废弃的值)
          console.warn('[loadConfig] radio', rc.name, '找不到 value=', saved[rc.allow], '— 已忽略, 保持 HTML 默认');
        }
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
  // 2026-07-05: radio 组 — 存当前选中的 value
  RADIO_CONFIGS.forEach(rc => {
    const checked = document.querySelector(`input[name="${rc.name}"]:checked`);
    if (checked) data[rc.allow] = checked.value;
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
  // B-14: 构造 128 个 item — checked 态高亮 + 代码简写（去 .SH 后缀省空间）
  grid.innerHTML = _allSectors.map(s => {
    const checked = _selectedSectors.includes(s.code);
    const codeShort = String(s.code).replace(/\.\w+$/, '');
    return '<div class="sector-item' + (checked ? ' checked' : '') + '" data-name="' + esc(s.name) + '" data-code="' + esc(s.code) + '">' +
      '<input type="checkbox" value="' + esc(s.code) + '" ' + (checked ? 'checked' : '') + ' onchange="onSectorToggle(this)">' +
      '<label title="' + esc(s.code) + ' ' + esc(s.name) + '"><span class="name">' + esc(s.name) + '</span> <span class="code">' + esc(codeShort) + '</span></label>' +
      '</div>';
  }).join('');
}

function filterSectors() {
  const kw = (document.getElementById('sectorSearch').value || '').trim().toLowerCase();
  let matched = 0;
  document.querySelectorAll('.sector-item').forEach(item => {
    const name = (item.dataset.name || '').toLowerCase();
    const code = (item.dataset.code || '').toLowerCase();
    const hit = !kw || name.includes(kw) || code.includes(kw);
    item.classList.toggle('hidden', !hit);
    if (hit) matched++;
    // B-14: 名称匹配字高亮
    const nameEl = item.querySelector('.name');
    if (nameEl) {
      const orig = item.dataset.name || '';
      const idx = kw && name.includes(kw) ? orig.toLowerCase().indexOf(kw) : -1;
      if (idx >= 0) {
        nameEl.innerHTML = esc(orig.slice(0, idx)) + '<mark>' + esc(orig.slice(idx, idx + kw.length)) + '</mark>' + esc(orig.slice(idx + kw.length));
      } else {
        nameEl.textContent = orig;
      }
    }
  });
  const info = document.getElementById('sectorMatchInfo');
  if (info) info.textContent = kw ? '匹配 ' + matched + ' / ' + _allSectors.length : '';
}

function onSectorToggle(cb) {
  const code = cb.value;
  if (cb.checked) {
    if (!_selectedSectors.includes(code)) _selectedSectors.push(code);
  } else {
    _selectedSectors = _selectedSectors.filter(c => c !== code);
  }
  try { localStorage.setItem(SECTORS_KEY, JSON.stringify(_selectedSectors)); } catch(e) {}
  const item = cb.closest('.sector-item');
  if (item) item.classList.toggle('checked', cb.checked);
  updateSectorSummary();
  toggleUniverseDropdown();
}

function clearSectors() {
  _selectedSectors = [];
  try { localStorage.setItem(SECTORS_KEY, JSON.stringify(_selectedSectors)); } catch(e) {}
  document.querySelectorAll('#sectorGrid input[type=checkbox]').forEach(cb => {
    cb.checked = false;
    const item = cb.closest('.sector-item');
    if (item) item.classList.remove('checked');
  });
  updateSectorSummary();
  toggleUniverseDropdown();
}

function updateSectorSummary() {
  const box = document.getElementById('sectorSelected');
  if (_selectedSectors.length === 0) {
    box.innerHTML = '';
  } else {
    // B-14: 已选标签（计数走 header 徽章，这里不再重复显示个数）
    box.innerHTML = _selectedSectors.map(code => {
      const s = _allSectors.find(x => x.code === code);
      const name = s ? s.name : code;
      return '<span class="sector-tag" onclick="removeSector(\'' + code + '\')" title="点击移除">' +
        esc(name) + ' <span class="x">×</span></span>';
    }).join('');
  }
  updateSectorCount();
}

function removeSector(code) {
  _selectedSectors = _selectedSectors.filter(c => c !== code);
  try { localStorage.setItem(SECTORS_KEY, JSON.stringify(_selectedSectors)); } catch(e) {}
  // 同步取消 checkbox + checked 态
  const cb = document.querySelector('#sectorGrid input[value="' + code + '"]');
  if (cb) {
    cb.checked = false;
    const item = cb.closest('.sector-item');
    if (item) item.classList.remove('checked');
  }
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

// B-14: 折叠/展开板块面板（localStorage 记忆用户偏好）
function toggleSectorPanel() {
  const sec = document.querySelector('.sector-section');
  if (!sec) return;
  sec.classList.toggle('collapsed');
  try { localStorage.setItem('vera_sector_collapsed', sec.classList.contains('collapsed') ? '1' : '0'); } catch(e) {}
}
// B-14: 同步 header 计数徽章（已选 / 总数，有选中时高亮）
function updateSectorCount() {
  const badge = document.getElementById('sectorCountBadge');
  if (!badge) return;
  const n = _selectedSectors.length;
  const total = _allSectors.length || 128;
  badge.textContent = n + ' / ' + total;
  badge.classList.toggle('has', n > 0);
}

// C2 修正：refreshAllSummaries — 所有用户输入经 esc() 转义后再插入 innerHTML
function refreshAllSummaries() {
  const cs = esc(document.getElementById('cfgCostStopVal').value);
  document.getElementById('sumCostStop').innerHTML = '成本止损：亏损达到 <b>'+cs+'%</b> 全仓卖出 <span class="saved-badge saved">已保存</span>';
  const ta = esc(document.getElementById('cfgTrailingAct').value);
  const td = esc(document.getElementById('cfgTrailingDD').value);
  // 2026-07-05 v3: trailing 语义改为"盘中 Low 触及回撤线即按回撤线价成交"
  document.getElementById('sumTrailing').innerHTML = '移动止损：盈利 <b>'+ta+'%</b> 激活后，盘中 Low 触及回撤 <b>'+td+'%</b> 线即按回撤线价全仓卖出 <span class="saved-badge saved">已保存</span>';
  const lv = esc(document.getElementById('cfgLadderVal').value.replace(/,/g, ', '));
  document.getElementById('sumLadder').innerHTML = '阶梯止盈：<b>'+lv+'</b> <span class="saved-badge saved">已保存</span>';
  // 2026-07-05 v3: 优先级独立摘要 (三档)
  const priChecked = document.querySelector('input[name="cfgPriority"]:checked');
  const priLabelMap = {
    stop_first: '止损优先 (历史默认)',
    ladder_tp_first: '阶梯止盈优先',
    trailing_first: '移动止损优先 (盘中锁利)',
  };
  const priLabel = (priChecked && priLabelMap[priChecked.value]) || '移动止损优先 (盘中锁利)';
  const sumPriEl = document.getElementById('sumPriority');
  if (sumPriEl) sumPriEl.innerHTML = '优先级：'+priLabel+' <span class="saved-badge saved">已保存</span>';
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

// 2026-07-10: 抽取前端表单 → StrategyConfig payload（runPipeline 与 saveConfigToFile 共用）。
// 保证"跑回测"和"保存到文件"发同一份 payload（含 sectors），杜绝两份构建逻辑漂移。
function collectConfigFromForm() {
  const getVal = id => document.getElementById(id).value;
  const pct = (id) => parseFloat(getVal(id)) / 100;
  const safeFloat = (id) => { const v = parseFloat(getVal(id)); return isNaN(v) ? 0 : v; };
  const safeInt = (id) => { const v = parseInt(getVal(id)); return isNaN(v) ? 0 : v; };
  const ladderRaw = getVal('cfgLadderVal');
  const ladderParts = ladderRaw.split(',').map(s => {
    const [profit, ratio] = s.trim().split(':');
    return cleanNum(parseFloat(profit)/100) + ':' + cleanNum(parseFloat(ratio)/100);
  }).join(',');
  return {
    strategy_name: '',
    formula_name: getVal('cfgFormula'), formula_arg: getVal('cfgFormulaArg'),
    universe_type: getVal('cfgUniverse'), exclude_st: document.getElementById('cfgExcludeST').checked,
    include_etf: document.getElementById('cfgIncludeEtf').checked,
    etf_only: document.getElementById('cfgEtfOnly').checked,
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
    cost_stop_enabled: document.getElementById('cfgCostStopEn').checked, cost_stop_threshold: -Math.abs(pct('cfgCostStopVal')),
    trailing_enabled: document.getElementById('cfgTrailingEn').checked, trailing_activation: pct('cfgTrailingAct'), trailing_drawdown: pct('cfgTrailingDD'),
    ladder_enabled: document.getElementById('cfgLadderEn').checked, ladder_levels: ladderParts,
    time_enabled: document.getElementById('cfgTimeEn').checked, max_hold_days: safeInt('cfgTimeVal'),
    cond_time_enabled: document.getElementById('cfgCondTimeEn').checked,
    cond_time_days: safeInt('cfgCondTimeDays'),
    cond_time_profit: safeFloat('cfgCondTimeProfit') / 100,
    first_day_enabled: document.getElementById('cfgFirstDayEn').checked,
    first_day_target: safeFloat('cfgFirstDayTarget') / 100,
    priority: (document.querySelector('input[name="cfgPriority"]:checked') || {}).value || 'ladder_tp_first',
    formula_sell_enabled: document.getElementById('cfgFormulaSellEn').checked,
    formula_sell_name: document.getElementById('cfgFormulaSellName').value.trim(),
    formula_sell_ratio: Math.max(0, Math.min(100, safeFloat('cfgFormulaSellRatio'))) / 100,
    benchmark_indices: 'shanghai,chuangyeban,kechuang50,zhongzhengA500',
  };
}

// 2026-07-10: 后端完整 config dict → 前端表单（resetDefaults 与 loadConfigFromFile 共用）。
// 抽取自 resetDefaults 的 mapping，并补两处 gap：sectors 回填、formula_sell 三字段。
// 消除"恢复默认"与"从文件加载"两份 mapping 漂移。
function applyConfigDict(cfg) {
  cfg = cfg || {};
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
    cfgCostStopVal: cfg.stop_loss?.cost_stop?.threshold != null ? String(cleanNum(Math.abs(cfg.stop_loss.cost_stop.threshold * 100))) : null,
    cfgTrailingAct: cfg.stop_loss?.trailing_stop?.activation != null ? String(cleanNum(cfg.stop_loss.trailing_stop.activation * 100)) : null,
    cfgTrailingDD: cfg.stop_loss?.trailing_stop?.drawdown != null ? String(cleanNum(cfg.stop_loss.trailing_stop.drawdown * 100)) : null,
    cfgLadderVal: cfg.stop_loss?.ladder_tp?.levels?.map(l => cleanNum(l.profit*100)+':'+cleanNum(l.sell_ratio*100)).join(','),
    cfgTimeVal: cfg.stop_loss?.time_stop?.max_hold_days,
    cfgCondTimeDays: cfg.stop_loss?.cond_time_stop?.days,
    cfgCondTimeProfit: cfg.stop_loss?.cond_time_stop?.profit != null ? String(cleanNum(cfg.stop_loss.cond_time_stop.profit * 100)) : null,
    cfgFirstDayTarget: cfg.stop_loss?.first_day?.target != null ? String(cleanNum(cfg.stop_loss.first_day.target * 100)) : null,
    // gap B 修复：formula_sell name/ratio（原 resetDefaults 漏，加载/恢复时公式卖出 UI 不归位）
    cfgFormulaSellName: cfg.stop_loss?.formula_sell?.formula_name != null ? String(cfg.stop_loss.formula_sell.formula_name) : null,
    cfgFormulaSellRatio: cfg.stop_loss?.formula_sell?.sell_ratio != null ? String(cleanNum(cfg.stop_loss.formula_sell.sell_ratio * 100)) : null,
    cfgPriority: cfg.stop_loss?.priority || 'ladder_tp_first',
  };
  const checkboxMapping = {
    cfgExcludeST: cfg.selection?.universe?.exclude_st,
    cfgIncludeEtf: cfg.selection?.universe?.include_etf,
    cfgEtfOnly: cfg.selection?.universe?.etf_only,
    cfgCostStopEn: cfg.stop_loss?.cost_stop?.enabled,
    cfgTrailingEn: cfg.stop_loss?.trailing_stop?.enabled,
    cfgLadderEn: cfg.stop_loss?.ladder_tp?.enabled,
    cfgTimeEn: cfg.stop_loss?.time_stop?.enabled,
    cfgCondTimeEn: cfg.stop_loss?.cond_time_stop?.enabled,
    cfgFirstDayEn: cfg.stop_loss?.first_day?.enabled,
    cfgFormulaSellEn: cfg.stop_loss?.formula_sell?.enabled,   // gap B 修复
  };
  for (const [id, val] of Object.entries(mapping)) {
    const el = document.getElementById(id);
    if (!el) continue;
    el.value = val != null ? String(val) : el.defaultValue;
  }
  for (const [id, val] of Object.entries(checkboxMapping)) {
    const el = document.getElementById(id);
    if (!el) continue;
    if (val != null) el.checked = val;
  }
  RADIO_CONFIGS.forEach(rc => {
    const val = mapping[rc.allow];
    if (val != null) {
      const target = document.querySelector(`input[name="${rc.name}"][value="${val}"]`);
      if (target) target.checked = true;
      else console.warn('[applyConfigDict] radio', rc.name, '找不到 value=', val);
    }
  });
  // gap A 修复：sectors 回填（原 resetDefaults 不回填、还清空 → 从文件加载会丢板块勾选）
  const sectors = cfg.selection?.universe?.sectors;
  if (Array.isArray(sectors)) {
    _selectedSectors = sectors.slice();
    try { localStorage.setItem(SECTORS_KEY, JSON.stringify(_selectedSectors)); } catch(e) {}
    // 异步兜底：_allSectors 没加载完时只先存 _selectedSectors，loadSectors 的 renderSectors 会自动勾选
    if (_allSectors.length > 0) {
      renderSectors();
      updateSectorSummary();
      toggleUniverseDropdown();
    }
  }
}

// 2026-07-10: 前端配置存取 current.yaml（保存/加载/删除，单一覆盖文件）
let _savedFileExists = false;   // current.yaml 是否存在，控 Load/Delete 按钮态 + save confirm

function toggleSavedButtons(exists) {
  const load = document.getElementById('btnLoadFile');
  const del = document.getElementById('btnDeleteFile');
  if (load) load.disabled = !exists;
  if (del) del.disabled = !exists;
}

async function saveConfigToFile() {
  if (_savedFileExists && !confirm('已存在保存的配置（config/current.yaml），确定覆盖？')) return;
  const config = collectConfigFromForm();    // 与 runPipeline 同源，含 sectors
  try {
    const r = await fetch('/api/config/save', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(config)});
    const res = await r.json();
    if (res.success) {
      _savedFileExists = true;
      toggleSavedButtons(true);
      saveAllConfig();                        // 同步 localStorage，保持双轨一致
      showToast('配置已保存到 config/current.yaml' + (res.warnings && res.warnings.length ? '（'+res.warnings.length+'条告警）' : ''), 'ok');
      addLog('配置已保存到 config/current.yaml', 'ok');
    } else {
      showToast('保存失败: ' + (res.error || '未知错误'), 'error');
    }
  } catch(e) {
    showToast('保存失败（网络）: ' + e.message, 'error');
  }
}

async function loadConfigFromFile() {
  try {
    const r = await fetch('/api/config/saved');
    const res = await r.json();
    if (!res.success || !res.exists) { showToast(res.error || '暂无保存的配置', 'info'); return; }
    applyConfigDict(res.config);              // 合并后的完整 dict
    saveAllConfig();                          // 必须！否则刷新后回退到旧 localStorage
    refreshAllSummaries();
    showToast('已从 config/current.yaml 加载配置', 'ok');
    addLog('已从 config/current.yaml 加载配置', 'ok');
  } catch(e) {
    showToast('加载失败（网络）: ' + e.message, 'error');
  }
}

async function deleteSavedConfig() {
  if (!confirm('确定删除已保存的配置文件（config/current.yaml）？\n（当前表单值不会被清空，如需重置请点"恢复默认配置"）')) return;
  try {
    const r = await fetch('/api/config/saved', {method: 'DELETE'});
    const res = await r.json();
    if (res.success) {
      _savedFileExists = false;
      toggleSavedButtons(false);
      showToast('已删除 config/current.yaml（表单值未变）', 'ok');
      addLog('已删除 config/current.yaml', 'info');
    } else {
      showToast('删除失败: ' + (res.error || '未知错误'), 'error');
    }
  } catch(e) {
    showToast('删除失败（网络）: ' + e.message, 'error');
  }
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
    applyConfigDict(result.config || {});   // 抽取的公共填表逻辑（含 sectors/formula_sell 回填）
    localStorage.removeItem(STORAGE_KEY);   // 恢复默认 = 清浏览器存储（applyConfigDict 已按 default 重填表单 + 清板块）
    try { localStorage.removeItem(SECTORS_KEY); } catch(e) {}
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

  const config = collectConfigFromForm();   // 2026-07-10: 抽取，与 saveConfigToFile 同源（含 sectors）

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

  // H8 修正：加 AbortController + 10min 超时，防止网络挂死永远转圈
  // 注：后端 /api/run 是 FastAPI 同步路由 + 线程池，uvicorn 默认无超时；
  //     这里只是给前端 fetch 一个"宁死不等到天荒地老"的上限。
  //     2026-07-05: 后台跑长区间经常超 5min，调到 10min。
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 600000);
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
      pollActive = false; clearInterval(poll);
      // 2026-07-05: 抽出 resetRunUI 复用 catch 与 tryRecoverAbortedResult
      resetRunUI(btn, '错误', 'on');

      // 2026-07-05: 超时/断网兜底——后端是同步线程池，连接断了但线程还在跑，
      // 可能 10s/30s/几分钟后才落盘。先看看 status + last_result，能救就救。
      tryRecoverAbortedResult(config, e);
    });
}

// 重置运行期 UI（catch 与 tryRecoverAbortedResult 复用，.then 成功路径保持不变以免影响既有语义）
// statusText: '就绪' | '错误' | '自动恢复中…' 等
function resetRunUI(btn, statusText, dotClass) {
  document.getElementById('progressBar').style.display = 'none';
  document.getElementById('progressText').textContent = '';
  document.getElementById('statusDot').className = 'status-dot ' + (dotClass || 'on');
  document.getElementById('statusText').textContent = statusText;
  btn.disabled = false;
  btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg> 执行回测';
}

// 超时/断网后尝试从后端拉取已落盘的结果，避免用户白白等了 N 分钟
async function tryRecoverAbortedResult(cfg, originalErr) {
  // 顶层 try/catch: 防 addLog/showToast 在某些嵌入场景（无 #logPanel）抛 unhandled rejection
  try {
    const cfgStart = (cfg.start_time || '').toString();
    const cfgEnd   = (cfg.end_time   || '').toString();
    const cfgFml   = (cfg.formula_name || '').toString();

    addLog('尝试自动恢复：探测后端是否仍在运行/已落盘…', 'info');
    const btn = document.getElementById('btnRun');
    resetRunUI(btn, '自动恢复中…', 'busy');

    for (let i = 0; i < RECOVER.MAX_RETRY; i++) {
      try {
        // 1) 先看 status.running：还在跑就别报错，等它
        const sr = await fetch('/api/status');
        if (sr.ok) {
          const s = await sr.json();
          if (s && s.running) {
            addLog(`后端仍在运行（第 ${i+1}/${RECOVER.MAX_RETRY} 次探测，${s.step||''} ${s.progress||0}%）…`, 'info');
            await new Promise(r => setTimeout(r, RECOVER.INTERVAL_MS));
            continue;
          }
        }

        // 2) 拉 last_result：可能是本次 cfg 的结果，也可能是更早的
        const lr = await fetch('/api/last_result');
        if (lr.ok) {
          const data = await lr.json();
          // 成功判定：必须是 success=true 且 trade_count>=0（防误命中一个空结果）
          if (data && data.success && typeof data.trade_count === 'number') {
            // 比对 freshness：用 index 最新一条的 formula + date_range 判断是不是本次 cfg
            // （last_result.json 里没存 meta，最稳的 freshness 依据是 /api/results[0]）
            let isFresh = true;
            try {
              const ir = await fetch('/api/results');
              if (ir.ok) {
                const idx = await ir.json();
                const top = Array.isArray(idx) && idx.length > 0 ? idx[0] : null;
                if (top) {
                  const wantDr = `${cfgStart}~${cfgEnd}`;
                  isFresh = (top.formula === cfgFml) && (top.date_range === wantDr);
                }
              }
            } catch (_) { /* 索引拿不到就当作"新鲜"，保守放过 */ }

            if (isFresh) {
              addLog(`✓ 自动恢复成功：后端已完成回测（${data.trade_count}笔交易）`, 'ok');
              showToast(`✓ 已自动恢复回测结果（${data.trade_count}笔交易）`, 'ok');  // M-6 修复
              lastResult = data;
              renderAllCharts(data);
              checkEngineVersion(data);
              resetRunUI(btn, '就绪', 'on');
              return;
            } else {
              addLog('last_result 不是本次 cfg 的结果（可能是更早的历史）— 判定为未生成', 'info');
              break;
            }
          }
        }
        // 3) 不在跑、last_result 也没拿到 → 真没了
        break;
      } catch (probeErr) {
        addLog(`探测失败（第 ${i+1} 次）：${probeErr.message}`, 'info');
        await new Promise(r => setTimeout(r, RECOVER.INTERVAL_MS));
      }
    }

    // 全部恢复尝试都失败：才报原始网络错误
    const msg = originalErr && originalErr.name === 'AbortError'
      ? '请求超时（10分钟），后端未在超时内落盘。请缩小回测区间或稍后到历史结果中查看。'
      : (originalErr && originalErr.message) || '未知错误';
    addLog('网络错误: '+msg, 'error');
    showToast('网络错误: '+msg, 'error');
    resetRunUI(btn, '错误', 'on');
  } catch (fatalErr) {
    // 入口 try/catch: 防 addLog/showToast 抛 unhandled rejection 导致 UI 卡死
    // eslint-disable-next-line no-console
    console.error('[tryRecoverAbortedResult] 兜底失败:', fatalErr);
  }
}

// B-10: KPI 数字滚动 tween（从 0 到目标，easeOutCubic）
function tweenNumber(el, target, formatter, duration) {
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

// B-13: 迷你 sparkline — 纯 SVG polyline + 渐变面积，无依赖、无 ECharts 开销
function sparkline(values, color, fillAlpha) {
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

// B-11: hero 核心指标子行 — 趋势/评级 + vs 上证基准 + sparkline
function fillHeroSub(m, data, c) {
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
      cumSub.innerHTML = (win ? '▲' : '▼') + ' <span class="' + (win ? 'delta-up' : 'delta-down') + '">vs 上证 ' + (shRet*100).toFixed(2) + '%</span>';
    } else cumSub.textContent = '';
  }
  const ddSub = document.getElementById('kpiMaxDDSub');
  if (ddSub) ddSub.innerHTML = m.max_drawdown != null ? '<span style="color:var(--text2)">峰值到谷值最大跌幅</span>' : '';
  const shSub = document.getElementById('kpiSharpeSub');
  if (shSub) {
    if (m.sharpe_ratio != null) {
      let grade, cls;
      if (m.sharpe_ratio >= 2) { grade = '优秀'; cls = 'delta-up'; }
      else if (m.sharpe_ratio >= 1) { grade = '良好'; cls = 'delta-up'; }
      else if (m.sharpe_ratio >= 0) { grade = '一般'; cls = ''; }
      else { grade = '不佳'; cls = 'delta-down'; }
      shSub.innerHTML = '<span class="' + cls + '">' + grade + '</span>';
    } else shSub.textContent = '';
  }
  // B-13: 累计收益 + 最大回撤 sparkline（权益曲线 / 回撤曲线）
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

// B-12: 回测完成 — KPI 卡 + 图表卡 stagger 入场
function revealResults() {
  const cards = document.querySelectorAll('.kpi-card, .chart-box');
  cards.forEach(c => { c.classList.remove('reveal'); c.style.animationDelay = ''; });
  const grid = document.getElementById('kpiGrid');
  if (grid) void grid.offsetWidth;  // 强制 reflow 重启动画
  cards.forEach((c, i) => {
    c.style.animationDelay = (i * 40) + 'ms';
    c.classList.add('reveal');
  });
}

function renderAllCharts(data) {
  const c = getColors();
  const m = data.metrics || {};

  // KPI cards — B-10: 数字滚动（替代原直接赋值）
  const setKpi = (id, val, fmt) => {
    const el = document.getElementById(id);
    el.className = 'kpi-value';
    if (typeof val === 'number') { if (val > 0) el.classList.add('pos'); else if (val < 0) el.classList.add('neg'); }
    tweenNumber(el, val, fmt);
  };
  setKpi('kpiCumRet', m.cumulative_return, v => v != null ? (v*100).toFixed(2)+'%' : '--');
  setKpi('kpiAnnRet', m.annualized_return, v => v != null ? (v*100).toFixed(2)+'%' : '--');
  setKpi('kpiMaxDD', m.max_drawdown, v => v != null ? (v*100).toFixed(2)+'%' : '--');
  setKpi('kpiSharpe', m.sharpe_ratio, v => v != null ? v.toFixed(2) : '--');
  setKpi('kpiWinRate', m.win_rate, v => v != null ? (v*100).toFixed(1)+'%' : '--');
  setKpi('kpiPLR', m.profit_loss_ratio, v => v != null ? v.toFixed(2) : '--');
  setKpi('kpiTrades', m.total_trades, v => v != null ? Math.round(v) : '--');
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
    const reasonMap = { '成本止损': '成本止损', '移动止损': '移动止损', '移动止盈': '移动止盈', '阶梯止盈': '阶梯止盈', '时间止损': '时间止损', '时间止盈': '时间止盈', cond_time_stop: '条件时间止盈', trailing_stop: '移动止盈/止损', '换股卖出': '换股卖出', '首日未达标': '首日未达标', 'formula_sell': '公式止损', '退市': '退市' };
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

  // B-11/12: hero 子行（趋势/评级 + vs 上证）+ 卡片 stagger 入场
  fillHeroSub(m, data, c);
  revealResults();
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
const reasonShortMap = { '成本止损': '成本止损', '移动止损': '移动止损', '移动止盈': '移动止盈', '阶梯止盈': '阶梯止盈', '时间止损': '时间止损', '时间止盈': '时间止盈', cond_time_stop: '条件时间止盈', trailing_stop: '移动止盈/止损', '换股卖出': '换股卖出', '首日未达标': '首日未达标', 'formula_sell': '公式止损' };

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
  const reasonMap = { '成本止损': '成本止损', '移动止损': '移动止损', '移动止盈': '移动止盈', '阶梯止盈': '阶梯止盈', '时间止损': '时间止损', '时间止盈': '时间止盈', cond_time_stop: '条件时间止盈', trailing_stop: '移动止盈/止损', '换股卖出': '换股卖出', '首日未达标': '首日未达标', 'formula_sell': '公式止损' };
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
// 2026-07-10: 探测 current.yaml 是否存在（只控 Load/Delete 按钮启用态；不填表单——遵循"打开网页不自动加载"）
fetch('/api/config/saved').then(r => r.json()).then(res => {
  _savedFileExists = !!(res && res.exists);
  toggleSavedButtons(_savedFileExists);
}).catch(() => {});
// B-14: 恢复板块面板折叠状态（localStorage 记忆）
if (localStorage.getItem('vera_sector_collapsed') === '1') {
  document.querySelector('.sector-section').classList.add('collapsed');
}
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

// ====== 测试导出（Node 端单测用，浏览器中 typeof module === 'undefined 不会执行） ======
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { tryRecoverAbortedResult, resetRunUI, RECOVER };
}
