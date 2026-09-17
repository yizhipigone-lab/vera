// tests/web/test_data_cache.js — 数据准备 TAB 纯函数护栏 (node 直跑)
// 用法: node tests/web/test_data_cache.js
const assert = require('assert');
const dc = require('../../web/js/data_cache.js');

// statusBadge: 补拉中 > 过期 > 新鲜
assert.strictEqual(dc.statusBadge({ stale: false }, true).cls, 'busy');
assert.strictEqual(dc.statusBadge({ stale: true }, false).cls, 'err');
assert.strictEqual(dc.statusBadge({ stale: false }, false).cls, 'ok');

// statusRowHtml: 七列齐全 + 转义 + 缺值占位
const row = dc.statusRowHtml(
  { period: '5m', stocks: 6098, first_date: '20240627', last_date: '20260810',
    expected: '20260813', stale: true, not_intact: 3 }, false);
for (const cell of ['5m', '6098', '20240627', '20260810', '20260813', '过期', '3']) {
  assert.ok(row.includes(cell), `状态行缺 ${cell}: ${row}`);
}
// 2026-09-17 修正过期断言: 2026-09-07 的 tokens.css 设计令牌收口把 --red 改成了
// --up(#d6342f, A股红), 全项目已 0 处 var(--red) —— 旧断言让本文件自 09-07 起一直红,
// 红测试会掩盖真回归。语义不变: 过期仍是最醒目的红色。
assert.ok(row.includes('var(--up'), '过期应红色');

// 缺值显示 —（如 manifest 不存在）
const rowEmpty = dc.statusRowHtml(
  { period: '1m', stocks: 0, first_date: null, last_date: null,
    expected: '20260813', stale: true, not_intact: 0 }, false);
assert.ok(rowEmpty.includes('—'), '空日期应显示 —');

// XSS 转义（自定义 esc 验证透传）
const rowEsc = dc.statusRowHtml(
  { period: '<script>', stocks: 1, first_date: 'x', last_date: 'y',
    expected: 'z', stale: false, not_intact: 0 }, false);
assert.ok(!rowEsc.includes('<script>'), 'period 必须转义');

console.log('test_data_cache.js 全绿 ✓');
