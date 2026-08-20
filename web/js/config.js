// ====== VERA Config Adapter ======
// 前端表单 ↔ 后端配置 dict ↔ localStorage 翻译层。
// 接口: collectConfigFromForm() → payload / applyConfigDict(cfg) → void / 校验函数

// ── 常量 ──

export const STORAGE_KEY = 'vera_all_config';
export const CONFIG_IDS = [
  'cfgFormula', 'cfgFormulaArg', 'cfgUniverse', 'cfgPeriod',
  'cfgEntryPriceMode',
  'cfgStart', 'cfgEnd', 'cfgExcludeST',
  'cfgIncludeEtf', 'cfgEtfOnly',
  'cfgCapital', 'cfgCommission', 'cfgSlippage',
  'cfgMinBuy', 'cfgMaxBuy', 'cfgLotSize', 'cfgMinLots',
  'cfgCostStopEn', 'cfgCostStopVal',
  'cfgTrailingEn', 'cfgTrailingAct', 'cfgTrailingDD',
  'cfgLadderEn', 'cfgLadderVal',
  'cfgTimeEn', 'cfgTimeVal',
  'cfgCondTimeEn', 'cfgCondTimeDays', 'cfgCondTimeProfit',
  'cfgFirstDayEn', 'cfgFirstDayTarget',
  'cfgFormulaSellEn', 'cfgFormulaSellName', 'cfgFormulaSellRatio',
];

export const RADIO_CONFIGS = [
  { name: 'cfgPriority', allow: 'cfgPriority', fallback: 'trailing_first' },
];

// ── 工具 ──

export function cleanNum(x) {
  const n = Number(x);
  return isNaN(n) ? 0 : parseFloat(n.toPrecision(12));
}

// ── 表单校验 ──

export function validateDate(el) {
  const v = el.value.trim();
  el.classList.toggle('invalid', v.length > 0 && !/^\d{8}$/.test(v));
}

export function validatePositive(el) {
  el.classList.toggle('invalid', parseFloat(el.value) <= 0);
}

export function validateNonNeg(el) {
  el.classList.toggle('invalid', parseFloat(el.value) < 0);
}

export function validateLadder(el) {
  const v = el.value.trim();
  if (!v) { el.classList.remove('invalid'); return; }
  const valid = v.split(',').every(s => /^\d+(\.\d+)?\s*:\s*\d+(\.\d+)?$/.test(s.trim()));
  el.classList.toggle('invalid', !valid);
}

// ── localStorage 持久化 ──

export function loadConfig() {
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
      RADIO_CONFIGS.forEach(rc => {
        if (!(rc.allow in saved)) return;
        const target = document.querySelector('input[name="' + rc.name + '"][value="' + saved[rc.allow] + '"]');
        if (target) {
          target.checked = true;
        } else {
          console.warn('[loadConfig] radio', rc.name, '找不到 value=', saved[rc.allow], '— 已忽略, 保持 HTML 默认');
        }
      });
    }
  } catch(e) {}
  // refreshAllSummaries is called from vera-ui.js after importing this
}

export function saveAllConfig() {
  const data = {};
  CONFIG_IDS.forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    if (el.type === 'checkbox') data[id] = el.checked;
    else data[id] = el.value;
  });
  RADIO_CONFIGS.forEach(rc => {
    const checked = document.querySelector('input[name="' + rc.name + '"]:checked');
    if (checked) data[rc.allow] = checked.value;
  });
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
  } catch(e) {
    // showToast is imported separately by caller
    console.error('配置保存失败（浏览器存储不可用）', e);
  }
}

// ── 前端表单 → 后端 payload ──

export function collectConfigFromForm(_selectedSectors, collectFactorFilter, escFn) {
  const getVal = id => document.getElementById(id).value;
  const pct = (id) => parseFloat(getVal(id)) / 100;
  const safeFloat = (id) => { const v = parseFloat(getVal(id)); return isNaN(v) ? 0 : v; };
  const safeInt = (id) => { const v = parseInt(getVal(id)); return isNaN(v) ? 0 : v; };
  const ladderRaw = getVal('cfgLadderVal');
  const ladderParts = ladderRaw.split(',').map(s => {
    const parts = s.trim().split(':');
    return cleanNum(parseFloat(parts[0]) / 100) + ':' + cleanNum(parseFloat(parts[1]) / 100);
  }).join(',');
  return {
    strategy_name: '',
    formula_name: getVal('cfgFormula'), formula_arg: getVal('cfgFormulaArg'),
    universe_type: getVal('cfgUniverse'), exclude_st: document.getElementById('cfgExcludeST').checked,
    include_etf: document.getElementById('cfgIncludeEtf').checked,
    etf_only: document.getElementById('cfgEtfOnly').checked,
    sectors: (_selectedSectors || []).join(','),
    start_time: getVal('cfgStart'), end_time: getVal('cfgEnd'),
    period: getVal('cfgPeriod'), dividend_type: 1,
    // 2026-08-20: 买入价口径 (close_t=信号日收盘/open_t1=次日开盘, 一字涨停才放弃)
    entry_price_mode: (document.getElementById('cfgEntryPriceMode') || {}).value || 'close_t',
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
    trailing_confirm: (document.getElementById('cfgTrailingConfirm') || {}).value || 'real',
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
    benchmark_indices: 'shanghai,hs300,chuangyeban,kechuang50,zhongzhengA500',
    factor_filter: collectFactorFilter ? collectFactorFilter() : {},
  };
}

// ── 后端 dict → 前端表单 ──

export function applyConfigDict(cfg, renderSectorsFn, updateSectorSummaryFn, toggleUniverseDropdownFn, applyFactorFilterDictFn, _allSectors, setSelectedSectors) {
  cfg = cfg || {};
  const mapping = {
    cfgFormula: cfg.selection?.formula_name,
    cfgFormulaArg: cfg.selection?.formula_arg,
    cfgUniverse: cfg.selection?.universe?.type,
    cfgPeriod: cfg.backtest?.period === '1m' ? '1m' : cfg.backtest?.period === '5m' ? '5m' : cfg.backtest?.period === '1w' ? '1w' : '1d',
    cfgEntryPriceMode: cfg.backtest?.entry_price_mode || 'close_t',
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
    cfgTrailingConfirm: cfg.stop_loss?.trailing_stop?.confirm || 'real',
    cfgLadderVal: cfg.stop_loss?.ladder_tp?.levels?.map(l => cleanNum(l.profit * 100) + ':' + cleanNum(l.sell_ratio * 100)).join(','),
    cfgTimeVal: cfg.stop_loss?.time_stop?.max_hold_days,
    cfgCondTimeDays: cfg.stop_loss?.cond_time_stop?.days,
    cfgCondTimeProfit: cfg.stop_loss?.cond_time_stop?.profit != null ? String(cleanNum(cfg.stop_loss.cond_time_stop.profit * 100)) : null,
    cfgFirstDayTarget: cfg.stop_loss?.first_day?.target != null ? String(cleanNum(cfg.stop_loss.first_day.target * 100)) : null,
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
    cfgFormulaSellEn: cfg.stop_loss?.formula_sell?.enabled,
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
      const target = document.querySelector('input[name="' + rc.name + '"][value="' + val + '"]');
      if (target) target.checked = true;
      else console.warn('[applyConfigDict] radio', rc.name, '找不到 value=', val);
    }
  });
  // sectors 回填
  const sectors = cfg.selection?.universe?.sectors;
  if (Array.isArray(sectors)) {
    if (setSelectedSectors) setSelectedSectors(sectors.slice());
    try { localStorage.setItem('vera_selected_sectors', JSON.stringify(sectors)); } catch(e) {}
    if (_allSectors && _allSectors.length > 0) {
      if (renderSectorsFn) renderSectorsFn();
      if (updateSectorSummaryFn) updateSectorSummaryFn();
      if (toggleUniverseDropdownFn) toggleUniverseDropdownFn();
    }
  }
  // 因子过滤回填
  if (cfg.factor_filter && applyFactorFilterDictFn) applyFactorFilterDictFn(cfg.factor_filter);
}

// ── Block 编辑 ──

export function toggleEdit(blockId, saveAllConfigFn, refreshAllSummariesFn) {
  const block = document.getElementById(blockId);
  block.querySelectorAll('.cfg-fields input, .cfg-fields select').forEach(el => {
    if (!el.dataset.original) {
      el.dataset.original = el.type === 'checkbox' ? String(el.checked) : el.value;
    }
  });
  block.classList.add('editing');
  block.querySelector('.cancel-btn').style.display = '';
  block.querySelector('.edit-btn').style.display = 'none';
  const badge = block.querySelector('.saved-badge');
  if (badge) { badge.textContent = '未保存'; badge.className = 'saved-badge unsaved'; }
}

export function cancelEdit(blockId) {
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
  const badge = block.querySelector('.saved-badge');
  if (badge) { badge.textContent = '已保存'; badge.className = 'saved-badge saved'; }
}

export function saveBlock(blockId, saveAllConfigFn, refreshAllSummariesFn, addLogFn) {
  const block = document.getElementById(blockId);
  block.classList.remove('editing');
  block.querySelector('.cancel-btn').style.display = 'none';
  block.querySelector('.edit-btn').style.display = '';
  block.querySelectorAll('.cfg-fields input, .cfg-fields select').forEach(el => { delete el.dataset.original; });
  const badge = block.querySelector('.saved-badge');
  if (badge) { badge.textContent = '已保存'; badge.className = 'saved-badge saved'; }
  if (saveAllConfigFn) saveAllConfigFn();
  if (refreshAllSummariesFn) refreshAllSummariesFn();
  if (addLogFn) addLogFn('全部配置已保存到浏览器', 'ok');
}

// ── 摘要刷新 ──

export function refreshAllSummaries(escFn) {
  const esc = escFn || (s => s);
  const cs = esc(document.getElementById('cfgCostStopVal').value);
  document.getElementById('sumCostStop').innerHTML = '成本止损：亏损达到 <b>' + cs + '%</b> 全仓卖出 <span class="saved-badge saved">已保存</span>';
  const ta = esc(document.getElementById('cfgTrailingAct').value);
  const td = esc(document.getElementById('cfgTrailingDD').value);
  const confirmEl = document.getElementById('cfgTrailingConfirm');
  const confirmMap = {
    simple: '5M碰线按bar收盘价成交 / 1D收盘价确认',
    real: '条件单语义：创新高bar不触发，跳空按开盘价，触线按线价',
    intraday: '盘中 Low 触及线即按线价卖出',
    low: '当日最低价碰线，按收盘价卖出',
    close: '收盘价跌破线才按收盘价卖出',
  };
  const confirmLabel = confirmMap[(confirmEl || {}).value] || confirmMap.intraday;
  document.getElementById('sumTrailing').innerHTML = '移动止盈：盈利 <b>' + ta + '%</b> 激活后，回撤 <b>' + td + '%</b> 触发全仓卖出（' + confirmLabel + '） <span class="saved-badge saved">已保存</span>';
  const lv = esc(document.getElementById('cfgLadderVal').value.replace(/,/g, ', '));
  document.getElementById('sumLadder').innerHTML = '阶梯止盈：<b>' + lv + '</b> <span class="saved-badge saved">已保存</span>';
  const priChecked = document.querySelector('input[name="cfgPriority"]:checked');
  const priLabelMap = {
    stop_first: '止损优先 (历史默认)',
    ladder_tp_first: '阶梯止盈优先',
    trailing_first: '移动止盈优先 (盘中锁利)',
  };
  const priLabel = (priChecked && priLabelMap[priChecked.value]) || '移动止盈优先 (盘中锁利)';
  const sumPriEl = document.getElementById('sumPriority');
  if (sumPriEl) sumPriEl.innerHTML = '优先级：' + priLabel + ' <span class="saved-badge saved">已保存</span>';
  const tv = esc(document.getElementById('cfgTimeVal').value);
  document.getElementById('sumTime').innerHTML = '时间止盈：持仓 <b>' + tv + '</b> 天后无条件卖出 <span class="saved-badge saved">已保存</span>';
  const cd = esc(document.getElementById('cfgCondTimeDays').value);
  const cp = esc(document.getElementById('cfgCondTimeProfit').value);
  document.getElementById('sumCondTime').innerHTML = '条件时间止盈：持仓 <b>' + cd + '</b> 天后盈利 <b>>=' + cp + '%</b> 清仓 <span class="saved-badge saved">已保存</span>';
  const fd = esc(document.getElementById('cfgFirstDayTarget').value);
  document.getElementById('sumFirstDay').innerHTML = '首日未达标卖出：买入次日最高价涨幅未达 <b>' + fd + '%</b> 则收盘卖出 <span class="saved-badge saved">已保存</span>';
  const fsEn = document.getElementById('cfgFormulaSellEn').checked;
  const fsName = esc(document.getElementById('cfgFormulaSellName').value || '卖出XG');
  const fsRatio = esc(document.getElementById('cfgFormulaSellRatio').value);
  const fsState = fsEn ? '命中即止损' : '未启用';
  document.getElementById('sumFormulaSell').innerHTML =
    '公式止损：<b>[' + fsName + ']</b> ' + fsState + ' ' + fsRatio + '% <span class="saved-badge saved">已保存</span>';
}
