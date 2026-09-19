// tests/js/test_market_dashboard.js — 大盘环境仪表盘前端纯函数单测 (2026-09-19 UIUX 改造)
//
// 为什么要有它: 2026-09-19 审计发现 renderHistorySummary 的运算符优先级 bug ——
//   '总分从 X 到 Y（' + deltaCls(...) === 'up' ? '转暖' : '变化' + ...
// '+' 优先级高于 '===', 整句前缀被吞、永远显示"变化"。Node 实测复现后才修。
// 本测试把修复锁死, 顺带锁温度分三档着色与涨跌三态的语义边界。
//
// 用法: node tests/js/test_market_dashboard.js
'use strict';

let pass = 0, fail = 0;
function assert(cond, label) {
  if (cond) { console.log(`  ✓ ${label}`); pass++; }
  else      { console.log(`  ✗ ${label}`); fail++; }
}

// market_dashboard.js 是 ES module 且 import charts.js (其顶层有 getElementById),
// Node 下要先垫一个最小 document 桩再动态 import
globalThis.document = { getElementById: () => null };

const snap = (totals) => totals.map(t => ({ date: '2026-09-01', scores: { final_total: t } }));

(async () => {
  const md = await import('../../web/js/market_dashboard.js');

  console.log('historySummaryText 摘要文案 (2026-09-19 优先级 bug 回归锁):');
  const up = md.historySummaryText(snap([5.0, 5.2, 5.8]));
  assert(up !== null, '两条以上数据必须给摘要');
  assert(up.includes('近3个交易日'), '必须包含「近N个交易日」前缀 (原 bug 把前缀整句吞掉)');
  assert(up.includes('总分从 5.00 到 5.80'), '必须包含起止分 (原 bug 吞掉)');
  assert(up.includes('转暖'), '上升必须说「转暖」 (原 bug 永远显示「变化」)');
  assert(up.includes('+0.80'), '带符号变化量');
  assert(!up.includes('（变化'), '不许再出现无头的「变化」');
  const down = md.historySummaryText(snap([6.0, 5.5, 5.0]));
  assert(down.includes('转冷'), '下降必须说「转冷」');
  const flat = md.historySummaryText(snap([5.0, 5.0]));
  assert(flat.includes('持平'), '不变必须说「持平」');
  assert(md.historySummaryText(snap([5.0])) === null, '单条数据 → null (走"历史数据不足"分支)');
  assert(md.historySummaryText([]) === null, '空数据 → null');

  console.log('\nscoreClass 温度分三档 (与涨跌红绿脱钩):');
  assert(md.scoreClass(null) === 'md-flat', '缺分 → 灰');
  assert(md.scoreClass(7.2) === 'md-score-hot', '≥6.5 → 热(警示色)');
  assert(md.scoreClass(6.5) === 'md-score-hot', '6.5 边界 → 热');
  assert(md.scoreClass(5.5) === 'md-score-mid', '4.5~6.5 → 中性');
  assert(md.scoreClass(4.5) === 'md-score-mid', '4.5 边界 → 中性');
  assert(md.scoreClass(3.0) === 'md-score-cold', '<4.5 → 冷(信息蓝)');

  console.log('\ndeltaCls 方向三态 (红绿只留给方向):');
  assert(md.deltaCls(0.5) === 'md-up', '升 → 红');
  assert(md.deltaCls(-0.5) === 'md-down', '降 → 绿');
  assert(md.deltaCls(0) === 'md-flat', '持平 → 灰');

  console.log('\nradarFromDims 五维雷达 (2026-09-19 多维图表):');
  const rd = md.radarFromDims({ valuation: { avg: 7.5 }, macro: { avg: 4.5 },
    trend: { avg: 5 }, sentiment: { avg: 4 }, overseas: { avg: 4 } });
  assert(rd.indicators.length === 5, '五维齐全');
  assert(rd.indicators[0].name === '估值赔率', '第一维中文名');
  assert(rd.indicators[0].max === 10, '量纲 0~10');
  assert(rd.values[0] === 7.5, '值透传');
  const rd2 = md.radarFromDims({ valuation: { avg: 7.5 }, macro: { avg: null }, trend: { avg: 5 } });
  assert(rd2.indicators.length === 2, '缺维度/缺值要跳过不画');
  assert(md.radarFromDims(null).indicators.length === 0, '空快照 → 空雷达');

  console.log('\nbuildRegimeBands 牛熊带:');
  const dts = ['d1','d2','d3','d4','d5','d6','d7','d8','d9','d10'];
  const rg1 = ['bull','bull','bull','bull','bull','bear','bear','bear','bear','bear'];
  const bands = md.buildRegimeBands(dts, rg1);
  assert(bands.length === 2, '两段牛熊各合并成一带');
  assert(bands[0][0].xAxis === 'd1' && bands[0][1].xAxis === 'd5', '牛带起止');
  assert(bands[1][0].xAxis === 'd6' && bands[1][1].xAxis === 'd10', '熊带起止');
  const rg2 = ['bull','bear','bull','bear','bull','bear','bull','bear','bull','bear'];
  assert(md.buildRegimeBands(dts, rg2).length === 0, '全碎段(1天) → 全丢弃(斑马线过滤)');
  const rg3 = ['bull','bull','bull','bull','bull', null, null, null, null, null];
  assert(md.buildRegimeBands(dts, rg3).length === 1, 'null 段不画');

  console.log('\nbuildHeatRows 温度带:');
  const hr = md.buildHeatRows(dts.slice(0, 3), [
    { name: 'a', values: [10, null, 30] }, { name: 'b', values: [40, 50, 60] }]);
  assert(hr.length === 5, 'null 跳过不画 (3+3-1)');
  assert(hr[0][2] === 10 && hr[0][1] === 0, '第一行第一点是 a 序列');
  assert(hr[3][2] === 50 && hr[3][1] === 1, '行序 = 序列顺序');

  console.log(`\n结果: ${pass} 通过, ${fail} 失败`);
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('加载/执行失败:', e); process.exit(1); });
