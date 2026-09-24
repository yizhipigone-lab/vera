// ====== VERA 交易页 (P1 第三阶段, 2026-07-26) ======
// 读快照 + 发命令, 零业务判断 (铁律: 业务判断全在后端)。
// 交易 API 由交易进程 (trade_main.py, 默认 8081) 提供 —— 交易系统是
// 独立单进程, 不寄生在回测服务器 (8080) 里; 页面仍由 8080 serve。
// 轮询生命周期由 vera-ui.js 的 switchTab 经 window 钩子驱动。
//
// 2026-09-19 UIUX 改造 (docs/plan/2026-09-19_UIUX综合改造_计划书.md):
//   ① 转 ES module, 复用 decision_util 的 describeTradeError (错误口径统一:
//      "连不上"≠"服务端报错", 不再六处各写一份"不可达");
//   ② get() 补 r.ok 检查 (POST 2026-08-13 修过, GET 漏了 —— 后端 500 带 JSON body
//      会被当成功渲染成"该日无成交", 比报错更危险);
//   ③ 页面切后台停 1s 轮询, 回前台立即补刷 (手机版 9 月 16 日同款);
//   ④ 失败/废单文案色从涨红 --up 改 --danger-text (红绿只留给涨跌方向)。
import { describeTradeError, tradeApiBase } from './decision_util.mjs?v=20260919b';

(function () {
'use strict';

// 2026-09-19 架构修订批次 2.1: 8081 基址唯一出口 = decision_util.tradeApiBase
// (原来这里手写拼一份, 与 analysis.js/decision.js/mobile.html 共四份硬编码,
//  9-19 "404 被报成 8081 不可达"事故的同源温床)
var BASE = tradeApiBase(location.hostname);
var pollTimer = null;

function _serverMsg(d, r) {
  // 2026-09-20 审计 P2-10: 两种许可形状都认 (detail / success:false+error)
  var d1 = (d && d.detail) || (d && d.error);
  if (!d1) return 'HTTP ' + r.status;
  return (typeof d1 === 'string') ? d1
    : (Array.isArray(d1)
      ? d1.map(function (e) { return (e.loc ? e.loc.join('.') + ': ' : '') + e.msg; }).join('; ')
      : JSON.stringify(d1));
}
function get(u, signal) { return fetch(BASE + u, { signal: signal }).then(function (r) {
  // 2026-09-19: GET 与 POST 同口径 —— r.ok 不通过时, JSON body 里的 detail
  // 才是真话, 不能当成功数据往下渲染
  return r.json().then(function (d) {
    if (!r.ok) {
      var msg = _serverMsg(d, r);
      var err = new Error(msg);
      err.serverMsg = msg;
      throw err;
    }
    return d;
  });
}); }
function post(u, body, signal) {
  return fetch(BASE + u, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}), signal: signal,
  }).then(function (r) {
    // 2026-08-13 修复: 后端 4xx/5xx (如代码格式 422) 也返回 JSON,
    // 旧实现不查 r.ok, 把校验失败静默显示成"命令已受理", 实际从未下单。
    return r.json().then(function (d) {
      if (!r.ok) {
        var msg = _serverMsg(d, r);
        var err = new Error(msg);
        err.serverMsg = msg;
        throw err;
      }
      return d;
    });
  });
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}
// 2026-08-26: 股票代码/简称 → 同花顺标的首页链接 (web/js/stock_link.js, 先于本文件加载)。
// stock_link.js 缺失时降级为纯文本, 不炸。
function thsLink(code, text) {
  return window.stockLink ? window.stockLink(code, text) : esc(text != null && text !== '' ? text : (code || ''));
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

// ── 排序 ─────────────────────────────────────────────
// 2026-08-03: 持仓表按列排序 —— 点表头切换升降序, 第二次点同一列反转方向

var _posSort = { field: 'entry_ts', asc: false };   // 默认按入场时间最新在前
var _lastClosed = null;                      // 缓存已平仓数据, sort 重渲染时复用

// 排序用值提取: null/undefined 一律排到最后
function _sortVal(p, field) {
  var v = p[field];
  if (v === null || v === undefined) return null;
  return v;
}

function _applySort(positions) {
  if (!_posSort.field) return positions;
  var field = _posSort.field;
  var asc = _posSort.asc;
  return positions.slice().sort(function (a, b) {
    var va = _sortVal(a, field);
    var vb = _sortVal(b, field);
    if (va === null && vb === null) return 0;
    if (va === null) return 1;
    if (vb === null) return -1;
    if (asc) return va > vb ? 1 : va < vb ? -1 : 0;
    return va < vb ? 1 : va > vb ? -1 : 0;
  });
}

// ── 渲染 ─────────────────────────────────────────────

// 2026-08-01: 急停状态记忆 —— renderStatus 只在它变化时重写 tdHint,
// 不覆盖 cmd() 写入的命令反馈 (首次渲染 null≠bool 会写一次默认文案)
var _tdLastKillActive = null;

function renderStatus(s) {
  badge(document.getElementById('tdConn'), s.connected, s.connected ? '已连接' : '未连接');
  badge(document.getElementById('tdReconciled'), s.reconciled, s.reconciled ? '对账通过' : '对账未通过');
  // 2026-07-27 裁决②: 按时段显示人话原因, 不再笼统红色"降级/断连"
  var healthy = s.monitor_healthy;   // true / false / null(非连续竞价不适用)
  badge(document.getElementById('tdQuote'),
        healthy === null ? null : healthy,
        s.monitor_reason || (healthy ? '盘中·订阅正常' : '盘中·轮询兜底'));
  _renderChannelBadge(s);
  var btn = document.getElementById('tdKillBtn');
  btn.textContent = s.kill_active ? '急停中 · 点击解除' : '急停';
  btn.classList.toggle('armed', !s.kill_active);
  // 2026-08-01: tdHint 只在急停状态变化(或首次渲染)时重写 —— 否则 5s 轮询
  // 会把 cmd() 刚写入的命令反馈 ("急停命令已受理" 等) 闪现覆盖 (浏览器 E2E 实证)
  if (s.kill_active !== _tdLastKillActive) {
    _tdLastKillActive = s.kill_active;
    document.getElementById('tdHint').textContent =
      s.kill_active ? '急停激活中: 全面拒单, 解除需二次确认 (只许人工解除)'
      : 'ETF 持仓不纳入自动管理 (规则: 沪 51/56/58、深 15/16/18 前缀)';
  }
}

// ── 审计M11修复: 宕机不留"看起来正常"的陈旧数据 ──

function staleTag(id) {
  var box = document.getElementById(id);
  box.style.opacity = '0.55';
  if (!box.querySelector('.trade-stale-tag')) {
    var tag = document.createElement('div');
    tag.className = 'trade-stale-tag';
    tag.style.cssText = 'font-size:var(--fs-xs);color:var(--pending);margin-bottom:var(--sp-1)';
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
  // 徽章置断连/未知, 数据区打离线缓存角标 + 灰化
  badge(document.getElementById('tdConn'), false, '断连');
  badge(document.getElementById('tdReconciled'), null, '对账未知');
  badge(document.getElementById('tdQuote'), null, '行情未知');
  document.getElementById('tdHint').textContent =
    '交易服务 (8081) 不可达 — 显示的是最后一次成功刷新的缓存数据';
  // 2026-07-30: 对账/审计已迁交易记录 TAB (查询页, 不做 stale 角标)
  ['tdPositions', 'tdOrders'].forEach(staleTag);
}

// 2026-08-03: 生成可排序表头 —— data-sort 字段与 position 对象字段对应
function _sortTh(field, label) {
  var arrow = '';
  if (_posSort.field === field) {
    arrow = _posSort.asc ? ' ▲' : ' ▼';
  }
  return '<th class="td-sortable" data-sort="' + field + '" tabindex="0" role="button">' + label
    + '<span class="td-sort-arrow">' + arrow + '</span></th>';
}

function renderPositions(d) {
  var box = document.getElementById('tdPositions');
  clearStale('tdPositions');
  // 2026-07-30: 缓存最近持仓供卖出可用校验 (tdSellBtn 复用数量框)
  // 2026-08-03: 存原始未排序数据 —— sort 重渲染时 _lastPositions 不变
  window._lastPositions = d.positions || [];
  _lastClosed = d.closed || null;
  if (!d.positions || !d.positions.length) {
    box.innerHTML = '<div style="color:var(--text2);font-size:var(--fs-sm)">无持仓</div>'; return;
  }
  // 2026-07-30: 持仓明细增强 — 简称/入场时间/市值/盈亏比例 + 已平仓
  // 2026-07-31: 当日涨幅 (价格口径) + 当日盈亏额 ((现价-昨收)×数量)
  // 2026-08-03: 7 个可排序列: 当日涨幅/当日盈亏/市值/浮盈/盈亏%/入场时间/持仓天数
  var html = '<table class="td-table"><tr><th>代码</th><th>简称</th><th>数量</th><th>可用</th>'
    + '<th>成本</th><th>现价</th>'
    + _sortTh('day_chg_pct', '当日涨幅')
    + _sortTh('day_chg_amt', '当日盈亏')
    + _sortTh('market_value', '市值')
    + _sortTh('pnl', '浮盈')
    + _sortTh('pnl_pct', '盈亏%')
    + _sortTh('entry_ts', '入场时间')
    + _sortTh('hold_days', '持仓天数')
    + '<th>已挂阶梯档</th><th></th></tr>';
  // 2026-08-03: 应用排序后再渲染
  var positions = _applySort(window._lastPositions);
  positions.forEach(function (p) {
    var pnl = p.pnl === null ? '—' : p.pnl.toFixed(2);
    // W2-3e: 亏损色统一 --down (原用 --ok 健康语义色, 与 --down 两变量干一件事)
    var pnlColor = p.pnl === null ? '' : p.pnl >= 0 ? 'color:var(--up)' : 'color:var(--down)';
    var dayColor = p.day_chg_pct === null || p.day_chg_pct === undefined ? ''
      : p.day_chg_pct >= 0 ? 'color:var(--up)' : 'color:var(--down)';
    var dayPct = (p.day_chg_pct === null || p.day_chg_pct === undefined) ? '—'
      : (p.day_chg_pct > 0 ? '+' : '') + p.day_chg_pct.toFixed(2) + '%';
    var dayAmt = (p.day_chg_amt === null || p.day_chg_amt === undefined) ? '—'
      : (p.day_chg_amt > 0 ? '+' : '') + p.day_chg_amt.toLocaleString('zh-CN', {maximumFractionDigits: 0});
    // 2026-07-27 裁决③: ETF 行灰化 + "不管理"徽标, 无卖出按钮
    // 2026-08-11: 已平仓 (volume=0) 行同样灰化 + "已平仓"徽标 —— 后端已把
    //   avg_cost/pnl/pnl_pct/hold_days 覆盖成 买入均价/已实现盈亏/已实现%/持有天数,
    //   数量列显示买入量, 现价列显示卖出均价, 入场时间追加出场时间 (用户裁决:
    //   留在持仓表灰显展示真实盈亏, 不再是一堆 0)
    var dim = p.managed === false || p.closed === true;
    var rowStyle = dim ? ' style="opacity:.5"' : '';
    var tag = p.managed === false
      ? ' <span class="trade-badge wait">ETF·不管理</span>'
      : (p.closed === true ? ' <span class="trade-badge wait">已平仓</span>' : '');
    var qtyCell = p.closed === true
      ? (p.buy_qty != null ? p.buy_qty + '<span class="hint"> 已平</span>' : '0')
      : p.volume;
    var priceCell = p.closed === true
      ? (p.sell_avg != null ? p.sell_avg.toFixed(2) : '—')
      : (p.last === null ? '—' : p.last.toFixed(2));
    var entryCell = p.entry_ts ? fmtTs(p.entry_ts) : '—';
    if (p.closed === true && p.exit_ts) {
      entryCell += '<div style="color:var(--text2)">→ ' + fmtTs(p.exit_ts) + '</div>';
    }
    html += '<tr' + rowStyle + '><td>' + thsLink(p.code) + tag + '</td>'
      + '<td>' + (p.name ? thsLink(p.code, p.name) : '—') + '</td>'
      + '<td>' + qtyCell + '</td><td>' + (p.closed === true ? '—' : p.can_use)
      + '</td><td>' + (p.avg_cost === null ? '—' : p.avg_cost.toFixed(2))
      + '</td><td>' + priceCell
      + '</td><td style="' + dayColor + '">' + dayPct
      + '</td><td style="' + dayColor + '">' + dayAmt
      + '</td><td>' + (p.market_value === null ? '—' : p.market_value.toLocaleString('zh-CN', {maximumFractionDigits: 0}))
      + '</td><td style="' + pnlColor + '">' + pnl
      + '</td><td style="' + pnlColor + '">' + (p.pnl_pct === null ? '—' : (p.pnl_pct > 0 ? '+' : '') + p.pnl_pct.toFixed(2) + '%')
      + '</td><td style="font-size:var(--fs-xs)">' + entryCell
      + '</td><td>' + (p.hold_days === null ? '—' : p.hold_days + ' 天')
      + '</td><td>'
      + (p.tiers_done.length ? p.tiers_done.join(',') : '—')
      + '</td><td>' + (dim ? ''
      : '<button class="trade-sell-btn" data-code="' + esc(p.code) + '"' + (cmdInFlight ? ' disabled' : '') + '>卖出</button>') + '</td></tr>';
  });
  html += '</table>';
  // 已平仓 (整周期闭环: 买入量=卖出量; 出场时间 = 最后一笔卖出)
  // 2026-08-11: 本进程卖光的票已在上方持仓表灰显行展示真实盈亏, 这里只兜底
  // 已不在持仓表的历史平仓 (重启后 book 清空幽灵, 此区仍可看历史), 去重避免重复
  var _posCodes = {};
  (d.positions || []).forEach(function (p) { _posCodes[p.code] = 1; });
  var _histClosed = (d.closed || []).filter(function (c) { return !_posCodes[c.code]; });
  if (_histClosed.length) {
    html += '<div style="margin-top:var(--sp-2);color:var(--text2);font-size:var(--fs-xs)">历史已平仓（已不在持仓）</div>'
      + '<table class="td-table"><tr><th>代码</th><th>简称</th><th>数量</th>'
      + '<th>买入均价</th><th>卖出均价</th><th>已实现盈亏</th><th>盈亏%</th>'
      + '<th>持仓天数</th><th>入场时间</th><th>出场时间</th></tr>';
    _histClosed.forEach(function (c) {
      var cColor = c.realized_pnl >= 0 ? 'color:var(--up)' : 'color:var(--down)';
      html += '<tr><td>' + thsLink(c.code) + '</td><td>' + (c.name ? thsLink(c.code, c.name) : '—') + '</td>'
        + '<td>' + c.qty + '</td>'
        + '<td>' + (c.buy_avg != null ? c.buy_avg.toFixed(2) : '—') + '</td>'
        + '<td>' + (c.sell_avg != null ? c.sell_avg.toFixed(2) : '—') + '</td>'
        + '<td style="' + cColor + '">' + (c.realized_pnl >= 0 ? '+' : '') + c.realized_pnl.toFixed(2) + '</td>'
        + '<td style="' + cColor + '">' + (c.realized_pnl_pct != null ? (c.realized_pnl_pct > 0 ? '+' : '') + c.realized_pnl_pct.toFixed(2) + '%' : '—') + '</td>'
        + '<td>' + (c.hold_days != null ? c.hold_days + ' 天' : '—') + '</td>'
        + '<td style="font-size:var(--fs-xs)">' + fmtTs(c.entry_ts) + '</td>'
        + '<td style="font-size:var(--fs-xs)">' + fmtTs(c.exit_ts) + '</td></tr>';
    });
    html += '</table>';
  }
  box.innerHTML = html;
  box.querySelectorAll('.trade-sell-btn').forEach(function (b) {
    b.addEventListener('click', function () {
      var code = b.getAttribute('data-code');
      if (!confirm('确认卖出 ' + code + ' 全部可用持仓?\n(走撤单流水线: 撤预埋 → 买一价 → 超时升级逃生通道 (限价))')) return;
      // 审计M12修复: 失败提示 + 防连点, 与买入路径一致
      cmd(b, '/api/trade/sell', { code: code }, '卖出命令已受理，等待执行结果…',
        'tdBuyHint', function () {
          watchCmdResult(code, document.getElementById('tdBuyHint'));
        });
    });
  });
  // 2026-08-03: 可排序表头点击 —— 同列反转方向, 不同列切列且默认升序
  box.querySelectorAll('.td-sortable').forEach(function (th) {
    // 2026-09-06 审计: 键盘可达 —— Enter/Space 等同点击 (原只绑 click, 键盘用户进不来)
    th.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); th.click(); }
    });
    th.addEventListener('click', function () {
      var field = th.getAttribute('data-sort');
      if (!field) return;
      if (_posSort.field === field) {
        _posSort.asc = !_posSort.asc;
      } else {
        _posSort.field = field;
        _posSort.asc = true;
      }
      // 用缓存的原始数据重渲染 (不发起网络请求)
      renderPositions({ positions: window._lastPositions, closed: _lastClosed });
    });
  });
}

// 订单状态码 → 中文说明 (对齐 trade/book.py:26-37, xtquant xtconstant)。
// 废单标红 (要关注: 超涨停/验资失败等), 在途 (已报/待撤/部成) 标 pending 色。
var ORDER_STATUS_TEXT = {
  48: '未报', 49: '待报', 50: '已报', 51: '已报待撤', 52: '部成待撤',
  53: '部撤', 54: '已撤', 55: '部成', 56: '已成', 57: '废单', 255: '未知'
};
function orderStatusHtml(st, msg) {
  var txt = ORDER_STATUS_TEXT[st] || ('未知(' + st + ')');
  var html;
  if (st === 57) html = '<span style="color:var(--danger-text);font-weight:700">' + txt + '</span>';
  else if (st === 50 || st === 51 || st === 55) html = '<span style="color:var(--pending-text)">' + txt + '</span>';
  else html = txt;
  // 2026-08-07: 废单/已撤原因 (XtOrder.status_msg, 券商原话) 挂在状态下方
  if (msg) html += '<br><span style="color:var(--text2);font-size:var(--fs-xs)">' + esc(msg) + '</span>';
  return html;
}

function renderOrders(d) {
  var box = document.getElementById('tdOrders');
  clearStale('tdOrders');
  if (!d.orders || !d.orders.length) {
    box.innerHTML = '<div style="color:var(--text2);font-size:var(--fs-sm)">当日无委托</div>'; return;
  }
  // 2026-07-30: 委托表加撤单按钮 (用户要求) — 终态 (部撤53/已撤/已成/废单) 不可撤
  // 2026-08-07 (用户要求): 状态说明列 —— 原始代码是 xtquant 状态码, 小白看不懂
  // 2026-08-10 (用户要求): 时间列 = 下单时刻 created_ts (不再用 updated_ts ——
  // 那是最后一次对账回写时刻, 已成单曾显示成 15:10 同步时间而非真实委托时间)
  var html = '<table class="td-table"><tr><th>时间</th><th>备注</th><th>代码</th>'
    + '<th>简称</th><th>方向</th><th>价格</th><th>数量</th><th>已成交</th><th>状态</th><th>状态说明</th><th></th></tr>';
  d.orders.forEach(function (o) {
    // 审计L12修复: qty/filled_qty/status 统一 Number() 强转 ——
    // 其他字段走 esc, 这三个直插 innerHTML 是纵深防御缺口
    var st = Number(o.status);
    var canCancel = (st === 50 || st === 51 || st === 55);   // 已报/待撤/部成 可撤
    // 2026-07-30: remark 为空 = 手工单 (券商端/手机端委托, 同步认领进表)
    var remark = o.remark ? esc(o.remark) : '<span class="trade-badge wait">手工</span>';
    html += '<tr><td>' + fmtTs(o.created_ts || o.updated_ts) + '</td><td>' + remark + '</td><td>'
      + thsLink(o.code) + '</td><td>' + (o.name ? thsLink(o.code, o.name) : '—') + '</td><td>' + (Number(o.direction) === 23 ? '买' : '卖') + '</td><td>'
      + Number(o.price).toFixed(2) + '</td><td>' + Number(o.qty) + '</td><td>'
      + Number(o.filled_qty) + '</td><td>' + st + '</td><td>' + orderStatusHtml(st, o.status_msg || '') + '</td><td>'
      + (canCancel
        ? '<button class="trade-sell-btn trade-cancel-btn" data-oid="' + esc(o.order_id) + '"' + (cmdInFlight ? ' disabled' : '') + '>撤单</button>'
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
    box.innerHTML = '<div style="color:var(--text2);font-size:var(--fs-sm)">暂无对账记录</div>'; return;
  }
  var html = '<table class="td-table"><tr><th>时间</th><th>级别</th><th>代码</th><th>简称</th>'
    + '<th>本地</th><th>QMT</th><th>说明</th></tr>';
  d.reconciles.slice(0, 20).forEach(function (r) {
    var color = r.level === 'CRITICAL' ? 'color:var(--danger-text);font-weight:700'
      : r.level === 'WARN' ? 'color:var(--pending-text)' : 'color:var(--ok-text)';
    var detail = '';
    try { detail = JSON.parse(r.detail_json || '{}').reason || ''; } catch (e) {}
    html += '<tr><td>' + fmtTs(r.ts) + '</td><td style="' + color + '">' + esc(r.level)
      + '</td><td>' + (r.code ? thsLink(r.code) : '—') + '</td><td>' + (r.name ? thsLink(r.code, r.name) : '')
      + '</td><td>' + esc(r.expected) + '</td><td>'
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

// ── 交易记录 TAB (2026-07-30): 查询型页面, 不进 5s 轮询 ──
// 切入时自动加载 + 手动查询/刷新; 成交/委托支持 YYYYMMDD 查历史。

function renderDeals(d) {
  var box = document.getElementById('recDeals');
  if (!d.deals || !d.deals.length) {
    box.innerHTML = '<div style="color:var(--text2);font-size:var(--fs-sm)">该日无成交</div>'; return;
  }
  // 2026-07-30: 来源列 — 区分手工单 (券商端/手机端, 对账认领) 与系统单
  // 2026-07-31: 原因列 — 为什么成交 (trades.reason: 阶梯止盈·档1/移动止盈/
  // 硬止损/人工卖出...); 空 = 无 fill context (买入/部成续笔/历史行)
  // 2026-08-03: 盈亏金额/盈亏比例列 — 卖出记录计算 (卖价-买入均价)×数量 及百分比
  var html = '<table class="td-table"><tr><th>时间</th><th>代码</th><th>简称</th>'
    + '<th>方向</th><th>来源</th><th>原因</th><th>价格</th><th>数量</th><th>金额</th>'
    + '<th>盈亏金额</th><th>盈亏比例</th><th>委托号</th></tr>';
  d.deals.forEach(function (t) {
    var isBuy = Number(t.direction) === 23;
    var isSell = Number(t.direction) === 24;
    var src = t.source === 'manual' ? '<span class="trade-badge wait">手工</span>'
      : t.source === 'system' ? '系统' : '—';
    // 盈亏列: 仅卖出有值, 买入显示 "—"
    var pnlAmt = '—', pnlPct = '—', pnlColor = '';
    if (isSell && t.pnl_amount !== null && t.pnl_amount !== undefined) {
      pnlColor = t.pnl_amount >= 0 ? 'color:var(--up)' : 'color:var(--down)';
      pnlAmt = (t.pnl_amount >= 0 ? '+' : '') + t.pnl_amount.toLocaleString('zh-CN', {maximumFractionDigits: 2});
      pnlPct = (t.pnl_pct >= 0 ? '+' : '') + t.pnl_pct.toFixed(2) + '%';
    }
    html += '<tr><td>' + fmtTs(t.ts) + '</td><td>' + thsLink(t.code) + '</td><td>'
      + (t.name ? thsLink(t.code, t.name) : '—') + '</td><td style="color:'
      + (isBuy ? 'var(--up)' : 'var(--down)') + '">' + (isBuy ? '买' : '卖') + '</td><td>'
      + src + '</td><td>' + (t.reason ? esc(t.reason) : '—') + '</td><td>'
      + Number(t.price).toFixed(2) + '</td><td>' + Number(t.qty) + '</td><td>'
      + Number(t.amount).toLocaleString('zh-CN', {maximumFractionDigits: 2}) + '</td>'
      + '<td style="' + pnlColor + '">' + pnlAmt + '</td>'
      + '<td style="' + pnlColor + '">' + pnlPct + '</td><td>'
      + esc(t.order_id) + '</td></tr>';
  });
  box.innerHTML = html + '</table>';
}

function renderHistoryOrders(d) {
  var box = document.getElementById('recOrders');
  if (!d.orders || !d.orders.length) {
    box.innerHTML = '<div style="color:var(--text2);font-size:var(--fs-sm)">该日无委托</div>'; return;
  }
  // 历史台账只读 — 撤单按钮只在交易页"当日委托"卡片 (操作区)
  var html = '<table class="td-table"><tr><th>时间</th><th>备注</th><th>代码</th>'
    + '<th>简称</th><th>方向</th><th>价格</th><th>数量</th><th>已成交</th><th>状态</th><th>状态说明</th></tr>';
  d.orders.forEach(function (o) {
    var remark = o.remark ? esc(o.remark) : '<span class="trade-badge wait">手工</span>';
    html += '<tr><td>' + fmtTs(o.created_ts || o.updated_ts) + '</td><td>' + remark + '</td><td>'
      + thsLink(o.code) + '</td><td>' + (o.name ? thsLink(o.code, o.name) : '—') + '</td><td>' + (Number(o.direction) === 23 ? '买' : '卖') + '</td><td>'
      + Number(o.price).toFixed(2) + '</td><td>' + Number(o.qty) + '</td><td>'
      + Number(o.filled_qty) + '</td><td>' + Number(o.status) + '</td><td>'
      + orderStatusHtml(Number(o.status), o.status_msg || '') + '</td></tr>';
  });
  box.innerHTML = html + '</table>';
}

function _dateParam(inputId, hintId) {
  var v = document.getElementById(inputId).value.trim();
  if (v && !/^\d{8}$/.test(v)) {
    document.getElementById(hintId).textContent = '日期格式应为 YYYYMMDD';
    return null;
  }
  document.getElementById(hintId).textContent = '';
  return v ? '?date=' + v : '';
}

// W2-2: 查询按钮 loading 态 — 点击即禁用+文案加"…", 完成恢复 (旧行为挂起时像"没反应")
function _withBtnLoading(btn, fn) {
  var original = btn.textContent;
  btn.disabled = true;
  btn.textContent = original + '…';
  Promise.resolve(fn()).finally(function () { btn.disabled = false; btn.textContent = original; });
}

function loadDeals() {
  var q = _dateParam('recDealDate', 'recDealHint');
  if (q === null) return;
  _withBtnLoading(document.getElementById('recDealBtn'), function () {
    return get('/api/trade/deals' + q).then(renderDeals).catch(function (err) {
      document.getElementById('recDealHint').textContent = '查询失败: ' + describeTradeError(err);
    });
  });
}

function loadHistoryOrders() {
  var q = _dateParam('recOrderDate', 'recOrderHint');
  if (q === null) return;
  _withBtnLoading(document.getElementById('recOrderBtn'), function () {
    return get('/api/trade/orders' + q).then(renderHistoryOrders).catch(function (err) {
      document.getElementById('recOrderHint').textContent = '查询失败: ' + describeTradeError(err);
    });
  });
}

function loadReconciles() {
  _withBtnLoading(document.getElementById('recReconcileBtn'), function () {
    return get('/api/trade/reconciles?limit=50').then(renderReconciles).catch(function (err) {
      document.getElementById('tdReconciles').innerHTML =
        '<div style="color:var(--danger-text);font-size:var(--fs-sm)">查询失败: ' + esc(describeTradeError(err)) + '</div>';
    });
  });
}

function loadAudits() {
  _withBtnLoading(document.getElementById('recAuditBtn'), function () {
    return get('/api/trade/audits?limit=100').then(renderAudits).catch(function (err) {
      document.getElementById('tdAudits').textContent = '查询失败: ' + describeTradeError(err);
    });
  });
}

window.recordsPageEnter = function () {
  loadDeals(); loadHistoryOrders(); loadReconciles(); loadAudits();
};
window.recordsPageLeave = function () {};   // 无轮询可停, 钩子对齐 trade 页生命周期

document.getElementById('recDealBtn').addEventListener('click', loadDeals);
document.getElementById('recOrderBtn').addEventListener('click', loadHistoryOrders);
document.getElementById('recReconcileBtn').addEventListener('click', loadReconciles);
document.getElementById('recAuditBtn').addEventListener('click', loadAudits);

// ── 资产卡片 (2026-07-30: 总资产/当日盈亏/可用/市值/冻结, QMT query_asset) ──

function renderAsset(a) {
  var card = document.getElementById('tdAssetCard');
  if (!a || a.error) { if (card) card.style.display = 'none'; return; }
  card.style.display = '';
  card.style.opacity = '1';  // 清除 staleTag 灰显
  function fmt(v) { return v != null ? '¥' + Number(v).toLocaleString('zh-CN', {maximumFractionDigits:2}) : '—'; }
  document.getElementById('tdTotalAsset').textContent = fmt(a.total_asset);
  document.getElementById('tdCash').textContent = fmt(a.cash);
  document.getElementById('tdMarketValue').textContent = fmt(a.market_value);
  document.getElementById('tdFrozenCash').textContent = fmt(a.frozen_cash);
  // 当日盈亏基准: 优先昨日日终资产快照 (后端 prev_day_asset, 与
  // 分析板块日历同口径, 2026-08-07); 取不到回退 localStorage 首拉基准。
  var base = null;
  if (a.prev_day_asset != null && a.prev_day_asset > 0) {
    base = String(a.prev_day_asset);
  } else {
    var today = new Date().toDateString();
    var baseKey = 'vera_asset_base_' + today;
    base = localStorage.getItem(baseKey);
    if (!base && a.total_asset != null) {
      localStorage.setItem(baseKey, a.total_asset);
      base = a.total_asset;
    }
  }
  var pnlEl = document.getElementById('tdDayPnl');
  var pctEl = document.getElementById('tdDayPnlPct');
  if (base && a.total_asset != null) {
    var pnl = a.total_asset - parseFloat(base);
    var pct = parseFloat(base) > 0 ? pnl / parseFloat(base) * 100 : 0;
    pnlEl.textContent = (pnl >= 0 ? '+¥' : '-¥') + Math.abs(pnl).toLocaleString('zh-CN', {maximumFractionDigits:2});
    pnlEl.style.color = pnl >= 0 ? 'var(--up)' : 'var(--down)';  // A股涨红跌绿
    pctEl.textContent = (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';
    pctEl.style.color = pnl >= 0 ? 'var(--up)' : 'var(--down)';
  } else {
    pnlEl.textContent = '—'; pnlEl.style.color = '';
    pctEl.textContent = '';
  }
}

// ── 轮询 ─────────────────────────────────────────────

// 审计L12修复: 在途保护 (一轮未回完不发起下一轮) + fetch 4s 超时
var inflight = false;

// W2-1 (2026-09-05): 脏检查 — 数据没变不重绘。
// 旧实现每秒全量 innerHTML 重建持仓/委托表: 选中文字 1 秒被清、悬停闪烁、
// cmd() 刚禁用的行内按钮被换成全新可用按钮。
// 比较键只取各 render 实际消费的字段(见 _consumed), 不 stringify 整个响应 —
// 防响应体携带前端未消费的 server_time 类字段让脏检查每秒白干。
// 失败分支删除对应 key: 服务恢复后即使数据与断线前相同也强制重绘(清离线角标)。
var _lastPayload = {};
function _consumed(key, d) {
  if (!d || typeof d !== 'object') return JSON.stringify(d);
  switch (key) {
    case 'status': return JSON.stringify([d.connected, d.reconciled, d.monitor_healthy, d.monitor_reason, d.kill_active]);
    case 'positions': return JSON.stringify([d.positions, d.closed]);
    case 'orders': return JSON.stringify([d.orders]);
    case 'auto_buy': return JSON.stringify([d.config, d.last]);
    case 'rotation': return JSON.stringify([d.config, d.last]);
    case 'asset': return JSON.stringify([d.total_asset, d.cash, d.market_value, d.frozen_cash, d.prev_day_asset]);
  }
  return JSON.stringify(d);
}
function _changed(key, d) {
  var s = _consumed(key, d);
  if (_lastPayload[key] === s) return false;
  _lastPayload[key] = s;
  return true;
}
// 命令受理/保存成功后强制下次全量刷新
function invalidatePayload() { _lastPayload = {}; }

function refresh() {
  if (inflight) return;
  inflight = true;
  var ctl = new AbortController();
  var timer = setTimeout(function () { ctl.abort(); }, 4000);
  var s = ctl.signal;
  // 2026-07-30: 对账/审计迁交易记录 TAB (查询页手动加载), 驾驶舱轮询瘦身
  Promise.allSettled([
    get('/api/trade/status', s).then(function (d) {
      if (_changed('status', d)) renderStatus(d);
    }).catch(function () { delete _lastPayload.status; markOffline(); }),
    get('/api/trade/positions', s).then(function (d) {
      if (_changed('positions', d)) renderPositions(d); else clearStale('tdPositions');
    }).catch(function () { delete _lastPayload.positions; staleTag('tdPositions'); }),
    get('/api/trade/orders', s).then(function (d) {
      if (_changed('orders', d)) renderOrders(d); else clearStale('tdOrders');
    }).catch(function () { delete _lastPayload.orders; staleTag('tdOrders'); }),
    get('/api/trade/auto_buy/last', s).then(function (d) {
      if (_changed('auto_buy', d)) renderAutoBuy(d);
    }).catch(function () { delete _lastPayload.auto_buy; }),
    get('/api/trade/rotation/last', s).then(function (d) {
      if (_changed('rotation', d)) renderRotation(d);
    }).catch(function () { delete _lastPayload.rotation; }),
    get('/api/trade/asset', s).then(function (d) {
      if (_changed('asset', d)) renderAsset(d);
    }).catch(function () { delete _lastPayload.asset; staleTag('tdAssetCard'); }),
  ]).then(function () { clearTimeout(timer); inflight = false; });
}

window.tradePageEnter = function () {
  refresh();
  if (pollTimer) clearInterval(pollTimer);
  // 2026-08-06 (用户要求): 5s → 1s 准实时。后端 positions 读 tick 内存
  // 缓存 (微秒级), 5 接口 × 1Hz 对本地 uvicorn 无感; 在途保护照旧
  pollTimer = setInterval(refresh, 1000);
};
window.tradePageLeave = function () {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
};

// 2026-09-19 UIUX: 页面切后台(浏览器 tab 隐藏/锁屏)停 1s 轮询, 回前台立即补刷 —
// 手机版 9 月 16 日已修同款问题, PC 端补齐 (window._veraTab 由 vera-ui.js 维护)
document.addEventListener('visibilitychange', function () {
  if (document.hidden) {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    return;
  }
  if (window._veraTab === 'trade' && !pollTimer) {
    refresh();
    pollTimer = setInterval(refresh, 1000);
  }
});

// ── 命令绑定 ─────────────────────────────────────────

// W2-4: 命令在途标志 —— cmd 期间重渲染的行内按钮保持禁用 (与 W2-1 脏检查、
// confirm 阻塞叠加成三层防线; 旧行为靠 confirm 模态阻塞"碰巧"防住双击)
var cmdInFlight = false;

// 审计M12修复: 所有命令统一收口 —— 失败有明确提示 (与买入一致),
// 请求期间按钮 disable 防连点, 4s 超时
// 2026-08-01: hintId 可选 —— 反馈写就近 hint (买/卖/预埋 tdBuyHint),
// 默认 tdHint (急停/尾盘选股); renderStatus 已改为不覆盖非急停期的 tdHint
function cmd(btn, url, body, okMsg, hintId, onAccepted) {
  btn.disabled = true;
  cmdInFlight = true;
  // 已在页面上的行内按钮 (卖出/撤单) 同步禁用, 防在途期间点击
  document.querySelectorAll('.trade-sell-btn,.trade-cancel-btn').forEach(function (b) { b.disabled = true; });
  var hintEl = document.getElementById(hintId || 'tdHint');
  var ctl = new AbortController();
  var timer = setTimeout(function () { ctl.abort(); }, 4000);
  post(url, body, ctl.signal).then(function () {
    hintEl.textContent = okMsg;
    if (onAccepted) onAccepted();
    invalidatePayload();   // W2-1: 命令受理后强制下次全量刷新
    refresh();
  }).catch(function (e) {
    hintEl.textContent = (e && e.serverMsg)
      ? ('后端拒绝: ' + e.serverMsg)
      : '命令发送失败: 交易服务 (8081) 不可达';
  }).finally(function () {
    clearTimeout(timer);
    cmdInFlight = false;
    btn.disabled = false;
  });
}

// 2026-08-13 (用户要求): 委托结果回显 —— "已受理"只表示命令进了队列,
// 过闸/下单是异步的。受理后轮询审计日志 (1s×10), 把该代码的真实结果
// (下单成功/风控拒绝/无行情/下单报错) 直接显示在按钮旁, 不用翻审计 Tab。
var CMD_RESULT_KINDS = {
  manual_buy: '买入已下单',
  buy_fail_closed: '买入被拒',
  risk_reject: '风控拒绝',
  exit_sell: '卖出已下单',
  exit_risk_reject: '卖出被拒',
  exit_skip: '卖出被拒',
  exit_fail_closed: '卖出被拒',
  exit_lock_fail: '卖出未执行',
  order_error: '下单报错',
};

function watchCmdResult(code, hintEl) {
  var since = Date.now() / 1000 - 3;  // 3s 余量吸收前后端时钟差
  var tries = 0;
  var timer = setInterval(function () {
    tries += 1;
    if (tries > 10) { clearInterval(timer); return; }
    get('/api/trade/audits?limit=20').then(function (d) {
      var list = (d && d.audits) || [];
      for (var i = 0; i < list.length; i++) {
        var a = list[i];
        if (!CMD_RESULT_KINDS[a.kind] || a.ts < since) continue;
        var detail = {};
        try { detail = JSON.parse(a.detail_json || '{}'); } catch (e) {}
        if (detail.code && detail.code !== code) continue;
        clearInterval(timer);
        var msg = a.message;
        if (a.kind === 'manual_buy' && detail.order_id) {
          msg += ' (委托号 ' + detail.order_id + ')';
        }
        hintEl.textContent = CMD_RESULT_KINDS[a.kind] + ' — ' + msg;
        refresh();
        return;
      }
    }).catch(function () {});
  }, 1000);
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

// 2026-08-25: 人工买入支持 ETF —— 代码输入归一化: 裸 6 位/小写/sh 前缀
// 写法自动补市场后缀 (51/56/58/6→SH, 15/16/18/0/3→SZ, 4/8/92→BJ);
// 显式带后缀时信任用户输入; 无法推导返回 null 维持原格式提示, 不瞎猜。
function normalizeTradeCode(raw) {
  var c = (raw || '').trim().toUpperCase();
  var m = c.match(/^(SH|SZ|BJ)?\.?(\d{6})(?:\.(SH|SZ|BJ))?$/);
  if (!m) return null;
  var num = m[2], suffix = m[1] || m[3];
  if (!suffix) {
    if (/^(51|56|58|6)/.test(num)) suffix = 'SH';
    else if (/^(15|16|18|0|3)/.test(num)) suffix = 'SZ';
    else if (/^(4|8|92)/.test(num)) suffix = 'BJ';
    else return null;
  }
  return num + '.' + suffix;
}
// ETF 判定口径与后端 trade/book.is_etf 一致 (沪 51/56/58, 深 15/16/18)
function isEtfCode(c) {
  return /^(51|56|58|15|16|18)/.test((c || '').split('.')[0]);
}
// ETF 最小价位 0.001 (股票 0.01), 价格输入 step 随代码动态切换
document.getElementById('tdBuyCode').addEventListener('input', function () {
  document.getElementById('tdBuyPrice').step = isEtfCode(this.value) ? '0.001' : '0.01';
});

document.getElementById('tdBuyBtn').addEventListener('click', function () {
  var code = normalizeTradeCode(document.getElementById('tdBuyCode').value);
  var qty = parseInt(document.getElementById('tdBuyQty').value, 10);
  var priceRaw = document.getElementById('tdBuyPrice').value.trim();
  var hint = document.getElementById('tdBuyHint');
  if (!code) {
    hint.textContent = '代码格式: 6位数字 (自动补市场后缀), 如 600519 / 518880 / 600519.SH'; return;
  }
  document.getElementById('tdBuyCode').value = code;  // 回填规范形, 所见即所买
  if (!qty || qty <= 0 || qty % 100 !== 0) { hint.textContent = '数量必须是 100 的整数倍'; return; }
  var body = { code: code, qty: qty };
  if (priceRaw) body.price = parseFloat(priceRaw);
  var etfNote = isEtfCode(code)
    ? '\n注意: ETF 不纳入自动止盈止损/预埋, 需人工盯盘管理。' : '';
  if (!confirm('待确认买入: ' + code + ' ' + qty + ' 股'
      + (body.price ? ' @' + body.price : ' (最新价)') + etfNote
      + '\n将过风控闸门后下单, 确认?')) return;
  cmd(this, '/api/trade/buy', body, '买入命令已受理, 等待过闸结果…', 'tdBuyHint',
    function () { watchCmdResult(code, hint); });
});

// 2026-07-30: 配套手工卖出 — 复用代码+数量输入框, 校验可用后走 /api/trade/sell
document.getElementById('tdSellBtn').addEventListener('click', function () {
  var code = normalizeTradeCode(document.getElementById('tdBuyCode').value);
  var qtyRaw = document.getElementById('tdBuyQty').value.trim();
  var hint = document.getElementById('tdBuyHint');
  if (!code) {
    hint.textContent = '代码格式: 6位数字 (自动补市场后缀), 如 600519 / 518880 / 600519.SH'; return;
  }
  document.getElementById('tdBuyCode').value = code;
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
  if (!confirm('确认卖出 ' + code + ' ' + qtyText + '?\n(走撤单流水线: 撤预埋 → 买一价 → 超时升级逃生通道 (限价))')) return;
  cmd(this, '/api/trade/sell', body, '卖出命令已受理，等待执行结果…', 'tdBuyHint',
    function () { watchCmdResult(code, hint); });
});

document.getElementById('tdLadderBtn').addEventListener('click', function () {
  // W1-6: 预埋会真实挂出阶梯委托, 与买入/卖出/撤单一致补二次确认 (其余交易动作均有 confirm)
  if (!confirm('确认手动触发当日预埋单? 将按阶梯价格真实挂出委托。')) return;
  cmd(this, '/api/trade/ladder', {}, '预埋命令已受理', 'tdBuyHint');});

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
    box.innerHTML = '<div style="color:var(--text2);font-size:var(--fs-sm)">今日尚未运行</div>';
    return;
  }
  if (last.error) {
    box.innerHTML = '<div style="color:var(--danger-text);font-size:var(--fs-sm)">运行失败: '
      + esc(last.error) + '</div>';
    return;
  }
  var html = '<div style="font-size:var(--fs-sm);margin-bottom:var(--sp-2)">最近运行 '
    + fmtTs(last.ts) + ' (' + esc(last.source) + '): 选中 <b>' + last.selected
    + '</b> / 买入 <b>' + last.bought + '</b></div>';
  if (last.dispositions && last.dispositions.length) {
    html += '<table class="td-table"><tr><th>代码</th><th>处置</th><th>价</th><th>量</th><th>状态</th><th>说明</th></tr>';
    last.dispositions.forEach(function (dp) {
      var isBuy = dp.action === 'buy';
      html += '<tr><td>' + thsLink(dp.code) + '</td><td style="color:'
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

// ── ETF 轮动系统卡片 (2026-08-14; 2026-08-20 动量改造) ────────────────────

function renderRotation(d) {
  var cfgBox = document.getElementById('tdRotCfg');
  var nTr = (d.config.signal_day && d.config.signal_day.length) || 1;
  cfgBox.textContent = (d.config.enabled ? '已启用' : '已停用')
    + ' · ETF池 ' + Math.round(d.config.etf_ratio * 100) + '%'
    + (nTr > 1 ? ' · ' + nTr + '份错峰' : '')
    + ' · 执行 ' + d.config.execute_time;
  var box = document.getElementById('tdRotLast');
  var last = d.last;
  if (!last) {
    box.innerHTML = '<div style="color:var(--text2);font-size:var(--fs-sm)">尚未运行</div>';
    return;
  }
  if (last.error) {
    box.innerHTML = '<div style="color:var(--danger-text);font-size:var(--fs-sm)">失败: '
      + esc(last.error) + '</div>';
    return;
  }
  // 2026-09-16 三份错峰: 逐份渲染 (每份一行: 信号日/目标/动量/止损基准)
  var trs = last.tranches;
  if (trs && trs.length) {
    var html = '<div style="font-size:var(--fs-sm);margin-bottom:var(--sp-2)">最近 '
      + fmtTs(last.ts) + ' (' + esc(last.source) + ')</div>';
    trs.forEach(function (t) {
      var s = t.signal || {};
      var line = '<div style="font-size:var(--fs-sm);margin-bottom:var(--sp-1)">'
        + '<b>份' + (t.tranche + 1) + '·' + esc(t.anchor) + '</b>'
        + ' 目标 <b style="color:var(--up)">' + esc(s.target || '避险篮子') + '</b>';
      if (s.momentum) {
        var legs = [];
        Object.keys(s.momentum).forEach(function (c) {
          var m = s.momentum[c];
          legs.push(c + ' ' + (m == null ? '—' : (m * 100).toFixed(1) + '%'));
        });
        line += ' <span style="color:var(--text2)">动量 '
          + legs.map(function (x) { return esc(x); }).join(' / ') + '</span>';
      }
      if (s.entry_high && Object.keys(s.entry_high).length) {
        var eh = Object.keys(s.entry_high).map(function (c) {
          return c + '@' + Number(s.entry_high[c]).toFixed(3);
        }).join(' / ');
        line += ' <span style="color:var(--text2)">止损基准 ' + esc(eh) + '</span>';
      }
      if (s.note) {
        line += ' <span style="color:var(--text2)">' + esc(s.note) + '</span>';
      }
      html += line + '</div>';
    });
    box.innerHTML = html;
    return;
  }
  var s = last.signal || {};
  // 跳过/非交易日 (只有 note, 无 target/momentum)
  if (s.target === undefined && s.momentum === undefined && s.note) {
    box.innerHTML = '<div style="color:var(--text2);font-size:var(--fs-sm)">'
      + esc(s.note) + '</div>';
    return;
  }
  var html = '<div style="font-size:var(--fs-sm);margin-bottom:var(--sp-2)">最近 ' + fmtTs(last.ts)
    + ' (' + esc(last.source) + '): 目标 <b style="color:var(--up)">'
    + esc(s.target || '避险篮子') + '</b>'
    + (s.date ? ' <span style="color:var(--text2)">(基于 ' + esc(s.date) + ' 收盘)</span>' : '')
    + (s.pending_target !== undefined
       ? ' <span style="color:var(--text2)">· 待执行 ' + esc(s.pending_target || '避险') + '</span>'
       : '')
    + '</div>';
  if (s.momentum) {
    var legs = [];
    Object.keys(s.momentum).forEach(function (c) {
      var m = s.momentum[c];
      legs.push(c + ' ' + (m == null ? '—' : (m * 100).toFixed(1) + '%'));
    });
    html += '<div style="font-size:var(--fs-sm);color:var(--text2)">4周动量 '
      + legs.map(function (x) { return esc(x); }).join(' / ')
      + (s.reason ? ' · ' + esc(s.reason) : '')
      + (s.data_source ? ' · 源 ' + esc(s.data_source) : '') + '</div>';
  }
  if (s.entry_high && Object.keys(s.entry_high).length) {
    var eh = Object.keys(s.entry_high).map(function (c) {
      return c + '@' + Number(s.entry_high[c]).toFixed(3);
    }).join(' / ');
    html += '<div style="font-size:var(--fs-sm);color:var(--text2)">移动止损基准(持仓期最高) '
      + esc(eh) + '</div>';
  }
  if (s.note && s.target !== undefined) {
    html += '<div style="font-size:var(--fs-sm);color:var(--text2)">' + esc(s.note) + '</div>';
  }
  box.innerHTML = html;
}

document.getElementById('tdRotRunBtn').addEventListener('click', function () {
  if (!confirm('立即执行一次 ETF 轮动 (算信号 + 调仓)?\n将按信号卖出超出的 ETF、买入目标 ETF, 确认?')) return;
  cmd(this, '/api/trade/rotation/run', {}, '已发起, 结果见本卡片与审计日志', 'tdRotHint');
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

// ETF 池占比滑杆 (2026-08-14): 拖动实时显示两边百分比
function _rotRatioLabel(v) {
  document.getElementById('tdsRotRatioLabel').textContent =
    'ETF池 ' + v + '% · 股票池 ' + (100 - v) + '%';
}
document.getElementById('tdsRotRatio').addEventListener('input', function () {
  _rotRatioLabel(parseInt(this.value, 10));
});

// 避险腿黄金占比滑杆 (2026-08-18): 拖动实时显示黄金/避险ETF2两边百分比
function _rotHedgeLabel(v) {
  document.getElementById('tdsRotHedgeLabel').textContent =
    '黄金 ' + v + '% · 避险ETF2 ' + (100 - v) + '%';
}
document.getElementById('tdsRotHedge').addEventListener('input', function () {
  _rotHedgeLabel(parseInt(this.value, 10));
});

// 移动止损滑杆 (2026-08-20 动量改造): 拖动实时显示回撤阈值百分比
function _rotTrailLabel(v) {
  document.getElementById('tdsRotTrailLabel').textContent =
    '回撤超 ' + v + '% 切避险';
}
document.getElementById('tdsRotTrail').addEventListener('input', function () {
  _rotTrailLabel(parseInt(this.value, 10));
});

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
  _setc('tdsSellEn', cfg.auto_sell_enabled);
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
  // ETF 轮动区 (2026-08-14)
  _setc('tdsRotEn', cfg.rotation.enabled);
  var rotPct = Math.round((cfg.rotation.etf_ratio || 0.5) * 100);
  _setv('tdsRotRatio', rotPct); _rotRatioLabel(rotPct);
  _setv('tdsRotTime', cfg.rotation.execute_time);
  _setv('tdsRotRisk2', cfg.rotation.risk_etf2 || '');
  _setv('tdsRotCyb', cfg.rotation.cyb_etf);
  _setv('tdsRotGold', cfg.rotation.gold_etf);
  _setv('tdsRotHedge2', cfg.rotation.hedge_etf2 || '');
  _setv('tdsRotMomWin', cfg.rotation.momentum_window != null
        ? cfg.rotation.momentum_window : 20);
  var trailPct = Math.round(((cfg.rotation.trailing_stop_pct != null
                              ? cfg.rotation.trailing_stop_pct : 0.15)) * 100);
  _setv('tdsRotTrail', trailPct); _rotTrailLabel(trailPct);
  // 2026-09-16 错峰: signal_day 多选 (数组), 兼容旧单字符串
  var sd = cfg.rotation.signal_day || ['friday'];
  if (typeof sd === 'string') sd = [sd];
  var sdSel = document.getElementById('tdsRotSignalDay');
  for (var sdOi = 0; sdOi < sdSel.options.length; sdOi++) {
    sdSel.options[sdOi].selected = sd.indexOf(sdSel.options[sdOi].value) >= 0;
  }
  var hedgePct = Math.round(((cfg.rotation.hedge_ratio != null
                              ? cfg.rotation.hedge_ratio : 1.0)) * 100);
  _setv('tdsRotHedge', hedgePct); _rotHedgeLabel(hedgePct);
  // 弱市择时闸门区 (2026-08-16)
  _setc('tdsRfEn', cfg.regime_filter && cfg.regime_filter.enabled);
  _setv('tdsRfIndex', cfg.regime_filter && cfg.regime_filter.index_code);
  _setv('tdsRfMa', cfg.regime_filter && cfg.regime_filter.ma_window);
  // 飞书通知区 (2026-07-31): 仅总开关 + AI 复盘开关; webhook URL 走环境变量
  _setc('tdsFeishuEn', cfg.feishu && cfg.feishu.enabled);
  _setc('tdsAiReview', cfg.feishu && cfg.feishu.ai_review);
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
    auto_sell_enabled: _chk('tdsSellEn'),
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
    rotation: {
      enabled: _chk('tdsRotEn'),
      etf_ratio: _num('tdsRotRatio') / 100,
      execute_time: document.getElementById('tdsRotTime').value.trim(),
      risk_etf2: document.getElementById('tdsRotRisk2').value.trim(),
      cyb_etf: document.getElementById('tdsRotCyb').value.trim(),
      gold_etf: document.getElementById('tdsRotGold').value.trim(),
      hedge_etf2: document.getElementById('tdsRotHedge2').value.trim(),
      hedge_ratio: _num('tdsRotHedge') / 100,
      momentum_window: _int('tdsRotMomWin'),
      trailing_stop_pct: _num('tdsRotTrail') / 100,
      signal_day: (function () {   // 2026-09-16 错峰: 多选 → 数组 (单份也发数组, 后端归一)
        var sel = document.getElementById('tdsRotSignalDay');
        var out = [];
        for (var oi = 0; oi < sel.options.length; oi++) {
          if (sel.options[oi].selected) out.push(sel.options[oi].value);
        }
        return out.length ? out : ['friday'];
      })(),
    },
    regime_filter: {
      enabled: _chk('tdsRfEn'),
      index_code: document.getElementById('tdsRfIndex').value.trim(),
      ma_window: _int('tdsRfMa'),
    },
    feishu: { enabled: _chk('tdsFeishuEn'), ai_review: _chk('tdsAiReview') },
  };
}

document.getElementById('tdSettings').addEventListener('toggle', function () {
  if (this.open && !settingsLoaded) {
    document.getElementById('tdsHint').textContent = '配置加载中…';
    get('/api/trade/config').then(function (cfg) {
      fillSettings(cfg);
      settingsLoaded = true;
      document.getElementById('tdsHint').textContent = '';
    }).catch(function (err) {
      document.getElementById('tdsHint').textContent = '配置加载失败: ' + describeTradeError(err);
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
  // W2-2: 补 AbortController+4s 超时 (cmd 同款) — 后端半死挂起时旧实现
  // finally 永不执行, 保存按钮永久锁死且无提示; 超时走 catch 恢复按钮
  var ctl = new AbortController();
  var timer = setTimeout(function () { ctl.abort(); }, 4000);
  fetch(BASE + '/api/trade/config', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(gatherSettings()),
    signal: ctl.signal,
  }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
    .then(function (res) {
      if (res.ok && res.d.accepted) {
        hint.textContent = '已热生效' + (res.d.changed && res.d.changed.length
          ? ' (变更: ' + res.d.changed.join(', ') + ')' : ' (无变更)');
        invalidatePayload();   // W2-1: 保存改了 auto_buy/rotation 的 config 显示, 强制下次全量刷新
        refresh();
      } else {
        // 422: 后端校验的字段错误原样展示 (真相校验在后端)
        hint.textContent = '保存被拒: ' + (res.d.detail || '未知错误');
      }
    }).catch(function (err) { hint.textContent = '保存失败: ' + describeTradeError(err); })
    .finally(function () { clearTimeout(timer); btn.disabled = false; });
});

// ══════════════════════════════════════════════════════════════
// T6 通道徽章 + 武装向导 (2026-09-07, 方案设计书 §5.5/§5.7)
// 徽章 = 状态只读 + 点击开向导。武装仪式 (用户拍板):
//   ① 探针通过 → ② 打字「确认实盘」→ ③ 武装
//   (武装意图未开时先热更 ths_armed=true, 再发武装命令, 同为队列命令
//    顺序消费)。解除武装零摩擦一键 (从危险回安全永远无仪式)。
// 向导不猜状态: 全部来自 GET /api/trade/channel 的 mgr 快照。
// ══════════════════════════════════════════════════════════════

// 通道徽章渲染 — badge() 会重写 className, clickable 须补挂
function _renderChannelBadge(s) {
  var el = document.getElementById('tdChannel');
  var ch = s.channel || 'qmt';
  if (ch === 'qmt') {
    badge(el, true, '通道 · QMT');
  } else if (ch === 'fake') {
    badge(el, null, '通道 · 模拟');
  } else if (s.channel_down) {
    badge(el, false, '同花顺 · 断线');
  } else if (s.armed_effective) {
    badge(el, true, '同花顺 · 已武装');
  } else if (s.armed_intent) {
    // 悬空态 (黄灯): 意图开但探针未过, 实际拒单
    el.className = 'trade-badge warn';
    el.textContent = '同花顺 · 悬空待探';
  } else {
    badge(el, null, '同花顺 · 未武装');
  }
  el.classList.add('clickable');
  el.title = '通道管理 — 点击查看状态 / 探针 / 武装';
}

var _chOverlay = null;   // 弹窗根 DOM (同时是"已打开"标记)
var _chState = null;     // 最近一次 GET /api/trade/channel 响应

function closeChannelWizard() {
  document.removeEventListener('keydown', _chOnKey);
  if (_chOverlay) { _chOverlay.remove(); _chOverlay = null; }
}
function _chOnKey(e) { if (e.key === 'Escape') closeChannelWizard(); }

// PUT 助手 (与设置保存同款语义; 本文件局部 post 只覆盖 POST)
function _chPut(u, body) {
  return fetch(BASE + u, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  }).then(function (r) {
    return r.json().then(function (d) {
      if (!r.ok) {
        var err = new Error('HTTP ' + r.status);
        err.serverMsg = (d && d.detail) ? String(d.detail) : ('HTTP ' + r.status);
        throw err;
      }
      return d;
    });
  });
}

// 轮询通道状态直到 pred 命中或超时 (命令走队列异步生效, 受理≠完成)
function _chPollUntil(pred, maxTries, done) {
  var tries = 0;
  var timer = setInterval(function () {
    tries += 1;
    get('/api/trade/channel').then(function (d) {
      if (pred(d) || tries >= maxTries) {
        clearInterval(timer);
        done(d, !(d && pred(d)));
      }
    }).catch(function () {
      if (tries >= maxTries) { clearInterval(timer); done(null, true); }
    });
  }, 1000);
}

function _chProbeText(mgr) {
  var lp = mgr && mgr.last_probe;
  if (!lp || !lp.ts) return '尚未探针';
  return (lp.ok ? '✓ 通过' : '✗ 未过') + ' (' + fmtTs(lp.ts) + ')'
    + (lp.error ? ' — ' + lp.error : '');
}

// 向导主体渲染 (innerHTML 重建 — 弹窗低频交互, 不走 W2-1 脏检查)
function _chRenderBody(d) {
  var body = document.getElementById('chwBody');
  if (!body) return;
  var mgr = d.mgr || null;
  var chName = { qmt: 'QMT', ths: '同花顺 (GUI)', fake: '模拟' }[d.channel] || d.channel;
  var html = '<div style="font-size:var(--fs-sm);color:var(--text);line-height:1.9">'
    + '<div>当前通道: <b>' + esc(chName) + '</b></div>';
  if (!mgr) {
    html += '<div style="color:var(--text2);margin-top:var(--sp-2)">'
      + (d.channel === 'ths' ? '通道管理器未运行 (服务重启后生效)'
        : '当前通道无武装概念 — QMT 通道由 SDK 连接即真相, 模拟通道不下实盘单。')
      + '</div><div style="color:var(--text2);margin-top:var(--sp-2)">'
      + '切换通道: 改配置 + 重启服务生效; 盘中 (连续竞价/集合竞价) 禁切。</div></div>';
    body.innerHTML = html;
    return;
  }
  html += '<div>武装意图: <b>' + (mgr.armed_intent ? '开' : '关') + '</b>'
    + ' &nbsp;·&nbsp; 武装生效: <b style="color:'
    + (mgr.armed_effective ? 'var(--ok-text)' : 'var(--warn-text)') + '">'
    + (mgr.armed_effective ? '是 (实盘下单放行)' : '否 (下单拒绝)') + '</b></div>'
    + '<div>上次探针: ' + esc(_chProbeText(mgr)) + '</div>'
    + (mgr.channel_down
      ? '<div style="color:var(--warn-text);font-weight:600">⚠ 断线哨兵触发: 轮询连续失败, 请检查同花顺客户端</div>' : '')
    + '</div>';

  if (mgr.armed_effective) {
    html += '<div style="margin-top:var(--sp-3);display:flex;gap:var(--sp-3);align-items:center">'
      + '<button id="chwDisarm" class="btn">解除武装</button>'
      + '<span style="font-size:var(--fs-xs);color:var(--text2)">零摩擦: 从危险回安全, 一键即生效</span></div>';
  } else {
    html += '<div style="margin-top:var(--sp-3);border-top:1px solid var(--border);padding-top:var(--sp-3)">'
      + '<div style="font-size:var(--fs-sm);margin-bottom:var(--sp-2)">① 先探针 (校验客户端连通 + 账本可读 + 行情活着):</div>'
      + '<button id="chwProbe" class="btn">重新探针</button>'
      + '<div style="font-size:var(--fs-sm);margin:var(--sp-3) 0 var(--sp-2)">② 打字确认后武装:</div>'
      + '<input id="chwConfirm" placeholder="输入「确认实盘」" style="padding:var(--sp-2);border:1px solid var(--border);'
      + 'border-radius:var(--radius-sm);background:var(--card);color:var(--text);width:11em;margin-right:var(--sp-2)">'
      + '<button id="chwArm" class="btn btn-primary" disabled>武装 (实盘下单)</button></div>';
  }
  html += '<div id="chwHint" class="trade-hint" aria-live="polite" style="margin-top:var(--sp-3)"></div>';
  body.innerHTML = html;

  var hint = document.getElementById('chwHint');
  var disarmBtn = document.getElementById('chwDisarm');
  if (disarmBtn) {
    disarmBtn.addEventListener('click', function () {
      disarmBtn.disabled = true;
      hint.textContent = '解除命令已受理, 等待生效…';
      post('/api/trade/channel/disarm', {}).then(function () {
        _chPollUntil(function (x) { return x.mgr && x.mgr.armed_effective === false; }, 8, function (x, timeout) {
          invalidatePayload(); refresh();
          _chReload(timeout ? '状态未刷新, 请看徽章或审计日志' : '✓ 已解除武装 (下单通道已锁)');
        });
      }).catch(function (e) {
        hint.textContent = '解除被拒: ' + ((e && e.serverMsg) || '服务不可达');
        disarmBtn.disabled = false;
      });
    });
    return;
  }

  var probeBtn = document.getElementById('chwProbe');
  var confirmInput = document.getElementById('chwConfirm');
  var armBtn = document.getElementById('chwArm');

  function _syncArmBtn() {
    var lp = _chState && _chState.mgr && _chState.mgr.last_probe;
    armBtn.disabled = !(confirmInput.value === '确认实盘' && lp && lp.ok === true);
  }
  confirmInput.addEventListener('input', _syncArmBtn);
  _syncArmBtn();

  probeBtn.addEventListener('click', function () {
    probeBtn.disabled = true;
    var prevTs = (_chState && _chState.mgr && _chState.mgr.last_probe
      && _chState.mgr.last_probe.ts) || 0;
    post('/api/trade/channel/probe', {}).then(function () {
      hint.textContent = '探针命令已受理, 执行中 (GUI 查询需几秒)…';
      _chPollUntil(function (x) {
        var lp = x.mgr && x.mgr.last_probe;
        return lp && lp.ts && lp.ts !== prevTs;
      }, 15, function (x, timeout) {
        probeBtn.disabled = false;
        var msg = timeout ? '探针超时未回 — 请确认同花顺客户端已登录, 或看审计日志'
          : (x.mgr.last_probe.ok ? '✓ 探针通过, 可以武装'
            : ('✗ 探针未过: ' + (x.mgr.last_probe.error || '未知原因')));
        invalidatePayload(); refresh(); _chReload(msg);
      });
    }).catch(function (e) {
      hint.textContent = '探针被拒: ' + ((e && e.serverMsg) || '服务不可达');
      probeBtn.disabled = false;
    });
  });

  armBtn.addEventListener('click', function () {
    armBtn.disabled = true;
    hint.textContent = '武装命令发送中…';
    // 意图未开先热更配置 (ths_armed=true 走 channel_mgr.apply 即时生效,
    // 与武装命令同队列顺序消费, 先有意图后武装)
    var seq = (_chState && _chState.mgr && _chState.mgr.armed_intent)
      ? Promise.resolve() : _chPut('/api/trade/config', { ths_armed: true });
    seq.then(function () {
      return post('/api/trade/channel/arm', { confirm: '确认实盘' });
    }).then(function () {
      hint.textContent = '武装命令已受理, 等待生效…';
      _chPollUntil(function (x) { return x.mgr && x.mgr.armed_effective === true; }, 10, function (x, timeout) {
        var msg = timeout ? '未生效 — 探针可能已过期/未过, 重新探针后再武装 (详情见审计)'
          : '✓ 已武装 — 同花顺 GUI 实盘下单放行, 急停随时可锁';
        invalidatePayload(); refresh(); _chReload(msg);
      });
    }).catch(function (e) {
      hint.textContent = '武装被拒: ' + ((e && e.serverMsg) || '服务不可达');
      _syncArmBtn();
    });
  });
}

function _chReload(msg) {
  get('/api/trade/channel').then(function (d) {
    _chState = d;
    _chRenderBody(d);
    // 结果提示在重建后写入 (innerHTML 重渲染会清掉先写的 hint)
    if (msg) {
      var hint = document.getElementById('chwHint');
      if (hint) hint.textContent = msg;
    }
  }).catch(function () {
    var body = document.getElementById('chwBody');
    if (body) body.textContent = '交易服务 (8081) 不可达';
  });
}

function openChannelWizard() {
  if (_chOverlay) return;   // 已打开不叠层
  var overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9999'
    + ';display:flex;align-items:center;justify-content:center';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-label', '通道管理');
  var box = document.createElement('div');
  box.style.cssText = 'width:min(560px,92vw);background:var(--card);border:1px solid var(--border)'
    + ';border-radius:10px;padding:var(--sp-4);box-shadow:0 8px 32px rgba(0,0,0,.4)';
  box.innerHTML = '<div style="display:flex;align-items:center;justify-content:space-between;'
    + 'margin-bottom:var(--sp-3)">'
    + '<b style="font-size:var(--fs-lg);color:var(--text)">通道管理</b>'
    + '<button id="chwClose" class="btn" style="padding:var(--sp-1) var(--sp-3)">×</button></div>'
    + '<div id="chwBody" style="color:var(--text2);font-size:var(--fs-sm)">加载中…</div>';
  overlay.appendChild(box);
  overlay.addEventListener('click', function (e) { if (e.target === overlay) closeChannelWizard(); });
  document.addEventListener('keydown', _chOnKey);
  document.body.appendChild(overlay);
  _chOverlay = overlay;
  document.getElementById('chwClose').addEventListener('click', closeChannelWizard);
  _chReload();
}

// 徽章点击/键盘 (Enter/Space) 开向导 — role=button 已在 HTML 标注
(function () {
  var el = document.getElementById('tdChannel');
  el.addEventListener('click', openChannelWizard);
  el.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openChannelWizard(); }
  });
})();

})();
