// 大盘位置页签前端纯函数单测（不碰 DOM/fetch —— 只测 module.exports 那几个）
// 用法: node tests/js/test_market_position.js

const path = require('path');
const mp = require(path.join(__dirname, '..', '..', 'web', 'js', 'market_position.js'));

let pass = 0, fail = 0;
function assert(cond, label) {
  if (cond) { console.log(`  ✓ ${label}`); pass++; }
  else      { console.log(`  ✗ ${label}`); fail++; }
}
function eq(a, b, label) { assert(a === b, `${label} (期望 ${b}, 实际 ${a})`); }

console.log('positionLabel 位置分档:');
eq(mp.positionLabel(95).text, '高位', '95 → 高位');
eq(mp.positionLabel(80).text, '高位', '80 边界 → 高位');
eq(mp.positionLabel(65).text, '偏高', '65 → 偏高');
eq(mp.positionLabel(50).text, '中位', '50 → 中位');
eq(mp.positionLabel(25).text, '偏低', '25 → 偏低');
eq(mp.positionLabel(5).text, '低位', '5 → 低位');
eq(mp.positionLabel(null).text, '【缺】', 'null → 缺（不冒充 0）');

console.log('\nfmtPct / fmtNum:');
eq(mp.fmtPct(12.34, true), '+12.3%', '带符号保留一位');
eq(mp.fmtPct(12.34, false), '12.3%', '不带符号');
eq(mp.fmtPct(-8.9, true), '-8.9%', '负数不加多余加号');
eq(mp.fmtPct(null), '—', 'null → 破折号');
eq(mp.fmtPct(undefined), '—', 'undefined → 破折号');
eq(mp.fmtPct(NaN), '—', 'NaN → 破折号');
eq(mp.fmtNum(1.005, 2), '1.00', 'fmtNum 保留两位');
eq(mp.fmtNum(null), '—', 'fmtNum null → 破折号');

console.log('\nesc 转义:');
eq(mp.esc('<b>"x"</b>'), '&lt;b&gt;&quot;x&quot;&lt;/b&gt;', '尖括号与引号转义');
eq(mp.esc(null), '', 'null → 空串');

console.log('\npositionRowsHtml 三大指数表:');
const rec = {
  indices: {
    shanghai:    { name: '上证指数', code: '000001.SH', close: 3864.3, pct_10y: 90.9,
                   from_high_pct: -8.9, vol_ann_20: 12.2, ret_1y_pct: -0.2,
                   ma250_dev_pct: -3.1, regime: 'range' },
    hs300:       { name: '沪深300', code: '000300.SH', close: 4450.0, pct_10y: 73.0,
                   from_high_pct: -23.4, vol_ann_20: 14.1, ret_1y_pct: -1.6,
                   ma250_dev_pct: -4.8, regime: 'range' },
    chuangyeban: { name: '创业板指', code: '399006.SZ', close: 3247.9, pct_10y: 88.4,
                   from_high_pct: -25.7, vol_ann_20: 31.8, ret_1y_pct: 7.5,
                   ma250_dev_pct: -5.3, regime: 'range' }
  }
};
const rows = mp.positionRowsHtml(rec);
assert((rows.match(/<tr>/g) || []).length === 3, '恰好渲染 3 行');
assert(rows.includes('上证指数(000001.SH)'), '标的全名+代码都在');
assert(rows.includes('90.9%') && rows.includes('高位'), '十年百分位带分档标签');
assert(rows.includes('震荡'), '牛熊映射成中文');
eq(mp.positionRowsHtml({}).split('<tr>').length - 1, 3, '缺数据也渲染 3 行（不崩）');
assert(mp.positionRowsHtml({}).includes('【缺】'), '缺数据标【缺】');

console.log('\nmirrorRowsHtml 照镜子表:');
assert(mp.mirrorRowsHtml({}).includes('没有可用的相似日'), '空 matches 有提示文案');
const md = { matches: [{ date: '2022-01-24', distance: 0.89, fwd_20_hs300_pct: -4.28,
                         fwd_60_hs300_pct: -18.62, fwd_20_sh_pct: -1.8 }] };
const mr = mp.mirrorRowsHtml(md);
assert(mr.includes('2022-01-24') && mr.includes('-4.3%'), '相似日与后续涨跌都渲染');
assert(mr.includes('var(--down)'), '下跌用绿色令牌（A 股红涨绿跌）');

console.log('\nshadowRowsHtml 影子规则表:');
assert(mp.shadowRowsHtml({ ok: false, reason: '录像太短' }).includes('录像太短'),
       '不可用时显示原因');
const sd = { ok: true, rows: [{ rule: 'ma20', annualized_pct: 2.6, max_drawdown_pct: -36.0,
                                sharpe: 0.15, calmar: 0.07, exposure_pct: 52.7 }],
             buy_hold: { rule: 'buy_hold', annualized_pct: 4.3, max_drawdown_pct: -46.7,
                         sharpe: 0.24, calmar: 0.09, exposure_pct: 100.0 } };
const sr = mp.shadowRowsHtml(sd);
assert(sr.includes('沪深300 &gt; MA20') || sr.includes('沪深300 > MA20'), '规则名映射成中文');
assert(sr.includes('买入持有（对照）'), '买入持有对照也在表里');

console.log('\ntrendSeries 趋势图数据:');
const ts = mp.trendSeries([
  { date: '2026-09-14', breadth: { above_ma20_pct: 29.1 }, indices: { shanghai: { pct_10y: 91.2 } } },
  { date: '2026-09-15', breadth: { above_ma20_pct: 21.2 }, indices: { shanghai: { pct_10y: 90.9 } } }
]);
eq(ts.dates.length, 2, '日期两条');
eq(ts.width[1], 21.2, '宽度取 breadth.above_ma20_pct');
eq(ts.pct[1], 90.9, '百分位取 indices.shanghai.pct_10y');
const ts2 = mp.trendSeries([{ date: 'x' }]);
eq(ts2.width[0], null, '缺字段补 null（不画 0 线）');
eq(mp.trendSeries(null).dates.length, 0, 'null 输入不崩');

console.log(`\n结果: ${pass} 通过, ${fail} 失败`);
process.exit(fail === 0 ? 0 : 1);
