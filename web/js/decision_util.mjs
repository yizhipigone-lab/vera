// ====== 决策台账前端纯函数 (2026-09-18) ======
// 为什么单独一个 .mjs: 这些"把后端的结构化行说成人话"的规则, 抽出来就能在
// node 下直接单测 (仿 web/js/reason_util.mjs 先例), 不必打开浏览器靠肉眼看。
// 零浏览器依赖 —— 不碰 document / window / fetch。
//
// 大白话要求 (AGENTS.md 第 7 条): 用户是量化入门者, 卡片上的每句话都要先有
// 人话再说数字。所以这里所有文案都写成完整的句子, 术语首次出现配解释。

/** 色调 → CSS 变量 (全站红绿铁律: 买=红 --up / 卖=绿 --down / 警示=黄 --warn)。 */
export const TONE_VAR = {
  up: 'var(--up)', down: 'var(--down)', muted: 'var(--text2)',
  warn: 'var(--warn)', info: 'var(--info)', ok: 'var(--ok)',
};

/** 可信度来源 → 卡片上的小角标。空/未知给空串 (不显示角标)。 */
export const SOURCE_LABELS = {
  live: '',                    // 当场记录 = 正常情况, 不打扰
  backfill_exact: '历史复原',
  backfill_text: '历史原文',
  inferred: '推断',
};

/** 动作 → 徽章文案与色调 (文字 + 颜色双编码, 不能只靠颜色区分买卖)。 */
const ACTION_TEXT = { BUY: '买入', SELL: '卖出', HOLD: '没动', FAIL: '没做成', INFO: '告知' };
const ACTION_TONE = { BUY: 'up', SELL: 'down', HOLD: 'muted', FAIL: 'warn', INFO: 'info' };

/**
 * 动作徽章。
 * @param {string} action - BUY/SELL/HOLD/FAIL/INFO
 * @returns {{text: string, tone: string}}
 */
export function actionBadge(action) {
  const a = String(action || '').toUpperCase();
  return { text: ACTION_TEXT[a] || a || '—', tone: ACTION_TONE[a] || 'muted' };
}

/**
 * 卡片一顶部那条状态: 今天到底跑没跑 / 有没有人工单。
 *
 * 三态 (计划书 §3.8): 跑过了(绿) / 半跑(黄, 有零散痕迹但没收盘归档) / 没跑(黄)。
 * 休市是第四态: 灰字直说"本来就不该有动作", 免得被误当成程序挂了。
 *
 * @param {object} data - /api/trade/decisions 的响应
 * @returns {{tone: string, text: string}|null}
 */
export function statusLine(data) {
  if (!data) return null;
  const manual = Number(data.manual_trades_today || 0);
  const tail = manual > 0
    ? '另外，今天有 ' + manual + ' 笔人工成交（记在下面的成交记录里）' : '';
  const withTail = (s) => (tail ? s + '；' + tail + '。' : s);
  if (!data.trading_day) {
    return { tone: 'muted',
             text: withTail('这一天休市（不是交易日），系统本来就不该有动作') };
  }
  const t = data.run_trace || {};
  const parts = [];
  if (t.rotation) parts.push('ETF 轮动（' + t.rotation + '）');
  if (t.auto_buy) parts.push('尾盘选股（' + t.auto_buy + '）');
  if (t.eod) parts.push('收盘归档（' + t.eod + '）');
  if (parts.length && t.eod) {
    return { tone: 'ok', text: withTail('今天系统跑过了：' + parts.join('、')) };
  }
  if (parts.length) {
    return { tone: 'warn',
             text: withTail('今天有零散运行痕迹（' + parts.join('、')
                            + '），但没有收盘归档 —— 程序可能提前退了') };
  }
  return { tone: 'warn',
           text: withTail('今天没有任何运行痕迹 —— 可能是休市，也可能程序没开机') };
}

/**
 * 明细字段 → 中文标签。表外字段原样显示键名 (不吞信息, 便于排查新字段)。
 */
const EVIDENCE_LABELS = {
  code: '代码', last: '最新价', avg_cost: '持仓成本', peak: '持仓期最高价',
  cost_stop_price: '硬止损价', gap_to_cost_stop_pct: '距硬止损',
  trailing_stop_price: '移动止盈线', gap_to_trailing_pct: '距移动止盈',
  hold_days: '已持有天数', ladder_next_tier: '下一个阶梯档位',
  ladder_next_price: '下一档价位', momentum: '两条腿动量', pool: '该份资金',
  anchor: '信号日', target: '目标腿', stop_off: '是否触发移动止损',
  stop_leg: '被止损的腿', index_code: '大盘指数', ma_window: '均线窗口',
  selected: '公式选出的只数', bought: '实际买入只数', filters: '被过滤的原因',
  picks: '每只票的最终结果', tiers: '已挂出的档位', daily_asset_source: '当天资产来源',
  audit_message: '当时的记录', reason: '成交原因', price: '成交价', qty: '数量',
  pnl_amount: '本次盈亏', pnl_pct: '本次盈亏率', hit_rule: '触发的是哪条规则',
  has_any_quote: '当时有没有行情', source: '来源', events: '当天还发生过',
  signal: '当期信号', target_leg: '目标腿',
};

/** 百分数类字段后缀 (用于决定要不要加 %)。 */
const PCT_KEYS = /(_pct|drawdown|profit)$/;

/** 明细里"当天还发生过"最多展开几条, 超出只说还有几条 (避免一屏几百行)。 */
const EVENTS_SHOWN = 12;

/** 时间戳 (秒) → "HH:MM:SS"。认不出来给空串 —— 宁可只显示内容, 也不显示 NaN。 */
function hhmmss(ts) {
  const n = Number(ts);
  if (!isFinite(n) || n <= 0) return '';
  const d = new Date(n * 1000);
  if (isNaN(d.getTime())) return '';
  const p = (x) => String(x).padStart(2, '0');
  return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

/**
 * 明细里 `events` 那一栏 → 一行一条的流水 (时间 + 动作 + 当时那句话)。
 *
 * 这一栏是台账里信息量最大的地方: 主结论只留最强的那条动作, 而那些"想卖没卖成"
 * 的插曲全在这里 —— 例如 2026 年 7 月 30 日某只票一天重试了 14 次才卖出去。
 * 之前它直接走 `JSON.stringify` 兜底, 页面上是一大坨带引号和大括号的原始 JSON,
 * 用户根本读不下去 (2026-09-18 展开 7 月 30 日那一行时发现)。
 *
 * 排序按时间升序 (回填的顺序本就是升序, 实时写入可能夹杂, 排一下更保险);
 * 缺 ``ts`` 的条目排在**最前面** —— 没有时间就当它发生在最早, 不要塞到中间
 * 让人误以为时间线错乱。
 *
 * @param {Array} events - [{ts, action, reason_code, reason_text}, ...]
 * @returns {string} 用换行分隔的多行文本 (渲染需要 white-space: pre-line)
 */
export function fmtEvents(events) {
  if (!Array.isArray(events) || !events.length) return '';
  const list = events.filter((e) => e && typeof e === 'object');
  if (!list.length) return '';
  const sorted = list.slice().sort(
    (a, b) => (Number(a.ts) || 0) - (Number(b.ts) || 0));
  const shown = sorted.slice(0, EVENTS_SHOWN).map(function (e) {
    const t = hhmmss(e.ts);
    const a = String(e.action || '').toUpperCase();
    const badge = ACTION_TEXT[a] || a;
    const txt = String(e.reason_text || e.reason_code || '').trim();
    return (t ? t + '  ' : '') + (badge ? badge + '：' : '') + txt;
  });
  if (sorted.length > EVENTS_SHOWN) {
    shown.push('……还有 ' + (sorted.length - EVENTS_SHOWN) + ' 条没有展开');
  }
  return shown.join('\n');
}

/**
 * 单个明细值的显示文本 (数字该带 % 的带 %, 布尔转是/否, 对象转短串)。
 */
function fmtEvidence(key, v) {
  if (typeof v === 'boolean') return v ? '是' : '否';
  if (typeof v === 'number') {
    if (PCT_KEYS.test(key)) return (v * (Math.abs(v) <= 1 ? 100 : 1)).toFixed(2) + '%';
    if (Number.isInteger(v)) return String(v);
    return String(Number(v.toFixed(3)));
  }
  if (typeof v === 'string') return v;
  if (key === 'events') return fmtEvents(v);   // 一行一条流水, 不是原始 JSON
  if (key === 'momentum' && v && typeof v === 'object') {
    return Object.keys(v).map(function (c) {
      const n = Number(v[c]);
      return c + ' ' + (isNaN(n) ? String(v[c]) : (n * 100).toFixed(1) + '%');
    }).join(' / ');
  }
  if (key === 'filters' && v && typeof v === 'object') {
    return Object.keys(v).map(function (k) { return k + ' ' + v[k] + ' 只'; }).join('、');
  }
  try { return JSON.stringify(v); } catch (e) { return String(v); }
}

/**
 * 明细字典 → 可渲染的 [{label, value}]。空值一律跳过 (不显示空行)。
 * @param {object} evidence
 * @returns {Array<{label: string, value: string}>}
 */
export function evidenceRows(evidence) {
  if (!evidence || typeof evidence !== 'object') return [];
  const out = [];
  Object.keys(evidence).forEach(function (k) {
    const v = evidence[k];
    if (v === null || v === undefined || v === '') return;
    if (Array.isArray(v) && !v.length) return;
    if (typeof v === 'object' && !Array.isArray(v) && !Object.keys(v).length) return;
    out.push({ label: EVIDENCE_LABELS[k] || k, value: fmtEvidence(k, v) });
  });
  return out;
}

/**
 * 日历格上那一行字。休市 / 没运行 / 摘要 三选一。
 * @param {object} day - 日历接口里的一天
 * @returns {string}
 */
export function calCellLabel(day) {
  if (!day) return '';
  if (!day.trading_day) return '休市';
  if (!day.rows) return '';
  return day.headline || (day.rows + ' 条');
}

/**
 * 一格里的主色调 (次数最多的那种; 次数相同按"更值得注意"排序: 买>卖>警示>告知>没动)。
 * 用于给日历格上底色条 —— 一眼就能看出这个月哪天动过。
 *
 * **不认识的色调会被忽略**: 后端将来加了新色调而前端还没学会, 若不忽略,
 * 这一格会以那个新色调胜出, 而 `TONE_VAR` 里查不到 → 底色变成一个无效 CSS 值 →
 * 格子静默失去颜色。宁可退回一个认识的颜色, 也不要一格白白没有底色。
 * @param {object} tones - {up: 1, muted: 3}
 * @returns {string}
 */
export function dominantTone(tones) {
  const rank = { up: 5, down: 4, warn: 3, info: 2, muted: 1 };
  let best = 'muted';
  let bestN = -1;
  Object.keys(tones || {}).forEach(function (t) {
    if (!(t in TONE_VAR) || !(t in rank)) return;   // 表外色调: 忽略, 不参与评选
    const n = Number(tones[t]) || 0;
    if (n > bestN || (n === bestN && (rank[t] || 0) > (rank[best] || 0))) {
      best = t;
      bestN = n;
    }
  });
  return best;
}

/**
 * 某一天的日期串 → 日历格需要的年月日。
 *
 * 只认严格的 ``YYYY-MM-DD``; 认不出来返回 ``null``。
 * 为什么必须严格 (2026-09-18 补测试时发现): 原来的写法靠 ``Number()`` 硬取,
 * 畸形输入 (比如月份字段缺了) 会得到 ``NaN``, 而 ``new Date(NaN, ...)`` 的天数是
 * ``NaN`` —— 于是 ``dayNum`` 的比较全部为假, 日历会认认真真地渲染出 42 个写着
 * "NaN" 的格子。给 ``null`` 让调用方"说一句数据不对", 比画一屏 NaN 强。
 * @param {string} iso - 'YYYY-MM-DD'
 * @returns {{year: number, month: number, day: number}|null}
 */
export function parseIso(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ''));
  if (!m) return null;
  return { year: Number(m[1]), month: Number(m[2]), day: Number(m[3]) };
}
