// ====== VERA 交易页 (P1 第三阶段, 2026-07-26) ======
// 读快照 + 发命令, 零业务判断 (铁律: 业务判断全在后端)。
// 交易 API 由交易进程 (trade_main.py, 默认 8081) 提供 —— 交易系统是
// 独立单进程, 不寄生在回测服务器 (8080) 里; 页面仍由 8080 serve。
// 轮询生命周期由 vera-ui.js 的 switchTab 经 window 钩子驱动。
(function () {
'use strict';

var BASE = 'http://' + location.hostname + ':8081';
var pollTimer = null;

function get(u, signal) { return fetch(BASE + u, { signal: signal }).then(function (r) { return r.json(); }); }
function post(u, body, signal) {
  return fetch(BASE + u, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}), signal: signal,
  }).then(function (r) { return r.json(); });
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}
function fmtTs(ts) {
  if (!ts) return '';
  var d = new Date(ts * 1000);
  function p(n) { return (n < 10 ? '0' : '') + n; }
  // 2026-07-30: 带日期 (原仅时分秒, 跨日持仓分不清哪天)
  return p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' '
    + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}
function badge(el, ok, text) {
  el.className = 'trade-badge ' + (ok === null ? 'wait' : ok ? 'ok' : 'bad');
  el.textContent = text;
}

// ── 渲染 ─────────────────────────────────────────────

function renderStatus(s) {
  badge(document.getElementById('tdConn'), s.connected, s.connected ? '已连接' : '未连接');
  badge(document.getElementById('tdReconciled'), s.reconciled, s.reconciled ? '对账通过' : '对账未通过');
  // 2026-07-27 裁决②: 按时段显示人话原因, 不再笼统红色"降级/断连"
  var healthy = s.monitor_healthy;   // true / false / null(非连续竞价不适用)
  badge(document.getElementById('tdQuote'),
        healthy === null ? null : healthy,
        s.monitor_reason || (healthy ? '盘中·订阅正常' : '盘中·轮询兜底'));
  var btn = document.getElementById('tdKillBtn');
  btn.textContent = s.kill_active ? '急停中 · 点击解除' : '急停';
  btn.classList.toggle('armed', !s.kill_active);
  document.getElementById('tdHint').textContent =
    s.kill_active ? '急停激活中: 全面拒单, 解除需二次确认 (只许人工解除)'
    : 'ETF 持仓不纳入自动管理 (规则: 沪 51/56/58、深 15/16/18 前缀)';
}

// ── 审计M11修复: 宕机不留"看起来正常"的陈旧数据 ──

function staleTag(id) {
  var box = document.getElementById(id);
  box.style.opacity = '0.55';
  if (!box.querySelector('.trade-stale-tag')) {
    var tag = document.createElement('div');
    tag.className = 'trade-stale-tag';
    tag.style.cssText = 'font-size:10px;color:var(--pending);margin-bottom:4px';
    tag.textContent = '离线缓存 (服务不可达, 数据未更新)';
    box.insertBefore(tag, box.firstChild);
  }
}
function clearStale(id) {
  var box = document.getElementById(id);
  box.style.opacity = '';
  var tag = box.querySelector('.trade-stale-tag');
  if (tag) tag.remove();
}
function markOffline() {
  // 徽章置断连/未知, 四个数据区打离线缓存角标 + 灰化
  badge(document.getElementById('tdConn'), false, '断连');
  badge(document.getElementById('tdReconciled'), null, '对账未知');
  badge(document.getElementById('tdQuote'), null, '行情未知');
  document.getElementById('tdHint').textContent =
    '交易服务 (8081) 不可达 — 显示的是最后一次成功刷新的缓存数据';
  ['tdPositions', 'tdOrders', 'tdReconciles', 'tdAudits'].forEach(staleTag);
}

function renderPositions(d) {
  var box = document.getElementById('tdPositions');
  clearStale('tdPositions');
  // 2026-07-30: 缓存最近持仓供卖出可用校验 (tdSellBtn 复用数量框)
  window._lastPositions = d.positions || [];
  if (!d.positions || !d.positions.length) {
    box.innerHTML = '<div style="color:var(--text2);font-size:12px">无持仓</div>'; return;
  }
  // 2026-07-30: 持仓明细增强 — 简称/入场时间/市值/盈亏比例 + 已平仓
  var html = '<table class="td-table"><tr><th>代码</th><th>简称</th><th>数量</th><th>可用</th>'
    + '<th>成本</th><th>现价</th><th>市值</th><th>浮盈</th><th>盈亏%</th>'
    + '<th>入场时间</th><th>持仓天数</th><th>已触发档</th><th></th></tr>';
  d.positions.forEach(function (p) {
    var pnl = p.pnl === null ? '—' : p.pnl.toFixed(2);
    var pnlColor = p.pnl === null ? '' : p.pnl >= 0 ? 'color:var(--up)' : 'color:var(--ok)';
    // 2026-07-27 裁决③: ETF 行灰化 + "不管理"徽标, 无卖出按钮
    var rowStyle = p.managed === false ? ' style="opacity:.5"' : '';
    var etfTag = p.managed === false
      ? ' <span class="trade-badge wait">ETF·不管理</span>' : '';
    html += '<tr' + rowStyle + '><td>' + esc(p.code) + etfTag + '</td>'
      + '<td>' + esc(p.name || '—') + '</td>'
      + '<td>' + p.volume + '</td><td>' + p.can_use
      + '</td><td>' + p.avg_cost.toFixed(2) + '</td><td>' + (p.last === null ? '—' : p.last.toFixed(2))
      + '</td><td>' + (p.market_value === null ? '—' : p.market_value.toLocaleString('zh-CN', {maximumFractionDigits: 0}))
      + '</td><td style="' + pnlColor + '">' + pnl
      + '</td><td style="' + pnlColor + '">' + (p.pnl_pct === null ? '—' : (p.pnl_pct > 0 ? '+' : '') + p.pnl_pct.toFixed(2) + '%')
      + '</td><td style="font-size:10px">' + (p.entry_ts ? fmtTs(p.entry_ts) : '—')
      + '</td><td>' + (p.hold_days === null ? '—' : p.hold_days + ' 天')
      + '</td><td>'
      + (p.tiers_done.length ? p.tiers_done.join(',') : '—')
      + '</td><td>' + (p.managed === false ? ''
      : '<button class="trade-sell-btn" data-code="' + esc(p.code) + '">卖出</button>') + '</td></tr>';
  });
  html += '</table>';
  // 已平仓 (整周期闭环: 买入量=卖出量; 出场时间 = 最后一笔卖出)
  if (d.closed && d.closed.length) {
    html += '<div style="margin-top:8px;color:var(--text2);font-size:11px">已平仓</div>'
      + '<table class="td-table"><tr><th>代码</th><th>简称</th><th>数量</th>'
      + '<th>入场时间</th><th>出场时间</th><th>已实现盈亏</th></tr>';
    d.closed.forEach(function (c) {
      var cColor = c.realized_pnl >= 0 ? 'color:var(--up)' : 'color:var(--ok)';
      html += '<tr><td>' + esc(c.code) + '</td><td>' + esc(c.name || '—') + '</td>'
        + '<td>' + c.qty + '</td>'
        + '<td style="font-size:10px">' + fmtTs(c.entry_ts) + '</td>'
        + '<td style="font-size:10px">' + fmtTs(c.exit_ts) + '</td>'
        + '<td style="' + cColor + '">' + (c.realized_pnl >= 0 ? '+' : '') + c.realized_pnl.toFixed(2) + '</td></tr>';
    });
    html += '</table>';
  }
  box.innerHTML = html;
  box.querySelectorAll('.trade-sell-btn').forEach(function (b) {
    b.addEventListener('click', function () {
      var code = b.getAttribute('data-code');
      if (!confirm('确认卖出 ' + code + ' 全部可用持仓?\n(走撤单流水线: 撤预埋 → 买一价 → 超时升级对手最优)')) return;
      // 审计M12修复: 失败提示 + 防连点, 与买入路径一致
      cmd(b, '/api/trade/sell', { code: code }, '卖出命令已受理, 结果见审计日志');
    });
  });
}

function renderOrders(d) {
  var box = document.getElementById('tdOrders');
  clearStale('tdOrders');
  if (!d.orders || !d.orders.length) {
    box.innerHTML = '<div style="color:var(--text2);font-size:12px">当日无委托</div>'; return;
  }
  // 2026-07-30: 委托表加撤单按钮 (用户要求) — 终态 (部撤53/已撤/已成/废单) 不可撤
  var html = '<table class="td-table"><tr><th>时间</th><th>备注</th><th>代码</th>'
    + '<th>方向</th><th>价格</th><th>数量</th><th>已成交</th><th>状态</th><th></th></tr>';
  d.orders.forEach(function (o) {
    // 审计L12修复: qty/filled_qty/status 统一 Number() 强转 ——
    // 其他字段走 esc, 这三个直插 innerHTML 是纵深防御缺口
    var st = Number(o.status);
    var canCancel = (st === 50 || st === 51 || st === 55);   // 已报/待撤/部成 可撤
    html += '<tr><td>' + fmtTs(o.created_ts) + '</td><td>' + esc(o.remark) + '</td><td>'
      + esc(o.code) + '</td><td>' + (Number(o.direction) === 23 ? '买' : '卖') + '</td><td>'
      + Number(o.price).toFixed(2) + '</td><td>' + Number(o.qty) + '</td><td>'
      + Number(o.filled_qty) + '</td><td>' + st + '</td><td>'
      + (canCancel
        ? '<button class="trade-sell-btn trade-cancel-btn" data-oid="' + esc(o.order_id) + '">撤单</button>'
        : '')
      + '</td></tr>';
  });
  box.innerHTML = html + '</table>';
  box.querySelectorAll('.trade-cancel-btn').forEach(function (b) {
    b.addEventListener('click', function () {
      var oid = b.getAttribute('data-oid');
      if (!confirm('确认撤销委托 ' + oid + ' ?\n(预埋单撤掉后, 该档今日不会自动重挂)')) return;
      cmd(b, '/api/trade/cancel', { order_id: oid }, '撤单命令已受理, 结果见审计日志');
    });
  });
}

function renderReconciles(d) {
  var box = document.getElementById('tdReconciles');
  clearStale('tdReconciles');
  if (!d.reconciles || !d.reconciles.length) {
    box.innerHTML = '<div style="color:var(--text2);font-size:12px">暂无对账记录</div>'; return;
  }
  var html = '<table class="td-table"><tr><th>时间</th><th>级别</th><th>代码</th>'
    + '<th>本地</th><th>QMT</th><th>说明</th></tr>';
  d.reconciles.slice(0, 20).forEach(function (r) {
    var color = r.level === 'CRITICAL' ? 'color:var(--up);font-weight:700'
      : r.level === 'WARN' ? 'color:var(--pending)' : 'color:var(--ok)';
    var detail = '';
    try { detail = JSON.parse(r.detail_json || '{}').reason || ''; } catch (e) {}
    html += '<tr><td>' + fmtTs(r.ts) + '</td><td style="' + color + '">' + esc(r.level)
      + '</td><td>' + esc(r.code || '—') + '</td><td>' + esc(r.expected) + '</td><td>'
      + esc(r.actual) + '</td><td style="text-align:left">' + esc(detail) + '</td></tr>';
  });
  box.innerHTML = html + '</table>';
}

function renderAudits(d) {
  var el = document.getElementById('tdAudits');
  clearStale('tdAudits');
  if (!d.audits || !d.audits.length) { el.textContent = '暂无审计日志'; return; }
  el.textContent = d.audits.map(function (a) {
    return fmtTs(a.ts) + '  [' + a.kind + ']  ' + a.message;
  }).join('\n');
}

// ── 轮询 ─────────────────────────────────────────────

// 审计L12修复: 在途保护 (一轮未回完不发起下一轮) + fetch 4s 超时
var inflight = false;

function refresh() {
  if (inflight) return;
  inflight = true;
  var ctl = new AbortController();
  var timer = setTimeout(function () { ctl.abort(); }, 4000);
  var s = ctl.signal;
  Promise.allSettled([
    get('/api/trade/status', s).then(renderStatus).catch(markOffline),
    get('/api/trade/positions', s).then(renderPositions).catch(function () { staleTag('tdPositions'); }),
    get('/api/trade/orders', s).then(renderOrders).catch(function () { staleTag('tdOrders'); }),
    get('/api/trade/reconciles?limit=20', s).then(renderReconciles).catch(function () { staleTag('tdReconciles'); }),
    get('/api/trade/audits?limit=100', s).then(renderAudits).catch(function () { staleTag('tdAudits'); }),
    get('/api/trade/auto_buy/last', s).then(renderAutoBuy).catch(function () {}),
  ]).then(function () { clearTimeout(timer); inflight = false; });
}

window.tradePageEnter = function () {
  refresh();
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(refresh, 5000);
};
window.tradePageLeave = function () {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
};

// ── 命令绑定 ─────────────────────────────────────────

// 审计M12修复: 所有命令统一收口 —— 失败有明确提示 (与买入一致),
// 请求期间按钮 disable 防连点, 4s 超时
function cmd(btn, url, body, okMsg) {
  btn.disabled = true;
  var ctl = new AbortController();
  var timer = setTimeout(function () { ctl.abort(); }, 4000);
  post(url, body, ctl.signal).then(function () {
    document.getElementById('tdHint').textContent = okMsg;
    refresh();
  }).catch(function () {
    document.getElementById('tdHint').textContent = '命令发送失败: 交易服务 (8081) 不可达';
  }).finally(function () {
    clearTimeout(timer);
    btn.disabled = false;
  });
}

document.getElementById('tdKillBtn').addEventListener('click', function () {
  var armed = this.textContent.indexOf('急停中') === 0;
  if (armed) {
    if (!confirm('确认解除急停?\n解除后立即补一次全量对账, 通过才恢复交易。')) return;
    cmd(this, '/api/trade/unkill', {}, '解除急停命令已受理 (结果见审计日志)');
  } else {
    if (!confirm('确认激活急停?\n激活后全面拒单, 只许人工解除。')) return;
    cmd(this, '/api/trade/kill', {}, '急停命令已受理 (结果见审计日志)');
  }
});

document.getElementById('tdBuyBtn').addEventListener('click', function () {
  var code = document.getElementById('tdBuyCode').value.trim();
  var qty = parseInt(document.getElementById('tdBuyQty').value, 10);
  var priceRaw = document.getElementById('tdBuyPrice').value.trim();
  var hint = document.getElementById('tdBuyHint');
  if (!code) { hint.textContent = '请填代码'; return; }
  if (!qty || qty <= 0 || qty % 100 !== 0) { hint.textContent = '数量必须是 100 的整数倍'; return; }
  var body = { code: code, qty: qty };
  if (priceRaw) body.price = parseFloat(priceRaw);
  if (!confirm('待确认买入: ' + code + ' ' + qty + ' 股'
      + (body.price ? ' @' + body.price : ' (最新价)') + '\n将过风控闸门后下单, 确认?')) return;
  cmd(this, '/api/trade/buy', body, '买入命令已受理 (过闸结果见审计日志)');
});

// 2026-07-30: 配套手工卖出 — 复用代码+数量输入框, 校验可用后走 /api/trade/sell
document.getElementById('tdSellBtn').addEventListener('click', function () {
  var code = document.getElementById('tdBuyCode').value.trim();
  var qtyRaw = document.getElementById('tdBuyQty').value.trim();
  var hint = document.getElementById('tdBuyHint');
  if (!code) { hint.textContent = '请填代码'; return; }
  var pos = (window._lastPositions || []).find(function (p) { return p.code === code; });
  var canUse = pos ? pos.can_use : 0;
  var body = { code: code };
  var qtyText = '全部可用 (' + canUse + ' 股)';
  if (qtyRaw) {
    var qty = parseInt(qtyRaw, 10);
    if (!qty || qty <= 0) { hint.textContent = '数量必须为正整数'; return; }
    if (!pos) { hint.textContent = code + ' 无持仓可查, 无法校验可用, 请留空数量按全部可用卖'; return; }
    if (qty > canUse) { hint.textContent = '数量 ' + qty + ' 超过可用 ' + canUse + ', 无法卖出'; return; }
    if (qty % 100 !== 0 && qty !== canUse) {
      hint.textContent = '部分卖出须为 100 整数倍 (全部卖出 ' + canUse + ' 股除外)'; return;
    }
    body.qty = qty;
    qtyText = qty + ' 股 (可用 ' + canUse + ' 股)';
  }
  if (!confirm('确认卖出 ' + code + ' ' + qtyText + '?\n(走撤单流水线: 撤预埋 → 买一价 → 超时升级对手最优)')) return;
  cmd(this, '/api/trade/sell', body, '卖出命令已受理, 结果见审计日志');
});

document.getElementById('tdLadderBtn').addEventListener('click', function () {
  cmd(this, '/api/trade/ladder', {}, '预埋命令已受理');});

// ── 尾盘自动选股卡片 (2026-07-27 MVP) ────────────────

function renderAutoBuy(d) {
  var cfgBox = document.getElementById('tdAbCfg');
  cfgBox.textContent = (d.config.enabled ? '已启用' : '已停用') + ' · 计划 '
    + d.config.time + ' · ' + d.config.formula_name
    + ' · 单票≤' + d.config.amount_per_stock + ' 元 · 日限 '
    + d.config.max_buys_per_day + ' 笔';
  var box = document.getElementById('tdAbLast');
  var last = d.last;
  if (!last) {
    box.innerHTML = '<div style="color:var(--text2);font-size:12px">今日尚未运行</div>';
    return;
  }
  if (last.error) {
    box.innerHTML = '<div style="color:var(--up);font-size:12px">运行失败: '
      + esc(last.error) + '</div>';
    return;
  }
  var html = '<div style="font-size:12px;margin-bottom:6px">最近运行 '
    + fmtTs(last.ts) + ' (' + esc(last.source) + '): 选中 <b>' + last.selected
    + '</b> / 买入 <b>' + last.bought + '</b></div>';
  if (last.dispositions && last.dispositions.length) {
    html += '<table class="td-table"><tr><th>代码</th><th>处置</th><th>价</th><th>量</th><th>状态</th><th>说明</th></tr>';
    last.dispositions.forEach(function (dp) {
      var isBuy = dp.action === 'buy';
      html += '<tr><td>' + esc(dp.code) + '</td><td style="color:'
        + (isBuy ? 'var(--up)' : 'var(--text2)') + '">'
        + (isBuy ? '已买' : '跳过') + '</td><td>'
        + (isBuy ? Number(dp.price).toFixed(2) : '—') + '</td><td>'
        + (isBuy ? dp.qty : '—') + '</td><td>'
        + esc(dp.status || '—') + '</td><td style="text-align:left">'
        + esc(dp.reason) + '</td></tr>';
    });
    html += '</table>';
  }
  box.innerHTML = html;
}

document.getElementById('tdAbRunBtn').addEventListener('click', function () {
  if (!confirm('立即执行一次尾盘选股买入?\n将按设置选股并自动下单 (过风控闸门), 确认?')) return;
  cmd(this, '/api/trade/auto_buy', {}, '已发起, 选股约 1 分钟, 结果见本卡片与审计日志');
});

// ── 设置面板 (2026-07-26) ────────────────────────────
// 打开时 GET 填充; 保存只发面板覆盖的字段 (后端深合并 —— 账号/路径
// 不在面板内, 永不被面板清空)。真相校验在后端 (判断不出后端铁律),
// 前端只做展示级提示。

var settingsLoaded = false;

function _num(id) { return parseFloat(document.getElementById(id).value); }
function _int(id) { return parseInt(document.getElementById(id).value, 10); }
function _chk(id) { return document.getElementById(id).checked; }
function _setv(id, v) { document.getElementById(id).value = v; }
function _setc(id, v) { document.getElementById(id).checked = !!v; }

function _ladderRow(profit, ratio) {
  var row = document.createElement('div');
  row.className = 'tds-lv-row';
  row.innerHTML = '涨幅 <input type="number" step="0.01" min="0" max="1" class="tds-lv-profit">'
    + ' 卖出比例 <input type="number" step="0.01" min="0" max="1" class="tds-lv-ratio">'
    + ' <button class="tds-del">删除</button>';
  row.querySelector('.tds-lv-profit').value = profit;
  row.querySelector('.tds-lv-ratio').value = ratio;
  row.querySelector('.tds-del').addEventListener('click', function () { row.remove(); });
  return row;
}

function fillSettings(cfg) {
  var s = cfg.stop;
  _setv('tdsPriority', s.priority);
  _setc('tdsCostEn', s.cost_stop.enabled); _setv('tdsCostThreshold', s.cost_stop.threshold);
  _setc('tdsTrailEn', s.trailing_stop.enabled);
  _setv('tdsTrailAct', s.trailing_stop.activation); _setv('tdsTrailDd', s.trailing_stop.drawdown);
  _setc('tdsLadderEn', s.ladder_tp.enabled);
  var list = document.getElementById('tdsLvList');
  list.innerHTML = '';
  s.ladder_tp.levels.forEach(function (lv) { list.appendChild(_ladderRow(lv.profit, lv.sell_ratio)); });
  _setc('tdsTimeEn', s.time_stop.enabled); _setv('tdsTimeDays', s.time_stop.max_hold_days);
  _setc('tdsCondEn', s.cond_time_stop.enabled);
  _setv('tdsCondDays', s.cond_time_stop.days); _setv('tdsCondProfit', s.cond_time_stop.profit);
  _setc('tdsFirstEn', s.first_day.enabled); _setv('tdsFirstTarget', s.first_day.target);
  _setv('tdsLossLimit', cfg.daily_loss_limit);
  _setv('tdsMinAmt', cfg.position_sizing.min_buy_amount);
  _setv('tdsMaxAmt', cfg.position_sizing.max_buy_amount);
  _setv('tdsLot', cfg.position_sizing.lot_size);
  _setv('tdsMaxPos', cfg.position_sizing.max_positions);
  _setv('tdsScanSec', cfg.monitor_scan_interval_sec);
  _setv('tdsHbSec', cfg.tick_heartbeat_sec);
  _setv('tdsStaleSec', cfg.quote_stale_sec);
  // 尾盘自动买入区 (2026-07-27 MVP)
  _setc('tdsAbEn', cfg.auto_buy.enabled);
  _setv('tdsAbTime', cfg.auto_buy.time);
  _setv('tdsAbFormula', cfg.auto_buy.formula_name);
  _setv('tdsAbArg', cfg.auto_buy.formula_arg);
  _setv('tdsAbAmt', cfg.auto_buy.amount_per_stock);
  _setv('tdsAbMax', cfg.auto_buy.max_buys_per_day);
}

function gatherSettings() {
  var levels = [];
  document.querySelectorAll('#tdsLvList .tds-lv-row').forEach(function (row) {
    var p = parseFloat(row.querySelector('.tds-lv-profit').value);
    var r = parseFloat(row.querySelector('.tds-lv-ratio').value);
    if (!isNaN(p) && !isNaN(r)) levels.push({ profit: p, sell_ratio: r });
  });
  return {
    stop: {
      priority: document.getElementById('tdsPriority').value,
      cost_stop: { enabled: _chk('tdsCostEn'), threshold: _num('tdsCostThreshold') },
      trailing_stop: { enabled: _chk('tdsTrailEn'), activation: _num('tdsTrailAct'),
                       drawdown: _num('tdsTrailDd') },
      ladder_tp: { enabled: _chk('tdsLadderEn'), levels: levels },
      time_stop: { enabled: _chk('tdsTimeEn'), max_hold_days: _int('tdsTimeDays') },
      cond_time_stop: { enabled: _chk('tdsCondEn'), days: _int('tdsCondDays'),
                        profit: _num('tdsCondProfit') },
      first_day: { enabled: _chk('tdsFirstEn'), target: _num('tdsFirstTarget') },
    },
    position_sizing: { min_buy_amount: _num('tdsMinAmt'), max_buy_amount: _num('tdsMaxAmt'),
                       lot_size: _int('tdsLot'), max_positions: _int('tdsMaxPos') },
    daily_loss_limit: _num('tdsLossLimit'),
    monitor_scan_interval_sec: _int('tdsScanSec'),
    tick_heartbeat_sec: _int('tdsHbSec'),
    quote_stale_sec: _int('tdsStaleSec'),
    auto_buy: {
      enabled: _chk('tdsAbEn'),
      time: document.getElementById('tdsAbTime').value.trim(),
      formula_name: document.getElementById('tdsAbFormula').value.trim(),
      formula_arg: document.getElementById('tdsAbArg').value.trim(),
      amount_per_stock: _num('tdsAbAmt'),
      max_buys_per_day: _int('tdsAbMax'),
    },
  };
}

document.getElementById('tdSettings').addEventListener('toggle', function () {
  if (this.open && !settingsLoaded) {
    get('/api/trade/config').then(function (cfg) {
      fillSettings(cfg);
      settingsLoaded = true;
    }).catch(function () {
      document.getElementById('tdsHint').textContent = '配置加载失败: 交易服务不可达';
    });
  }
});

document.getElementById('tdsLvAdd').addEventListener('click', function () {
  document.getElementById('tdsLvList').appendChild(_ladderRow(0.05, 0.5));
});

document.getElementById('tdsSaveBtn').addEventListener('click', function () {
  var btn = this;
  var hint = document.getElementById('tdsHint');
  btn.disabled = true;
  fetch(BASE + '/api/trade/config', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(gatherSettings()),
  }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
    .then(function (res) {
      if (res.ok && res.d.accepted) {
        hint.textContent = '已热生效' + (res.d.changed && res.d.changed.length
          ? ' (变更: ' + res.d.changed.join(', ') + ')' : ' (无变更)');
        refresh();
      } else {
        // 422: 后端校验的字段错误原样展示 (真相校验在后端)
        hint.textContent = '保存被拒: ' + (res.d.detail || '未知错误');
      }
    }).catch(function () { hint.textContent = '保存失败: 交易服务不可达'; })
    .finally(function () { btn.disabled = false; });
});

})();
