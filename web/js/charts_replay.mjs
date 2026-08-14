// ====== VERA 图表深挖包 · Phase 3: 单笔交易 K 线回放 ======
// 点回测交易表某一行 → 弹窗显示该票日 K (蜡烛图 + 成交量副图), 标出买点/卖点。
// 深模块: fetch + 弹窗 DOM + ECharts option 全在门内, 调用方只传 trade + 配色。
// 数学口径在 deep_math.mjs (klineWindow / toCandleRows), 本文件只管取数和渲染。
//
// 后端契约 (钉死, 不猜):
//   GET /api/stock/kline?code=600000&start=2024-01-01&end=2024-03-01
//   → 200 {"code", "period", "rows":[{date,open,high,low,close,volume} 升序无 NaN]}
//   rows 空 → 弹窗内提示「无 K 线数据（停牌或区间无交易）」;
//   422 非法 code / 502 数据层故障 → 弹窗内友好错误, 不 throw 到页面, console.warn 留痕。
//
// 弹窗生命周期: 遮罩层 + 居中容器; 关闭按钮 / ESC / 点遮罩都可关;
//   关闭时 dispose ECharts 实例、移除 DOM 和全部事件监听 (防泄漏);
//   连续点不同行先关再开, 不产生叠层。
// 弹窗自包含 (内联样式), index.html 零改动。

import { esc, getColors, hexToRgba } from './charts.js';
import { klineWindow, toCandleRows, formatMoney } from './deep_math.mjs';

// ── 模块级状态 (同一时刻最多一个弹窗) ──
let _overlay = null;    // 当前弹窗根 DOM (同时是"已打开"标记)
let _chart = null;      // 弹窗内 ECharts 实例
let _onKey = null;      // ESC 监听引用 (关闭时移除)
let _onResize = null;   // resize 监听引用 (关闭时移除)

// ── 弹窗生命周期 ──

/** 关闭回放弹窗: dispose 图表、移除 DOM 和事件监听。未打开时调用安全。 */
export function closeTradeReplay() {
  if (_chart) { try { _chart.dispose(); } catch (e) {} _chart = null; }
  if (_onKey) { document.removeEventListener('keydown', _onKey); _onKey = null; }
  if (_onResize) { window.removeEventListener('resize', _onResize); _onResize = null; }
  if (_overlay) { _overlay.remove(); _overlay = null; }
}

/**
 * 弹窗内错误提示 (不 throw 到页面): 替换图表区为友好错误文案。
 * @param {HTMLElement} bodyEl 弹窗内容区
 * @param {string} msg 给用户看的大白话
 * @param {string} detail console.warn 留痕用细节
 * @param {Object} c 配色
 */
function _showError(bodyEl, msg, detail, c) {
  console.warn('[trade-replay] ' + (detail || msg));
  bodyEl.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%'
    + ';color:' + c.warn + ';font-size:13px;padding:20px;text-align:center">' + esc(msg) + '</div>';
}

/**
 * 打开单笔交易 K 线回放弹窗。
 * @param {Object} trade 交易行 (stock_code/stock_name/entry_date/exit_date/entry_price/exit_price/profit_pct)
 * @param {Object} [colors] getColors() 结果, 不传则现场取 (主题自适应)
 * @returns {Promise<{close: function}|null>} 关闭句柄 (测试用); trade 为空返回 null
 */
export async function openTradeReplay(trade, colors) {
  if (!trade) return null;
  closeTradeReplay();   // 连续点不同行: 先关再开, 不叠层
  const c = colors || getColors();

  // ── 遮罩 + 居中容器 (全内联样式, 自包含) ──
  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9999'
    + ';display:flex;align-items:center;justify-content:center';
  const box = document.createElement('div');
  box.style.cssText = 'width:min(920px,92vw);height:min(620px,86vh);background:' + c.bg
    + ';border:1px solid ' + c.border + ';border-radius:10px;display:flex;flex-direction:column'
    + ';box-shadow:0 8px 32px rgba(0,0,0,.4);overflow:hidden';
  overlay.appendChild(box);

  // 标题行: 「名称(代码) 入场日 ~ 出场日 盈亏%」 (盈亏红涨绿跌) + 关闭按钮
  const pnl = (trade.profit_pct != null ? trade.profit_pct : trade.return) || 0;
  const pnlPct = pnl * 100;
  const pnlColor = pnlPct > 0 ? c.up : pnlPct < 0 ? c.down : c.text2;
  const pnlTxt = (pnlPct > 0 ? '+' : '') + pnlPct.toFixed(2) + '%'
    + (typeof trade.pnl === 'number' && Number.isFinite(trade.pnl) ? ' (' + formatMoney(trade.pnl) + ')' : '');
  const header = document.createElement('div');
  header.style.cssText = 'display:flex;align-items:center;justify-content:space-between'
    + ';padding:10px 14px;border-bottom:1px solid ' + c.border + ';font-size:13px;color:' + c.text;
  const title = document.createElement('span');
  title.innerHTML = '<b>' + esc(trade.stock_name || trade.stock_code || '') + '</b>'
    + '<span style="color:' + c.text2 + '">(' + esc(trade.stock_code || '') + ')</span> '
    + esc(String(trade.entry_date || '').slice(0, 10)) + ' ~ ' + esc(String(trade.exit_date || '').slice(0, 10))
    + ' <b style="color:' + pnlColor + '">' + esc(pnlTxt) + '</b>';
  const btnClose = document.createElement('button');
  btnClose.textContent = '×';
  btnClose.title = '关闭 (ESC)';
  btnClose.style.cssText = 'background:none;border:none;color:' + c.text2 + ';font-size:20px'
    + ';cursor:pointer;line-height:1;padding:0 4px';
  btnClose.addEventListener('click', closeTradeReplay);
  header.appendChild(title);
  header.appendChild(btnClose);
  box.appendChild(header);

  // 内容区 (加载中 → 图表 或 错误提示)
  const body = document.createElement('div');
  body.style.cssText = 'flex:1;position:relative;min-height:0';
  body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%'
    + ';color:' + c.text2 + ';font-size:12px">K 线加载中…</div>';
  box.appendChild(body);

  overlay.addEventListener('click', function (e) { if (e.target === overlay) closeTradeReplay(); });
  _onKey = function (e) { if (e.key === 'Escape') closeTradeReplay(); };
  document.addEventListener('keydown', _onKey);
  document.body.appendChild(overlay);
  _overlay = overlay;

  // ── 取数 (契约见文件头) ──
  const win = klineWindow(trade.entry_date, trade.exit_date);
  if (!win) { _showError(body, '交易记录缺入场日期，无法回放', '缺 entry_date: ' + JSON.stringify(trade), c); return _handle(); }
  const url = '/api/stock/kline?code=' + encodeURIComponent(trade.stock_code || '')
    + '&start=' + win.start + '&end=' + win.end;
  let data;
  try {
    const res = await fetch(url);
    if (_overlay !== overlay) return _handle();   // 等待期间已被关闭/换行, 丢弃结果
    if (res.status === 422) { _showError(body, '股票代码无效 (HTTP 422)', '422 code=' + trade.stock_code, c); return _handle(); }
    if (res.status === 502) { _showError(body, '行情数据层故障 (HTTP 502)，请稍后重试', '502 code=' + trade.stock_code, c); return _handle(); }
    if (!res.ok) { _showError(body, 'K 线接口异常 (HTTP ' + res.status + ')', 'HTTP ' + res.status + ' ' + url, c); return _handle(); }
    data = await res.json();
  } catch (e) {
    if (_overlay !== overlay) return _handle();
    _showError(body, 'K 线加载失败（网络异常）: ' + (e && e.message ? e.message : e), 'fetch 异常: ' + e, c);
    return _handle();
  }

  const kd = toCandleRows(data && data.rows);
  if (kd.dates.length === 0) {
    _showError(body, '无 K 线数据（停牌或区间无交易）', 'rows 为空 code=' + trade.stock_code, c);
    return _handle();
  }
  if (typeof echarts === 'undefined') {
    _showError(body, 'ECharts 未加载，无法渲染 K 线', 'echarts undefined', c);
    return _handle();
  }

  // ── 渲染蜡烛图 + 成交量副图 + 买卖点标记 ──
  body.innerHTML = '';
  const chartDiv = document.createElement('div');
  chartDiv.style.cssText = 'position:absolute;inset:4px 8px 4px 4px';
  body.appendChild(chartDiv);
  _chart = echarts.init(chartDiv);
  _onResize = function () { if (_chart) _chart.resize(); };
  window.addEventListener('resize', _onResize);
  _chart.setOption(_replayOption(kd, trade, c));
  return _handle();

  function _handle() { return { close: closeTradeReplay }; }
}

// ── ECharts option 组装 (门内细节) ──

/** 在已取到的日期轴上找标记落点: 买 = 首个 ≥ entry 的交易日, 卖 = 最后一个 ≤ exit 的交易日 (停牌日容错) */
function _resolveIdx(dates, day, prefer) {
  const d = String(day || '').slice(0, 10);
  if (!d) return -1;
  let lastLe = -1;
  for (let i = 0; i < dates.length; i++) {
    if (dates[i] === d) return i;
    if (dates[i] < d) lastLe = i; else if (prefer === 'ge') return i;
  }
  return prefer === 'ge' ? (dates.length ? 0 : -1) : lastLe;
}

function _replayOption(kd, trade, c) {
  const dates = kd.dates, candles = kd.candles, volumes = kd.volumes;
  const ei = _resolveIdx(dates, trade.entry_date, 'ge');
  const xi = _resolveIdx(dates, trade.exit_date, 'le');
  const markPointData = [];
  const markLineData = [];
  if (ei >= 0 && trade.entry_price != null) {
    // 买点: K 线下方红色上三角「买」 + 买入价虚线
    markPointData.push({
      coord: [dates[ei], candles[ei][2]], symbol: 'triangle', symbolSize: 12, symbolRotate: 0,
      symbolOffset: [0, 10], itemStyle: { color: c.up },
      label: { show: true, formatter: '买', color: c.up, fontSize: 11, fontWeight: 'bold', position: 'bottom', distance: 14 },
    });
    markLineData.push({ yAxis: trade.entry_price,
      label: { formatter: '买 ' + Number(trade.entry_price).toFixed(2), color: c.up, fontSize: 9, position: 'insideStartTop' },
      lineStyle: { color: c.up, type: 'dashed', width: 1 } });
  }
  if (xi >= 0 && trade.exit_price != null) {
    // 卖点: K 线上方绿色下三角「卖」 + 卖出价虚线
    markPointData.push({
      coord: [dates[xi], candles[xi][3]], symbol: 'triangle', symbolSize: 12, symbolRotate: 180,
      symbolOffset: [0, -10], itemStyle: { color: c.down },
      label: { show: true, formatter: '卖', color: c.down, fontSize: 11, fontWeight: 'bold', position: 'top', distance: 14 },
    });
    markLineData.push({ yAxis: trade.exit_price,
      label: { formatter: '卖 ' + Number(trade.exit_price).toFixed(2), color: c.down, fontSize: 9, position: 'insideStartTop' },
      lineStyle: { color: c.down, type: 'dashed', width: 1 } });
  }

  return {
    animation: false,
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' }, formatter: function (params) {
      const i = params[0].dataIndex;
      const o = candles[i][0], cl = candles[i][1], lo = candles[i][2], hi = candles[i][3];
      const prev = i > 0 ? candles[i - 1][1] : o;
      const chg = prev ? (cl / prev - 1) * 100 : 0;
      const chgColor = chg > 0 ? c.up : chg < 0 ? c.down : c.text2;
      return dates[i]
        + '<br/>开: <b>' + o.toFixed(2) + '</b> 收: <b>' + cl.toFixed(2) + '</b>'
        + '<br/>高: <b>' + hi.toFixed(2) + '</b> 低: <b>' + lo.toFixed(2) + '</b>'
        + '<br/>量: <b>' + Math.round(volumes[i][0]).toLocaleString('en-US') + '</b>'
        + ' 涨跌幅: <b style="color:' + chgColor + '">' + (chg > 0 ? '+' : '') + chg.toFixed(2) + '%</b>';
    }},
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1] },
      { type: 'slider', xAxisIndex: [0, 1], bottom: 4, height: 16,
        borderColor: c.border, fillerColor: hexToRgba(c.accent, 0.13),
        textStyle: { color: c.text2, fontSize: 9 } },
    ],
    // 上下两段 grid: 上蜡烛下量
    grid: [
      { left: 56, right: 20, top: 16, height: '56%' },
      { left: 56, right: 20, top: '72%', height: '17%' },
    ],
    xAxis: [
      { type: 'category', data: dates, gridIndex: 0, boundaryGap: true,
        axisLine: { lineStyle: { color: c.border } }, axisLabel: { color: c.text2, fontSize: 9 } },
      { type: 'category', data: dates, gridIndex: 1, boundaryGap: true,
        axisLine: { lineStyle: { color: c.border } }, axisLabel: { show: false }, axisTick: { show: false } },
    ],
    yAxis: [
      { scale: true, gridIndex: 0,
        axisLabel: { color: c.text2, fontSize: 9 }, splitLine: { lineStyle: { color: c.border } } },
      { scale: true, gridIndex: 1,
        axisLabel: { color: c.text2, fontSize: 9, formatter: function (v) {
          return v >= 1e8 ? (v / 1e8).toFixed(1) + '亿' : v >= 1e4 ? (v / 1e4).toFixed(0) + '万' : v;
        } }, splitLine: { show: false } },
    ],
    series: [
      { name: '日K', type: 'candlestick', xAxisIndex: 0, yAxisIndex: 0, data: candles,
        // A 股色例: 涨红跌绿 (color=阳线, color0=阴线)
        itemStyle: { color: c.up, color0: c.down, borderColor: c.up, borderColor0: c.down },
        markPoint: { silent: true, data: markPointData },
        markLine: { silent: true, symbol: 'none', data: markLineData } },
      { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1,
        data: volumes.map(function (vp) {
          return { value: vp[0], itemStyle: { color: vp[1] > 0 ? c.up : c.down } };  // 红涨绿跌, 与蜡烛一致
        }) },
    ],
  };
}

// ── 交易表点击接线 (事件委托) ──

let _tradesRef = [];                 // renderDeepCharts 保存的全量交易引用
let _delegated = null;               // {el, fn} 当前委托监听 (重复渲染先移除旧的)

/**
 * 从被点的 <tr> 找回 trade。
 * 快路径: data-tidx 是行在「传给 renderTradeTable 的数组」里的索引 (charts.js 行内换算),
 *   未筛选时即 _tradesRef 下标; 用第二列代码校验防错位。
 * 兜底: 用户筛选/搜索后表格是子集渲染, tidx 对不上全量数组 ——
 *   按行内的 代码+入场日+出场日 回全量里 find (同一票同日买卖两笔的概率可忽略)。
 */
function _tradeFromRow(tr) {
  const cells = tr.children;
  if (!cells || cells.length < 9) return null;
  const code = cells[1].textContent.trim();
  const idx = parseInt(tr.dataset.tidx, 10);
  const t = _tradesRef[idx];
  if (t && String(t.stock_code) === code) return t;
  const eDate = cells[5].textContent.trim();
  const xDate = cells[8].textContent.trim();
  return _tradesRef.find(function (u) {
    return String(u.stock_code) === code
      && String(u.entry_date || '').slice(0, 10) === eDate
      && String(u.exit_date || '').slice(0, 10) === xDate;
  }) || null;
}

/**
 * 给交易表挂点击委托 (重复渲染幂等: 先移除旧监听再挂)。
 * 委托挂在常驻的 tbody 上, renderTradeTable 每次只换 innerHTML, 监听不失效。
 * @param {Array<Object>} trades renderDeepCharts 收到的 data.trades (全量)
 */
export function wireTradeReplay(trades) {
  _tradesRef = Array.isArray(trades) ? trades : [];
  const tbody = document.getElementById('tradeTableBody');
  if (!tbody) return;
  if (_delegated) { _delegated.el.removeEventListener('click', _delegated.fn); _delegated = null; }
  const fn = function (e) {
    const tr = e.target && e.target.closest ? e.target.closest('tr[data-tidx]') : null;
    if (!tr) return;
    const t = _tradeFromRow(tr);
    if (t) openTradeReplay(t, getColors());
  };
  tbody.addEventListener('click', fn);
  _delegated = { el: tbody, fn };
}
