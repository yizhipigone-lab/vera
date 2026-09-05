// ====== brain_viz.js 纯函数契约测试 (node 直跑, 零框架依赖) ======
// 2026-09-02, 研究大脑输出可视化改造 L2: ```chart 图表块 → echarts 交互图
// 用法: node tests/web/test_brain_viz.mjs
// 覆盖: ① chart 围栏块提取 (合法→占位符 / 非法→原样保留降级)
//       ② 白名单 schema 校验 (类型/字段/长度/等长全维度)
//       ③ 反证段拆分 <counter_evidence>
//       ④ 【事实】/【解读】/【待验证】/【缺】徽章替换
//       ⑤ 带符号百分比红涨绿跌 (markPct)
//       ⑥ 占位符 → 图表挂载点注入 (injectChartDivs)
import assert from 'node:assert/strict';

// ── 最小 DOM stub (同 test_stock_link.mjs 口径; 本测试只碰纯函数, 不跑 mountAll) ──
function fakeEl(tag) {
  const el = {
    tagName: String(tag || 'div').toUpperCase(), children: [], style: {},
    textContent: '', className: '', dataset: {}, _html: '', value: '', options: [],
    appendChild(c) { el.children.push(c); c._parent = el; return c; },
    remove() { const p = el._parent; if (p) p.children = p.children.filter(x => x !== el); },
    addEventListener() {}, removeEventListener() {}, setAttribute() {},
    querySelector() { return fakeEl('button'); },
  };
  Object.defineProperty(el, 'innerHTML', { get: () => el._html || el.textContent, set: v => { el._html = v; } });
  return el;
}
const byId = {};
globalThis.document = {
  createElement: t => fakeEl(t),
  getElementById: id => (byId[id] = byId[id] || fakeEl('div')),
  body: fakeEl('body'), documentElement: fakeEl('html'),
  addEventListener() {}, removeEventListener() {},
};
globalThis.window = { addEventListener() {}, removeEventListener() {} };

let _pass = 0, _fail = 0;
function case_(desc, cond) {
  try { assert.ok(cond); _pass++; }
  catch (e) { _fail++; console.error(`[FAIL] ${desc}\n  ${e.message}`); }
}

// ── 加载被测脚本 (经典脚本挂 window.BrainViz; 不存在 → 整体红) ──
let BrainViz = null;
try {
  const { createRequire } = await import('node:module');
  createRequire(import.meta.url)('../../web/js/brain_viz.js');
  BrainViz = globalThis.window.BrainViz;
} catch (e) {
  console.error('[FAIL] brain_viz.js 加载失败:', e.message);
}
case_('brain_viz.js 可加载且挂 window.BrainViz', !!BrainViz);
if (!BrainViz) { console.log(`\n${_pass} passed, ${_fail} failed`); process.exit(1); }

// ── ② 白名单 schema 校验 ──
const V = BrainViz.validateSpec;
case_('accept: 最小合法 spec (x 可省)', V({ series: [{ data: [1, 2, 3] }] }).ok === true);
case_('accept: bar + x + horizontal', V({ type: 'bar', x: ['a', 'b'], series: [{ name: 's', data: [1, 2] }], horizontal: true }).ok === true);
case_('accept: line 多序列', V({ type: 'line', x: ['a', 'b', 'c'], series: [{ name: 'x', data: [1, 2, 3] }, { name: 'y', data: [3, 2, 1] }] }).ok === true);
case_('accept: pie 单序列', V({ type: 'pie', x: ['a', 'b', 'c'], series: [{ data: [4, 5, 6] }] }).ok === true);
case_('accept: title/source/unit 长度边界内', V({ title: '指'.repeat(40), source: 's'.repeat(60), unit: '%', series: [{ data: [1] }] }).ok === true);

case_('reject: 非 JSON 对象 (数组)', V([{ series: [{ data: [1] }] }]).ok === false);
case_('reject: type 不在白名单', V({ type: 'radar', series: [{ data: [1] }] }).ok === false);
case_('reject: 未知顶层字段', V({ series: [{ data: [1] }], foo: 1 }).ok === false);
case_('reject: series 缺失', V({ type: 'bar' }).ok === false);
case_('reject: series 空数组', V({ series: [] }).ok === false);
case_('reject: series 超 3 条', V({ series: Array.from({ length: 4 }, () => ({ data: [1] })) }).ok === false);
case_('reject: data 非数字', V({ series: [{ data: ['a', 'b'] }] }).ok === false);
case_('reject: 多序列 data 不等长', V({ series: [{ data: [1, 2] }, { data: [1] }] }).ok === false);
case_('reject: x 长度与 data 不匹配', V({ x: ['a', 'b', 'c'], series: [{ data: [1, 2] }] }).ok === false);
case_('reject: data 超 60 点', V({ series: [{ data: Array.from({ Length: 0, length: 61 }, (_, i) => i) }] }).ok === false);
case_('reject: pie 多序列', V({ type: 'pie', series: [{ data: [1] }, { data: [2] }] }).ok === false);
case_('reject: title 超长', V({ title: 'x'.repeat(41), series: [{ data: [1] }] }).ok === false);
case_('reject: series 元素带未知字段', V({ series: [{ data: [1], stack: 'y' }] }).ok === false);
case_('reject: data 为 null', V({ series: [{ data: [1, null] }] }).ok === false);

// ── ① chart 围栏块提取 ──
const GOOD_MD = '前文\n\n```chart\n{"type":"bar","title":"指数涨跌幅(%)","x":["上证","深成"],"series":[{"name":"涨跌","data":[0.5,-0.3]}],"source":"market_snapshot 2026-09-02"}\n```\n\n后文';
BrainViz.beginRender();
const out1 = BrainViz.extractCharts(GOOD_MD);
case_('提取: 合法块替换为占位符 token', /BMVIZCHART0BMVIZCHART/.test(out1));
case_('提取: 原围栏从文中消失', !out1.includes('```chart'));
case_('提取: 前后文保留', out1.includes('前文') && out1.includes('后文'));
case_('提取: spec 已收集', BrainViz.specs().length === 1 && BrainViz.specs()[0].type === 'bar');

BrainViz.beginRender();
const out2 = BrainViz.extractCharts('```chart\n{oops 非法}\n```');
case_('降级: 非法 JSON 原样保留为代码块', out2.includes('```chart') && out2.includes('{oops'));
case_('降级: 非法块不收集 spec', BrainViz.specs().length === 0);

BrainViz.beginRender();
const out3 = BrainViz.extractCharts('```chart\n{"type":"radar","series":[{"data":[1]}]}\n```');
case_('降级: 白名单不过也原样保留', out3.includes('```chart'));

BrainViz.beginRender();
const out4 = BrainViz.extractCharts('```python\nprint(1)\n```');
case_('不误伤: 其他语言围栏原样', out4.includes('```python') && !/BMVIZCHART/.test(out4));

// ── ③ 反证段拆分 ──
const sc = BrainViz.splitCounter('结论 A。\n\n<counter_evidence>\n- 风险1\n- 风险2\n</counter_evidence>\n');
case_('反证: 拆出 counter 内容', sc.counter && sc.counter.includes('风险1'));
case_('反证: main 不再含标签', !sc.main.includes('counter_evidence'));
const sc2 = BrainViz.splitCounter('没有反证段的回答');
case_('反证: 无标签时 counter 为空', sc2.counter == null && sc2.main === '没有反证段的回答');

// ── ④ 徽章替换 ──
const bg = BrainViz.badgeify('【事实】价格 10 元【解读】偏贵【待验证】题材【缺】舆情【其他】不动');
case_('徽章: 事实', bg.includes('bm-fact'));
case_('徽章: 解读', bg.includes('bm-view'));
case_('徽章: 待验证', bg.includes('bm-todo'));
case_('徽章: 缺', bg.includes('bm-miss'));
case_('徽章: 其他【】不动', bg.includes('【其他】不动'));

// ── ⑤ 带符号百分比上色 (纯函数) ──
const pct = BrainViz.markPct('上证 +2.35%, 深成 -1.2%, 创业板 −3% (U+2212), 平盘 0.52%');
case_('上色: +2.35% → bm-up', /<span class="bm-up">\+2\.35%<\/span>/.test(pct));
case_('上色: -1.2% → bm-down', /<span class="bm-down">-1\.2%<\/span>/.test(pct));
case_('上色: −3% (U+2212) → bm-down', /<span class="bm-down">−3%<\/span>/.test(pct));
case_('上色: 无符号百分比不动', !/bm-(up|down)「?[^>]*>0\.52%/.test(pct) && pct.includes('0.52%'));
case_('上色: 文本被转义防注入', BrainViz.markPct('<img> +1%').includes('&lt;img&gt;'));

// ── ⑥ 占位符 → 挂载点注入 ──
const inj = BrainViz.injectChartDivs('<p>BMVIZCHART0BMVIZCHART</p><p>x BMVIZCHART1BMVIZCHART y</p>');
case_('注入: p 包裹占位符变挂载点', /<div class="bm-chart" data-bviz="0"><\/div>/.test(inj));
case_('注入: 裸 token 也替换', /<div class="bm-chart" data-bviz="1"><\/div>/.test(inj));
case_('注入: 无占位符时原样', BrainViz.injectChartDivs('<p>普通</p>') === '<p>普通</p>');

// ── ⑦ renderBody 集成: 真实 marked (web/marked.min.js) + 假 DOMPurify 走完整字符串管线 ──
// (源自 2026-09-02 浏览器侧饼图疑云的排查: 用真 marked 锁死管线行为,
//  防止未来 marked 升级悄悄吞 token/徽章/反证段。)
{
  const { createRequire } = await import('node:module');
  const req = createRequire(import.meta.url);
  globalThis.window.marked = req('../../web/marked.min.js');
  globalThis.window.DOMPurify = { sanitize: (h) => h };   // 假消毒: 只验证管线自身
  const mdFull = [
    '# 标题', '',
    '**结论**:【事实】机构占比 +5.19pp, 散户 -4,986 户。', '',
    '```chart',
    '{"type":"pie","title":"行业分布","x":["软件","半导体"],"series":[{"data":[3,5]}],"source":"zt_pool 2026-09-02"}',
    '```', '',
    '<counter_evidence>',
    '- 风险A',
    '</counter_evidence>'
  ].join('\n');
  BrainViz.beginRender();
  const htmlFull = BrainViz.renderBody(mdFull);
  case_('集成: marked 渲染出 h1', htmlFull.includes('<h1>标题</h1>'));
  case_('集成: chart 块变挂载点 div', /<div class="bm-chart" data-bviz="0"><\/div>/.test(htmlFull));
  case_('集成: 徽章在 marked 输出后仍生效', htmlFull.includes('bm-fact'));
  case_('集成: 反证卡生成', htmlFull.includes('bm-counter') && htmlFull.includes('风险A'));
  case_('集成: counter 标签不残留正文', !htmlFull.includes('counter_evidence'));
  case_('集成: spec 已收集 (pie)', BrainViz.specs().length === 1 && BrainViz.specs()[0].type === 'pie');
}

console.log(`\n${_pass} passed, ${_fail} failed`);
process.exit(_fail ? 1 : 0);
