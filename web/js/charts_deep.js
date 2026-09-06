// ====== VERA 图表深挖包 · Phase 1+2 ======
// Phase 1: 水下曲线 (回撤) + 蒙特卡洛收益扇面。
// Phase 2: 滚动指标面板 (滚动夏普/收益/波动率) + 业绩归因 (板块/个股 Top10)。
// 深模块: 小接口藏大实现,
// ECharts option 组装全在门内, 调用方只传 domId + 数据 + 配色。
// 数学口径全在 deep_math.mjs (纯函数, node 可测), 本文件只管渲染。
// 降级约定: 数据不足/异常 → 隐藏容器 (display:none), 不报错不崩页面。

import { hexToRgba, getColors, echartsInit } from './charts.js?v=20260906d';
import { computeDrawdown, tradeNetReturns, monteCarloFan, chooseScale, formatMoney } from './deep_math.mjs';
import { wireTradeReplay } from './charts_replay.mjs?v=20260906c';   // Phase 3: 交易行点击 → K 线回放

// 样本不足 20 笔时蒙特卡洛结果仅供参考 (与 deep_math 的 lowSample 阈值一致)
const MC_SIMS = 1000;   // bootstrap 模拟次数
const MC_SEED = 42;     // 固定种子: 同一批交易刷新页面扇面不变, 避免"每次看都不一样"的困惑

// ── 内部工具 ──

/** 隐藏图表所在的 .chart-box 容器 (没有容器则隐藏图表 dom 本身) */
function _hideBox(domId) {
  const dom = document.getElementById(domId);
  if (!dom) return;
  const box = dom.closest('.chart-box');
  (box || dom).style.display = 'none';
}

/** 显示图表所在的 .chart-box 容器 (上次隐藏过再出新数据时要恢复) */
function _showBox(domId) {
  const dom = document.getElementById(domId);
  if (!dom) return;
  const box = dom.closest('.chart-box');
  (box || dom).style.display = '';
}

/** 最多保留 1 位小数并去掉尾随 .0 (大白话大数用) */
function _trimNum(n) {
  const s = n >= 100 ? Math.round(n) : Math.round(n * 10) / 10;
  return String(s);
}

/**
 * 净值倍数大白话格式化: 0.50x / 1x / 10x / 100x, ≥1万 用中文大数 (2万x / 1.5亿x)。
 * 用于对数轴刻度与 tooltip —— 复利跑上千笔后倍数可能到几十万, 裸数字没人数得清几个零。
 */
function _fmtMult(v) {
  if (v >= 1e8) return _trimNum(v / 1e8) + '亿x';
  if (v >= 1e4) return _trimNum(v / 1e4) + '万x';
  if (v >= 100) return Math.round(v) + 'x';
  if (v >= 1) return _trimNum(v) + 'x';
  return v.toFixed(2) + 'x';
}

// ── 水下曲线 (回撤填色区域图) ──

/**
 * 渲染水下曲线: 0 轴以下的回撤填色区域图, 一眼看清"套了多深、套了多久"。
 * @param {string} domId 图表容器 id (.chart-box-body)
 * @param {Array<{date:string, equity:number, drawdown?:number}>} equityPoints
 *   日级权益点; 缺 drawdown 字段时用 computeDrawdown 从 equity 兜底 (同后端口径)
 * @param {Object} [colors] getColors() 结果, 不传则现场取 (主题自适应)
 * @returns {Object|null} ECharts 实例; 数据 <2 点隐藏容器并返回 null
 */
export function renderUnderwater(domId, equityPoints, colors) {
  if (!equityPoints || equityPoints.length < 2) { _hideBox(domId); return null; }
  const c = colors || getColors();
  const chart = echartsInit(domId);
  if (!chart) return null;
  _showBox(domId);

  const dates = equityPoints.map(r => String(r.date || '').slice(0, 10));
  // 优先用后端 drawdown (小数 ≤0); 没有就从 equity 现算, 口径一致
  const hasDd = equityPoints.every(r => typeof r.drawdown === 'number');
  const dd = hasDd
    ? equityPoints.map(r => r.drawdown * 100)
    : computeDrawdown(equityPoints.map(r => r.equity)).map(v => v * 100);
  const maxDd = Math.min.apply(null, dd);

  chart.setOption({
    tooltip: { trigger: 'axis', formatter: function (params) {
      const p = params[0];
      return p.axisValue + '<br/>回撤: <b>' + (p.value != null ? p.value.toFixed(2) + '%' : '-') + '</b>';
    }},
    dataZoom: [
      { type: 'slider', xAxisIndex: 0, bottom: 10, height: 20,
        borderColor: c.border, fillerColor: hexToRgba(c.down, 0.13),
        textStyle: { color: c.text2, fontSize: 9 } },
    ],
    // containLabel: 轴标签完整收进网格不被裁; 无图例无标题, top 40 只给 toolbox/轴名留位
    grid: { left: 20, right: 50, top: 40, bottom: 60, containLabel: true },
    xAxis: { type: 'category', data: dates, axisLine: { lineStyle: { color: c.border } }, axisLabel: { color: c.text2, fontSize: 9 } },
    yAxis: { type: 'value', name: '回撤 %', nameTextStyle: { color: c.text2, fontSize: 10 },
      max: 0,  // 水下曲线恒在 0 轴以下, 锁顶避免留白误读
      axisLabel: { color: c.text2, fontSize: 9, formatter: '{value}%' },
      splitLine: { lineStyle: { color: c.border } } },
    toolbox: { right: 10, top: 0, feature: {
      saveAsImage: { title: '保存图片', pixelRatio: 2 },
      dataZoom: { title: { zoom: '区域缩放', back: '还原' } },
      restore: { title: '刷新' },
    }, iconStyle: { borderColor: c.text2 } },
    series: [
      { name: '回撤', type: 'line', data: dd, symbol: 'none',
        itemStyle: { color: c.down },
        lineStyle: { color: c.down, width: 1.5 },
        // 0 轴向下绿色系渐变填充, 越深越绿 (W2-3b: 修正与代码矛盾的旧注释"红色系")
        areaStyle: { color: {
          type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [
            { offset: 0, color: hexToRgba(c.down, 0.05) },
            { offset: 1, color: hexToRgba(c.down, 0.35) },
          ],
        } },
        markLine: { silent: true, symbol: 'none', data: [
          { yAxis: 0, lineStyle: { color: c.text2, type: 'dashed', width: 1 } },
          { yAxis: maxDd, label: { formatter: '最大回撤 ' + maxDd.toFixed(2) + '%', color: c.down, fontSize: 10 },
            lineStyle: { color: c.down, type: 'dotted', width: 1 } },
        ] },
      },
    ],
  }, true);
  return chart;
}

// ── 蒙特卡洛收益扇面 ──

/**
 * 渲染蒙特卡洛收益扇面: bootstrap 重抽样历史交易, 展示"未来再做同样笔数交易"
 * 净值倍数的分布范围 (5/25/50/75/95 分位)。中位线加粗, p5~p95 / p25~p75 两层扇面。
 * 口径: 单笔收益率 = pnl / 入场时点总权益 (见 deep_math.tradeNetReturns 为什么不能用仓位口径);
 *   pnl 未扣买入费用; 假设各笔交易独立同分布。
 * 展示: 纵轴模式由 deep_math.chooseScale 决定 —— 默认线性轴 + 累计收益%;
 *   全路径外圈极差 (p95max/p5min) > 50 时自动切对数轴 + 净值倍数兜底,
 *   防极端策略扇面在线性轴上压成一条线 (2026-08-13 事故复盘)。
 * @param {string} domId 图表容器 id (.chart-box-body)
 * @param {Array<Object>} trades 后端透传的交易记录 (字段见 deep_math.tradeNetReturns)
 * @param {Object} [colors] getColors() 结果, 不传则现场取 (主题自适应)
 * @param {{equity?: Array<{date:string, equity:number}>, actualReturn?: number}} [opts]
 *   equity: 权益序列 (折算口径用, 缺省回退旧仓位口径);
 *   actualReturn: 真实回测总收益小数 (data.metrics.cumulative_return), 有值时画「实际回测终点」横线
 * @returns {Object|null} ECharts 实例; trades 为空隐藏容器并返回 null
 */
export function renderMonteCarlo(domId, trades, colors, opts) {
  const o = opts || {};
  const returns = tradeNetReturns(trades, o.equity);
  if (returns.length === 0) { _hideBox(domId); return null; }
  const c = colors || getColors();
  const chart = echartsInit(domId);
  if (!chart) return null;
  _showBox(domId);

  const fan = monteCarloFan(returns, { sims: MC_SIMS, seed: MC_SEED });
  const steps = fan.steps.map(s => s.step);
  // 净值倍数 = deep_math 输出的原始累计净值 (起点 1)
  const p5 = fan.steps.map(s => s.p5);
  const p25 = fan.steps.map(s => s.p25);
  const p50 = fan.steps.map(s => s.p50);
  const p75 = fan.steps.map(s => s.p75);
  const p95 = fan.steps.map(s => s.p95);

  // 纵轴模式由 chooseScale 决定: 全路径外圈极差 p95max/p5min > 50 → log 兜底,
  // 否则 linear 累计收益%。为什么需要 log 兜底: 2026-08-13 事故 —— 旧仓位口径下
  // P95 冲到几百万%, 线性轴上中位线被压成贴着底部的一条线; 口径修正后常态是个位数倍,
  // 但极端策略 (或未来口径再出 bug) 仍可能撑爆线性轴, log 是保险。
  const scale = chooseScale(Math.min.apply(null, p5), Math.max.apply(null, p95));
  // toY: 净值倍数 → 纵轴值 (linear 换算累计收益%, log 保留倍数); toMult 是其逆变换 (tooltip 用)
  const toY = scale === 'log' ? (v => v) : (v => (v - 1) * 100);
  const toMult = scale === 'log' ? (v => v) : (v => v / 100 + 1);
  // 刻度/标签格式化: linear 用带逗号的 % (大数字自动千分位), log 用 1x/10x/1万x
  const fmtY = v => scale === 'log' ? _fmtMult(v) : Number(v).toLocaleString('en-US', { maximumFractionDigits: 1 }) + '%';
  const fmtMultLabel = m => scale === 'log' ? _fmtMult(m) : fmtY((m - 1) * 100);

  const p5y = p5.map(toY), p25y = p25.map(toY), p50y = p50.map(toY), p75y = p75.map(toY), p95y = p95.map(toY);
  // 堆叠 area 技巧: 下层透明占位 (把上层顶到正确高度), 上层填色带 = 分位差。
  // log 模式下 toY 是恒等变换, 两种模式统一用 toY 差值作带宽, 带边缘都精确落在分位线上。
  const bandOuter = fan.steps.map(s => toY(s.p95) - toY(s.p5));   // p5~p95 带宽
  const bandInner = fan.steps.map(s => toY(s.p75) - toY(s.p25));  // p25~p75 带宽
  const finalMed = p50[p50.length - 1];
  // log 轴不允许 ≤0: 净值倍数恒 >0 (单笔最多亏 100%), 但 P5 可能极接近 0,
  // 显式取一个 >0 的下限并留一点底部空间, 不写死 0 (仅 log 模式需要)
  const minP5 = Math.min.apply(null, p5);
  const yMin = minP5 > 0 ? Math.max(1e-6, minP5 * 0.8) : 1e-6;
  // 真实回测终点: 给用户一个"扇面中位数 vs 实际"的锚
  const hasActual = typeof o.actualReturn === 'number' && Number.isFinite(o.actualReturn) && o.actualReturn > -1;
  const actualMult = hasActual ? 1 + o.actualReturn : null;
  // 口径说明 (副标题第二行)
  const note = (fan.lowSample ? '⚠ 交易样本不足 20 笔，分布仅供参考' : '假设各笔交易独立同分布，非收益预测')
    + ' · 单笔按入场时总权益折算';

  // 横线: 本金线 (linear 在 0%, log 在 1x) + (有数据时) 实际回测终点
  const markLineData = [
    { yAxis: toY(1), label: { formatter: '本金 ' + fmtMultLabel(1), color: c.text2, fontSize: 9 },
      lineStyle: { color: c.text2, type: 'dashed', width: 1 } },
  ];
  if (hasActual) {
    markLineData.push({
      yAxis: toY(actualMult),
      label: { formatter: '实际回测终点 ' + fmtMultLabel(actualMult), color: c.up, fontSize: 9, position: 'insideStartTop' },
      lineStyle: { color: c.up, type: 'solid', width: 1.5 } });
  }

  chart.setOption({
    // 标题两行放左上; 图例放右上并给 toolbox 让位 (right 110), 三者互不叠压
    title: { left: 10, top: 4,
      text: (scale === 'log' ? '净值倍数分布（对数轴）' : '累计收益分布')
        + ' · bootstrap ' + MC_SIMS + ' 次 · 净收益口径(pnl)，未扣买入费用',
      subtext: note,
      textStyle: { color: c.text2, fontSize: 10, fontWeight: 'normal' },
      subtextStyle: { color: fan.lowSample ? c.warn : c.text2, fontSize: 10 } },
    tooltip: { trigger: 'axis', formatter: function (params) {
      let s = '第 ' + params[0].axisValue + ' 笔后（净值倍数 / 累计收益）<br/>';
      const order = { 'P95': 0, 'P75': 1, '中位': 2, 'P25': 3, 'P5': 4 };
      params.filter(p => order[p.seriesName] != null)
        .sort((a, b) => order[a.seriesName] - order[b.seriesName])
        .forEach(p => {
          const m = toMult(p.value);  // 两种模式统一换回倍数, tooltip 保持「倍数 + %」双口径
          s += '<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:' + p.color + ';margin-right:var(--sp-1)"></span>';
          s += p.seriesName + ': <b>' + _fmtMult(m) + '</b>（' + ((m - 1) * 100).toFixed(1) + '%）<br/>';
        });
      if (hasActual) s += '实际回测终点: <b>' + _fmtMult(actualMult) + '</b><br/>';
      return s;
    }},
    legend: { top: 4, right: 110, textStyle: { color: c.text, fontSize: 10 },
      data: ['中位', 'P25~P75', 'P5~P95'] },
    dataZoom: [
      { type: 'slider', xAxisIndex: 0, bottom: 10, height: 20,
        borderColor: c.border, fillerColor: hexToRgba(c.accent, 0.13),
        textStyle: { color: c.text2, fontSize: 9 } },
    ],
    // containLabel: 轴标签完整收进网格, 不被裁到图表外;
    // top 90 给标题两行+图例留位, right 80 给末位中位数标注留位, bottom 70 给轴名+缩放条留位
    grid: { left: 20, right: 80, top: 90, bottom: 70, containLabel: true },
    xAxis: { type: 'category', data: steps, name: '交易笔数', nameLocation: 'middle', nameGap: 24,
      nameTextStyle: { color: c.text2, fontSize: 10 },
      axisLine: { lineStyle: { color: c.border } }, axisLabel: { color: c.text2, fontSize: 9 } },
    // 单位已在刻度标签里 (% 或 x), 不再放轴名
    yAxis: scale === 'log'
      ? { type: 'log', min: yMin,
          axisLabel: { color: c.text2, fontSize: 9, formatter: function (v) { return _fmtMult(v); } },
          splitLine: { lineStyle: { color: c.border } } }
      : { type: 'value',
          axisLabel: { color: c.text2, fontSize: 9, formatter: function (v) { return fmtY(v); } },
          splitLine: { lineStyle: { color: c.border } } },
    toolbox: { right: 10, top: 0, feature: {
      saveAsImage: { title: '保存图片', pixelRatio: 2 },
      dataZoom: { title: { zoom: '区域缩放', back: '还原' } },
      restore: { title: '刷新' },
    }, iconStyle: { borderColor: c.text2 } },
    series: [
      // 外层扇面 p5~p95 (堆叠: 透明占位 p5 + 填色带)
      { name: '_p5base', type: 'line', stack: 'outer', data: p5y, symbol: 'none',
        lineStyle: { opacity: 0 }, areaStyle: { opacity: 0 }, silent: true, tooltip: { show: false } },
      { name: 'P5~P95', type: 'line', stack: 'outer', data: bandOuter, symbol: 'none',
        lineStyle: { opacity: 0 }, areaStyle: { color: hexToRgba(c.accent, 0.10) },
        itemStyle: { color: hexToRgba(c.accent, 0.35) },  // 图例色块贴近带填充色
        silent: true, tooltip: { show: false } },
      // 内层扇面 p25~p75 (堆叠: 透明占位 p25 + 填色带)
      { name: '_p25base', type: 'line', stack: 'inner', data: p25y, symbol: 'none',
        lineStyle: { opacity: 0 }, areaStyle: { opacity: 0 }, silent: true, tooltip: { show: false } },
      { name: 'P25~P75', type: 'line', stack: 'inner', data: bandInner, symbol: 'none',
        lineStyle: { opacity: 0 }, areaStyle: { color: hexToRgba(c.accent, 0.22) },
        itemStyle: { color: hexToRgba(c.accent, 0.55) },  // 图例色块贴近带填充色
        silent: true, tooltip: { show: false } },
      // 分位线: P5/P95 细虚线, P25/P75 细线, 中位加粗
      { name: 'P5', type: 'line', data: p5y, symbol: 'none',
        itemStyle: { color: c.accent }, lineStyle: { color: c.accent, width: 1, type: 'dashed', opacity: 0.6 } },
      { name: 'P25', type: 'line', data: p25y, symbol: 'none',
        itemStyle: { color: c.accent }, lineStyle: { color: c.accent, width: 1, opacity: 0.6 } },
      { name: '中位', type: 'line', data: p50y, symbol: 'none',
        itemStyle: { color: c.accent }, lineStyle: { color: c.accent, width: 2.5 },
        markLine: { silent: true, symbol: 'none', data: markLineData },
        // 末位中位数标注: endLabel 跟线尾, clip:false + grid.right 80 保证完整可见
        endLabel: { show: true, formatter: '中位 ' + fmtMultLabel(finalMed), color: c.accent, fontSize: 10, distance: 6 },
        clip: false },
      { name: 'P75', type: 'line', data: p75y, symbol: 'none',
        itemStyle: { color: c.accent }, lineStyle: { color: c.accent, width: 1, opacity: 0.6 } },
      { name: 'P95', type: 'line', data: p95y, symbol: 'none',
        itemStyle: { color: c.accent }, lineStyle: { color: c.accent, width: 1, type: 'dashed', opacity: 0.6 } },
    ],
  }, true);
  return chart;
}

// ── 滚动指标面板 (Phase 2) ──

/**
 * 渲染滚动指标: 滚动夏普 (左轴) + 滚动年化收益% / 滚动年化波动率% (右轴)。
 * 一眼看清"策略状态是不是稳定的" —— 夏普长期趴在 0 以下, 总收益再好也是运气段撑的。
 * 契约 (后端钉死): rolling = {window, dates, rolling_sharpe, rolling_return, rolling_vol},
 *   return/vol 是小数 (0.15 = 15%), 三序列含 null (窗口不足) → connectNulls:false 画断点线;
 *   key 整个不存在 → 静默隐藏容器。
 * @param {string} domId 图表容器 id (.chart-box-body)
 * @param {Object} rolling 后端 rolling_metrics / 实盘 equity 响应的 rolling key
 * @param {Object} [colors] getColors() 结果, 不传则现场取 (主题自适应)
 * @returns {Object|null} ECharts 实例; 数据不足隐藏容器并返回 null
 */
export function renderRolling(domId, rolling, colors) {
  if (!rolling || !Array.isArray(rolling.dates) || rolling.dates.length < 2) { _hideBox(domId); return null; }
  // 序列防御: 缺序列按空数组; 非有限值一律归一为 null (断线)
  const toNum = arr => (Array.isArray(arr) ? arr : []).map(v => (typeof v === 'number' && Number.isFinite(v)) ? v : null);
  const sharpe = toNum(rolling.rolling_sharpe);
  const ret = toNum(rolling.rolling_return).map(v => v == null ? null : v * 100);  // 小数 → %
  const vol = toNum(rolling.rolling_vol).map(v => v == null ? null : v * 100);
  if (![sharpe, ret, vol].some(a => a.some(v => v != null))) { _hideBox(domId); return null; }
  const c = colors || getColors();
  const chart = echartsInit(domId);
  if (!chart) return null;
  _showBox(domId);

  const win = rolling.window || 60;
  const dates = rolling.dates.map(d => String(d || '').slice(0, 10));

  chart.setOption({
    // 卡片头已有标题, 图内只留一行口径说明, 避免与图例/纵轴挤在一起 (2026-08-14 重叠修复)
    title: { left: 10, top: 4,
      text: win + ' 个交易日滑动窗口 · 年化 · 窗口不足处断线 · 夏普看左轴，收益/波动率看右轴(%)',
      textStyle: { color: c.text2, fontSize: 10, fontWeight: 'normal' } },
    tooltip: { trigger: 'axis', formatter: function (params) {
      let s = params[0].axisValue + '<br/>';
      params.forEach(p => {
        const v = p.value;
        const txt = v == null ? '-' : (p.seriesName === '滚动夏普' ? Number(v).toFixed(2) : Number(v).toFixed(1) + '%');
        s += '<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:' + p.color + ';margin-right:var(--sp-1)"></span>';
        s += p.seriesName + ': <b>' + txt + '</b><br/>';
      });
      return s;
    }},
    // 图例给 toolbox 让位 (right 110), 与 renderMonteCarlo 同约定
    legend: { top: 4, right: 110, textStyle: { color: c.text, fontSize: 10 },
      data: ['滚动夏普', '滚动年化收益', '滚动年化波动率'] },
    dataZoom: [
      { type: 'slider', xAxisIndex: 0, bottom: 10, height: 20,
        borderColor: c.border, fillerColor: hexToRgba(c.accent, 0.13),
        textStyle: { color: c.text2, fontSize: 9 } },
    ],
    // containLabel 收全左右两轴标签; 顶部只剩一行说明+图例, grid.top 收紧
    grid: { left: 20, right: 20, top: 36, bottom: 60, containLabel: true },
    xAxis: { type: 'category', data: dates, axisLine: { lineStyle: { color: c.border } }, axisLabel: { color: c.text2, fontSize: 9 } },
    yAxis: [
      // 不放轴名 (说明行已注明左右轴分工), 轴名是压刻度标签的高发区
      { type: 'value',
        axisLabel: { color: c.text2, fontSize: 9 }, splitLine: { lineStyle: { color: c.border } } },
      { type: 'value',
        axisLabel: { color: c.text2, fontSize: 9, formatter: '{value}%' }, splitLine: { show: false } },
    ],
    toolbox: { right: 10, top: 0, feature: {
      saveAsImage: { title: '保存图片', pixelRatio: 2 },
      dataZoom: { title: { zoom: '区域缩放', back: '还原' } },
      restore: { title: '刷新' },
    }, iconStyle: { borderColor: c.text2 } },
    series: [
      { name: '滚动夏普', type: 'line', yAxisIndex: 0, data: sharpe, symbol: 'none',
        connectNulls: false,  // 窗口不足的 null 画断点线, 不硬连
        itemStyle: { color: c.accent }, lineStyle: { color: c.accent, width: 2 },
        markLine: { silent: true, symbol: 'none', data: [
          { yAxis: 0, lineStyle: { color: c.text2, type: 'dashed', width: 1 } },
        ] } },
      { name: '滚动年化收益', type: 'line', yAxisIndex: 1, data: ret, symbol: 'none',
        connectNulls: false,
        itemStyle: { color: c.up }, lineStyle: { color: c.up, width: 1.5 } },
      { name: '滚动年化波动率', type: 'line', yAxisIndex: 1, data: vol, symbol: 'none',
        connectNulls: false,
        itemStyle: { color: c.warn }, lineStyle: { color: c.warn, width: 1.5, type: 'dashed' } },
    ],
  }, true);
  return chart;
}

// ── 业绩归因 (Phase 2) ──

/**
 * 归因横向条形图共用实现: pnl 降序 (后端已排好), 正贡献红 / 负贡献绿 (A股红涨绿跌),
 * 条端标签 + tooltip 用 formatMoney 大白话金额, 另附占比%。
 * @param {string} domId 图表容器 id
 * @param {Array<{label:string, pnl:number, pct:number}>} rows 已带显示标签的行
 * @param {Object} colors getColors() 结果
 * @param {number} skipped meta.skipped_no_pnl (>0 时副标题提示, 实盘早期卖出无盈亏记录)
 */
function _renderAttrBars(domId, rows, colors, skipped) {
  const valid = (rows || []).filter(r => typeof r.pnl === 'number' && Number.isFinite(r.pnl));
  if (valid.length === 0) { _hideBox(domId); return null; }
  const c = colors || getColors();
  const chart = echartsInit(domId);
  if (!chart) return null;
  _showBox(domId);

  const note = skipped > 0 ? '⚠ ' + skipped + ' 笔早期卖出无盈亏记录，未计入' : '';
  chart.setOption({
    title: note ? { left: 10, top: 4, text: '', subtext: note,
      subtextStyle: { color: c.warn, fontSize: 10 } } : undefined,
    tooltip: { trigger: 'item', formatter: function (p) {
      const pct = p.data.pct;
      return p.name + '<br/>盈亏: <b>' + formatMoney(p.value) + '</b>'
        + (typeof pct === 'number' && Number.isFinite(pct) ? '<br/>占总盈亏: <b>' + (pct * 100).toFixed(1) + '%</b>' : '');
    }},
    // 板块多于 15 个时给 y 轴缩放条 (默认露出贡献最大的前 15 个)
    dataZoom: valid.length > 15 ? [
      { type: 'slider', yAxisIndex: 0, right: 6, top: note ? 40 : 30, bottom: 20, width: 14,
        start: 0, end: Math.round(1500 / valid.length),
        borderColor: c.border, fillerColor: hexToRgba(c.accent, 0.13),
        textStyle: { color: c.text2, fontSize: 9 } },
    ] : [],
    // containLabel 收全行业名; right 60 给条端金额标签留位
    grid: { left: 20, right: 60, top: note ? 40 : 30, bottom: 30, containLabel: true },
    xAxis: { type: 'value',
      axisLabel: { color: c.text2, fontSize: 9, formatter: function (v) { return formatMoney(v); } },
      splitLine: { lineStyle: { color: c.border } } },
    yAxis: { type: 'category', inverse: true,  // 降序第一条在最上面
      data: valid.map(r => r.label),
      axisLine: { lineStyle: { color: c.border } },
      axisLabel: { color: c.text2, fontSize: 9, width: 90, overflow: 'truncate' } },
    series: [
      { name: '盈亏', type: 'bar', barMaxWidth: 18,
        data: valid.map(r => ({
          value: r.pnl, pct: r.pct,
          itemStyle: { color: r.pnl >= 0 ? c.up : c.down },  // 赚钱红 / 亏钱绿
          label: { position: r.pnl >= 0 ? 'right' : 'left' },
        })),
        label: { show: true, fontSize: 9, color: c.text2,
          formatter: function (p) { return formatMoney(p.value); } },
        markLine: { silent: true, symbol: 'none', data: [
          { xAxis: 0, lineStyle: { color: c.text2, type: 'dashed', width: 1 } },
        ] },
      },
    ],
  }, true);
  return chart;
}

/**
 * 渲染业绩归因 · 板块: 横向条形图, pnl 降序 (后端已排好), 未标行业照常显示。
 * 契约 (后端钉死): attribution.by_sector = [{name, pnl, pct}], pnl 单位元, pct 小数;
 *   key 不存在 → 静默隐藏容器; attribution.meta.skipped_no_pnl > 0 → 副标题提示 (实盘侧)。
 * @param {string} domId 图表容器 id (.chart-box-body)
 * @param {Object} attribution 后端归因响应 (回测 data.attribution / 实盘 /attribution 响应)
 * @param {Object} [colors] getColors() 结果, 不传则现场取
 * @returns {Object|null} ECharts 实例; 无数据隐藏容器并返回 null
 */
export function renderAttribution(domId, attribution, colors) {
  const rows = (attribution && Array.isArray(attribution.by_sector) ? attribution.by_sector : [])
    .map(r => ({ label: r.name || '未标', pnl: r.pnl, pct: r.pct }));
  const skipped = attribution && attribution.meta && attribution.meta.skipped_no_pnl > 0
    ? attribution.meta.skipped_no_pnl : 0;
  return _renderAttrBars(domId, rows, colors, skipped);
}

/**
 * 渲染业绩归因 · 个股 Top10: 同板块图风格, 标签「名称(代码)」, name 空则只显示 code。
 * 契约: attribution.by_stock_top = [{code, name, pnl, pct}], 已按 |pnl| 取前 10。
 * @param {string} domId 图表容器 id (.chart-box-body)
 * @param {Object} attribution 后端归因响应
 * @param {Object} [colors] getColors() 结果, 不传则现场取
 * @returns {Object|null} ECharts 实例; 无数据隐藏容器并返回 null
 */
export function renderAttributionStocks(domId, attribution, colors) {
  const rows = (attribution && Array.isArray(attribution.by_stock_top) ? attribution.by_stock_top : [])
    .map(r => ({ label: r.name ? r.name + '(' + r.code + ')' : String(r.code || ''), pnl: r.pnl, pct: r.pct }));
  const skipped = attribution && attribution.meta && attribution.meta.skipped_no_pnl > 0
    ? attribution.meta.skipped_no_pnl : 0;
  return _renderAttrBars(domId, rows, colors, skipped);
}

// ── 回测 Tab 收口 ──

/**
 * 回测 Tab 深挖图收口: 水下曲线 + 蒙特卡洛扇面 + 滚动指标 + 业绩归因一次全渲。
 * 每张图各自 try/catch, 单图失败不影响其他图 (catch 里 console.warn + 隐藏该容器)。
 * rolling_metrics / attribution key 不存在 (老历史结果) 时对应容器静默隐藏。
 * @param {Object} data 回测结果 (与 renderAllCharts 同一份: {equity, trades, ...})
 */
export function renderDeepCharts(data) {
  if (!data) return;
  try {
    renderUnderwater('chartUnderwater', data.equity, getColors());
  } catch (e) {
    console.warn('renderUnderwater 失败:', e);
    _hideBox('chartUnderwater');
  }
  try {
    // 权益序列给蒙特卡洛折算口径 (单笔 pnl / 入场时总权益);
    // metrics.cumulative_return 画「实际回测终点」对比线
    renderMonteCarlo('chartMonteCarlo', data.trades || [], getColors(), {
      equity: data.equity,
      actualReturn: data.metrics ? data.metrics.cumulative_return : null,
    });
  } catch (e) {
    console.warn('renderMonteCarlo 失败:', e);
    _hideBox('chartMonteCarlo');
  }
  try {
    renderRolling('chartRolling', data.rolling_metrics, getColors());
  } catch (e) {
    console.warn('renderRolling 失败:', e);
    _hideBox('chartRolling');
  }
  try {
    renderAttribution('chartAttrSector', data.attribution, getColors());
  } catch (e) {
    console.warn('renderAttribution 失败:', e);
    _hideBox('chartAttrSector');
  }
  try {
    renderAttributionStocks('chartAttrStock', data.attribution, getColors());
  } catch (e) {
    console.warn('renderAttributionStocks 失败:', e);
    _hideBox('chartAttrStock');
  }
  // Phase 3: 保存全量 trades 引用并给交易表挂点击委托 (幂等, 内部先移除旧监听)
  try {
    wireTradeReplay(data.trades || []);
  } catch (e) {
    console.warn('wireTradeReplay 失败:', e);
  }
}
