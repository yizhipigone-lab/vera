// ====== Phase 3 交易回放弹窗降级路径测试 (node + DOM/fetch stub, 零框架依赖) ======
// 用法: node tests/web/test_trade_replay.mjs
// 目的: 锁死 openTradeReplay 的降级约定 —— fetch 失败 / 空 rows / 422 / 502
//   一律弹窗内友好错误, 不 throw 到页面; 连续打开不叠层; close 清理 DOM 和监听。
// 只测降级路径 (无 ECharts 成功渲染), 真实渲染冒烟由人工做。
import assert from 'node:assert/strict';
import { register } from 'node:module';

// 让 node 能把 web/js/*.js (浏览器 ES module) 当 ESM 加载, 必须在 import 被测模块之前
register('./esm_js_loader.mjs', import.meta.url);

// ── 最小 DOM stub (在 import charts_replay.js 之前装好, charts.js 顶层会用 document) ──
function fakeEl(tag) {
  const el = {
    tagName: String(tag || 'div').toUpperCase(), children: [], style: {},
    textContent: '', className: '', dataset: {}, _html: '',
    appendChild(c) { el.children.push(c); c._parent = el; return c; },
    remove() { const p = el._parent; if (p) p.children = p.children.filter(x => x !== el); },
    addEventListener() {}, removeEventListener() {}, setAttribute() {},
  };
  // 真实 DOM 里 innerHTML 反映 textContent; stub 里 _html 为空时回退 textContent,
  // 否则 charts.js 的 esc() (textContent 进 / innerHTML 出) 会吞掉所有文本
  Object.defineProperty(el, 'innerHTML', { get: () => el._html || el.textContent, set: v => { el._html = v; } });
  return el;
}
function collectText(el, acc) {
  acc.push(el.textContent || '', el._html || '');
  (el.children || []).forEach(ch => collectText(ch, acc));
  return acc.join('\n');
}

const body = fakeEl('body');
const docListeners = {};
globalThis.document = {
  createElement: t => fakeEl(t),
  getElementById: () => null,
  body,
  addEventListener: (ev, fn) => { docListeners[ev] = (docListeners[ev] || 0) + 1; },
  removeEventListener: (ev) => { docListeners[ev] = Math.max(0, (docListeners[ev] || 1) - 1); },
};
globalThis.window = { addEventListener() {}, removeEventListener() {} };

let _pass = 0, _fail = 0;
function case_(desc, cond) {
  try { assert.ok(cond); _pass++; }
  catch (e) { _fail++; console.error(`[FAIL] ${desc}\n  ${e.message}`); }
}

const COLORS = { up: '#d6342f', down: '#1f8a5b', accent: '#b8860b', accent2: '#2a9d8f',
  warn: '#c47f17', text: '#222', text2: '#888', bg: '#fff', border: '#ddd' };
const TRADE = { stock_code: '600000', stock_name: '浦发银行', entry_date: '2024-01-05',
  exit_date: '2024-03-01', entry_price: 10.0, exit_price: 11.0, profit_pct: 0.1, pnl: 1000 };
const KLINE_URL_PART = '/api/stock/kline?code=600000&start=2023-10-07&end=2024-03-16';

const { openTradeReplay, closeTradeReplay } = await import('../../web/js/charts_replay.mjs');

// 原始 console.warn 接管: 降级约定要求留痕
let warns = [];
const origWarn = console.warn;
console.warn = (...a) => { warns.push(a.join(' ')); };

// ── 1) fetch 网络异常 → 弹窗内错误, 不 throw ──
globalThis.fetch = () => Promise.reject(new Error('network down'));
warns = [];
let h = await openTradeReplay(TRADE, COLORS);
case_('fetch 异常: 不 throw, 返回关闭句柄', h && typeof h.close === 'function');
case_('fetch 异常: 弹窗已挂载 (body 恰好 1 层, 不叠层)', body.children.length === 1);
case_('fetch 异常: 弹窗内有友好错误', collectText(body, []).includes('K 线加载失败'));
case_('fetch 异常: console.warn 留痕', warns.some(w => w.includes('[trade-replay]')));
h.close();
case_('close 后 DOM 移除', body.children.length === 0);
case_('close 后 ESC 监听移除', (docListeners.keydown || 0) === 0);

// ── 2) 空 rows → 「无 K 线数据（停牌或区间无交易）」 ──
let lastUrl = '';
globalThis.fetch = (u) => { lastUrl = u; return Promise.resolve({ ok: true, status: 200,
  json: () => Promise.resolve({ code: '600000', period: '1d', rows: [] }) }); };
h = await openTradeReplay(TRADE, COLORS);
case_('空 rows: 不 throw', !!h);
case_('空 rows: 提示停牌/无交易', collectText(body, []).includes('无 K 线数据（停牌或区间无交易）'));
case_('取数 URL 符合契约 (klineWindow 区间)', lastUrl === KLINE_URL_PART);
closeTradeReplay();

// ── 3) 422 非法 code → 友好错误 ──
globalThis.fetch = () => Promise.resolve({ ok: false, status: 422, json: () => Promise.resolve({}) });
h = await openTradeReplay(TRADE, COLORS);
case_('422: 不 throw + 弹窗内提示代码无效', !!h && collectText(body, []).includes('422'));
closeTradeReplay();

// ── 4) 502 数据层故障 → 友好错误 ──
globalThis.fetch = () => Promise.resolve({ ok: false, status: 502, json: () => Promise.resolve({}) });
h = await openTradeReplay(TRADE, COLORS);
case_('502: 不 throw + 弹窗内提示数据层故障', !!h && collectText(body, []).includes('502'));
closeTradeReplay();

// ── 5) 连续打开不叠层: 不关直接开第二个 ──
// fetch 永不 resolve: 弹窗在 await 之前就同步挂载, 正好验证"先关再开";
// 不能 await (永不返回), 同步连调两次即可
globalThis.fetch = () => new Promise(() => {});
openTradeReplay(TRADE, COLORS);
openTradeReplay(TRADE, COLORS);
case_('连续点不同行: 仍只有 1 层弹窗', body.children.length === 1);
closeTradeReplay();

// ── 6) 标题含 名称(代码) 日期区间 盈亏% (红绿着色在 style 里) ──
globalThis.fetch = () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ rows: [] }) });
await openTradeReplay(TRADE, COLORS);
const txt = collectText(body, []);
case_('标题: 名称+代码+日期+盈亏%', txt.includes('浦发银行') && txt.includes('600000')
  && txt.includes('2024-01-05 ~ 2024-03-01') && txt.includes('+10.00%'));
closeTradeReplay();

console.warn = origWarn;
console.log(`[OK] test_trade_replay: ${_pass} passed, ${_fail} failed`);
process.exit(_fail ? 1 : 0);
