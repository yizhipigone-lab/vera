// tests/js/test_tab_wiring.js — 页签接线对账（2026-09-17 用户实测"点大盘位置没反应"之后补）
//
// **为什么要有它**：大盘位置页签当时三处都做了 ——
//   ① `web/index.html` 里有 <button id="tabBtnMarket">
//   ② `switchTab()` 里支持 name==='market'
//   ③ hash 白名单 `_validTabs` 里有 'market'
// **唯独漏了第 4 处：`addEventListener('click', ...)`** → 点上去毫无反应。
// 前端 Node 单测当时测的全是 `market_position.js` 的纯函数，
// **DOM 接线一处都没测**，所以这个洞整整一轮没被发现。
//
// 这条测试把"四处必须齐全"变成机器判据：
//   A. index.html 里每个 `tabBtnXxx` 按钮  →  vera-ui.js 里必须有点击监听
//   B. 每个按钮对应一个 `pageXxx` 容器      →  否则切过去是白屏
//   C. 每个页签名必须在 `_validTabs` 里     →  否则刷新后回不到这个页签
//   D. `switchTab` 里必须真的处理这个页签名（toggle active 或调 PageEnter 钩子）
//
// 用法: node tests/js/test_tab_wiring.js
'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..', '..');
const HTML = fs.readFileSync(path.join(ROOT, 'web', 'index.html'), 'utf8');
const UI = fs.readFileSync(path.join(ROOT, 'web', 'js', 'vera-ui.js'), 'utf8');

let pass = 0, fail = 0;
function assert(cond, label, extra) {
  if (cond) { console.log(`  ✓ ${label}`); pass++; }
  else { console.log(`  ✗ ${label}${extra ? '  —— ' + extra : ''}`); fail++; }
}

// ── 从 HTML 里抠出所有页签按钮 ──────────────────────────────────────────
const btnIds = [...HTML.matchAll(/id="(tabBtn[A-Za-z]+)"/g)].map(m => m[1]);
assert(btnIds.length >= 10, `index.html 里找到 ${btnIds.length} 个页签按钮`, btnIds.join(','));

// 按钮 id → 页签名（tabBtnMarket → market；驼峰转小写即可，逐个都符合现约定）
const tabName = id => id.replace(/^tabBtn/, '').toLowerCase();

// ── A. 每个按钮都必须有点击监听 ────────────────────────────────────────
console.log('\nA. 每个页签按钮都要有点击监听（这条就是漏掉的那处）:');
for (const id of btnIds) {
  const re = new RegExp(`getElementById\\('${id}'\\)\\??\\.addEventListener\\('click'`);
  assert(re.test(UI), `${id} → 有 click 监听`);
}

// ── B. 每个页签都要有对应的内容容器 ────────────────────────────────────
console.log('\nB. 每个页签都要有内容容器（否则切过去是白屏）:');
// `backtest` 是**主界面本身**（`.app` 那块），不是浮层页签 —— 它没有也不该有 pageBacktest。
// 其余 9 个都是 fixed 定位的浮层，容器 id 约定 page + 首字母大写。
const MAIN_AREA_TABS = new Set(['backtest']);
assert(HTML.includes('class="app"'), 'backtest 用的主界面 .app 存在');
for (const id of btnIds) {
  const name = tabName(id);
  const pageId = 'page' + name.charAt(0).toUpperCase() + name.slice(1);
  if (MAIN_AREA_TABS.has(name)) {
    const re = new RegExp(`classList\\.toggle\\('active'[^)]*'${name}'|'${name}'[^)]*classList`);
    assert(re.test(UI) || UI.includes(`name==='backtest'`) || UI.includes(`name==='${name}'`),
           `${id} → 主界面页签（切 .app 显隐，无 page 容器）`);
  } else {
    assert(HTML.includes(`id="${pageId}"`), `${id} → 容器 ${pageId} 存在`);
  }
}

// ── C. 每个页签名都要在 hash 白名单里 ──────────────────────────────────
console.log('\nC. 每个页签名都要在 hash 白名单 _validTabs 里:');
const m = UI.match(/_validTabs\s*=\s*\[([^\]]*)\]/);
assert(!!m, '找得到 _validTabs 定义');
const valid = m ? m[1].split(',').map(s => s.trim().replace(/^['"]|['"]$/g, '')).filter(Boolean) : [];
for (const id of btnIds) {
  const name = tabName(id);
  assert(valid.includes(name), `${name} 在 _validTabs 里`, `白名单: ${valid.join(',')}`);
}

// ── D. switchTab 必须真的处理这个页签名 ───────────────────────────────
console.log('\nD. switchTab 里必须处理这个页签名（点亮点亮/切页/进页钩子至少一处）:');
const sw = UI.slice(UI.indexOf('function switchTab('), UI.indexOf('function switchTab(') + 6000);
for (const id of btnIds) {
  const name = tabName(id);
  const handled = sw.includes(`'${name}'`) || sw.includes(`"${name}"`);
  assert(handled, `switchTab 处理了 '${name}'`);
}

console.log(`\n结果: ${pass} 通过, ${fail} 失败`);
process.exit(fail ? 1 : 0);
