// ====== VERA API Client ======
// 所有后端 HTTP 请求的单一入口。每个函数只做 fetch + JSON 解析，不处理业务逻辑。
// 统一返回 { success, data/error } 形状（与后端约定一致）。

// W2-2 (2026-09-05): 统一请求基座 — AbortController 默认 8s 超时 + r.ok 检查 +
// FastAPI detail 解析 (r.ok/detail 模式参考 trade.js 的 post; 原实现不查 r.ok,
// 后端 5xx 返回 HTML 错误页时 r.json() 抛 SyntaxError, 错误信息不可控)。
// 超时豁免两条路径: (1) 调用方自带 signal (如 submitBacktest, vera-ui 有 2h 控制器)
// (2) 显式传 timeout=null (如流式端点)。
export const DEFAULT_TIMEOUT_MS = 8000;
function request(method, u, body, extra, timeout = DEFAULT_TIMEOUT_MS) {
  const opt = {
    method,
    ...(body !== undefined ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}),
    ...extra,
  };
  let timer = null;
  if (timeout != null && !opt.signal) {
    const ctl = new AbortController();
    opt.signal = ctl.signal;
    timer = setTimeout(() => ctl.abort(), timeout);
  }
  return fetch(u, opt).then(r =>
    r.json().catch(() => { throw new Error('HTTP ' + r.status + ' (响应非 JSON)'); })
      .then(d => {
        if (!r.ok) {
          let msg = 'HTTP ' + r.status;
          if (d && d.detail) {
            msg = (typeof d.detail === 'string') ? d.detail
              : (Array.isArray(d.detail)
                ? d.detail.map(e => (e.loc ? e.loc.join('.') + ': ' : '') + e.msg).join('; ')
                : JSON.stringify(d.detail));
          }
          throw new Error(msg);
        }
        return d;
      })
  ).finally(() => { if (timer) clearTimeout(timer); });
}
const get = (u, timeout) => request('GET', u, undefined, undefined, timeout);
const post = (u, body, extra, timeout) => request('POST', u, body, extra, timeout);
const del = u => request('DELETE', u);
const put = (u, body, extra, timeout) => request('PUT', u, body, extra, timeout);

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

// ── 公式农场 (2026-09-06 三段闸门; 2026-09-11 补第四闸门「定量复核」) ──

export const fetchFarmStatus = () => get('/api/farm/status');
export const farmCheck = () => post('/api/farm/check', {});
export const farmOnboard = () => post('/api/farm/onboard', {});
export const farmVerify = () => post('/api/farm/verify', {});
export const farmBacktest = () => post('/api/farm/backtest', {});
export const farmStop = () => post('/api/farm/stop', {});
export const fetchFarmReports = () => get('/api/farm/reports');
export const fetchFarmReport = file => get('/api/farm/report?file=' + encodeURIComponent(file));
export const fetchFarmLog = gate => get('/api/farm/log?gate=' + encodeURIComponent(gate));
export const fetchFarmPrefill = gs => get('/api/farm/prefill?gs=' + encodeURIComponent(gs));

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
// 流式端点: 回答可持续数分钟 (深度思考档), 豁免默认超时
export const postResearchChatStream = body => post('/api/research/chat/stream', body, undefined, null);
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
