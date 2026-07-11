// 单元测试: tryRecoverAbortedResult 里的 freshness 判定逻辑
// 复刻 vera-ui.js 里的判定核心, 用 mock fetch 验证 4 个场景
// 用法: node tests/js/test_recover_freshness.js

let pass = 0, fail = 0;
function assert(cond, label) {
  if (cond) { console.log(`  ✓ ${label}`); pass++; }
  else      { console.log(`  ✗ ${label}`); fail++; }
}

// ── 复刻 vera-ui.js 里的判定 ──
// 输入: cfg, statusResponse, lastResultResponse, indexResponse
// 输出: 'recovered' | 'mismatch' | 'no-result' | 'still-running'
function decide(cfg, statusResp, lastResultResp, indexResp) {
  if (statusResp && statusResp.running) return 'still-running';
  if (!(lastResultResp && lastResultResp.success && typeof lastResultResp.trade_count === 'number')) return 'no-result';
  const cfgStart = cfg.start_time, cfgEnd = cfg.end_time, cfgFml = cfg.formula_name;
  if (indexResp && indexResp.length > 0) {
    const top = indexResp[0];
    const wantDr = `${cfgStart}~${cfgEnd}`;
    const isFresh = (top.formula === cfgFml) && (top.date_range === wantDr);
    if (!isFresh) return 'mismatch';
  }
  return 'recovered';
}

const cfg = { start_time: '20240101', end_time: '20240601', formula_name: 'MYFRML' };
const okResult = { success: true, trade_count: 12, metrics: {} };
const okIndex = [{ formula: 'MYFRML', date_range: '20240101~20240601', trade_count: 12 }];
const staleIndex = [{ formula: 'OLDFML', date_range: '20230101~20231231', trade_count: 5 }];

// ── 测试 ──
console.log('\n[1] 后端还在跑');
assert(decide(cfg, { running: true, progress: 50 }, null, null) === 'still-running', '应判定 still-running，不渲染');

console.log('\n[2] 跑完 + last_result 是本次 cfg');
assert(decide(cfg, { running: false }, okResult, okIndex) === 'recovered', '应判定 recovered，渲染结果');

console.log('\n[3] 跑完 + last_result 是更早的旧结果');
assert(decide(cfg, { running: false }, okResult, staleIndex) === 'mismatch', '应判定 mismatch，不渲染旧结果');

console.log('\n[4] 后端挂了/没落盘 + last_result 不存在');
assert(decide(cfg, { running: false }, null, null) === 'no-result', '应判定 no-result');

console.log('\n[5] 索引拿不到 + last_result 是 success');
assert(decide(cfg, { running: false }, okResult, null) === 'recovered', '应保守放过，按 recovered 处理');

console.log('\n[6] last_result.success=false (本次跑挂了)');
assert(decide(cfg, { running: false }, { success: false, error: 'xxx' }, okIndex) === 'no-result', '应判定 no-result');

console.log(`\n=== ${pass} passed, ${fail} failed ===`);
process.exit(fail > 0 ? 1 : 0);