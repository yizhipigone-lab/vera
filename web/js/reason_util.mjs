// ====== 实盘卖出原因归一工具 (纯函数, 零浏览器依赖, 可在 node 下直接 import 测试) ======
// 2026-08-07 从 analysis.js renderExitPie 提纯出来, 目的:
//   1) 锁死 reason 归一契约 (后端 trade_main._reason_from_ctx 改格式时, tests/web/test_reason_bucket.mjs 会红);
//   2) 消除"验证脚本跑完就删"的回归空窗。
//
// 病根背景: 实盘 trades.reason 是带数字描述的长句子, 例如
//   "移动止盈: 最高 12.00 (峰值涨幅 +20%...) 触发" / "阶梯止盈·档1" / "硬止损: 成本 10.00 现价 9.50"。
// 直接当图例名会因每笔数字不同拆成无数扇区, 必须先归一成策略类目再聚合上色。

/**
 * 把一笔成交的 reason 归一成策略类目 (图例名 = 上色 key)。
 * 规则: 取冒号 / 中文冒号 / 中点 之前的策略名前缀; 空串或空前缀按来源兜底。
 * @param {string} reasonText - trades.reason 原文 (可能为空 / null / undefined)
 * @param {string} [source]   - trades.source ('manual' 表示人工单)
 * @returns {string} 类目名 (命中表则按预设色上色, 表外则走兜底调色板, 均不再退回灰)
 */
export function bucketReason(reasonText, source) {
  const raw = reasonText || '';
  if (!raw) return source === 'manual' ? '人工卖出' : '未标注';
  const head = raw.split(/[:：·]/)[0].trim();
  return head || (source === 'manual' ? '人工卖出' : '未标注');
}

// 预设类目表 (对齐实盘真实用词 trade/executor.py:_REASON_LABELS + 兜底类目)。
// 命中表的类目用 reasonColor 专属色; 表外的走 fallbackPalette 调色板兜底。
// isKnownReason 仅供可观测性使用 (表外类目 console.debug, 便于排查再次掉灰)。
const REASON_KNOWN = new Set([
  '硬止损', '移动止盈', '阶梯止盈', '时间止损', '条件时间止盈',
  '首日不达标', '换股卖出', '公式止损', '退市', '人工卖出', '未标注', '系统卖出',
]);

export function isKnownReason(label) {
  return REASON_KNOWN.has(label);
}
