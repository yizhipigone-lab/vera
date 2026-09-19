// ====== decision.js 接线契约测试: 请求必须打到 8081 (node 直接跑) ======
// 用法: node tests/web/test_decision_wiring.mjs
//
// 为什么单独一个文件 (2026-09-19 上线当天踩的坑):
// 哥在页面上看到「查询失败: 交易服务 (8081) 不可达」, 而 8081 其实**好好的** ——
// curl 直打 http://127.0.0.1:8081/api/trade/decisions 返回 200。
// 真因: decision.js 原本想复用 trade.js 的 `get()`, 写成
//     if (typeof window.get === 'function') return window.get(path);
//     return fetch(path)…
// 但 trade.js 整个被包在 `(function () { … })()` 里, 它那个 `get` 是**函数内部**的
// 局部函数, 从来没挂到 window 上 (trade.js 只暴露了 recordsPageEnter 等 4 个钩子)。
// 于是每一次都走到 fallback 的 `fetch(path)` —— **同源**相对路径, 打到页面的 8080 上,
// 而 8080 根本没有 /api/trade/* 路由 → 404 → 被 catch 吞掉, 统一报成"8081 不可达"。
// **报出来的话和真正的错因完全不是一回事**, 于是白查了一轮。
//
// 为什么上一轮没抓到: test_decision_util.mjs 只测「文案怎么生成」,
// test_decision_api.py 只测「后端接口对不对」—— 中间这段
// 「**请求到底发去了哪个地址**」谁都没管。这个文件补的就是它。
//
// 做法: 把 decision.js 真加载起来 (假 DOM + 假 fetch + 假 location),
// 走一遍页面真实的入口 (window.recordsPageEnter), 然后断言它请求的 URL。
// 不联网、不起服务、不改仓库里的任何文件 (decision.js 复制到系统临时目录再加载,
// 因为 node 会把 .js 当 CommonJS, 而它是 ES module)。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '../..');
// 默认测仓库里那份; 允许用环境变量指向别的副本 —— 这样就能拿"修复前"的旧版本
// 跑同一组断言, 证明这些断言真的能抓到那个 bug (测试不对着旧代码红过, 就是摆设)。
const DECISION_SRC = process.env.VERA_DECISION_SRC
  || path.join(ROOT, 'web', 'js', 'decision.js');
const UTIL_URL = pathToFileURL(path.join(ROOT, 'web', 'js', 'decision_util.mjs')).href;

let _pass = 0;
let _fail = 0;
const _tmpDirs = [];

function case_(desc, got, want) {
  try {
    assert.deepEqual(got, want);
    _pass++;
  } catch (e) {
    _fail++;
    console.error(`[FAIL] ${desc}\n  got:  ${JSON.stringify(got)}\n  want: ${JSON.stringify(want)}`);
  }
}

/** 一个够用的假元素: decision.js 只用到这几个属性/方法。 */
function fakeEl(id) {
  return {
    id,
    value: '', textContent: '', innerHTML: '', hidden: false, disabled: false,
    style: {}, dataset: {}, _h: {},
    addEventListener(ev, fn) { this._h[ev] = fn; },
    querySelectorAll() { return []; },
    scrollIntoView() {},
  };
}

/**
 * 把 decision.js 加载一遍, 返回它实际发出的请求列表。
 * @param {string} hostname - 冒充页面的 location.hostname
 * @param {object} [opts] - {status} 让服务端返回非 2xx; {failWith} 让 fetch 被拒
 */
async function loadDecision(hostname, opts) {
  const o = opts || {};
  const calls = [];
  const dom = new Map();
  ['decDate', 'decHint', 'decBtn', 'decBody', 'decStatus',
    'decPrevBtn', 'decNextBtn', 'decCal', 'decCalMonth', 'decCalHint',
    'decTodayCard'].forEach((id) => dom.set(id, fakeEl(id)));

  globalThis.document = {
    readyState: 'complete',        // 不是 'loading' → decision.js 加载完立刻 hookPage()
    addEventListener() {},
    getElementById: (id) => dom.get(id) || null,
  };
  globalThis.location = { hostname };
  // 冒充 trade.js 已经跑完并放好了钩子 —— 这就是页面上的真实顺序
  const win = {
    recordsPageEnter() { win._prevEntered = true; },
    recordsPageLeave() {},
  };
  globalThis.window = win;
  globalThis.fetch = (url) => {
    calls.push(url);
    if (o.failWith) return Promise.reject(o.failWith);
    if (o.status) {
      return Promise.resolve({ ok: false, status: o.status, json: () => Promise.resolve({}) });
    }
    const body = String(url).includes('calendar')
      ? { month: '2026-09', days: [] }
      : { date: '2026-09-18', trading_day: true, groups: [], summary: {} };
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
  };

  // 复制到临时目录并改扩展名为 .mjs; 相对 import 换成指向真文件的绝对 file:// URL。
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vera-decision-'));
  _tmpDirs.push(dir);
  const tmp = path.join(dir, 'decision_under_test_'
    + Math.random().toString(36).slice(2) + '.mjs');
  const src = fs.readFileSync(DECISION_SRC, 'utf8')
    .replace("'./decision_util.mjs'", JSON.stringify(UTIL_URL));
  fs.writeFileSync(tmp, src, 'utf8');
  await import(pathToFileURL(tmp).href);
  return { calls, dom, win };
}

/** 等 Promise 链跑完 (假 fetch 是同步 resolve 的, 几十毫秒足够)。 */
const settle = () => new Promise((r) => setTimeout(r, 20));

// ── 场景 1: 正常加载 (页面在 127.0.0.1) ──────────────────────────
{
  const t = await loadDecision('127.0.0.1', {});
  case_('加载后包装了 recordsPageEnter', typeof t.win.recordsPageEnter, 'function');
  t.win.recordsPageEnter();          // 进"交易记录"页 —— 和真实页面同一条路径
  await settle();

  case_('两个请求都发出去了 (当日 + 日历)', t.calls.length, 2);
  case_('请求 1 打到 8081 的当日端点', t.calls[0],
    'http://127.0.0.1:8081/api/trade/decisions');
  case_('请求 2 打到 8081 的日历端点', t.calls[1],
    'http://127.0.0.1:8081/api/trade/decisions/calendar');
  case_('没有一个请求是裸相对路径 (裸路径=打到 8080=就是那个 bug)',
    t.calls.filter((u) => u.startsWith('/')).length, 0);
  case_('每个请求都带着 8081', t.calls.every((u) => u.includes(':8081')), true);
  case_('没有报错提示 (hint 是空的)', t.dom.get('decHint').textContent, '');
  case_('日历也没报错', t.dom.get('decCalHint').textContent, '');
  case_('原来的钩子仍然被调用 (没把别的模块踢掉)', t.win._prevEntered, true);
}

// ── 场景 2: 手机走局域网 IP 打开页面 ────────────────────────────
{
  const t = await loadDecision('192.168.1.9', {});
  t.win.recordsPageEnter();
  await settle();
  case_('局域网打开时, 请求跟到局域网 IP 而不是回环',
    t.calls[0], 'http://192.168.1.9:8081/api/trade/decisions');
}

// ── 场景 3: 服务端报错 (404) —— 必须说"报错", 不能说"连不上" ────
{
  const t = await loadDecision('127.0.0.1', { status: 404 });
  t.win.recordsPageEnter();
  await settle();
  const hint = t.dom.get('decHint').textContent;
  case_('404 时说"交易服务报错"', hint.includes('交易服务报错'), true);
  case_('404 时不说"连不上" (那会把哥引去开进程, 白折腾)',
    hint.includes('连不上'), false);
  case_('404 时把状态码报出来', hint.includes('404'), true);
}

// ── 场景 4: 真的连不上 (浏览器把 fetch 拒了) ────────────────────
{
  const t = await loadDecision('127.0.0.1', { failWith: new TypeError('Failed to fetch') });
  t.win.recordsPageEnter();
  await settle();
  const hint = t.dom.get('decHint').textContent;
  case_('连不上时说"连不上"', hint.includes('连不上'), true);
  case_('连不上时告诉他去开哪个进程', hint.includes('start_vera.bat'), true);
  case_('连不上时不再提"交易服务报错"', hint.includes('交易服务报错'), false);
}

// 收尾: 清掉临时目录, 不给系统留垃圾
_tmpDirs.forEach((d) => { try { fs.rmSync(d, { recursive: true, force: true }); } catch (e) { /* 忽略 */ } });

console.log(`\n${_fail === 0 ? '[OK]' : '[FAIL]'} decision.js 接线契约测试: ${_pass} passed, ${_fail} failed`);
process.exit(_fail === 0 ? 0 : 1);
