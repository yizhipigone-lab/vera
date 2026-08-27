// web/js/stock_link.js
// 2026-08-26 (用户要求): 全局股票/ETF 代码与简称 → 同花顺标的首页跳转。
// 口径: 同花顺标的首页 = https://stockpage.10jqka.com.cn/{6位代码}/
//   实测 200: 沪市股票(600519)/沪市ETF(510300)/深市ETF(159915)/北交所(430047);
//   可转债(113044) 307 自动跳转债页, 浏览器无感。港股(HK00700)格式不同, 本期不支持。
// 用法:
//   stockLink(code, text) → '<a ...>text</a>'  HTML 字符串, text 缺省回退显示 code;
//                           提取不到 6 位代码时退化为纯文本(不套链接)。
//   stockUrl(code)        → 同花顺 URL 或空串。
// 依赖: 无 (自带 esc); 在 index.html 里须早于 trade.js 加载。
//   <script type="module"> 的 charts.js/analysis.js 经 window.stockLink 访问,
//   普通脚本先于 module 执行, 时序安全。
(function () {
  'use strict';
  function escHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  // 从 "600519" / "600519.SH" / "SH600519" 等形式提取 6 位证券代码; 提不到返回空串
  function normStockCode(code) {
    var m = String(code == null ? '' : code).match(/\d{6}/);
    return m ? m[0] : '';
  }
  function stockUrl(code) {
    var c = normStockCode(code);
    return c ? 'https://stockpage.10jqka.com.cn/' + c + '/' : '';
  }
  // 渲染可点击文本。onclick stopPropagation: 防止触发行级点击
  // (回测成交流水表整行绑了「点击查看 K 线回放」)。
  function stockLink(code, text) {
    var label = escHtml(text != null && text !== '' ? text : (code || '—'));
    var url = stockUrl(code);
    if (!url) return label;
    return '<a class="stock-link" href="' + url + '" target="_blank" rel="noopener noreferrer"'
      + ' title="同花顺查看 ' + label + '" onclick="event.stopPropagation()">' + label + '</a>';
  }
  window.stockLink = stockLink;
  window.stockUrl = stockUrl;
  window.normStockCode = normStockCode;
})();
