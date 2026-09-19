// tests/web/test_error_contract.mjs — 错误契约两形状都认 (2026-09-20 审计 P2-10/P1-1)
//
// 背景: 服务端有**两种许可形状** —— 传输/意外错误是 {detail}; 业务软失败是
// 400/409 + {success:false, error}。前端原先只认 detail, 于是"管线正在运行中"
// 这类人话被丢掉, 用户只看到 "HTTP 409"。
// 另一处更危险: mobile 的 POST 原本连 r.ok 都不查 —— 服务端 500 从纯文本
// (JSON 解析失败→reject) 改成 JSON {detail} 之后, 它会 resolve, 于是
// **急停/解除急停遇服务端报错被当成成功**。
//
// 本测试: ① node 侧直接测 decision_util.fetchJson 读体; ② 从 mobile.html 里
// 抽出真实 tradeParse 源码用假 fetch 跑 (不是文本匹配, 是真执行)。
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { fetchJson } from '../../web/js/decision_util.mjs';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
let pass = 0, fail = 0;
function case_(name, cond) {
  if (cond) pass++;
  else { fail++; console.log('  [FAIL] ' + name); }
}
const resp = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  json: () => Promise.resolve(body),
});

// ── ① decision_util.fetchJson: 失败时读响应体人话 ──
{
  const err = await fetchJson(() => Promise.resolve(resp(500, { detail: '服务器内部错误: 炸了' })),
    'http://x:8081', '/api/trade/status').then(() => null, e => e);
  case_('fetchJson 500 带出 detail', !!err && /炸了/.test(err.message));
}
{
  const err = await fetchJson(() => Promise.resolve(resp(409, { success: false, error: '管线正在运行中' })),
    'http://x:8081', '/api/run').then(() => null, e => e);
  case_('fetchJson 409 业务软失败带出 error', !!err && /管线正在运行中/.test(err.message));
}
{
  const ok = await fetchJson(() => Promise.resolve(resp(200, { success: true })),
    'http://x', '/api/ok').then(d => d, () => null);
  case_('fetchJson 200 正常返回 body', !!ok && ok.success === true);
}
{
  const err = await fetchJson(() => Promise.resolve({
    ok: false, status: 500, json: () => Promise.reject(new Error('not json')),
  }), 'http://x', '/api/y').then(() => null, e => e);
  case_('fetchJson 非 JSON 失败仍抛 (带 URL)', !!err && /HTTP 500/.test(err.message));
}

// ── ② mobile.html 的真实 tradeParse: 500/409 必须 reject ──
function extractMobileTradeParse() {
  const html = readFileSync(join(root, 'web', 'mobile.html'), 'utf8');
  const start = html.indexOf('function tradeParse(r){');
  if (start < 0) throw new Error('mobile.html 里找不到 tradeParse');
  let depth = 0, i = html.indexOf('{', start);
  for (let j = i; j < html.length; j++) {
    if (html[j] === '{') depth++;
    else if (html[j] === '}') {
      depth--;
      if (depth === 0) return html.slice(start, j + 1);
    }
  }
  throw new Error('tradeParse 括号不配平');
}
const tradeParse = new Function('return ' + extractMobileTradeParse())();
{
  const err = await tradeParse(resp(503, { detail: '资产查询超时 —— 交易进程忙' }))
    .then(() => null, e => e);
  case_('mobile: 503 必须 reject 且带出人话', !!err && /交易进程忙/.test(err.message));
}
{
  const err = await tradeParse(resp(500, { detail: '服务器内部错误: 炸了' }))
    .then(() => null, e => e);
  case_('mobile: 500 JSON 必须 reject (急停不会被当成功)', !!err && /炸了/.test(err.message));
}
{
  const err = await tradeParse(resp(409, { success: false, error: '仅同花顺通道有武装概念' }))
    .then(() => null, e => e);
  case_('mobile: 409 业务软失败带出 error', !!err && /武装/.test(err.message));
}
{
  const ok = await tradeParse(resp(200, { kill_active: false })).then(d => d, () => null);
  case_('mobile: 200 正常返回', !!ok && ok.kill_active === false);
}

// ── ③ PC 侧两个封装都认两种形状 (源码级断言, 有先例: test_trade_base_wiring) ──
const apiSrc = readFileSync(join(root, 'web', 'js', 'api.js'), 'utf8');
const tradeSrc = readFileSync(join(root, 'web', 'js', 'trade.js'), 'utf8');
case_('api.js 认 detail', /d\.detail/.test(apiSrc));
case_('api.js 认 error (业务软失败)', /d\.error/.test(apiSrc));
case_('trade.js 认 detail', /\.detail\b/.test(tradeSrc));
case_('trade.js 认 error (业务软失败)', /\.error\b/.test(tradeSrc));
case_('mobile POST 走 tradeParse (查 r.ok)', /tradePost[\s\S]{0,120}tradeParse/.test(
  readFileSync(join(root, 'web', 'mobile.html'), 'utf8')));

console.log('\n' + pass + ' passed, ' + fail + ' failed');
process.exit(fail ? 1 : 0);
