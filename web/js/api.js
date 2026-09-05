// ====== VERA API Client ======
// 所有后端 HTTP 请求的单一入口。每个函数只做 fetch + JSON 解析，不处理业务逻辑。
// 统一返回 { success, data/error } 形状（与后端约定一致）。

const json = p => p.then(r => r.json());
const get = u => json(fetch(u));
const post = (u, body, extra) => json(fetch(u, {
  method: 'POST',
  ...(body !== undefined ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}),
  ...extra,
}));
const del = u => json(fetch(u, { method: 'DELETE' }));
const put = (u, body, extra) => json(fetch(u, {
  method: 'PUT',
  ...(body !== undefined ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}),
  ...extra,
}));

// ── 回测管线 ──

export const fetchStatus = () => get('/api/status');
export const submitBacktest = (config, signal) => post('/api/run', config, { signal: signal || undefined });
export const stopBacktest = () => post('/api/stop');
export const fetchLastResult = () => get('/api/last_result');
export const fetchResults = () => get('/api/results');
export const fetchResult = id => get('/api/results/' + encodeURIComponent(id));

// ── 配置管理 ──

export const fetchConfigDefaults = () => get('/api/config/defaults');
export const saveConfig = config => post('/api/config/save', config);
export const fetchSavedConfig = () => get('/api/config/saved');
export const deleteSavedConfig = () => del('/api/config/saved');

// ── 数据 ──

export const fetchSectors = () => get('/api/sectors');
export const fetchFactorRules = formula => get('/api/factor-rules?formula=' + encodeURIComponent(formula));

// ── 公式体检 ──

export const submitLabJob = body => post('/api/lab/run', body);
export const stopLabJob = () => post('/api/lab/stop');
export const fetchLabStatus = () => get('/api/lab/status');
export const fetchLabHistory = () => get('/api/lab/history');
export const fetchLabReport = formula => get('/api/lab/report?formula=' + encodeURIComponent(formula));

// ── 数据准备 / 分析 (治理III W4-e 补全, 2026-09-05) ──

export const fetchCalendar = (year = 0, month = 0) =>
  get('/api/calendar?year=' + year + '&month=' + month);
export const fetchBenchmarkHistory = (params) => {
  const qs = Object.entries(params || {}).map(
    ([k, v]) => k + '=' + encodeURIComponent(String(v))).join('&');
  return get('/api/benchmark/history' + (qs ? '?' + qs : ''));
};
export const fetchStockKline = params => {
  const qs = Object.entries(params || {}).map(
    ([k, v]) => k + '=' + encodeURIComponent(String(v))).join('&');
  return get('/api/stock/kline' + (qs ? '?' + qs : ''));
};

// ── 数据准备 TAB (data_cache.js classic 页经全局桥用) ──

export const fetchDataCacheStatus = () => get('/api/data_cache/status');
export const fetchDataCacheLog = (tail = 60) =>
  get('/api/data_cache/log?tail=' + tail);
export const submitDataCacheBackfill = body => post('/api/data_cache/backfill', body);

// ── 研究对话 (研究 TAB; stream 端点走 fetch 流式, 此处给 stop/reset/
// 标准 chat —— brain_chat.js 迁流式时保留) ──

export const postResearchChat = body => post('/api/research/chat', body);
export const postResearchChatStream = body => post('/api/research/chat/stream', body);
// 后端 reset/stop 均为必填 body (dict); 缺省 {} 防无 body 调用 422,
// 调用方仍需按其契约带 conv / {run_id} (brain_chat 迁移时)。
export const resetResearchChat = (body = {}) => post('/api/research/chat/reset', body);
export const stopResearchChat = (body = {}) => post('/api/research/chat/stop', body);
// 8081 交易 API (跨源): 基址工厂 + 预置客户端 (analysis.js/trade.js 迁移用)。
// 页面由 8080 serve、打 8081 属跨源, 依赖后端 CORS 放行 (同既有裸 fetch)。
export const createTradeClient = (base) => ({
  get: u => get(base + u),
  post: (u, body) => post(base + u, body),
  put: (u, body) => put(base + u, body),
});
