// ====== VERA API Client ======
// 所有后端 HTTP 请求的单一入口。每个函数只做 fetch + JSON 解析，不处理业务逻辑。
// 统一返回 { success, data/error } 形状（与后端约定一致）。

// ── 回测管线 ──

export async function fetchStatus() {
  const r = await fetch('/api/status');
  return r.json();
}

export async function submitBacktest(config, signal) {
  const r = await fetch('/api/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config),
    signal: signal || undefined,
  });
  return r.json();
}

export async function stopBacktest() {
  const r = await fetch('/api/stop', { method: 'POST' });
  return r.json();
}

export async function fetchLastResult() {
  const r = await fetch('/api/last_result');
  return r.json();
}

export async function fetchResults() {
  const r = await fetch('/api/results');
  return r.json();
}

export async function fetchResult(id) {
  const r = await fetch('/api/results/' + encodeURIComponent(id));
  return r.json();
}

// ── 配置管理 ──

export async function fetchConfigDefaults() {
  const r = await fetch('/api/config/defaults');
  return r.json();
}

export async function saveConfig(config) {
  const r = await fetch('/api/config/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config),
  });
  return r.json();
}

export async function fetchSavedConfig() {
  const r = await fetch('/api/config/saved');
  return r.json();
}

export async function deleteSavedConfig() {
  const r = await fetch('/api/config/saved', { method: 'DELETE' });
  return r.json();
}

// ── 数据 ──

export async function fetchSectors() {
  const r = await fetch('/api/sectors');
  return r.json();
}

export async function fetchFactorRules(formula) {
  const r = await fetch('/api/factor-rules?formula=' + encodeURIComponent(formula));
  return r.json();
}

// ── 公式体检 ──

export async function submitLabJob(body) {
  const r = await fetch('/api/lab/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return r.json();
}

export async function fetchLabStatus() {
  const r = await fetch('/api/lab/status');
  return r.json();
}

export async function fetchLabHistory() {
  const r = await fetch('/api/lab/history');
  return r.json();
}

export async function fetchLabReport(formula) {
  const r = await fetch('/api/lab/report?formula=' + encodeURIComponent(formula));
  return r.json();
}
