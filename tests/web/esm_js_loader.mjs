// Node ESM customization hook: 让 tests 能 import web/js 下的 .js 前端 ES module。
// 背景: 浏览器里 vera-ui.js 以 <script type="module"> 加载, import './charts.js' 天然是 ESM;
//   但 node 默认把 .js 当 CommonJS (仓库无 package.json type:module),
//   直接 import web/js/charts.js 会 SyntaxError。
// 用法 (仅测试): import { register } from 'node:module';
//   register('./esm_js_loader.mjs', import.meta.url);  // 必须在动态 import 被测模块之前
import { readFile } from 'node:fs/promises';

export async function load(url, context, nextLoad) {
  // 只放行本项目 web/js 下的 .js, 其余走默认解析
  if (/\/web\/js\/[^/]+\.js$/.test(url)) {
    return { format: 'module', source: await readFile(new URL(url), 'utf8'), shortCircuit: true };
  }
  return nextLoad(url, context);
}
