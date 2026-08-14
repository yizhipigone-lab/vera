// ====== 图表深挖包 · 纯数学层 ======
// 零 DOM 依赖, node 可直接测 (tests/web/test_deep_math.mjs)。
// 被 charts_deep.js (ECharts 组装层) import, 口径改动必须先改测试。

/** 最多 2 位小数并去掉尾随 0 和小数点 (formatMoney 内部用) */
function _trim2(n) {
  return n.toFixed(2).replace(/\.?0+$/, '');
}

/**
 * 金额大白话格式化 (单位元): 满万用「万」、满亿用「亿」简写, 不足万取整千分位。
 * 归因图 tooltip/标签用 —— 裸报 1234567.89 元没人数得清几位数。
 * 例: 12345.67 → '1.23万'; -45000 → '-4.5万'; 123456789 → '1.23亿';
 *     4567.89 → '4,568'; 非法输入 (null/NaN/非数) → '-'。
 * @param {number} v 金额 (元)
 * @returns {string} 友好文本
 */
export function formatMoney(v) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '-';
  const abs = Math.abs(v);
  if (abs >= 1e8) return _trim2(v / 1e8) + '亿';
  if (abs >= 1e4) return _trim2(v / 1e4) + '万';
  return Math.round(v).toLocaleString('en-US');
}

/**
 * 计算回撤序列: 每点 dd = 当前值 / 历史峰值 - 1 (≤0)。
 * 与后端 equity.drawdown 同口径, 用于前端缺 drawdown 字段时兜底。
 * @param {number[]} equityValues 权益值序列 (如 [100, 120, 90, 130])
 * @returns {number[]} 回撤小数序列 (如 [0, 0, -0.25, 0]), 空输入返回 []
 */
export function computeDrawdown(equityValues) {
  if (!equityValues || equityValues.length === 0) return [];
  let peak = -Infinity;
  return equityValues.map(v => {
    if (v > peak) peak = v;
    return peak > 0 ? v / peak - 1 : 0;
  });
}

/**
 * 查入场时点的总权益: 按日期前缀 (前 10 字符) 找「entry_date 当天或之前最近一个」权益点。
 * 权益点假定按时间升序 (回测 equity 序列天然升序), 用二分; 找不到当天就向前取最近交易日;
 * 入场日早于所有权益点时用第一个点 (回测起点权益 ≈ 初始资金, 误差可接受)。
 * @param {Array<{date:string, equity:number}>} points 权益点 (升序)
 * @param {string} entryDate 交易入场时间 ('2024-01-05' 或 '2024-01-05 10:35:00')
 * @returns {number} 匹配到的权益值; 取不到返回 NaN
 */
function _equityAt(points, entryDate) {
  const d = String(entryDate || '').slice(0, 10);
  let lo = 0, hi = points.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const md = String(points[mid].date || '').slice(0, 10);
    if (md <= d) { ans = mid; lo = mid + 1; } else { hi = mid - 1; }
  }
  const p = ans >= 0 ? points[ans] : points[0];
  const v = p ? p.equity : NaN;
  return typeof v === 'number' ? v : NaN;
}

/**
 * 提取单笔交易收益率序列 (蒙特卡洛输入)。
 * 新口径 (传了 equityPoints): 单笔收益率 = pnl / 入场时点总权益。
 *   为什么: 旧口径 pnl/仓位金额 等于假设每笔全仓一只票, 而真实策略同时持多只票、
 *   单票只占部分资金 —— 按仓位复利会把总收益放大几个数量级 (1351 笔中位数算出几万倍)。
 *   新口径把每笔盈亏折算成「占当时总资金的比例」再复利, 量级与真实回测终点一致。
 *   pnl 缺失/非有限时回退 (profit_pct || return) × entry_price × shares 当 pnl 用。
 *   匹配到的权益 ≤0 或非有限 → 该笔跳过。
 * 旧口径兜底 (equityPoints 为空/未传): 单笔收益率 = pnl / (entry_price × shares),
 *   pnl 缺失回退 profit_pct, 再缺回退 return; 仓位金额为 0 的脏数据跳过。
 *   保留此路径是因为实盘侧将来可能不传权益序列, 函数必须健壮。
 * 注意: pnl 已扣卖出侧佣金+印花税+滑点, 但未扣买入侧费用 (后端口径, 图表副标题需注明)。
 * @param {Array<Object>} trades 后端透传的交易记录
 * @param {Array<{date:string, equity:number}>} [equityPoints] 权益序列 (bar 级或日级, 升序)
 * @returns {number[]} 单笔收益率小数序列, 空输入返回 []
 */
export function tradeNetReturns(trades, equityPoints) {
  if (!trades || trades.length === 0) return [];
  const useEquity = !!(equityPoints && equityPoints.length > 0);
  const out = [];
  for (const t of trades) {
    if (useEquity) {
      let pnl = t.pnl;
      if (typeof pnl !== 'number' || !Number.isFinite(pnl)) {
        const fb = t.profit_pct != null ? t.profit_pct : t.return;
        const amount = (t.entry_price || 0) * (t.shares || 0);
        if (typeof fb !== 'number' || !Number.isFinite(fb) || amount <= 0) continue;
        pnl = fb * amount;
      }
      const eq = _equityAt(equityPoints, t.entry_date);
      if (!Number.isFinite(eq) || eq <= 0) continue;
      const r = pnl / eq;
      if (Number.isFinite(r)) out.push(r);
    } else {
      const amount = (t.entry_price || 0) * (t.shares || 0);
      if (amount === 0) continue;  // 脏数据: 无法算收益率, 跳过
      let r;
      if (typeof t.pnl === 'number' && Number.isFinite(t.pnl)) {
        r = t.pnl / amount;
      } else {
        const fb = t.profit_pct != null ? t.profit_pct : t.return;
        if (typeof fb !== 'number' || !Number.isFinite(fb)) continue;
        r = fb;
      }
      if (Number.isFinite(r)) out.push(r);
    }
  }
  return out;
}

/**
 * 线性插值分位数 (输入必须已升序排序)。
 * pos = q * (n-1), 落在两元素之间时按比例插值。
 * @param {number[]} sortedArr 已升序排序的数组
 * @param {number} q 分位点, 0~1
 * @returns {number} 分位值; 空数组返回 NaN
 */
export function quantile(sortedArr, q) {
  const n = sortedArr.length;
  if (n === 0) return NaN;
  if (n === 1) return sortedArr[0];
  const pos = q * (n - 1);
  const lo = Math.floor(pos), hi = Math.ceil(pos);
  if (lo === hi) return sortedArr[lo];
  const frac = pos - lo;
  return sortedArr[lo] + (sortedArr[hi] - sortedArr[lo]) * frac;
}

/**
 * 选择扇面图纵轴刻度: 线性为默认, 对数为保险。
 * 用途: 防止极端策略的扇面在线性轴上压成一条线 (2026-08-13 事故复盘:
 *   旧仓位口径下 P95 冲到几百万%, 线性轴上中位线贴着底部看不见)。
 * 规则: p95max / p5min > 50 → 'log', 否则 'linear'; 恰好 50 算 linear;
 *   非有限或 ≤0 输入一律 'log' (安全兜底, log 轴配合 >0 下限不会崩)。
 * @param {number} p5min 扇面最外圈最小值 (净值倍数, 正常恒 >0)
 * @param {number} p95max 扇面最外圈最大值 (净值倍数)
 * @returns {'linear'|'log'} 纵轴模式
 */
export function chooseScale(p5min, p95max) {
  if (!Number.isFinite(p5min) || !Number.isFinite(p95max) || p5min <= 0 || p95max <= 0) return 'log';
  return p95max / p5min > 50 ? 'log' : 'linear';
}

/**
 * mulberry32 种子随机数发生器 (确定性, 测试可复现)。
 * 固定 seed → 固定序列, 保证同一批交易每次刷新页面扇面图一致。
 * @param {number} seed 整数种子
 * @returns {function(): number} 返回 [0, 1) 均匀随机数的函数
 */
function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/**
 * 蒙特卡洛收益扇面: bootstrap 有放回重抽样交易收益率, 复利成路径, 逐 step 取分位。
 * 警告: 假设各笔交易独立同分布 (i.i.d.) —— 真实交易有连续盈亏的聚集性,
 *   扇面展示的是"如果未来交易和过去同类"的分布范围, 不是预测。
 * @param {number[]} returns 单笔收益率小数序列 (见 tradeNetReturns)
 * @param {{sims?: number, seed?: number}} [opts] sims 模拟次数 (默认 1000), seed 随机种子 (默认 42)
 * @returns {{steps: Array<{step:number, p5:number, p25:number, p50:number, p75:number, p95:number}>,
 *            sampleSize: number, lowSample: boolean}}
 *   steps[k] 是第 k+1 笔交易后跨所有模拟的累计净值分位 (从 1.0 起算);
 *   returns 为空时 steps 为 []; sampleSize < 20 时 lowSample=true (仅供参考)。
 */
export function monteCarloFan(returns, opts) {
  const { sims = 1000, seed = 42 } = opts || {};
  const n = returns ? returns.length : 0;
  const result = { steps: [], sampleSize: n, lowSample: n < 20 };
  if (n === 0) return result;

  const rand = mulberry32(seed);
  // paths[s][k] = 第 s 个模拟第 k 笔交易后的累计净值 (从 1.0 复利)
  const paths = [];
  for (let s = 0; s < sims; s++) {
    const path = new Array(n);
    let v = 1;
    for (let k = 0; k < n; k++) {
      const r = returns[Math.floor(rand() * n)];  // 有放回随机抽一笔
      v *= 1 + r;
      path[k] = v;
    }
    paths.push(path);
  }

  for (let k = 0; k < n; k++) {
    const col = paths.map(p => p[k]).sort((a, b) => a - b);
    result.steps.push({
      step: k + 1,
      p5: quantile(col, 0.05),
      p25: quantile(col, 0.25),
      p50: quantile(col, 0.50),
      p75: quantile(col, 0.75),
      p95: quantile(col, 0.95),
    });
  }
  return result;
}

// ── Phase 3: 单笔交易 K 线回放 ──

/** 取 ISO 日期前 10 字符并校验 YYYY-MM-DD, 非法返回 null */
function _isoDay(s) {
  const d = String(s == null ? '' : s).slice(0, 10);
  return /^\d{4}-\d{2}-\d{2}$/.test(d) ? d : null;
}

/** 日期平移 (UTC 计算, 跨年/跨月/闰年安全), 输入输出均为 YYYY-MM-DD */
function _shiftDay(day, delta) {
  const d = new Date(day + 'T00:00:00Z');
  d.setUTCDate(d.getUTCDate() + delta);
  return d.toISOString().slice(0, 10);
}

/**
 * K 线回放取数区间: 入场前 beforeDays 天 ~ 出场后 afterDays 天 (日历日, 含非交易日)。
 * 为什么要上下文: 只看持仓段几根 K 线看不出"买点是不是追在山顶" ——
 *   前 90 天给趋势背景, 后 15 天给"卖完又涨/跌了"的复盘视角。
 * @param {string} entryDate 入场日 ('2024-01-05' 或带时间后缀 '2024-01-05 09:30:00')
 * @param {string} exitDate 出场日 (缺失/非法时以入场日兜底)
 * @param {number} [beforeDays=90] 入场前日历天数
 * @param {number} [afterDays=15] 出场后日历天数
 * @returns {{start:string, end:string}|null} YYYY-MM-DD 区间; 入场日非法返回 null
 */
export function klineWindow(entryDate, exitDate, beforeDays, afterDays) {
  const b = beforeDays == null ? 90 : beforeDays;
  const a = afterDays == null ? 15 : afterDays;
  const entry = _isoDay(entryDate);
  if (!entry) return null;
  const exit = _isoDay(exitDate) || entry;
  return { start: _shiftDay(entry, -b), end: _shiftDay(exit, a) };
}

/**
 * 后端 K 线行 → ECharts candlestick 数据 (K 线回放弹窗用)。
 * candles[i] = [open, close, low, high] —— 注意是 ECharts candlestick 约定顺序, 不是 OHLC;
 * volumes[i] = [volume, 涨跌方向], 方向 1 = 收≥开 (A股红), -1 = 收<开 (绿), 供涨跌着色;
 * 价格非有限的行跳过 (契约保证无 NaN, 这里是防御); 输入升序原样保留。
 * @param {Array<{date:string, open:number, high:number, low:number, close:number, volume:number}>} rows
 * @returns {{dates:string[], candles:number[][], volumes:number[][]}} 等长三数组, 空输入给空结构
 */
export function toCandleRows(rows) {
  const out = { dates: [], candles: [], volumes: [] };
  for (const r of rows || []) {
    const o = +r.open, cl = +r.close, lo = +r.low, hi = +r.high, v = +r.volume;
    if (![o, cl, lo, hi].every(Number.isFinite)) continue;
    out.dates.push(String(r.date || '').slice(0, 10));
    out.candles.push([o, cl, lo, hi]);
    out.volumes.push([Number.isFinite(v) ? v : 0, cl >= o ? 1 : -1]);
  }
  return out;
}
