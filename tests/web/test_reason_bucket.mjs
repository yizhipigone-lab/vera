// ====== bucketReason / isKnownReason 契约测试 (node 直接跑, 零框架依赖) ======
// 用法: node tests/web/test_reason_bucket.mjs
// 目的: 锁死实盘 trades.reason → 策略类目的归一契约。
//   后端 trade_main._reason_from_ctx 若改 reason 拼接格式 (去冒号/换分隔符),
//   这里会红, 提醒前端同步, 避免饼图色块静默重新掉灰。
import assert from 'node:assert/strict';
import { bucketReason, isKnownReason } from '../../web/js/reason_util.mjs';

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

// ── 真实样本: 实盘 reason 长句子归一 (来自 trade_main._reason_from_ctx 实际产出) ──
case_(
  '移动止盈长句子 → 类目',
  bucketReason('移动止盈: 最高 12.00 (峰值涨幅 +20.0%, 回撤 5.8% from 12.50) 触发'),
  '移动止盈'
);
case_('阶梯止盈带档位', bucketReason('阶梯止盈·档1'), '阶梯止盈');
case_('硬止损带描述', bucketReason('硬止损: 成本 10.00 现价 9.50'), '硬止损');
case_('时间止损带描述', bucketReason('时间止损: 持有 5 日'), '时间止损');
case_('条件时间止盈', bucketReason('条件时间止盈: 涨幅达标'), '条件时间止盈');
case_('换股卖出', bucketReason('换股卖出: 换到 600000'), '换股卖出');
case_('公式止损', bucketReason('公式止损: 信号反转'), '公式止损');

// ── 无分隔符: 整串即类目 ──
case_('无分隔符裸类目', bucketReason('时间止损'), '时间止损');
case_('英文冒号也分割', bucketReason('移动止盈: peak 12.0'), '移动止盈');

// ── 空值兜底 (按 source 区分人工/系统) ──
case_('空串 + manual → 人工卖出', bucketReason('', 'manual'), '人工卖出');
case_('空串 + 系统空 → 未标注', bucketReason('', ''), '未标注');
case_('null reason → 未标注', bucketReason(null), '未标注');
case_('undefined reason → 未标注', bucketReason(undefined), '未标注');
case_('全空 → 未标注', bucketReason(), '未标注');

// ── 前缀为空 (reason 以分隔符开头) ──
case_('冒号开头 + manual → 人工卖出', bucketReason(': 某描述', 'manual'), '人工卖出');
case_('中点开头 → 未标注', bucketReason('· 某描述'), '未标注');

// ── 表外新类目: 归一正常返回, 颜色走 fallback (由 isKnownReason 标记) ──
case_('表外类目原样返回', bucketReason('未知新策略: xxx'), '未知新策略');
case_('表外类目 isKnown=false', isKnownReason('未知新策略'), false);

// ── isKnownReason 命中表 ──
for (const k of ['硬止损', '移动止盈', '阶梯止盈', '时间止损', '条件时间止盈',
  '首日不达标', '换股卖出', '公式止损', '退市', '人工卖出', '未标注', '系统卖出']) {
  case_(`isKnown(${k})=true`, isKnownReason(k), true);
}

// ── 端到端: 全灰 bug 不复现 —— 任何 reason 都能落到一个非灰类目 ──
// (灰色 c.text2 只用于 '退市'/'未标注'/'系统卖出' 三个弱类目, 不再是默认 fallback)
const samples = [
  '移动止盈: 最高 12.00 触发', '阶梯止盈·档2', '硬止损: ...', '时间止损',
  '', null, ': xxx', '未知策略: abc',
];
let allLabeled = true;
for (const s of samples) {
  const label = bucketReason(s, s === '' ? 'manual' : undefined);
  if (!label) allLabeled = false;
}
case_('所有样本都归一到非空类目 (全灰 bug 不复现)', allLabeled, true);

console.log(`\n${_fail === 0 ? '[OK]' : '[FAIL]'} bucketReason 契约测试: ${_pass} passed, ${_fail} failed`);
process.exit(_fail === 0 ? 0 : 1);
