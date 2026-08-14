// ====== deep_math 纯函数契约测试 (node 直接跑, 零框架依赖) ======
// 用法: node tests/web/test_deep_math.mjs
// 目的: 锁死图表深挖包 (水下曲线 + 蒙特卡洛扇面) 的数学口径。
//   收益率口径: 净口径 = pnl / (entry_price * shares), pnl 缺失回退 profit_pct/return (小数)。
import assert from 'node:assert/strict';
import { computeDrawdown, tradeNetReturns, quantile, monteCarloFan, chooseScale, formatMoney, klineWindow, toCandleRows } from '../../web/js/deep_math.mjs';

let _pass = 0, _fail = 0;
function case_(desc, got, want) {
  try {
    assert.equal(got, want);
    _pass++;
  } catch (e) {
    _fail++;
    console.error(`[FAIL] ${desc}\n  got:  ${JSON.stringify(got)}\n  want: ${JSON.stringify(want)}`);
  }
}
function caseDeep(desc, got, want) {
  try {
    assert.deepEqual(got, want);
    _pass++;
  } catch (e) {
    _fail++;
    console.error(`[FAIL] ${desc}\n  got:  ${JSON.stringify(got)}\n  want: ${JSON.stringify(want)}`);
  }
}

// ── computeDrawdown: 每点 dd = 当前值/历史峰值 - 1 (≤0) ──
caseDeep('回撤序列 [100,120,90,130]', computeDrawdown([100, 120, 90, 130]), [0, 0, -0.25, 0]);
caseDeep('回撤空数组 → []', computeDrawdown([]), []);
caseDeep('回撤单点 → [0]', computeDrawdown([50]), [0]);
// 单调下跌: 峰值一直是首点
caseDeep('回撤单调下跌', computeDrawdown([100, 50]), [0, -0.5]);

// ── tradeNetReturns: 净口径 pnl/(entry_price*shares), 缺失回退 ──
// (不传权益序列 = 旧口径兜底, 实盘侧将来可能不传权益)
caseDeep('pnl 正常 → pnl/(价*量)', tradeNetReturns([{ pnl: 100, entry_price: 10, shares: 100 }]), [0.1]);
caseDeep('pnl 为 null → 回退 profit_pct', tradeNetReturns([{ pnl: null, profit_pct: 0.05, entry_price: 10, shares: 100 }]), [0.05]);
caseDeep('pnl 为 NaN → 回退 profit_pct', tradeNetReturns([{ pnl: NaN, profit_pct: 0.05, entry_price: 10, shares: 100 }]), [0.05]);
caseDeep('pnl/profit_pct 都缺 → 回退 return', tradeNetReturns([{ pnl: null, profit_pct: null, return: -0.03, entry_price: 10, shares: 100 }]), [-0.03]);
caseDeep('成交额为 0 的脏数据跳过', tradeNetReturns([{ pnl: 50, entry_price: 0, shares: 100 }]), []);
caseDeep('shares 为 0 也跳过', tradeNetReturns([{ pnl: 50, entry_price: 10, shares: 0 }]), []);
caseDeep('trades 空数组 → []', tradeNetReturns([]), []);
// 混合: 好数据保留, 脏数据跳过
caseDeep('混合好坏数据', tradeNetReturns([
  { pnl: 200, entry_price: 10, shares: 100 },
  { pnl: 50, entry_price: 0, shares: 100 },
  { pnl: null, profit_pct: -0.02, entry_price: 5, shares: 200 },
]), [0.2, -0.02]);

// ── tradeNetReturns 新口径: 单笔收益率 = pnl / 入场时点总权益 ──
// 旧口径 = 假设每笔全仓一只票, 复利虚高几个数量级; 新口径 = 收益占当时总资金的比例
const EQ1M = [
  { date: '2024-01-01', equity: 1000000 },
  { date: '2024-01-10', equity: 1200000 },
];
caseDeep('新口径: 权益100万, pnl=1000 → 0.001',
  tradeNetReturns([{ pnl: 1000, entry_date: '2024-01-05', entry_price: 10, shares: 100 }], EQ1M), [0.001]);
caseDeep('新口径: 入场日=第二点当天(带时间) → 用120万',
  tradeNetReturns([{ pnl: 1200, entry_date: '2024-01-10 09:30:00', entry_price: 10, shares: 100 }], EQ1M), [0.001]);
caseDeep('新口径: 入场日早于所有权益点 → 用第一个点',
  tradeNetReturns([{ pnl: 1000, entry_date: '2023-12-01', entry_price: 10, shares: 100 }], EQ1M), [0.001]);
caseDeep('新口径: 权益点为 0 → 该笔跳过',
  tradeNetReturns([{ pnl: 1000, entry_date: '2024-01-01' }], [{ date: '2024-01-01', equity: 0 }]), []);
caseDeep('新口径: pnl 缺失 → (profit_pct×仓位金额)/入场权益',
  tradeNetReturns([{ pnl: null, profit_pct: 0.02, entry_price: 10, shares: 100, entry_date: '2024-01-05' }], EQ1M),
  [(0.02 * 1000) / 1000000]);
caseDeep('equityPoints 空数组 → 回退旧口径',
  tradeNetReturns([{ pnl: 100, entry_date: '2024-01-05', entry_price: 10, shares: 100 }], []), [0.1]);
caseDeep('equityPoints 未传 → 回退旧口径',
  tradeNetReturns([{ pnl: 100, entry_price: 10, shares: 100 }], undefined), [0.1]);

// ── quantile: 线性插值分位 ──
case_('分位 q=0 → 最小值', quantile([1, 2, 3, 4, 5], 0), 1);
case_('分位 q=1 → 最大值', quantile([1, 2, 3, 4, 5], 1), 5);
case_('分位 q=0.5 奇数个 → 中位数', quantile([1, 2, 3, 4, 5], 0.5), 3);
case_('分位 q=0.5 偶数个 → 插值', quantile([1, 2, 3, 4], 0.5), 2.5);
case_('分位 q=0.25 插值', quantile([0, 10, 20, 30], 0.25), 7.5);
case_('分位单元素', quantile([42], 0.95), 42);

// ── monteCarloFan: bootstrap 收益扇面 ──
// 固定 seed 确定性: 同 seed 两次结果全等
const r1 = monteCarloFan([0.01, -0.02, 0.03, 0.005, -0.01], { sims: 200, seed: 42 });
const r2 = monteCarloFan([0.01, -0.02, 0.03, 0.005, -0.01], { sims: 200, seed: 42 });
caseDeep('同 seed 两次模拟结果全等', r1, r2);

// 恒定收益: 所有模拟路径相同, 每个分位都等于 1.01^step (从 1.0 复利)
const constFan = monteCarloFan(Array(10).fill(0.01), { sims: 100, seed: 7 });
case_('恒定收益: 路径长度 = 笔数', constFan.steps.length, 10);
case_('恒定收益: step 从 1 开始', constFan.steps[0].step, 1);
{
  let v = 1, ok = true;
  for (let i = 0; i < 10; i++) {
    v *= 1.01;  // 与实现同序累乘, 浮点精确可比
    const s = constFan.steps[i];
    if (s.p5 !== v || s.p25 !== v || s.p50 !== v || s.p75 !== v || s.p95 !== v) ok = false;
  }
  case_('恒定收益: 所有分位 = 1.01^step', ok, true);
}
case_('sampleSize = 交易笔数', constFan.sampleSize, 10);

// 返回结构
caseDeep('返回结构键名', Object.keys(constFan.steps[0]).sort(), ['p25', 'p5', 'p50', 'p75', 'p95', 'step']);

// 全正收益: 任何路径都上涨, p5 末步 > 1
const posFan = monteCarloFan([0.01, 0.02, 0.03, 0.015], { sims: 100, seed: 1 });
case_('全正收益: 末步 p5 > 1', posFan.steps[posFan.steps.length - 1].p5 > 1, true);

// 空输入 → steps 为空
caseDeep('returns 为空 → steps 为空', monteCarloFan([], { sims: 100, seed: 1 }).steps, []);

// 小样本标记: <20 笔 lowSample=true, ≥20 为 false
case_('样本 <20 → lowSample=true', monteCarloFan(Array(10).fill(0.01), { seed: 1 }).lowSample, true);
case_('样本 =20 → lowSample=false', monteCarloFan(Array(20).fill(0.01), { seed: 1 }).lowSample, false);

// ── chooseScale: 线性为默认, 对数为保险 ──
// p95max / p5min > 50 → log (防极端策略扇面在线性轴压成一条线, 2026-08-13 事故复盘)
case_('比值 20 → linear', chooseScale(0.5, 10), 'linear');
case_('比值 60 → log', chooseScale(0.5, 30), 'log');
case_('比值恰好 50 → linear (边界)', chooseScale(1, 50), 'linear');
case_('p5min = 0 → log (安全兜底)', chooseScale(0, 10), 'log');
case_('NaN 输入 → log (安全兜底)', chooseScale(NaN, 10), 'log');
case_('p95max 非有限 → log', chooseScale(0.5, Infinity), 'log');

// ── 合理性锚点: 新口径放大链路 ──
// 5 笔交易, 每笔 pnl = 入场权益的 1% → 收益率 0.01/笔 → 中位终点应 = 1.01^5
{
  const eqPts = [{ date: '2024-01-01', equity: 1000000 }];
  const trades5 = [1, 2, 3, 4, 5].map(i => ({ pnl: 10000, entry_date: '2024-01-0' + i, entry_price: 10, shares: 100 }));
  const rets = tradeNetReturns(trades5, eqPts);
  caseDeep('锚点: 每笔收益率 = pnl/权益 = 0.01', rets, [0.01, 0.01, 0.01, 0.01, 0.01]);
  const fan5 = monteCarloFan(rets, { sims: 100, seed: 7 });
  let v = 1; for (let i = 0; i < 5; i++) v *= 1.01;  // 与实现同序累乘, 浮点精确可比
  case_('锚点: 中位终点 = 1.01^5', fan5.steps[4].p50, v);
}

// ── formatMoney: 元的友好格式化 (万/亿简写, 归因图 tooltip/标签用) ──
case_('1.2万+ → 万简写', formatMoney(12345.67), '1.23万');
case_('负数 → -4.5万 (去尾随0)', formatMoney(-45000), '-4.5万');
case_('亿简写', formatMoney(123456789), '1.23亿');
case_('负亿简写', formatMoney(-250000000), '-2.5亿');
case_('1万整 → 1万 (去尾随 .00)', formatMoney(10000), '1万');
case_('不足万 → 整数千分位', formatMoney(4567.89), '4,568');
case_('不足万负数 → 千分位', formatMoney(-1234.4), '-1,234');
case_('0 → 0', formatMoney(0), '0');
case_('9999 不满万不简写', formatMoney(9999), '9,999');
case_('null → -', formatMoney(null), '-');
case_('NaN → -', formatMoney(NaN), '-');
case_('undefined → -', formatMoney(undefined), '-');

// ── klineWindow (Phase 3): 入场前 90 天 ~ 出场后 15 天的 K 线上下文区间 ──
// 手算锚点: 2024-01-05 往前 90 天 = 2023-10-07; 2024-03-01 往后 15 天 = 2024-03-16
caseDeep('基本: 入场前90天(跨年)', klineWindow('2024-01-05', '2024-03-01'), { start: '2023-10-07', end: '2024-03-16' });
caseDeep('自定义前后天数', klineWindow('2024-06-10', '2024-06-20', 10, 5), { start: '2024-05-31', end: '2024-06-25' });
caseDeep('带时间后缀的 ISO 字符串', klineWindow('2024-01-05 09:30:00', '2024-03-01 15:00:00'), { start: '2023-10-07', end: '2024-03-16' });
caseDeep('出场缺失 → 以入场日兜底', klineWindow('2024-06-10', ''), { start: '2024-03-12', end: '2024-06-25' });
caseDeep('闰年 2 月: 2024-03-01 前 10 天 = 02-20', klineWindow('2024-03-01', '2024-03-01', 10, 0), { start: '2024-02-20', end: '2024-03-01' });
case_('入场日期非法 → null', klineWindow('foo', '2024-01-01'), null);
case_('入场日期空 → null', klineWindow('', ''), null);

// ── toCandleRows (Phase 3): 后端 K 线行 → ECharts candlestick 数据 ──
const KROWS = [
  { date: '2024-01-05', open: 10.0, high: 10.5, low: 9.9, close: 10.2, volume: 123456.0 },
  { date: '2024-01-06', open: 10.2, high: 10.3, low: 10.0, close: 10.1, volume: 99999.0 },
];
const K = toCandleRows(KROWS);
caseDeep('dates 提取 (前10字符)', K.dates, ['2024-01-05', '2024-01-06']);
caseDeep('candles = [open,close,low,high] (ECharts 约定, 非 OHLC 顺序)', K.candles, [[10.0, 10.2, 9.9, 10.5], [10.2, 10.1, 10.0, 10.3]]);
caseDeep('volumes = [量, 涨跌方向] 涨1跌-1', K.volumes, [[123456.0, 1], [99999.0, -1]]);
caseDeep('收=开 记为涨方向(1)', toCandleRows([{ date: '2024-01-05', open: 10, high: 10.1, low: 9.9, close: 10, volume: 5 }]).volumes, [[5, 1]]);
caseDeep('volume 缺失 → 0', toCandleRows([{ date: '2024-01-05', open: 10, high: 10.5, low: 9.9, close: 10.2 }]).volumes, [[0, 1]]);
caseDeep('价格非有限的行跳过 (防御)', toCandleRows([KROWS[0], { date: '2024-01-07', open: NaN, high: 1, low: 1, close: 1, volume: 1 }]).dates, ['2024-01-05']);
caseDeep('空输入 → 空结构', toCandleRows([]), { dates: [], candles: [], volumes: [] });
caseDeep('null 输入 → 空结构', toCandleRows(null), { dates: [], candles: [], volumes: [] });

console.log(`[OK] test_deep_math: ${_pass} passed, ${_fail} failed`);
process.exit(_fail ? 1 : 0);
