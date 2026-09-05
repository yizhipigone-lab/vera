/* brain_chat.js — 研究 TAB 对话大脑 (P3, 2026-07-28)
 *
 * 多会话 (每条独立 channel, 上下文隔离可并行) + 直接等待 (转圈+耗时)
 * + marked/DOMPurify 渲染大脑 markdown 回答。
 * 富呈现 (2026-09-02): 完成态回答优先走 window.BrainViz (brain_viz.js:
 * ```chart 图表块→echarts 交互图 + 事实徽章 + 反证卡 + 涨跌上色, 样式 brain-md.css);
 * BrainViz 缺失/流式中自动回退本文件原有 marked 路径 (松耦合)。
 * 松耦合: 本文件挂了不影响其他页; 卸载 = 删本文件 +
 * index.html 卡片 + server.py 两条路由。
 * 刷新页面: 消息存 localStorage 不丢; 服务端 session 记忆还在 (可追问上文)。
 */
(function () {
  var LS_CONVS = 'brain_convs';
  var LS_MSGS = 'brain_msgs_';
  var LS_ACTIVE = 'brain_active';
  var MAX_MSGS = 100;  // 每会话 localStorage 上限 (防膨胀)

  var convs = [];      // [{id, title}]
  var activeId = null;
  var pending = {};    // {convId: {startTs, timer}}

  function $(id) { return document.getElementById(id); }
  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }

  function loadConvs() {
    try { convs = JSON.parse(localStorage.getItem(LS_CONVS) || '[]'); } catch (e) { convs = []; }
    if (!convs.length) { convs = [{ id: 'c' + Date.now().toString(36), title: '会话 1' }]; saveConvs(); }
    activeId = localStorage.getItem(LS_ACTIVE);
    if (!activeId || !convs.some(function (c) { return c.id === activeId; })) activeId = convs[0].id;
  }
  function saveConvs() { localStorage.setItem(LS_CONVS, JSON.stringify(convs)); }
  function loadMsgs(id) { try { return JSON.parse(localStorage.getItem(LS_MSGS + id) || '[]'); } catch (e) { return []; } }
  function saveMsgs(id, msgs) {
    if (msgs.length > MAX_MSGS) msgs = msgs.slice(-MAX_MSGS);
    localStorage.setItem(LS_MSGS + id, JSON.stringify(msgs));
  }
  function activeConv() { return convs.filter(function (c) { return c.id === activeId; })[0]; }

  function renderConvBar() {
    var bar = $('brainConvBar');
    bar.innerHTML = convs.map(function (c) {
      var on = c.id === activeId;
      var dot = pending[c.id] ? ' ⏳' : '';
      return '<span style="display:inline-flex;align-items:center;gap:2px">' +
        '<button data-conv="' + c.id + '" class="btn' + (on ? ' btn-primary' : '') + '" ' +
        'style="padding:4px 10px;font-size:12px;' + (on ? '' : 'opacity:.75') + '">' +
        esc(c.title) + dot + '</button>' +
        '<span data-close="' + c.id + '" title="关闭并清空此会话 (归档已保存, 不丢)" ' +
        'style="cursor:pointer;color:var(--text2);font-size:12px;padding:0 3px">×</span>' +
        '</span>';
    }).join('') + '<button id="brainNewConvBtn" class="btn" style="padding:4px 10px;font-size:11px" title="新会话 (独立上下文)">＋</button>';
    Array.prototype.forEach.call(bar.querySelectorAll('button[data-conv]'), function (b) {
      b.addEventListener('click', function () {
        activeId = b.getAttribute('data-conv');
        localStorage.setItem(LS_ACTIVE, activeId);
        renderAll();
      });
    });
    Array.prototype.forEach.call(bar.querySelectorAll('span[data-close]'), function (x) {
      x.addEventListener('click', function () { closeConv(x.getAttribute('data-close')); });
    });
    $('brainNewConvBtn').addEventListener('click', function () {
      var id = 'c' + Date.now().toString(36);
      convs.push({ id: id, title: '会话 ' + (convs.length + 1) });
      activeId = id;
      saveConvs();
      localStorage.setItem(LS_ACTIVE, activeId);
      renderAll();
      $('brainInput').focus();
    });
  }

  function closeConv(id) {
    var c = convs.filter(function (v) { return v.id === id; })[0];
    if (!confirm('关闭「' + (c ? c.title : id) + '」?\n页面消息将清空, 服务端会话记忆同时重置。\n(归档文件不受影响, 思考沉淀都在)')) return;
    // 服务端 session 一并重置 (归档在 vault, 不丢)
    fetch('/api/research/chat/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conv: id }),
    }).catch(function () { /* 松耦合: server 不在也照清本地 */ });
    localStorage.removeItem(LS_MSGS + id);
    convs = convs.filter(function (v) { return v.id !== id; });
    if (!convs.length) convs = [{ id: 'c' + Date.now().toString(36), title: '会话 1' }];
    if (activeId === id) {
      activeId = convs[0].id;
      localStorage.setItem(LS_ACTIVE, activeId);
    }
    saveConvs();
    renderAll();
  }

  function bubbleHtml(m) {
    if (m.role === 'user') {
      return '<div style="display:flex;justify-content:flex-end;margin:6px 0">' +
        '<div style="max-width:80%;padding:8px 12px;border-radius:10px;background:var(--accent);color:#fff;white-space:pre-wrap;word-break:break-word">' +
        esc(m.text) + '</div></div>';
    }
    var badge = m.lowConf ? ' <span style="padding:1px 6px;border-radius:4px;font-size:10px;background:var(--pending);color:#fff">低置信</span>' : '';
    var body;
    if (window.BrainViz && !m.streaming) {
      // 富呈现: 图表块/徽章/反证卡/涨跌上色 (brain_viz.js; 缺它就走下面旧路径)
      body = window.BrainViz.renderBody(m.text);
    } else {
      try {
        body = (window.marked && window.DOMPurify)
          ? DOMPurify.sanitize(marked.parse(m.text))
          : '<pre style="white-space:pre-wrap;margin:0">' + esc(m.text) + '</pre>';
      } catch (e) {
        body = '<pre style="white-space:pre-wrap;margin:0">' + esc(m.text) + '</pre>';
      }
    }
    var warn = (m.warnings && m.warnings.length)
      ? '<div style="font-size:12px;color:var(--text2);margin-top:4px">⚠ ' + m.warnings.map(esc).join('；') + '</div>' : '';
    return '<div style="display:flex;justify-content:flex-start;margin:6px 0">' +
      '<div style="max-width:100%;width:100%;padding:10px 14px;border-radius:10px;background:var(--card);border:1px solid var(--border);color:var(--text);word-break:break-word" class="brain-md">' +
      '<div style="font-size:12px;color:var(--text2);margin-bottom:4px">' + _modeBadge(m.via) + badge + '</div>' +
      body + warn + '</div></div>';
  }

  function renderMessages() {
    var box = $('brainMessages');
    var msgs = loadMsgs(activeId);
    if (window.BrainViz) window.BrainViz.disposeAll();   // innerHTML 清空前释放 echarts 实例
    if (!msgs.length) {
      box.innerHTML = '<div style="color:var(--text2);padding:8px">新会话。问点政策/持仓/图谱相关的, 如 "当前持仓哪些在 AVOID 档?"</div>';
      return;
    }
    if (window.BrainViz) window.BrainViz.beginRender();  // 重置本轮图表 spec 收集
    box.innerHTML = msgs.map(bubbleHtml).join('');
    if (window.BrainViz) window.BrainViz.mountAll(box);  // 表格包裹/涨跌上色/挂载交互图
    box.scrollTop = box.scrollHeight;
  }

  function renderStatus() {
    var st = $('brainStatus');
    var p = pending[activeId];
    if (p) {
      var secs = Math.floor((Date.now() - p.startTs) / 1000);
      if (p.streamLine) {
        st.textContent = p.streamLine + ' · ' + secs + 's';
      } else {
        st.textContent = '大脑思考中… 已耗时 ' + secs + ' 秒';
      }
    } else {
      st.textContent = '';
    }
    $('brainSendBtn').disabled = !!p;
    $('brainInput').disabled = !!p;
    var stopBtn = $('brainStopBtn');
    if (stopBtn) stopBtn.style.display = (p && p.runId) ? '' : 'none';
  }

  function renderAll() { renderConvBar(); renderMessages(); renderStatus(); }

  function setPending(id, on) {
    if (on) {
      var p = { startTs: Date.now(), timer: null };
      p.timer = setInterval(function () { if (id === activeId) renderStatus(); renderConvBar(); }, 1000);
      pending[id] = p;
    } else {
      if (pending[id]) { clearInterval(pending[id].timer); delete pending[id]; }
    }
    renderConvBar();
    renderStatus();
  }

  function _mode() {
    /* 三态档位 (2026-09-05): fast=⚡快速 / standard=标准 / deep=🧠深度思考 */
    var el = document.querySelector('input[name="brainMode"]:checked');
    var m = el ? el.value : 'fast';
    return (m === 'standard' || m === 'deep') ? m : 'fast';
  }

  /* 档位大白话注释 (2026-09-05): 随所选档位实时切换, 不悬停也看得懂。
     brainModeHint 容器在 index.html 研究页, 缺失则静默跳过 (松耦合)。 */
  var _MODE_HINTS = {
    fast: '⚡快速: 1~3秒秒回, 只答知识/概念类轻问题 (像随口问一句), 不查你的数据、不碰代码。问个股/大盘要看数字的, 请切下面两档。',
    standard: '标准: 正经深度回答。它会自己动手查你的数据、翻项目代码再答, 需要十几秒到几分钟。',
    deep: '🧠深度思考: 最难、要动手的事交给它。独立进程, 自己写代码/读文件/联网核实, 能读写 VERA 项目代码, 最慢但最全, 每次自动留档。',
  };
  function updateModeHint() {
    var el = $('brainModeHint');
    if (!el) return;
    el.innerHTML = _MODE_HINTS[_mode()] || _MODE_HINTS.fast;
  }

  function _modeBadge(mode) {
    if (mode === 'deep') return '大脑 · 🧠深度';
    if (mode === 'fast') return '大脑 · ⚡快速';
    return '大脑';
  }

  function send() {
    var input = $('brainInput');
    var q = input.value.trim();
    if (!q || pending[activeId]) return;
    var mode = _mode();
    var isDeep = mode === 'deep';
    var convId = activeId;
    var msgs = loadMsgs(convId);
    msgs.push({ role: 'user', text: q });
    var c = activeConv();
    if (c && msgs.length === 1) { c.title = q.slice(0, 10) + (q.length > 10 ? '…' : ''); saveConvs(); }
    // ★v2 SSE: 先 push streaming 占位 (实时更新 text = claude stdout)
    msgs.push({ role: 'brain', text: '思考中…', lowConf: false, warnings: [], streaming: true, via: mode });
    saveMsgs(convId, msgs);
    input.value = '';
    setPending(convId, true);
    renderMessages();

    var bodyObj = { question: q, conv: convId, mode: mode };
    // fast(直答无会话)/deep(DSH 无会话) 需打包近 10 条带走; standard 走 claude 自身 session 记忆不用打包
    if (mode !== 'standard') {
      bodyObj.history = msgs.filter(function (m) { return m.role === 'user' || m.role === 'brain'; })
        .slice(-11, -1)  // 去掉刚 push 的 streaming 占位
        .slice(-10)
        .map(function (m) { return { role: m.role === 'user' ? 'user' : 'assistant', content: m.text }; });
    }

    fetch('/api/research/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(bodyObj),
    }).then(async function (resp) {
      if (!resp.ok) throw new Error('SSE HTTP ' + resp.status);
      var reader = resp.body.getReader();
      var p3 = pending[convId];
      if (p3) p3.reader = reader;
      var decoder = new TextDecoder();
      var buf = '';
      var streamText = '';
      while (true) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buf += decoder.decode(chunk.value, { stream: true });
        var lines = buf.split('\n');
        buf = lines.pop();
        for (var i = 0; i < lines.length; i++) {
          if (!lines[i].startsWith('data: ')) continue;
          try {
            var d = JSON.parse(lines[i].slice(6));
            if (d.type === 'channel') {
              var p0 = pending[convId];
              if (p0) { p0.runId = d.run_id; renderStatus(); }
              continue;
            }
            if (d.type === 'line') {
              streamText += d.text + '\n';
              // 更新状态栏：截断防爆，跳过空行
              if (d.text && d.text.trim()) {
                var p2 = pending[convId];
                if (p2) {
                  p2.streamLine = d.text.length > 120 ? d.text.slice(0, 120) + '\u2026' : d.text;
                  if (convId === activeId) renderStatus();
                }
              }
              var m = loadMsgs(convId);
              var last = m[m.length - 1];
              if (last && last.streaming) {
                last.text = streamText;
                saveMsgs(convId, m);
                if (convId === activeId) renderMessages();
              }
            } else if (d.type === 'done') {
              var m2 = loadMsgs(convId);
              var last2 = m2[m2.length - 1];
              if (last2 && last2.streaming) {
                last2.text = d.answer || streamText || '(空回答)';
                last2.lowConf = !!d.low_confidence;
                last2.warnings = d.warnings || [];
                delete last2.streaming;
                saveMsgs(convId, m2);
              }
            }
          } catch (e) {}
        }
      }
    }).catch(function (e) {
      // SSE 失败 → 移除 streaming 占位 + 降级旧端点
      var m = loadMsgs(convId);
      if (m.length && m[m.length - 1].streaming) { m.pop(); saveMsgs(convId, m); }
      _fallbackSend(q, convId);
    }).then(function () {
      setPending(convId, false);
      if (convId === activeId) renderMessages();
    });
  }

  function _fallbackSend(q, convId) {
    // SSE 降级: 走旧同步端点 (带当前档位, fast 后端同样直答)
    fetch('/api/research/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: q, conv: convId, mode: _mode() }),
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var msgs2 = loadMsgs(convId);
        msgs2.push({ role: 'brain', text: d.answer || '(空回答)', lowConf: !!d.low_confidence, warnings: d.warnings || [] });
        saveMsgs(convId, msgs2);
      })
      .catch(function (e) {
        var msgs2 = loadMsgs(convId);
        msgs2.push({ role: 'brain', text: '调用失败: ' + e + '\n(server 未重启或大脑异常)', lowConf: true, warnings: [] });
        saveMsgs(convId, msgs2);
      });
  }

  function resetConv() {
    if (!confirm('重开「' + (activeConv() || {}).title + '」? 本会话记忆将清空 (其他会话不受影响)')) return;
    fetch('/api/research/chat/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conv: activeId }),
    }).catch(function () { /* 松耦合: 重置失败也清本地 */ });
    saveMsgs(activeId, []);
    renderMessages();
  }

  function initFolds() {
    /* 折叠记忆: 仅旧版 <details> 卡片适用 (2026-09-05 研究页全页化后 foldBrain
       已改普通 div, 不再折叠; 保留此函数兼容其它未来 details 卡片) */
    ['foldBrain'].forEach(function (id) {
      var el = $(id);
      if (!el || el.tagName !== 'DETAILS') return;
      if (localStorage.getItem('brain_fold_' + id) === '1') el.open = true;
      el.addEventListener('toggle', function () {
        localStorage.setItem('brain_fold_' + id, el.open ? '1' : '0');
      });
    });
  }

  function init() {
    if (!$('brainInput') || !$('brainMessages')) return;  // 卡片不存在则静默退出
    initFolds();
    loadConvs();
    renderAll();
    $('brainSendBtn').addEventListener('click', send);
    $('brainResetBtn').addEventListener('click', resetConv);
    /* 三档 radio 切换时刷新大白话注释 (无 brainModeHint 容器也无妨, 内部有守卫) */
    Array.prototype.forEach.call(document.querySelectorAll('input[name="brainMode"]'), function (r) {
      r.addEventListener('change', updateModeHint);
    });
    updateModeHint();
    var stopBtn = $('brainStopBtn');
    if (stopBtn) stopBtn.addEventListener('click', function () {
      var p = pending[activeId];
      if (!p || !p.runId) return;
      var m = loadMsgs(activeId);
      var last = m[m.length - 1];
      if (last && last.streaming) {
        last.text = '已停止: 本次深度思考被手动终止。';
        delete last.streaming;
        saveMsgs(activeId, m);
        renderMessages();
      }
      fetch('/api/research/chat/stop', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ run_id: p.runId }),
      }).catch(function () { /* 松耦合 */ });
      if (p.reader) p.reader.cancel().catch(function () {});
    });
    $('brainInput').addEventListener('keydown', function (e) { if (e.key === 'Enter') send(); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
