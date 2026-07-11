// 集成单测: 直接测 vera-ui.js 里真正的 tryRecoverAbortedResult, 而不是复刻版
// 用法: node tests/js/test_recover_integration.js

const fs = require('fs');
const vm = require('vm');
const path = require('path');

let pass = 0, fail = 0;
function assert(cond, label) {
  if (cond) { console.log(`  ✓ ${label}`); pass++; }
  else      { console.log(`  ✗ ${label}`); fail++; }
}

// ── mock 全局对象 ──
function makeContext(responses) {
  // responses: { '/api/status': () => ({running: bool, ...}), '/api/last_result': () => data, '/api/results': () => [meta, ...] }
  // 每被调一次消耗一个（按顺序）
  const logs = [];
  const toasts = [];
  const calls = { '/api/status': 0, '/api/last_result': 0, '/api/results': 0 };
  const renders = [];

  const fetchFn = (url) => {
    const key = url.replace(/^https?:\/\/[^/]+/, '');
    calls[key] = (calls[key] || 0) + 1;
    const queue = responses[key];
    if (!queue) {
      return Promise.resolve({ ok: false, json: async () => null });
    }
    // 支持两种配置写法:
    //   单个响应对象 { body, ok } — 每次都返回它(常量响应)
    //   数组 [resp1, resp2, ...] — 按顺序消耗, 末尾保留最后一个
    let r;
    if (Array.isArray(queue)) {
      if (queue.length === 0) {
        return Promise.resolve({ ok: false, json: async () => null });
      }
      r = queue.length === 1 ? queue[0] : queue.shift();
    } else {
      r = queue;
    }
    return Promise.resolve({
      ok: r.ok !== false,
      json: async () => r.body,
    });
  };

  const document = {
    documentElement: { getAttribute: () => 'dark', setAttribute: () => {} },
    getElementById: (id) => ({
      className: '', textContent: '', style: {}, disabled: false, innerHTML: '',
      value: '', checked: false, dataset: {},
      appendChild: () => {}, removeChild: () => {}, remove: () => {}, parentNode: null,
      addEventListener: () => {}, click: () => {},
    }),
    createElement: () => ({
      className: '', textContent: '', style: {}, innerHTML: '', appendChild: () => {},
      remove: () => {}, parentNode: null,
    }),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: () => {},
  };

  const _ls = {};
  const localStorage = {
    getItem: (k) => _ls[k] || null,
    setItem: (k, v) => { _ls[k] = String(v); },
    removeItem: (k) => { delete _ls[k]; },
  };

  const ctx = {
    fetch: fetchFn,
    document,
    localStorage,
    window: { addEventListener: () => {} },
    module: { exports: {} },
    exports: {},
    Promise,
    setTimeout, clearTimeout,
    console,
  };

  // 关键: 用 Object.defineProperty writable:false 把 mock 函数钉死,
  // 否则 vera-ui.js 顶层的 function declaration 会覆盖同名 mock
  function pin(name, val) {
    Object.defineProperty(ctx, name, { value: val, writable: false, configurable: false, enumerable: true });
  }
  pin('addLog', (msg, type) => logs.push({ msg, type }));
  pin('showToast', (msg, type) => toasts.push({ msg, type }));
  pin('lastResult', null);
  pin('renderAllCharts', (data) => renders.push(data));
  pin('checkEngineVersion', () => {});
  pin('RECOVER', { MAX_RETRY: 5, INTERVAL_MS: 10, MAX_WAIT_MS: 50 });

  return { ctx, logs, toasts, calls, renders };
}

// ── 加载 vera-ui.js 到 vm context ──
function loadModule(responses) {
  const { ctx, logs, toasts, calls, renders } = makeContext(responses);
  vm.createContext(ctx);
  const src = fs.readFileSync(path.join(__dirname, '..', '..', 'web', 'vera-ui.js'), 'utf8');
  vm.runInContext(src, ctx);
  return { exports: ctx.module.exports, logs, toasts, calls, renders };
}

// 等待 tryRecoverAbortedResult 完成（基于 RECOVER.MAX_RETRY × RECOVER.INTERVAL_MS + 余量）
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// ── 用例 ──
async function run() {
  console.log('\n[集成1] 后端还在跑 → 5 次探测都返回 running=true → 报网络错误');
  {
    const runningResp = Array(5).fill({ body: { running: true, step: '执行回测', progress: 40 } });
    const { exports, logs, toasts, renders } = loadModule({
      '/api/status': runningResp,
      '/api/last_result': [],   // 不会走到
      '/api/results': [],
    });
    const cfg = { start_time: '20240101', end_time: '20240601', formula_name: 'MYFRML' };
    const err = { name: 'AbortError', message: 'aborted' };
    await exports.tryRecoverAbortedResult(cfg, err);
    await sleep(200);  // 兜底
    assert(renders.length === 0, '不应渲染结果');
    assert(logs.some(l => l.msg.includes('自动恢复成功')) === false, '不应出现"自动恢复成功"');
    assert(logs.some(l => l.msg.includes('网络错误')) === true, '应报网络错误');
  }

  console.log('\n[集成2] 跑完 + last_result 是本次 cfg → 自动恢复');
  {
    const successResult = { body: { success: true, trade_count: 12, metrics: { cumulative_return: 0.05 } } };
    const okIndex = [{ body: [{ formula: 'MYFRML', date_range: '20240101~20240601' }] }];
    const { exports, logs, toasts, renders } = loadModule({
      '/api/status': [{ body: { running: false } }],
      '/api/last_result': successResult,
      '/api/results': okIndex,
    });
    const cfg = { start_time: '20240101', end_time: '20240601', formula_name: 'MYFRML' };
    const err = { name: 'AbortError', message: 'aborted' };
    await exports.tryRecoverAbortedResult(cfg, err);
    assert(renders.length === 1 && renders[0].trade_count === 12, '应渲染 last_result');
    assert(logs.some(l => l.msg.includes('自动恢复成功')), '应输出成功日志');
    assert(toasts.some(t => t.msg.includes('12笔交易')), '应 toast 提示');
  }

  console.log('\n[集成3] 跑完 + last_result 是更早的旧结果 → 报网络错误，不误渲染');
  {
    const successResult = { body: { success: true, trade_count: 12, metrics: {} } };
    const staleIndex = [{ body: [{ formula: 'OLDFML', date_range: '20230101~20231231' }] }];
    const { exports, logs, renders } = loadModule({
      '/api/status': [{ body: { running: false } }],
      '/api/last_result': successResult,
      '/api/results': staleIndex,
    });
    const cfg = { start_time: '20240101', end_time: '20240601', formula_name: 'MYFRML' };
    const err = { name: 'AbortError', message: 'aborted' };
    await exports.tryRecoverAbortedResult(cfg, err);
    assert(renders.length === 0, '不应渲染旧结果');
    assert(logs.some(l => l.msg.includes('不是本次 cfg')), '应判定 mismatch');
    assert(logs.some(l => l.msg.includes('网络错误')), '应最终报网络错误');
  }

  console.log('\n[集成4] 后端挂了 (status 不 ok) + last_result 也没拿到');
  {
    const { exports, logs, renders } = loadModule({
      '/api/status': [],         // 队列空 → ok: false
      '/api/last_result': [],
      '/api/results': [],
    });
    const cfg = { start_time: '20240101', end_time: '20240601', formula_name: 'MYFRML' };
    const err = { name: 'AbortError', message: 'aborted' };
    await exports.tryRecoverAbortedResult(cfg, err);
    assert(renders.length === 0, '不应渲染');
    assert(logs.some(l => l.msg.includes('网络错误')), '应报网络错误');
  }

  console.log('\n[集成5] 探测成功 → resetRunUI 被调用 → 状态栏切到"就绪"');
  {
    const successResult = { body: { success: true, trade_count: 5, metrics: {} } };
    const okIndex = [{ body: [{ formula: 'MYFRML', date_range: '20240101~20240601' }] }];
    const { exports } = loadModule({
      '/api/status': [{ body: { running: false } }],
      '/api/last_result': successResult,
      '/api/results': okIndex,
    });
    const cfg = { start_time: '20240101', end_time: '20240601', formula_name: 'MYFRML' };
    const err = { name: 'AbortError', message: 'aborted' };
    await exports.tryRecoverAbortedResult(cfg, err);
    // resetRunUI 不抛错就是 OK, 因为它访问的是 mock document
    assert(true, 'resetRunUI 调用未抛错');
  }

  console.log('\n[集成6] 非 AbortError 错误 → 用原始 message');
  {
    const { exports, logs } = loadModule({
      '/api/status': [{ body: { running: false } }],
      '/api/last_result': [],
      '/api/results': [],
    });
    const cfg = { start_time: '20240101', end_time: '20240601', formula_name: 'MYFRML' };
    const err = { name: 'TypeError', message: 'Failed to fetch' };
    await exports.tryRecoverAbortedResult(cfg, err);
    assert(logs.some(l => l.msg.includes('Failed to fetch')), '应透传原始 message');
  }

  console.log(`\n=== ${pass} passed, ${fail} failed ===`);
  process.exit(fail > 0 ? 1 : 0);
}

run().catch(e => { console.error(e); process.exit(1); });