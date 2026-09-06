// tests/web/test_ai_settings.js — AI 设置 TAB 纯函数护栏 (node 直跑)
// 用法: node tests/web/test_ai_settings.js
const assert = require('assert');
const ai = require('../../web/js/ai_settings.js');

// fillValuesFromView: 后端视图 → 表单回填值; Key 只给 placeholder 提示不回填明文
const vals = ai.fillValuesFromView({
  fast: { base_url: 'https://api.deepseek.com', model: 'deepseek-v4-flash',
          key_set: true, key_mask: 'sk-1****2345' },
  standard: { base_url: '', model: '', key_set: false, key_mask: '' },
  deep: { provider: 'deepseek-official', model: 'deepseek-v4-pro' },
});
assert.strictEqual(vals.fastBase, 'https://api.deepseek.com');
assert.strictEqual(vals.fastKeyHint, '已设置: sk-1****2345');
assert.strictEqual(vals.stdKeyHint, '未设置');
assert.strictEqual(vals.deepProvider, 'deepseek-official');
assert.strictEqual(vals.deepModel, 'deepseek-v4-pro');

// 空视图 → 空回填 + Key 未设置提示
const empty = ai.fillValuesFromView({});
assert.strictEqual(empty.fastBase, '');
assert.strictEqual(empty.fastKeyHint, '未设置');

// collectSaveBody: 表单 → 三档 save body; Key 留空 = 空串 (后端合并保留旧值)
const body = ai.collectSaveBody(function (id) {
  const map = {
    aiFastBase: 'https://a.com', aiFastKey: '', aiFastModel: 'm1',
    aiStdBase: '', aiStdKey: 'k2', aiStdModel: '',
    aiDeepProvider: 'deepseek-official', aiDeepModel: 'dm',
  };
  return map[id] || '';
});
assert.deepStrictEqual(body, {
  fast: { base_url: 'https://a.com', api_key: '', model: 'm1' },
  standard: { base_url: '', api_key: 'k2', model: '' },
  deep: { provider: 'deepseek-official', model: 'dm' },
});
assert.strictEqual(body.fast.api_key, '', 'Key 留空 = 空串, 不误发占位');

// clearBodyFor: 清空某档 → 只提交该档 __clear__ (其他档不提交由后端保留)
assert.deepStrictEqual(ai.clearBodyFor('fast'), { fast: { '__clear__': true } });
assert.deepStrictEqual(ai.clearBodyFor('deep'), { deep: { '__clear__': true } });

console.log('test_ai_settings.js 全绿 ✓');
