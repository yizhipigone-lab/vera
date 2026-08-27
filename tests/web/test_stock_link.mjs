// ====== 全局股票代码/简称 → 同花顺跳转 测试 ======
// 2026-08-26, 规格契约: docs/plan/2026-08-26_全局股票代码简称跳同花顺_规格契约.md
// 用法: node tests/web/test_stock_link.mjs
// 覆盖: ① stock_link.js 代码归一化/链接渲染/兜底纯文本
//       ② charts.js renderTradeTable 渲染点产出 stock-link 锚 (先红后绿)
//       ③ 无 window.stockLink 时 charts.js 降级纯文本不炸 (Node 无浏览器环境)
import assert from 'node:assert/strict';
import { register, createRequire } from 'node:module';

// 让 node 能把 web/js/*.js (浏览器 ES module) 当 ESM 加载, 必须在 import 被测模块之前
register('./esm_js_loader.mjs', import.meta.url);

// ── 最小 DOM stub (同 test_trade_replay.mjs 口径) ──
function fakeEl(tag) {
  const el = {
    tagName: String(tag || 'div').toUpperCase(), children: [], style: {},
    textContent: '', className: '', dataset: {}, _html: '', value: '',
    options: [],
    appendChild(c) { el.children.push(c); c._parent = el; return c; },
    remove() { const p = el._parent; if (p) p.children = p.children.filter(x => x !== el); },
    addEventListener() {}, removeEventListener() {}, setAttribute() {},
    querySelector() { return fakeEl('button'); },
  };
  Object.defineProperty(el, 'innerHTML', { get: () => el._html || el.textContent, set: v => { el._html = v; } });
  return el;
}

const byId = {};
function idEl(id) { if (!byId[id]) byId[id] = fakeEl('div'); return byId[id]; }
globalThis.document = {
  createElement: t => fakeEl(t),
  getElementById: id => idEl(id),
  body: fakeEl('body'),
  documentElement: fakeEl('html'),
  addEventListener() {}, removeEventListener() {},
};
globalThis.window = { addEventListener() {}, removeEventListener() {} };

let _pass = 0, _fail = 0;
function case_(desc, cond) {
  try { assert.ok(cond); _pass++; }
  catch (e) { _fail++; console.error(`[FAIL] ${desc}\n  ${e.message}`); }
}

// ── ① stock_link.js 单元行为 (经典脚本挂 window.*, 用 require 加载) ──
const require = createRequire(import.meta.url);
require('../../web/js/stock_link.js');
const { stockLink, stockUrl, normStockCode } = globalThis.window;

case_('normStockCode: 裸代码 600519', normStockCode('600519') === '600519');
case_('normStockCode: 带后缀 600519.SH', normStockCode('600519.SH') === '600519');
case_('normStockCode: 带前缀 SH600519', normStockCode('SH600519') === '600519');
case_('normStockCode: 空串 → 空', normStockCode('') === '');
case_('normStockCode: 纯名称 贵州茅台 → 空', normStockCode('贵州茅台') === '');
case_('stockUrl: ETF 510300', stockUrl('510300') === 'https://stockpage.10jqka.com.cn/510300/');

const a1 = stockLink('600519', '贵州茅台');
case_('stockLink: href 指向同花顺标的首页', a1.includes('href="https://stockpage.10jqka.com.cn/600519/"'));
case_('stockLink: 新标签页 target=_blank + noopener', a1.includes('target="_blank"') && a1.includes('rel="noopener noreferrer"'));
case_('stockLink: stopPropagation 防触发 K 线回放行点击', a1.includes('event.stopPropagation()'));
case_('stockLink: 显示简称文本', a1.includes('>贵州茅台</a>'));
case_('stockLink: 文本缺省回退显示代码', stockLink('600519').includes('>600519</a>'));
case_('stockLink: 无有效代码 → 纯文本无锚', stockLink('', '贵州茅台') === '贵州茅台');
case_('stockLink: 文本转义 (XSS)', !stockLink('600519', '<img>').includes('<img>'));

// ── ② charts.js renderTradeTable 渲染点 ──
const { renderTradeTable, resetTradePage } = await import('../../web/js/charts.js');

const TRADES = [
  { stock_code: '600519', stock_name: '贵州茅台', entry_date: '2024-01-05', exit_date: '2024-02-01',
    entry_price: 1600, exit_price: 1700, shares: 100, hold_days: 19, profit_pct: 0.0625, exit_reason: 'ladder_tp' },
  { stock_code: '', stock_name: '缺代码票', entry_date: '2024-01-05', exit_date: '2024-02-01',
    entry_price: 10, exit_price: 11, shares: 100, hold_days: 19, profit_pct: 0.1, exit_reason: '' },
];

resetTradePage();
renderTradeTable(TRADES, TRADES.length);
const html = idEl('tradeTableBody')._html;

case_('渲染点: 成交流水表代码列产出 stock-link 锚', html.includes('class="stock-link"'));
case_('渲染点: 锚指向 600519 同花顺首页', html.includes('https://stockpage.10jqka.com.cn/600519/'));
case_('渲染点: 简称贵州茅台包在锚里', /<a class="stock-link"[^>]*>贵州茅台<\/a>/.test(html));
case_('兜底: 无代码行不套链接', !/<a class="stock-link"[^>]*>缺代码票<\/a>/.test(html));

// ── ③ 降级: 无 window.stockLink 时 renderTradeTable 退化为纯文本不炸 ──
delete globalThis.window.stockLink;
resetTradePage();
let threw = false;
try { renderTradeTable(TRADES, TRADES.length); } catch (e) { threw = true; console.error(e); }
const html2 = idEl('tradeTableBody')._html;
case_('降级: 无 stock_link.js 不 throw', !threw);
case_('降级: 退化为纯文本', html2.includes('贵州茅台') && !html2.includes('class="stock-link"'));

// ── ④ 递归陷阱守卫: 经典脚本 (trade.js / mobile.html) 不得用顶层
//    function stockLink 覆盖 window.stockLink —— 包装函数调 window.stockLink
//    会调到自己 → 无限递归爆栈。经典脚本的包装必须另起名 (thsLink)。
import { readFileSync } from 'node:fs';
const tradeSrc = readFileSync(new URL('../../web/js/trade.js', import.meta.url), 'utf8');
const mobileSrc = readFileSync(new URL('../../web/mobile.html', import.meta.url), 'utf8');
case_('守卫: trade.js 无顶层 function stockLink (递归陷阱)', !/^function stockLink/m.test(tradeSrc));
case_('守卫: mobile.html 无顶层 function stockLink (递归陷阱)', !/^function stockLink/m.test(mobileSrc));
case_('守卫: trade.js 使用 thsLink 包装', tradeSrc.includes('function thsLink'));
case_('守卫: mobile.html 使用 thsLink 包装', mobileSrc.includes('function thsLink'));

console.log(`\n${ _pass } passed, ${ _fail } failed`);
process.exit(_fail ? 1 : 0);
