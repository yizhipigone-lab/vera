// tests/web/test_trade_base_wiring.mjs — 8081 基址唯一出口接线守卫 (2026-09-19 批次 2.1)
//
// 背景: 8081 基址曾四份硬编码 (trade.js:19 / analysis.js:8-9 / decision.js 经
// tradeApiBase / mobile.html:216), 9-19 "404 被报成 8081 不可达"事故的同源温床。
// 本测试锁死: 唯一实现 = decision_util.mjs 的 tradeApiBase, 其余文件一律引用,
// 谁也不许再手写 ':8081'。
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const web = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'web');
const read = (p) => readFileSync(join(web, p), 'utf8');

let pass = 0, fail = 0;
function case_(name, cond) {
  if (cond) { pass++; }
  else { fail++; console.log('  [FAIL] ' + name); }
}

// ── 唯一出口存在 ──
const util = read('js/decision_util.mjs');
case_('decision_util.mjs 定义 TRADE_API_PORT = 8081', /TRADE_API_PORT\s*=\s*8081/.test(util));
case_('decision_util.mjs 导出 tradeApiBase', /export function tradeApiBase/.test(util));

// ── 消费方一律引用, 零硬编码 ──
const consumers = ['js/trade.js', 'js/analysis.js', 'js/decision.js', 'mobile.html'];
for (const f of consumers) {
  const src = read(f);
  case_(f + ' 无 :8081 硬编码', !/['"]:8081/.test(src) && !/:8081\//.test(src));
}
case_('trade.js import tradeApiBase', /import \{[^}]*tradeApiBase[^}]*\} from '\.\/decision_util\.mjs/.test(read('js/trade.js')));
case_('analysis.js import tradeApiBase', /import \{[^}]*tradeApiBase[^}]*\} from '\.\/decision_util\.mjs/.test(read('js/analysis.js')));
case_('decision.js 使用 tradeApiBase(location.hostname)', /tradeApiBase\(location\.hostname\)/.test(read('js/decision.js')));

// ── mobile.html 桥接线 (classic 主脚本靠 window 桥 + DOMContentLoaded 时序) ──
const mob = read('mobile.html');
case_('mobile.html 有 module 桥 (import tradeApiBase → window)', /import \{ tradeApiBase \} from '\/web\/js\/decision_util\.mjs/.test(mob) && /window\.tradeApiBase = tradeApiBase/.test(mob));
case_('mobile.html 默认基址走 window.tradeApiBase', /var def = window\.tradeApiBase\(location\.hostname\)/.test(mob));
case_('mobile.html 主脚本包 DOMContentLoaded (等 module 桥)', /addEventListener\('DOMContentLoaded'/.test(mob));
case_('mobile.html 桥在主脚本之前', mob.indexOf('window.tradeApiBase = tradeApiBase') < mob.indexOf("DOMContentLoaded', function()"));

console.log('\n' + pass + ' passed, ' + fail + ' failed');
process.exit(fail ? 1 : 0);
