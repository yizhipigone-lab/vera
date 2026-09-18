// ====== 决策台账前端纯函数契约测试 (node 直接跑, 零框架依赖) ======
// 用法: node tests/web/test_decision_util.mjs
//
// 目的: 把"后端给的结构化行 → 页面上那几句话"的规则钉住。这些文案是用户唯一
// 看得到的东西 —— 后端算对了但文案说反了, 用户照样看到错的东西, 而且不会报错。
//
// 三条重点:
//   1. 卡片一顶部状态条的四态 (休市 / 跑过 / 半跑 / 没跑) 不能混;
//   2. 颜色铁律: 买=红(up) / 卖=绿(down) / 没动=灰(muted) / 该做没做成=黄(warn);
//   3. 日历格文案: 休市 ≠ 没运行 ≠ 有记录 —— 三者在页面上长得必须不一样。
import assert from 'node:assert/strict';
import {
  TONE_VAR, SOURCE_LABELS, actionBadge, statusLine, evidenceRows,
  calCellLabel, dominantTone, parseIso, fmtEvents,
} from '../../web/js/decision_util.mjs';

let _pass = 0, _fail = 0;
function case_(desc, got, want) {
  try {
    assert.deepEqual(got, want);
    _pass++;
  } catch (e) {
    _fail++;
    console.error(`[FAIL] ${desc}\n  got:  ${JSON.stringify(got)}\n  want: ${JSON.stringify(want)}`);
  }
}

// ── actionBadge: 文字 + 颜色双编码 ──────────────────────────────
// 只靠颜色区分买卖对色盲用户等于没有区分, 所以每个动作都要有文字。
case_('BUY 徽章', actionBadge('BUY'), { text: '买入', tone: 'up' });
case_('SELL 徽章', actionBadge('SELL'), { text: '卖出', tone: 'down' });
case_('HOLD 徽章', actionBadge('HOLD'), { text: '没动', tone: 'muted' });
case_('FAIL 徽章', actionBadge('FAIL'), { text: '没做成', tone: 'warn' });
case_('INFO 徽章', actionBadge('INFO'), { text: '告知', tone: 'info' });
case_('小写也认', actionBadge('buy'), { text: '买入', tone: 'up' });
case_('未知动作原样显示 + 灰', actionBadge('WEIRD'), { text: 'WEIRD', tone: 'muted' });
case_('空动作给破折号', actionBadge('').text, '—');
case_('null 不炸', actionBadge(null).text, '—');
case_('undefined 不炸', actionBadge(undefined).tone, 'muted');

// 买红卖绿 (中国股市习惯), 不能反。
case_('买是红 (--up)', TONE_VAR[actionBadge('BUY').tone], 'var(--up)');
case_('卖是绿 (--down)', TONE_VAR[actionBadge('SELL').tone], 'var(--down)');
case_('买不是绿', TONE_VAR[actionBadge('BUY').tone] !== 'var(--down)', true);

// ── SOURCE_LABELS: 当场记录不该被打扰 ────────────────────────────
case_('live 不给角标', SOURCE_LABELS.live, '');
case_('回填复原有角标', SOURCE_LABELS.backfill_exact, '历史复原');
case_('只有原文有角标', SOURCE_LABELS.backfill_text, '历史原文');
case_('推断有角标', SOURCE_LABELS.inferred, '推断');

// ── statusLine: 四态 ────────────────────────────────────────────
// ① 休市: 灰字直说"本来就不该有动作", 免得被误当成程序挂了
case_('休市 → 灰字说休市',
  statusLine({ trading_day: false }).tone, 'muted');
case_('休市文案点明"不是交易日"',
  statusLine({ trading_day: false }).text.includes('休市'), true);
case_('休市不提"没运行"',
  statusLine({ trading_day: false }).text.includes('没有运行痕迹'), false);

// ② 跑过了 (有 eod 归档) → 绿
const ran = statusLine({
  trading_day: true,
  run_trace: { rotation: '14:54 跑过', auto_buy: '14:54 跑过', eod: '15:05 已归档' },
});
case_('跑过了 → 绿', ran.tone, 'ok');
case_('跑过了文案说"系统跑过了"', ran.text.includes('今天系统跑过了'), true);
case_('跑过了列出三条腿', ran.text.includes('ETF 轮动') && ran.text.includes('尾盘选股'), true);

// ③ 半跑 (有零散痕迹但没归档) → 黄, 且要说"程序可能提前退了"
const half = statusLine({
  trading_day: true,
  run_trace: { rotation: '14:54 跑过' },
});
case_('半跑 → 黄', half.tone, 'warn');
case_('半跑文案提示提前退出', half.text.includes('程序可能提前退了'), true);
case_('半跑文案说"没有收盘归档"', half.text.includes('没有收盘归档'), true);

// ④ 没跑 (什么痕迹都没有) → 黄, 且不武断说"程序挂了"(也可能休市)
const none = statusLine({ trading_day: true, run_trace: {} });
case_('没痕迹 → 黄', none.tone, 'warn');
case_('没痕迹文案给两种可能', none.text.includes('可能') , true);
case_('没痕迹不武断说死', none.text.includes('程序没开机'), true);

// 人工成交提示
const withManual = statusLine({ trading_day: true, run_trace: {}, manual_trades_today: 2 });
case_('人工成交要提示笔数', withManual.text.includes('2 笔人工成交'), true);
const withoutManual = statusLine({ trading_day: true, run_trace: {} });
case_('没有人工成交就不提', withoutManual.text.includes('人工成交'), false);
case_('0 笔也不提', statusLine({ trading_day: true, run_trace: {}, manual_trades_today: 0 }).text.includes('人工成交'), false);
case_('休市也能带人工成交提示',
  statusLine({ trading_day: false, manual_trades_today: 1 }).text.includes('1 笔人工成交'), true);
case_('空输入给 null (调用方好兜底)', statusLine(null), null);

// ── evidenceRows: 明细中文标签 ──────────────────────────────────
const ev = evidenceRows({ code: '600519.SH', last: 10.5, avg_cost: 10.0,
                          gap_to_cost_stop_pct: 0.162, hold_days: 7 });
case_('明细有 5 行', ev.length, 5);
case_('代码列标签', ev[0].label, '代码');
case_('最新价列标签', ev[1].label, '最新价');
case_('距止损是百分数', /%$/.test(ev[3].value), true);
case_('天数不带 %', ev[4].value, '7');
case_('空明细给空数组', evidenceRows(null), []);
case_('空字典给空数组', evidenceRows({}), []);
case_('非字典给空数组', evidenceRows('x'), []);
case_('空值行跳过', evidenceRows({ code: null, last: undefined, avg_cost: '' }).length, 0);
case_('空数组跳过', evidenceRows({ events: [] }).length, 0);
case_('空对象跳过', evidenceRows({ filters: {} }).length, 0);

// 布尔 → 是/否
case_('true → 是', evidenceRows({ stop_off: true })[0].value, '是');
case_('false → 否', evidenceRows({ stop_off: false })[0].value, '否');

// 表外字段不吞信息 (原样显示键名, 便于排查新字段)
case_('表外字段用键名', evidenceRows({ brand_new: 1 })[0].label, 'brand_new');

// 动量字典 → "代码 xx.x% / 代码 yy.y%"
const mom = evidenceRows({ momentum: { '159949.SZ': -0.059, '513100.SH': 0.033 } })[0].value;
case_('动量转成带百分号的串', mom.includes('159949.SZ') && mom.includes('%'), true);
case_('动量保留符号', mom.includes('-'), true);

// 过滤统计 → "原因 N 只"
const filt = evidenceRows({ filters: { 涨停拒买: 2, 已持仓: 1 } })[0].value;
case_('过滤统计带"只"', filt.includes('只'), true);
case_('过滤统计含原因名', filt.includes('涨停拒买'), true);

// 事件列表 (当天还发生过) 不炸
const evs = evidenceRows({ events: [{ action: 'FAIL', reason_code: 'EXIT_ARM_FAIL' }] });
case_('事件列表能渲染', evs.length, 1);

// ── calCellLabel: 休市 / 没运行 / 有记录 三者必须能区分 ──────────
case_('休市格说"休市"', calCellLabel({ trading_day: false, rows: 0 }), '休市');
case_('休市优先于没运行', calCellLabel({ trading_day: false, rows: 1, headline: 'x' }), '休市');
case_('交易日没记录的格是空串', calCellLabel({ trading_day: true, rows: 0 }), '');
case_('有摘要用摘要', calCellLabel({ trading_day: true, rows: 3, headline: '止盈止损：触发了卖出' }),
  '止盈止损：触发了卖出');
case_('没摘要退成行数', calCellLabel({ trading_day: true, rows: 3, headline: '' }), '3 条');
case_('没这一天不炸', calCellLabel(null), '');
case_('没这一天不炸 (undefined)', calCellLabel(undefined), '');

// ── dominantTone: 一格的主色 ────────────────────────────────────
case_('最多数的色胜出', dominantTone({ muted: 3, up: 1 }), 'muted');
case_('次数相同时买更值得注意', dominantTone({ muted: 1, up: 1 }), 'up');
case_('卖也压过灰', dominantTone({ muted: 2, down: 2 }), 'down');
case_('买压过卖', dominantTone({ up: 1, down: 1 }), 'up');
case_('警示压过告知', dominantTone({ info: 1, warn: 1 }), 'warn');
case_('空对象给灰', dominantTone({}), 'muted');
case_('null 给灰', dominantTone(null), 'muted');
// 后端将来加了新色调而前端还没学会: 忽略它, 退回一个认识的色 ——
// 若让它胜出, TONE_VAR 查不到 → 底色变成无效 CSS → 这一格静默失去颜色
case_('未知色调忽略 (不静默丢底色)', dominantTone({ weird: 9, up: 1 }), 'up');
case_('全是未知色调时退回灰', dominantTone({ weird: 3, other: 2 }), 'muted');

// ── parseIso ────────────────────────────────────────────────────
case_('解出年月日', parseIso('2026-09-16'), { year: 2026, month: 9, day: 16 });
case_('01 月也认', parseIso('2026-01-05'), { year: 2026, month: 1, day: 5 });
// 严格只认 YYYY-MM-DD: 畸形输入给 null, 让调用方去说"数据不对"
case_('空串给 null', parseIso(''), null);
case_('undefined 给 null', parseIso(undefined), null);
case_('月份缺失给 null', parseIso('undefined-01'), null);
case_('只有年月给 null', parseIso('2026-09'), null);
case_('单数字月份给 null', parseIso('2026-9-16'), null);
case_('乱串给 null', parseIso('今天'), null);
case_('NaN 串给 null', parseIso('NaN-NaN-01'), null);

// ── fmtEvents: "当天还发生过"要写成流水, 不能吐原始 JSON ─────────
// 这一栏是台账里信息量最大的地方 (主结论之外的那些插曲, 例如"一天试了 14 次
// 才卖掉")。之前它走 JSON.stringify 兜底, 页面上是一大坨带引号大括号的原文,
// 用户根本读不下去 —— 2026-09-18 展开 7 月 30 日那一行时发现。
{
  const evs = [
    { ts: 1789745679, action: 'FAIL', reason_code: 'EXIT_ARM_FAIL',
      reason_text: '可用为 0, 无可卖 (现价 2.61)' },
    { ts: 1789745689, action: 'SELL', reason_code: 'EXIT_TRIGGERED',
      reason_text: '卖出 600 股 @2.61' },
  ];
  const out = fmtEvents(evs);
  case_('流水: 一行一条', out.split('\n').length, 2);
  case_('流水: 带时间 (HH:MM:SS)', /^\d{2}:\d{2}:\d{2}/.test(out.split('\n')[0]), true);
  case_('流水: 动作翻译成人话', out.includes('没做成'), true);
  case_('流水: 原文照抄', out.includes('可用为 0, 无可卖 (现价 2.61)'), true);
  case_('流水: 不是原始 JSON', out.includes('{'), false);
  case_('流水: 不是原始 JSON (无引号)', out.includes('"'), false);
}
// 按时间升序排 —— 顺序错了整条时间线就读反了。
case_('流水: 按时间升序', fmtEvents([
  { ts: 300, action: 'FAIL', reason_text: '晚' },
  { ts: 100, action: 'FAIL', reason_text: '早' },
]).split('\n').map(s => s.endsWith('早')), [true, false]);
// 缺时间戳的排最前 (当成最早发生), 且只显示内容不显示 "NaN"。
case_('流水: 缺时间排在前面', fmtEvents([
  { ts: 100, action: 'FAIL', reason_text: '有时间' },
  { action: 'FAIL', reason_text: '没时间' },
]).split('\n')[0].includes('没时间'), true);
case_('流水: 缺时间不显示 NaN', fmtEvents([
  { action: 'FAIL', reason_text: 'x' }]).includes('NaN'), false);
// 超量截断: 一屏几百行没法看, 但要说清"还有几条"。
case_('流水: 超 12 条截断', fmtEvents(
  Array.from({ length: 20 }, (_, i) => ({ ts: i + 100, action: 'FAIL', reason_text: '第 ' + i }))
).split('\n').length, 13);
case_('流水: 截断后说明还剩几条', fmtEvents(
  Array.from({ length: 20 }, (_, i) => ({ ts: i + 100, action: 'FAIL', reason_text: '第 ' + i }))
).includes('还有 8 条没有展开'), true);
// 脏数据不许把它弄崩 (这是渲染链路上的兜底)。
case_('流水: 空数组给空串', fmtEvents([]), '');
case_('流水: null 给空串', fmtEvents(null), '');
case_('流水: 非数组给空串', fmtEvents('x'), '');
case_('流水: 混入脏条目不炸', fmtEvents([null, 1, { action: 'FAIL' }]).includes('没做成'), true);
case_('流水: 只有原因码也能显示', fmtEvents([{ reason_code: 'EXIT_ARM_FAIL' }]), 'EXIT_ARM_FAIL');
// 没 action 就不硬编一个动作出来 (前端没有"原因码→动作"的映射表, 那张表在
// 后端 trade/decision_codes.py 里, 是唯一真相源 —— 前端猜一份迟早对不上)。
case_('流水: 缺 action 不瞎猜', fmtEvents([{ reason_code: 'X' }]).includes('：'), false);
// 接进 evidenceRows: events 那一栏必须走新渲染, 不再吐 JSON。
{
  const rows = evidenceRows({ price: 2.61, events: [
    { ts: 1789745679, action: 'FAIL', reason_text: '卖不掉' }] });
  const evRow = rows.find(r => r.label === '当天还发生过');
  case_('evidenceRows: events 有一行', !!evRow, true);
  case_('evidenceRows: 内容是流水不是 JSON', evRow.value.includes('卖不掉'), true);
  case_('evidenceRows: 没有大括号', evRow.value.includes('{'), false);
}

// ── 端到端: 一天的样子能拼出来 (不碰 DOM, 只验数据流) ───────────
const day = {
  date: '2026-09-18', trading_day: true,
  run_trace: { rotation: '14:54 跑过', auto_buy: '14:54 跑过', eod: '15:05 已归档' },
  manual_trades_today: 1,
  summary: { buy: 0, sell: 0, hold: 3, fail: 1, info: 0, inferred: 0 },
  groups: [
    { strategy: 'rotation', label: 'ETF 轮动', rows: [
      { action: 'HOLD', reason_code: 'ROT_HOLD_KEEP', reason_label: '维持持仓（不用换腿）',
        reason_text: '目标 513100.SH (当前生效目标)', source: 'live', evidence: {} },
    ] },
    { strategy: 'auto_buy', label: '尾盘选股买入', rows: [
      { action: 'FAIL', reason_code: 'PICK_REGIME_BLOCK', reason_label: '大盘没站上年线，今天不买新股',
        reason_text: '399006.SZ 未站上 MA200, 尾盘不买', source: 'live', evidence: {} },
    ] },
  ],
};
const line = statusLine(day);
case_('端到端: 状态条说跑过了', line.text.includes('今天系统跑过了'), true);
case_('端到端: 带上人工成交', line.text.includes('1 笔人工成交'), true);
const badges = day.groups.flatMap(g => g.rows.map(r => actionBadge(r.action).text));
case_('端到端: 徽章文字齐', badges, ['没动', '没做成']);
const tones = day.groups.flatMap(g => g.rows.map(r => actionBadge(r.action).tone));
case_('端到端: 一格主色是警示', dominantTone(Object.fromEntries(
  tones.map(t => [t, 1]))), 'warn');

console.log(`\n${_fail === 0 ? '[OK]' : '[FAIL]'} decision_util 契约测试: ${_pass} passed, ${_fail} failed`);
process.exit(_fail === 0 ? 0 : 1);
