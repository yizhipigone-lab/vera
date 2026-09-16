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

console.log('\nmirrorSummaryHtml / mirrorYearsHtml 分位带结论与逐年拆解:');
const mdFull = {
  ok: true, exclude_recent: 252,
  summary: { n: 110, fwd_20_median: 1.46, fwd_20_mean: -0.07, fwd_20_q25: -3.19,
             fwd_20_q75: 3.26, fwd_20_up_ratio: 57.0, fwd_60_median: 1.92,
             fwd_60_up_ratio: 65.0, n_eff_20: 5.5, n_eff_60: 1.8 },
  years: [{ year: '2016', n: 45, median_pct: 1.79, up_ratio_pct: 62.0 },
          { year: '2022', n: 24, median_pct: -6.87, up_ratio_pct: 0.0 }]
};
const ms2 = mp.mirrorSummaryHtml(mdFull);
assert(ms2.includes('110'), '结论句报的是分位带样本数');
assert(ms2.includes('5.5'), '必须把有效独立样本（不是 110）写出来');
assert(ms2.includes('排除最近 252'), '写明排除了最近一年');
assert(mp.mirrorSummaryHtml({ ok: false, reason: '录像太短' }) === '录像太短',
       '不可用时返回原因');
const ys = mp.mirrorYearsHtml(mdFull);
assert(ys.includes('2016 年') && ys.includes('2022 年'), '逐年拆解两行都在');
assert(ys.includes('-6.9%'), '该年后续中位数带符号渲染');
assert(mp.mirrorYearsHtml({}).includes('没有可拆解的年份'), '空年份有提示文案');

console.log('\nshadowRowsHtml 影子规则表（毛/净并列）:');
assert(mp.shadowRowsHtml({ ok: false, reason: '录像太短' }).includes('录像太短'),
       '不可用时显示原因');
const sdRow = { rule: 'ma20', round_trips: 196, annualized_pct: 2.6,
                exposure_pct: 52.7,
                net: { annualized_pct: 1.0, ci_low_pct: -6.1, ci_high_pct: 10.0,
                       max_drawdown_pct: -39.6 },
                segments: { mean_return_pct: 0.28, win_ratio_pct: 22.0,
                            median_days: 4, min_days: 1, max_days: 56, note: '' } };
const sd = { ok: true, rows: [sdRow],
             buy_hold: { rule: 'buy_hold', round_trips: 1, annualized_pct: 4.3,
                         exposure_pct: 100.0, net: { annualized_pct: 4.3 },
                         segments: {} } };
const sr = mp.shadowRowsHtml(sd);
assert(sr.includes(mp.RULE_CN.ma20), '规则名映射成人话（不出现裸键名 ma20）');
assert(sr.includes(mp.RULE_CN.buy_hold), '买入持有对照也在表里');
assert(sr.includes('196 次'), '建仓次数进表');
assert(sr.includes('+2.6%') && sr.includes('+1.0%'), '毛年化与净年化并排');
assert(sr.includes('-6.1%') && sr.includes('+10.0%'), '净口径 95% 区间进表');

console.log('\nshadowVerdictHtml 措辞纪律:');
const vCross = mp.shadowVerdictHtml(sd);
assert(vCross.includes('看不出显著的优势或劣势'), '区间跨过 0 → 只说看不出显著优劣');
assert(!vCross.includes('无效') && !vCross.includes('跑输'),
       '措辞纪律：不许写"无效"或"跑输"');
const vBad = mp.shadowVerdictHtml({ ok: true, rows: [Object.assign({}, sdRow, {
  net: { annualized_pct: -1.1, ci_low_pct: -8.2, ci_high_pct: -0.4 } })] });
assert(vBad.includes('明显比一直拿着差'), '区间整体在 0 以下 → 才说明显更差');
const vGood = mp.shadowVerdictHtml({ ok: true, rows: [Object.assign({}, sdRow, {
  net: { annualized_pct: 5.0, ci_low_pct: 0.4, ci_high_pct: 9.0 } })] });
assert(vGood.includes('明显比一直拿着好'), '区间整体在 0 以上 → 说明显更好');

console.log('\nshadowWindowsHtml 双窗口一致性:');
const swin = mp.shadowWindowsHtml({ ok: true, rows: [Object.assign({}, sdRow, {
  windows: { 'in': { annualized_pct: 6.2 }, out: { annualized_pct: -4.0 },
             consistent: false } })] });
assert(swin.includes('不一致，待复核'), '不同向 → 标待复核');
assert(swin.includes('+6.2%') && swin.includes('-4.0%'), '两段年化都渲染');
const swin2 = mp.shadowWindowsHtml({ ok: true, rows: [Object.assign({}, sdRow, {
  windows: { 'in': { annualized_pct: 6.5 }, out: { annualized_pct: 2.0 },
             consistent: true } })] });
assert(swin2.includes('同向，算数'), '同向 → 标算数');

console.log('\nregimeBands 牛熊背景带:');
const bandItems = [
  { date: '2024-01-01', indices: { hs300: { regime: 'bear' } } },
  { date: '2024-01-02', indices: { hs300: { regime: 'bear' } } },
  { date: '2024-01-03', indices: { hs300: { regime: 'range' } } },
  { date: '2024-01-04', indices: { hs300: { regime: 'range' } } },
  { date: '2024-01-05', indices: { hs300: { regime: 'bull' } } }
];
const bands = mp.regimeBands(bandItems, 'hs300', 'regime');
eq(bands.length, 3, '三段状态 = 三条背景带');
eq(bands[0].start + '~' + bands[0].end, '2024-01-01~2024-01-02', '第一段起止正确');
eq(bands[2].start + '~' + bands[2].end, '2024-01-05~2024-01-05', '最后一段收在末条录像');
eq(mp.regimeBands([], 'hs300', 'regime').length, 0, '空输入不崩');
assert(mp.REGIME_BAND_COLOR.bull && mp.REGIME_BAND_COLOR.bear, '三种状态都有底色');
const band20 = mp.regimeBands(
  [{ date: 'x', indices: { hs300: { regime: 'bull', regime_20: 'bear' } } }],
  'hs300', 'regime_20');
eq(band20[0].state, 'bear', '20% 法则口径取的是 regime_20 字段');

console.log('\nboxStats / boxByRegime 箱线图:');
eq(mp.boxStats([1, 2, 3, 4, 5]).join(','), '1,2,3,4,5', '五个数正确');
eq(mp.boxStats([]), null, '空样本返回 null');
eq(mp.boxStats([5, 1, 3]).join(','), '1,2,3,4,5', '乱序输入也按大小插值算');
const boxItems = [
  { indices: { shanghai: { pct_10y: 90, regime: 'bull' } } },
  { indices: { shanghai: { pct_10y: 80, regime: 'bull' } } },
  { indices: { shanghai: { pct_10y: 50, regime: 'range' } } },
  { indices: { shanghai: { pct_10y: 10, regime: 'bear' } } },
  { indices: { shanghai: { pct_10y: null, regime: 'bear' } } }
];
const bg = mp.boxByRegime(boxItems, 'shanghai', 'regime');
eq(bg.length, 3, '牛/震荡/熊三组都在');
eq(bg[2].n, 1, '缺值不计数（bear 只剩 1 个）');
eq(bg[0].box[2], 85, '牛组中位数 = (80+90)/2');
eq(mp.boxByRegime([], 'shanghai', 'regime').length, 0, '空输入不崩');

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
