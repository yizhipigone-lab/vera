// ====== VERA Recover — 断网/超时自动恢复 ======
// 零 import 依赖。外部函数通过 deps 对象注入（seam 模式）。
// 浏览器: 作为普通 script 在 module 之前加载，module 初始化时填充 deps。
// Node 测试: 直接 require()，测试注入 mock deps。

const RECOVER = { MAX_RETRY: 5, INTERVAL_MS: 2000, MAX_WAIT_MS: 10000 };

const deps = {
  addLog: null,
  showToast: null,
  renderAllCharts: null,
  checkEngineVersion: null,
  fetchStatus: null,
  fetchLastResult: null,
  fetchResults: null,
  lastResultRef: null,    // { value: lastResult } — 写入 lastResultRef.value
  allTradesRef: null,     // { value: allTrades }
};

function setBtnStopMode(btn) {
  btn.disabled = false;
  btn.classList.remove('btn-primary');
  btn.classList.add('btn-danger');
  btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M6 6h12v12H6z"/></svg> 停止回测';
}

function setBtnRunMode(btn) {
  btn.disabled = false;
  btn.classList.remove('btn-danger');
  btn.classList.add('btn-primary');
  btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg> 执行回测';
}

function resetRunUI(btn, statusText, dotClass) {
  document.getElementById('progressBar').style.display = 'none';
  document.getElementById('progressText').textContent = '';
  document.getElementById('statusDot').className = 'status-dot ' + (dotClass || 'on');
  document.getElementById('statusText').textContent = statusText;
  setBtnRunMode(btn);
}

async function tryRecoverAbortedResult(cfg, originalErr) {
  try {
    var cfgStart = (cfg.start_time || '').toString();
    var cfgEnd = (cfg.end_time || '').toString();
    var cfgFml = (cfg.formula_name || '').toString();

    deps.addLog('尝试自动恢复：探测后端是否仍在运行/已落盘…', 'info');
    var btn = document.getElementById('btnRun');
    resetRunUI(btn, '自动恢复中…', 'busy');

    for (var i = 0; i < RECOVER.MAX_RETRY; i++) {
      try {
        var sr = await deps.fetchStatus();
        if (sr && sr.running) {
          deps.addLog('后端仍在运行（第 ' + (i + 1) + '/' + RECOVER.MAX_RETRY + ' 次探测，' + (sr.step || '') + ' ' + (sr.progress || 0) + '%）…', 'info');
          await new Promise(function(r) { setTimeout(r, RECOVER.INTERVAL_MS); });
          continue;
        }

        var lr = await deps.fetchLastResult();
        if (lr && lr.success && typeof lr.trade_count === 'number') {
          var isFresh = true;
          try {
            var idx = await deps.fetchResults();
            var top = Array.isArray(idx) && idx.length > 0 ? idx[0] : null;
            if (top) {
              var wantDr = cfgStart + '~' + cfgEnd;
              isFresh = (top.formula === cfgFml) && (top.date_range === wantDr);
            }
          } catch (_) {}

          if (isFresh) {
            deps.addLog('✓ 自动恢复成功：后端已完成回测（' + lr.trade_count + '笔交易）', 'ok');
            deps.showToast('✓ 已自动恢复回测结果（' + lr.trade_count + '笔交易）', 'ok');
            if (deps.lastResultRef) deps.lastResultRef.value = lr;
            if (deps.allTradesRef) deps.allTradesRef.value = lr.trades || [];
            deps.renderAllCharts(lr);
            deps.checkEngineVersion(lr);
            resetRunUI(btn, '就绪', 'on');
            return;
          } else {
            deps.addLog('last_result 不是本次 cfg 的结果（可能是更早的历史）— 判定为未生成', 'info');
            break;
          }
        }
        break;
      } catch (probeErr) {
        deps.addLog('探测失败（第 ' + (i + 1) + ' 次）：' + probeErr.message, 'info');
        await new Promise(function(r) { setTimeout(r, RECOVER.INTERVAL_MS); });
      }
    }

    var msg = originalErr && originalErr.name === 'AbortError'
      ? '请求超时（2小时），后端未在超时内落盘。请缩小回测区间或稍后到历史结果中查看。'
      : (originalErr && originalErr.message) || '未知错误';
    deps.addLog('网络错误: ' + msg, 'error');
    deps.showToast('网络错误: ' + msg, 'error');
    resetRunUI(btn, '错误', 'on');
  } catch (fatalErr) {
    console.error('[tryRecoverAbortedResult] 兜底失败:', fatalErr);
  }
}

// Browser: expose on window for ES module to import
if (typeof window !== 'undefined') {
  window.VeraRecover = { RECOVER, deps, tryRecoverAbortedResult, resetRunUI, setBtnStopMode, setBtnRunMode };
}

// Node.js test: CommonJS export
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { RECOVER, deps, tryRecoverAbortedResult, resetRunUI, setBtnStopMode, setBtnRunMode };
}
