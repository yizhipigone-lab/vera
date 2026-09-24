/* brain_viz.js — 研究大脑输出富呈现渲染层 (2026-09-02, dsh-visualize 式改造 L2)
 *
 * 职责: 把大脑 markdown 回答里的 ```chart 白名单 JSON 块渲染成 echarts 交互图,
 *       外加排版增强三件套 (表格包裹/数值列右对齐/带符号百分比红涨绿跌) 和
 *       【事实】/【解读】/【待验证】/【缺】徽章、<counter_evidence> 反证卡。
 *
 * 松耦合: 本文件挂 window.BrainViz; brain_chat.js 优先走它, BrainViz 缺失时
 *       自动回退旧 marked 纯文本路径, 大脑功能不受影响。卸载 = 删本文件 +
 *       index.html 两行引用 + brain-md.css。
 *
 * 安全: chart 块走白名单 schema 校验 (validateSpec), 未知字段/超长/非等长一律
 *       拒绝并降级为普通代码块; 徽章/上色都发生在 DOMPurify 消毒之后或
 *       textContent 赋值, 不引入新注入面。
 *
 * 测试: tests/web/test_brain_viz.mjs (node 直跑, 纯函数契约)。
 */
(function () {
  'use strict';

  // ── 内部状态 ──
  var _specs = [];     // 本轮渲染收集的合法 chart spec (beginRender 重置)
  var _charts = [];    // 活着的 echarts 实例 (disposeAll 释放)
  var _root = null;    // 最近一次 mountAll 的容器 (主题切换时重挂)
  var _hooked = false; // resize/theme 钩子只装一次
  var _rzTimer = null;
  var _themeObs = null;

  var REDUCED = false;
  try {
    REDUCED = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  } catch (e) { /* 老 IE / Node stub */ }

  // ── 工具 ──
  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // W4-6: markdown h1 → h2 降级 (页内已有 <h1>VERA</h1>, 报告/回答再渲一个 h1 破坏标题层级)。
  // 字符串后处理, 不依赖 marked 版本的 renderer API; brain_chat 与 lab 报告两条渲染路径共用。
  function demoteH1(html) {
    return String(html == null ? '' : html).replace(/<(\/?)h1(\s|>)/gi, '<$1h2$2');
  }
  window.veraDemoteH1 = demoteH1;

  function hexToRgba(color, alpha) {
    if (!color) return 'rgba(120,120,120,' + alpha + ')';
    if (color.charAt(0) !== '#') return color;
    var h = color.slice(1);
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    var r = parseInt(h.slice(0, 2), 16), g = parseInt(h.slice(2, 4), 16), b = parseInt(h.slice(4, 6), 16);
    return 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')';
  }

  function themeColors() {
    /* 2026-09-06 审计: 兜底色收敛为一份 FB (原 v() 逐参数一份 + catch 再抄一份, 11 处重复) */
    var FB = { up: '#d6342f', down: '#1f8a5b', accent: '#b8860b', accent2: '#2a9d8f',
      warn: '#c47f17', text: '#1a1d24', text2: '#666', bg: '#f6f7f9', border: '#e2e4e9',
      bm: ['#b8860b', '#2a9d8f', '#e8833a', '#6a5acd', '#457b9d'] };
    try {
      var s = getComputedStyle(document.documentElement);
      function v(name, fb) {
        var val = s.getPropertyValue(name).trim();
        return val || fb;
      }
      return {
        up: v('--up', FB.up), down: v('--down', FB.down),
        accent: v('--accent', FB.accent), accent2: v('--accent2', FB.accent2),
        warn: v('--warn', FB.warn), text: v('--text', FB.text),
        text2: v('--text2', FB.text2), bg: v('--bg', FB.bg),
        border: v('--border', FB.border),
        bm: FB.bm.map(function (c, i) { return v('--bm-' + (i + 1), c); })
      };
    } catch (e) {
      return FB;
    }
  }

  // ── ② chart spec 白名单校验 (纯函数) ──
  // 白名单: type=bar/line/pie; title≤40; source≤60; unit≤10; x=1..60 类目;
  //         series≤3 条 [{name≤30, data=1..60 等长有限数字}]; horizontal=bool;
  //         pie 只许 1 条 series; 未知字段一律拒绝 (防 echarts option 注入)。
  function validateSpec(o) {
    function bad(r) { return { ok: false, r: r }; }
    if (!o || typeof o !== 'object' || Array.isArray(o)) return bad('不是 JSON 对象');
    var KEYS = ['type', 'title', 'source', 'unit', 'x', 'series', 'horizontal'];
    for (var k in o) if (KEYS.indexOf(k) < 0) return bad('未知字段 ' + k);

    var type = o.type || 'bar';
    if (['bar', 'line', 'pie'].indexOf(type) < 0) return bad('type 只支持 bar/line/pie');
    if (o.title != null && (typeof o.title !== 'string' || o.title.length > 40)) return bad('title 非法');
    if (o.source != null && (typeof o.source !== 'string' || o.source.length > 60)) return bad('source 非法');
    if (o.unit != null && (typeof o.unit !== 'string' || o.unit.length > 10)) return bad('unit 非法');
    if (o.horizontal != null && typeof o.horizontal !== 'boolean') return bad('horizontal 非法');

    if (o.x != null) {
      if (!Array.isArray(o.x) || o.x.length < 1 || o.x.length > 60) return bad('x 长度 1..60');
      for (var i = 0; i < o.x.length; i++) {
        var it = o.x[i];
        if (typeof it === 'number') continue;
        if (typeof it === 'string' && it.length <= 20) continue;
        return bad('x[' + i + '] 非法');
      }
    }

    var s = o.series;
    if (!Array.isArray(s) || s.length < 1) return bad('series 缺失');
    if (s.length > 3) return bad('series 最多 3 条');
    if (type === 'pie' && s.length > 1) return bad('pie 只支持 1 条 series');
    var len0 = null;
    for (var j = 0; j < s.length; j++) {
      var se = s[j];
      if (!se || typeof se !== 'object' || Array.isArray(se)) return bad('series[' + j + '] 非法');
      for (var k2 in se) if (['name', 'data'].indexOf(k2) < 0) return bad('series[' + j + '] 未知字段 ' + k2);
      if (se.name != null && (typeof se.name !== 'string' || se.name.length > 30)) return bad('name 非法');
      var d = se.data;
      if (!Array.isArray(d) || d.length < 1 || d.length > 60) return bad('data 长度 1..60');
      for (var n = 0; n < d.length; n++) {
        if (typeof d[n] !== 'number' || !isFinite(d[n])) return bad('data[' + n + '] 非数字');
      }
      if (len0 === null) len0 = d.length;
      else if (d.length !== len0) return bad('series data 不等长');
    }
    if (o.x != null && o.x.length !== len0) return bad('x 与 data 长度不匹配');
    return { ok: true };
  }

  // ── ① chart 围栏块提取 (合法→占位符, 非法→原样保留降级为代码块) ──
  var CHART_FENCE_RE = /(^|\n)[ \t]*```chart[ \t]*\n([\s\S]*?)[ \t]*```/g;
  function extractCharts(md) {
    return String(md == null ? '' : md).replace(CHART_FENCE_RE, function (all, lead, body) {
      var spec = null;
      if (body && body.length <= 4000) {
        try { spec = JSON.parse(body.trim()); } catch (e) { spec = null; }
      }
      if (!spec || !validateSpec(spec).ok) return all;   // 降级: 原样代码块
      _specs.push(spec);
      return lead + '\nBMVIZCHART' + (_specs.length - 1) + 'BMVIZCHART\n';
    });
  }

  // ── ⑥ 占位符 → 图表挂载点 (纯字符串函数) ──
  function chartDiv(i) { return '<div class="bm-chart" data-bviz="' + i + '"></div>'; }
  function injectChartDivs(html) {
    return String(html == null ? '' : html)
      .replace(/<p>\s*BMVIZCHART(\d+)BMVIZCHART\s*<\/p>/g, function (_, i) { return chartDiv(i); })
      .replace(/BMVIZCHART(\d+)BMVIZCHART/g, function (_, i) { return chartDiv(i); });
  }

  // ── ③ 反证段拆分 (counter.py 强制段 → 独立警示卡) ──
  function splitCounter(md) {
    var s = String(md == null ? '' : md);
    var m = s.match(/<counter_evidence>([\s\S]*?)<\/counter_evidence>/i);
    if (!m) return { main: s, counter: null };
    return {
      main: (s.slice(0, m.index) + s.slice(m.index + m[0].length)),
      counter: m[1].trim()
    };
  }

  // ── ④ 事实/解读/待验证/缺 徽章 ──
  function badgeify(html) {
    var s = String(html == null ? '' : html);
    var MAP = [['【事实】', 'bm-fact'], ['【解读】', 'bm-view'],
               ['【待验证】', 'bm-todo'], ['【缺】', 'bm-miss']];
    for (var i = 0; i < MAP.length; i++) {
      var tok = MAP[i][0], cls = MAP[i][1];
      s = s.split(tok).join('<span class="' + cls + '">' + tok.slice(1, -1) + '</span>');
    }
    return s;
  }

  // ── ⑤ 带符号百分比 → 红涨绿跌 (A股习惯; 纯函数, DOM 走 colorPercents) ──
  var PCT_RE = /[+＋−\-－]\d+(?:\.\d+)?\s*%/g;
  function markPct(text) {
    var s = escapeHtml(text);
    return s.replace(PCT_RE, function (m) {
      var cls = /^[+＋]/.test(m) ? 'bm-up' : 'bm-down';
      return '<span class="' + cls + '">' + m + '</span>';
    });
  }

  // ── markdown → 消毒 HTML (与 brain_chat 旧路径同款, 多一层失败兜底) ──
  function mdToHtml(md) {
    if (window.marked && window.DOMPurify) {
      // 2026-09-06 审查修复: 本路径此前漏接 demoteH1, 报告 h1 只走 brain_chat 那条线会漏
      try { return demoteH1(window.DOMPurify.sanitize(window.marked.parse(md))); } catch (e) { /* 走兜底 */ }
    }
    return '<pre style="white-space:pre-wrap;margin:0">' + escapeHtml(md) + '</pre>';
  }

  // ── 对外主入口: 回答全文 → 富 HTML (brain_chat.bubbleHtml 调用) ──
  function renderBody(text) {
    var md = String(text == null ? '' : text).replace(/\r\n/g, '\n');
    var parts = splitCounter(md);
    var mainHtml = badgeify(injectChartDivs(mdToHtml(extractCharts(parts.main))));
    var out = mainHtml;
    if (parts.counter) {
      out += '<div class="bm-counter"><div class="bm-counter-head">⚠ 反证 · 与主结论相反的事实或风险</div>'
           + badgeify(mdToHtml(parts.counter)) + '</div>';
    }
    return out;
  }

  // ── DOM 增强 (mountAll 内部) ──
  function wrapTables(root) {
    Array.prototype.forEach.call(root.querySelectorAll('table'), function (tb) {
      var p = tb.parentNode;
      if (!p) return;
      if (p.classList && p.classList.contains('bm-tbl-wrap')) return;
      var w = document.createElement('div');
      w.className = 'bm-tbl-wrap';
      p.insertBefore(w, tb);
      w.appendChild(tb);
    });
  }

  var NUM_RE = /^[+＋−\-－]?[\d,.]+[%万亿倍]?$/;
  function markNumCells(root) {
    Array.prototype.forEach.call(root.querySelectorAll('td,th'), function (el) {
      var t = (el.textContent || '').trim();
      if (t && /\d/.test(t) && NUM_RE.test(t) && el.classList) el.classList.add('bm-num');
    });
  }

  function colorPercents(root) {
    if (!document.createTreeWalker) return;
    var skip = { CODE: 1, PRE: 1, SCRIPT: 1, STYLE: 1, TEXTAREA: 1 };
    var walker;
    try {
      walker = document.createTreeWalker(root, 4 /* SHOW_TEXT */, null);
    } catch (e) { return; }
    var hits = [];
    while (walker.nextNode()) {
      var pn = walker.currentNode.parentNode;
      if (pn && skip[pn.tagName]) continue;
      PCT_RE.lastIndex = 0;
      if (PCT_RE.test(walker.currentNode.nodeValue)) hits.push(walker.currentNode);
    }
    PCT_RE.lastIndex = 0;
    hits.forEach(function (n) {
      var tmp = document.createElement('span');
      tmp.innerHTML = markPct(n.nodeValue);
      while (tmp.firstChild) n.parentNode.insertBefore(tmp.firstChild, n);
      n.parentNode.removeChild(n);
    });
  }

  // ── echarts option 构建 (主题色实时读 CSS 变量, 主题切换重挂) ──
  function buildOption(spec) {
    var c = themeColors();
    var type = spec.type || 'bar';
    var labels = (spec.x && spec.x.length)
      ? spec.x.map(String)
      : spec.series[0].data.map(function (_, i) { return '#' + (i + 1); });

    var opt = {
      animationDuration: REDUCED ? 0 : 350,
      color: c.bm,
      tooltip: {
        trigger: type === 'pie' ? 'item' : 'axis',
        backgroundColor: 'rgba(45,48,55,.94)', borderWidth: 0,
        textStyle: { color: '#e8e8e8', fontSize: 12 },
        confine: true
      }
    };
    if (spec.unit && type !== 'pie') {
      var u = spec.unit;
      opt.tooltip.valueFormatter = function (v) { return (v == null ? '-' : v) + u; };
    }

    if (type === 'pie') {
      opt.series = [{
        type: 'pie', radius: ['38%', '68%'], center: ['50%', '52%'],
        itemStyle: { borderColor: c.bg, borderWidth: 2 },
        label: { color: c.text2, fontSize: 11, formatter: '{b} {d}%' },
        labelLine: { length: 8, length2: 6 },
        data: labels.map(function (l, i) {
          return { name: l, value: spec.series[0].data[i] };
        })
      }];
      return opt;
    }

    var catAxis = {
      type: 'category', data: labels,
      axisLine: { lineStyle: { color: c.border } }, axisTick: { show: false },
      axisLabel: {
        color: c.text2, fontSize: 11,
        interval: labels.length > 12 ? 'auto' : 0,
        rotate: (labels.length > 8 && !spec.horizontal) ? 30 : 0
      }
    };
    var valAxis = {
      type: 'value',
      axisLine: { show: false }, axisTick: { show: false },
      splitLine: { lineStyle: { color: c.border, opacity: 0.5 } },
      axisLabel: { color: c.text2, fontSize: 11 }
    };
    if (spec.unit) {
      valAxis.name = spec.unit;
      valAxis.nameTextStyle = { color: c.text2, fontSize: 10 };
    }
    if (spec.horizontal && type === 'bar') { opt.xAxis = valAxis; opt.yAxis = catAxis; }
    else { opt.xAxis = catAxis; opt.yAxis = valAxis; }

    var multi = spec.series.length > 1;
    opt.grid = { left: 6, right: 18, top: spec.unit ? 26 : 14, bottom: multi ? 30 : 6, containLabel: true };
    if (multi) opt.legend = { bottom: 0, itemWidth: 14, textStyle: { color: c.text2, fontSize: 11 } };

    // 单序列柱图且含负值 → 按正负上色 (A股: 涨红跌绿), 其余走调色板
    var singleNeg = !multi && type === 'bar'
      && spec.series[0].data.some(function (v) { return v < 0; });

    opt.series = spec.series.map(function (s, i) {
      var col = c.bm[i % c.bm.length];
      var name = s.name || ('系列' + (i + 1));
      if (type === 'bar') {
        var data = s.data;
        if (singleNeg) {
          data = s.data.map(function (v) {
            return { value: v, itemStyle: { color: v < 0 ? c.down : c.up } };
          });
        }
        return { type: 'bar', name: name, data: data, barMaxWidth: 34,
                 itemStyle: { color: col, borderRadius: [3, 3, 0, 0] } };
      }
      var line = { type: 'line', name: name, data: s.data, smooth: 0.3,
                   symbol: 'circle', symbolSize: s.data.length > 24 ? 0 : 5,
                   lineStyle: { width: 2, color: col }, itemStyle: { color: col } };
      if (!multi) line.areaStyle = { color: hexToRgba(col, 0.14) };
      return line;
    });
    return opt;
  }

  // ── 图表挂载 / 释放 ──
  function mountCharts(root) {
    var nodes = root.querySelectorAll('.bm-chart[data-bviz]');
    if (!nodes || !nodes.length) return;
    var E = window.echarts;
    Array.prototype.forEach.call(nodes, function (node) {
      var idx = parseInt(node.getAttribute('data-bviz'), 10);
      var spec = _specs[idx];
      if (!spec) { node.style.display = 'none'; return; }   // 无对应 spec (旧消息/异常) → 隐藏
      node.innerHTML = '<div class="bm-chart-head"><span class="bm-chart-title"></span>' +
                       '<span class="bm-chart-src"></span></div><div class="bm-chart-canvas"></div>';
      var titleEl = node.querySelector('.bm-chart-title');
      var srcEl = node.querySelector('.bm-chart-src');
      var canvas = node.querySelector('.bm-chart-canvas');
      titleEl.textContent = spec.title || ((spec.type === 'pie') ? '占比图' : '图表');
      srcEl.textContent = spec.source ? ('数据: ' + spec.source) : '';
      if (!srcEl.textContent) srcEl.style.display = 'none';
      if (!E) {   // echarts 未加载 (理论上不会, index.html 已全局引入)
        canvas.className = 'bm-chart-note';
        canvas.textContent = '图表库未加载 (echarts.min.js), 无法渲染';
        return;
      }
      try {
        var inst = E.init(canvas);
        inst.setOption(buildOption(spec));
        _charts.push(inst);
      } catch (e) {
        canvas.className = 'bm-chart-note';
        canvas.textContent = '图表渲染失败: ' + (e && e.message ? e.message : e);
      }
    });
  }

  function disposeCharts() {
    _charts.forEach(function (inst) {
      try { inst.dispose(); } catch (e) { /* 已随 DOM 销毁 */ }
    });
    _charts = [];
  }

  // ── resize / 主题切换钩子 (只装一次) ──
  function ensureHooks() {
    if (_hooked) return;
    _hooked = true;
    window.addEventListener('resize', function () {
      clearTimeout(_rzTimer);
      _rzTimer = setTimeout(function () {
        _charts.forEach(function (c) { try { c.resize(); } catch (e) {} });
      }, 200);
    });
    // 主题切换 (data-theme) → 用新主题色重挂图表
    if (typeof MutationObserver !== 'undefined' && document.documentElement) {
      _themeObs = new MutationObserver(function () {
        disposeCharts();
        if (_root) { try { mountCharts(_root); } catch (e) {} }
      });
      _themeObs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    }
  }

  // ── 对外: 生命周期 ──
  function beginRender() { _specs = []; }                    // 每轮 renderMessages 前重置
  function disposeAll() { disposeCharts(); }                 // innerHTML 清空前释放 echarts
  function mountAll(root) {                                  // innerHTML 设置后调用
    if (!root || !root.querySelectorAll) return;
    _root = root;
    try { wrapTables(root); } catch (e) {}
    try { markNumCells(root); } catch (e) {}
    try { colorPercents(root); } catch (e) {}
    try { mountCharts(root); } catch (e) {}
    ensureHooks();
  }
  function specs() { return _specs.slice(); }

  // ── 挂载 (测试经 window.BrainViz 访问) ──
  window.BrainViz = {
    renderBody: renderBody,
    mountAll: mountAll,
    beginRender: beginRender,
    disposeAll: disposeAll,
    // 纯函数 (tests/web/test_brain_viz.mjs 契约测试锁定)
    validateSpec: validateSpec,
    extractCharts: extractCharts,
    injectChartDivs: injectChartDivs,
    splitCounter: splitCounter,
    badgeify: badgeify,
    markPct: markPct,
    specs: specs
  };
})();
