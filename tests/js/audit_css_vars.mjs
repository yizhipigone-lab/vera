// 静态审计: 页面/JS 里引用的 var(--xxx) 必须在 tokens.css 或页面自身有定义
import { readFileSync } from 'fs';
const tokens = readFileSync('web/css/tokens.css', 'utf8');
const files = ['web/index.html', 'web/mobile.html', 'web/css/polish.css', 'web/css/brain-md.css',
  'web/js/analysis.js', 'web/js/brain_chat.js', 'web/js/brain_viz.js', 'web/js/charts.js',
  'web/js/charts_deep.js', 'web/js/charts_replay.mjs', 'web/js/trade.js', 'web/js/vera-ui.js', 'web/js/data_cache.js',
  // 2026-09-19 UIUX: 大盘仪表盘/决策台账也纳入巡逻 (market_dashboard 曾整段写死 hex)
  'web/js/market_dashboard.js', 'web/js/decision.js', 'web/js/decision_util.mjs'];
const defined = new Set();
for (const m of tokens.matchAll(/(--[a-z0-9-]+)\s*:/gi)) defined.add(m[1]);
let bad = 0, checked = 0;
for (const f of files) {
  const s = readFileSync(f, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  for (const m of s.matchAll(/--[a-z0-9-]+(?=\s*:)/gi)) defined.add(m[0]);
  const refs = new Set();
  for (const m of s.matchAll(/var\((--[a-z0-9-]+)/gi)) refs.add(m[1]);
  for (const r of refs) { checked++; if (!defined.has(r)) { console.log(`未定义: ${r}  ← ${f}`); bad++; } }
}
console.log(`引用检查: ${checked} 个变量引用, 未定义 ${bad} 个`);
process.exit(bad ? 1 : 0);
