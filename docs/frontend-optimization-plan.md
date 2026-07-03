# VERA 前端优化实施计划书 v2

> 日期：2026-07-02
> 版本：v2（吸收审计报告修订）
> 审计来源：[2026-07-02_前端优化计划书审计报告.md](audit/2026-07-02_前端优化计划书审计报告.md)
> 范围：`web/index.html`（单文件前端，含 CSS + JS 内联）
> 后端：`server.py`（FastAPI，P1-2 需调用已有 API，其余无需改动）
> 目标：数据准确性修复 → 交互补全 → 图表增强 → 视觉/布局升级 → 高级功能

---

## ⚠️ 前置声明

> **在后端 CRITICAL #1（bfill 前视偏差 `engine.py:368,376,385`）和 #6（成交价乐观偏差 `engine.py:153-169`）未修复之前，P0 的"数据准确性修复"仅限于前端计算逻辑本身。** 修了月度收益的复合算法，底层数据仍然有前视偏差 + 乐观成交。用户看到的"修正后"数字比之前更精确，但并不更准确。这不是用精度掩盖准确度——复合公式本来就是对的，简单加总本来就是错的——但必须声明：**数据准确性仅限前端计算逻辑，后端数据源偏差见[全局架构审计报告](audit/2026-07-02_全局架构审计报告.md)**。

---

## 分期原则

| 期号 | 主题 | 原则 | 改动风险 |
|------|------|------|----------|
| P0 | 数据准确性 | 算错了比没画出来更严重 | 低（纯 JS 逻辑修复） |
| P1 | 交互补全 | 缺了按钮用户会卡住 | 低（加按钮/事件） |
| P2 | 图表增强 | 看不清图表等于白画 | 中（ECharts 配置） |
| P3 | 视觉与布局 | 好看是锦上添花 | 中（CSS 重构） |
| P4 | 高级功能 | 比较与导出是加分项 | 中高（新增 UI 组件） |

---

## P0 — 数据准确性修复（必须立即做）

### P0-1 月度收益计算错误：简单加总 → 复合收益

**现状**：
```javascript
// 当前代码（错误）：把每日收益率直接加总
monthly[k].push(r);  // r = 日收益率
months.map(m => (monthly[m].reduce((a,b)=>a+b,0)*100).toFixed(2))
```

**问题**：10 天每天 1%，正确月收益 = (1.01)¹⁰ - 1 ≈ 10.46%，当前算出 10%。

**修改方案**：
```javascript
// 改为复合收益：(1+r1)(1+r2)...(1+rn) - 1
months.map(m => {
  const product = monthly[m].reduce((acc, r) => acc * (1 + r), 1);
  return { name: m, value: ((product - 1) * 100).toFixed(2) };
})
```

**涉及行**：index.html 第 693-694 行
**验证脚本**（审计 M2 修正 — 至少 P0 有测试）：
```javascript
// 浏览器 console 中执行
(function testMonthlyCompound() {
  const daily = [0.01, 0.01, 0.01];  // 3 天各 +1%
  const simple = daily.reduce((a,b) => a+b, 0) * 100;  // 3.00%
  const compound = (daily.reduce((acc, r) => acc * (1 + r), 1) - 1) * 100;  // 3.0301%
  console.assert(Math.abs(compound - 3.0301) < 0.01, '复合收益计算错误');
  console.assert(simple === 3.00, '简单加总应 = 3.00%');
  console.log('✅ 月度收益测试通过');
})();
```
**验收标准补充**（审计 H1）：对月内交易日 ≥10 天的月份，复合收益与简单加总的差值应 > 0。
**风险**：极低，纯前端计算逻辑

### P0-2 权益曲线基准线断点修复

**现状**：
```javascript
bmVals = dates.map(d => bmMap[d] !== undefined ? bmMap[d] : null);
// connectNulls: false  ← 导致基准线出现大段空白
```

**问题**：科创50等指数上市较晚，早期日期无数据，基准线大段断裂。

**修改方案**（审计 C1 修正 — 原方案 B 有二次归一化错误）：

> ❌ 原方案 B 的 `v - bmStart` 是错误的。后端 `server.py:379` 返回的 `index_close` 已是归一化值，前端 `index.html:649` 的 `(r.index_close - 1) * 100` 已转为百分比偏离。再减 `bmStart` 会抹掉基准在策略开始前的涨幅。

**修正方案**：只裁掉开头无数据的 null 段，不做二次归一化，用 `connectNulls: true` 跳过中间空值后连续画线：

```javascript
// 找到基准首个有效数据的索引
const firstValidIdx = bmVals.findIndex(v => v !== null);
if (firstValidIdx < 0) continue;  // 完全无数据则跳过

// 构造与策略等长的 data 数组：前面填 null，后面保持原值
// 不做 v - bmStart 二次归一化！
const bmData = new Array(firstValidIdx).fill(null).concat(bmVals.slice(firstValidIdx));

series.push({
  name: bmNames[name] || name,
  type: 'line',
  data: bmData,           // 与策略等长，xAxis 对齐
  lineStyle: { color: bmColors[ci % 4], width: 1, type: 'dashed' },
  symbol: 'none',
  connectNulls: true       // 跳过开头 null 段后连线
});
```

**涉及行**：index.html 第 646-656 行
**验收标准补充**（审计）：基准线无空白断裂，且基准起点值与后端返回的归一化值一致（不得强制归零）
**风险**：低

---

## P1 — 交互补全（用户不再卡住）

### P1-1 止损配置编辑加"取消"按钮

**现状**：编辑后只有"保存"，改错无法回退。

**修改方案**：
1. 给每个 config-block 加"取消"按钮
2. 点击"取消"时，恢复编辑前的值
3. 快照方案：首次编辑时将各字段值存入 `data-original` 属性

```html
<!-- 新增取消按钮 -->
<div class="cfg-actions">
  <button class="btn btn-sm btn-secondary edit-btn" onclick="toggleEdit('blkCostStop')">编辑</button>
  <button class="btn btn-sm btn-primary save-btn" onclick="saveBlock('blkCostStop')">保存</button>
  <button class="btn btn-sm btn-secondary cancel-btn" onclick="cancelEdit('blkCostStop')" style="display:none">取消</button>
</div>
```

```javascript
function toggleEdit(blockId) {
  const block = document.getElementById(blockId);
  // 审计 H2 修正：只在首次编辑时快照，防止二次编辑覆盖原始值
  block.querySelectorAll('.cfg-fields input, .cfg-fields select').forEach(el => {
    if (!el.dataset.original) {  // 只在首次编辑时快照
      el.dataset.original = el.type === 'checkbox' ? el.checked : el.value;
    }
  });
  block.classList.add('editing');
  block.querySelector('.cancel-btn').style.display = '';
  block.querySelector('.edit-btn').style.display = 'none';  // 编辑时隐藏编辑按钮，防重复点击
}

function cancelEdit(blockId) {
  const block = document.getElementById(blockId);
  block.querySelectorAll('.cfg-fields input, .cfg-fields select').forEach(el => {
    if (el.type === 'checkbox') el.checked = el.dataset.original === 'true';
    else el.value = el.dataset.original;
    delete el.dataset.original;  // 取消后清除快照，下次编辑重新快照
  });
  block.classList.remove('editing');
  block.querySelector('.cancel-btn').style.display = 'none';
  block.querySelector('.edit-btn').style.display = '';
}
```

**涉及**：6 个 config-block 的 HTML + JS
**风险**：低

### P1-2 "恢复默认配置"按钮

**修改方案**（审计 C2 修正 — 不硬编码默认值，改调后端 API）：

> ❌ 原方案在前端硬编码默认值，但后端 `server.py:82` 的 DEFAULTS 和 `server.py:152` 的 `get()` fallback 本身不一致（如 `cond_time_profit`: 0.02 vs 0.01）。前端硬编码会与后端打架。

**修正方案**：调用后端已有的 `/api/config/defaults` 端点（`server.py:159`），不硬编码：

```html
<button class="btn btn-secondary btn-block btn-sm" onclick="resetDefaults()" style="margin-top:6px">
  恢复默认配置
</button>
```

```javascript
async function resetDefaults() {
  if (!confirm('确定恢复所有配置为默认值？')) return;
  try {
    const resp = await fetch('/api/config/defaults');
    const data = await resp.json();
    if (!data.success) { showToast('获取默认配置失败', 'error'); return; }
    // 从后端默认值映射到前端表单
    // TODO: 需要对照 server.py StrategyConfig 的默认值与前端 CONFIG_IDS 做映射
    // 映射关系在实现时根据 /api/config/defaults 的实际返回结构填写
    const cfg = data.config || {};
    // 基础字段映射（前端字段名 → 后端 YAML 路径）
    const mapping = {
      cfgFormula: cfg.selection?.formula_name,
      cfgFormulaArg: cfg.selection?.formula_arg,
      cfgUniverse: cfg.selection?.universe?.type,
      cfgPeriod: cfg.backtest?.period,
      cfgStart: cfg.time_range?.start,
      cfgEnd: cfg.time_range?.end,
      cfgCapital: cfg.backtest?.initial_capital,
      cfgCommission: cfg.backtest?.commission,
      cfgSlippage: cfg.backtest?.slippage,
      cfgMinBuy: cfg.backtest?.position_sizing?.min_buy_amount,
      cfgMaxBuy: cfg.backtest?.position_sizing?.max_buy_amount,
      cfgLotSize: cfg.backtest?.position_sizing?.lot_size,
      cfgMinLots: cfg.backtest?.position_sizing?.min_lots,
      // 止损字段需要从后端小数转为前端百分比
      cfgCostStopVal: cfg.stop_loss?.cost_stop?.threshold != null ? Math.abs(cfg.stop_loss.cost_stop.threshold * 100) : null,
      cfgTrailingAct: cfg.stop_loss?.trailing_stop?.activation != null ? cfg.stop_loss.trailing_stop.activation * 100 : null,
      cfgTrailingDD: cfg.stop_loss?.trailing_stop?.drawdown != null ? cfg.stop_loss.trailing_stop.drawdown * 100 : null,
      cfgLadderVal: cfg.stop_loss?.ladder_tp?.levels?.map(l => `${Math.round(l.profit*100)}:${Math.round(l.sell_ratio*100)}`).join(','),
      cfgTimeVal: cfg.stop_loss?.time_stop?.max_hold_days,
      cfgCondTimeDays: cfg.stop_loss?.cond_time_stop?.days,
      cfgCondTimeProfit: cfg.stop_loss?.cond_time_stop?.profit != null ? cfg.stop_loss.cond_time_stop.profit * 100 : null,
      cfgFirstDayTarget: cfg.stop_loss?.first_day?.target != null ? cfg.stop_loss.first_day.target * 100 : null,
    };
    const checkboxMapping = {
      cfgExcludeST: cfg.selection?.universe?.exclude_st,
      cfgCostStopEn: cfg.stop_loss?.cost_stop?.enabled,
      cfgTrailingEn: cfg.stop_loss?.trailing_stop?.enabled,
      cfgLadderEn: cfg.stop_loss?.ladder_tp?.enabled,
      cfgTimeEn: cfg.stop_loss?.time_stop?.enabled,
      cfgCondTimeEn: cfg.stop_loss?.cond_time_stop?.enabled,
      cfgFirstDayEn: cfg.stop_loss?.first_day?.enabled,
    };
    // 填入表单
    for (const [id, val] of Object.entries(mapping)) {
      if (val == null) continue;
      const el = document.getElementById(id);
      if (el) el.value = String(val);
    }
    for (const [id, val] of Object.entries(checkboxMapping)) {
      if (val == null) continue;
      const el = document.getElementById(id);
      if (el) el.checked = val;
    }
    localStorage.removeItem(STORAGE_KEY);
    refreshAllSummaries();
    showToast('已恢复默认配置', 'ok');
  } catch(e) {
    showToast('恢复默认失败: ' + e.message, 'error');
  }
}
```

> **注意**：此方案依赖 `/api/config/defaults` 返回完整的默认配置结构。当前 `server.py:159-166` 的实现调 `ConfigLoader.load_defaults()`，需确认返回的 YAML 结构与前端映射匹配。实现时需验证。

**验收标准补充**（审计）：恢复默认后，前端各字段值与 `/api/config/defaults` 返回值一致
**风险**：低

### P1-3 日志面板自动滚到底

**现状**：日志更新后不滚动，用户看不到最新日志。

**修改方案**（审计 L1 修正 — 加"用户是否在底部"判断）：
```javascript
function addLog(msg, type) {
  const now = new Date().toLocaleTimeString();
  logLines.push(`<span class="log-${type||'info'}">[${now}] ${msg}</span>`);
  if (logLines.length > 100) logLines.shift();
  const el = document.getElementById('logContent');
  if (el) {
    el.innerHTML = logLines.join('<br>');
    // 审计 L1 修正：只有用户在底部时才自动滚动，翻看历史时不打断
    const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
    if (atBottom) el.scrollTop = el.scrollHeight;
  }
}
```

**涉及行**：第 495 行
**风险**：极低

### P1-4 错误提示从 alert 改为 toast 通知

**现状**：`alert('回测失败: ...')` 阻塞浏览器。

**修改方案**：
1. 新增 CSS 样式（toast 通知栏，3 秒自动消失）
2. 新增 `showToast(msg, type)` 函数
3. 替换所有 `alert()` 调用

```css
/* Toast 通知 */
.toast-container { position: fixed; top: 20px; right: 20px; z-index: 9999; display: flex; flex-direction: column; gap: 8px; }
.toast { padding: 12px 20px; border-radius: 8px; font-size: 12px; color: #fff; max-width: 400px;
  animation: toastIn .3s ease, toastOut .3s ease 2.7s forwards; box-shadow: 0 4px 12px rgba(0,0,0,.15); }
.toast-error { background: var(--up); }
.toast-ok { background: var(--down); }
.toast-info { background: var(--accent); }
@keyframes toastIn { from { opacity:0; transform:translateX(40px); } to { opacity:1; transform:translateX(0); } }
@keyframes toastOut { from { opacity:1; } to { opacity:0; } }
```

```javascript
function showToast(msg, type) {
  const container = document.querySelector('.toast-container') || (() => {
    const c = document.createElement('div');
    c.className = 'toast-container';
    document.body.appendChild(c);
    return c;
  })();
  const toast = document.createElement('div');
  toast.className = 'toast toast-' + (type || 'info');
  toast.textContent = msg;
  container.appendChild(toast);
  setTimeout(() => toast.remove(), 3000);
}
```

**审计 H3 修正 — confirm 保留为标准二次确认模式**：
- `alert()` → 全部换为 `showToast()`
- `confirm()` → 保留原生 `confirm()`（阻塞式是二次确认的预期行为，与 alert 的非预期阻塞不同）
- 不混用自定义 modal 和原生 confirm

**风险**：低

### P1-5 前端表单即时校验

**修改方案**：给关键字段加 `oninput` / `onchange` 校验，显示 inline 错误提示。

| 字段 | 校验规则 | 错误提示 |
|------|----------|----------|
| 起始/结束日期 | 8位数字，起始 < 结束 | "日期格式：YYYYMMDD" |
| 手续费/滑点 | ≥ 0 | "不能为负数" |
| 阶梯止盈格式 | `数字:数字` 逗号分隔 | "格式：盈利%:卖出%，如 6:30,15:30" |
| 初始资金 | > 0 | "必须大于 0" |

```css
input.invalid { border-color: var(--up) !important; }
.input-error { color: var(--up); font-size: 10px; margin-top: 2px; display: none; }
input.invalid + .input-error { display: block; }
```

**风险**：低

---

## P2 — 图表增强（看得清才用得好）

### P2-1 权益曲线加 dataZoom 缩放

**修改方案**：在权益曲线 ECharts 配置中增加 dataZoom 组件。

```javascript
chart.setOption({
  // ... 现有配置 ...
  dataZoom: [
    { type: 'inside', xAxisIndex: 0, start: 0, end: 100 },  // 鼠标滚轮缩放
    { type: 'slider', xAxisIndex: 0, bottom: 10, height: 20, // 底部拖拽条
      borderColor: c.border,
      fillerColor: hexToRgba(c.accent, 0.13),  // 审计 H5 修正：用 rgba 代替 hex+alpha
      textStyle: { color: c.text2, fontSize: 9 } },
  ],
  grid: { left: 60, right: 70, top: 15, bottom: 70 },  // bottom 从 45 改为 70，给 slider 留空间
});
```

**涉及行**：第 659-678 行
**风险**：低

### P2-2 图表颜色跟随主题

**现状**：盈亏分布、退出原因饼图的颜色写死，主题切换后不变。

**修改方案**（审计 H5 修正 — 用 `rgba()` 代替 hex+alpha 后缀）：

先在 JS 中封装辅助函数：
```javascript
function hexToRgba(hex, alpha) {
  const r = parseInt(hex.slice(1,3), 16);
  const g = parseInt(hex.slice(3,5), 16);
  const b = parseInt(hex.slice(5,7), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}
```

然后用 `hexToRgba` 生成渐变色：
```javascript
const c = getColors();
const downShades = [hexToRgba(c.down, 0.5), hexToRgba(c.down, 0.7), c.down];       // 亏损：浅→深
const upShades = [c.up, hexToRgba(c.up, 0.8), hexToRgba(c.up, 0.5)];               // 盈利：深→浅
const rangeColors = [...downShades, ...upShades];
```

退出原因饼图也改用动态颜色：
```javascript
// 从 CSS 变量生成饼图色板
const pieColors = [c.up, c.warn, c.down, c.accent2, c.accent, c.text2];
```

**涉及行**：第 708-730 行 + 新增 `hexToRgba` 函数
**风险**：低
**依赖**：无（审计 M4 修正 — P2-2 不依赖 P1-4）

### P2-3 图表标题加动态信息

**现状**：图表标题是纯静态文字，不反映当前策略/时间范围。

**修改方案**（审计 L2 修正 — 给 header 加 id 代替三层 DOM 查询）：

HTML 改动：
```html
<div class="chart-box-header" id="equityChartHeader">权益曲线 &amp; 基准对比</div>
```

JS 改动：
```javascript
const formula = data.formula_name || lastResult?.formula_name || '';
const dateRange = `${getVal('cfgStart')}~${getVal('cfgEnd')}`;
document.getElementById('equityChartHeader').textContent =
  `权益曲线 & 基准对比 — ${formula} ${dateRange}`;
```

**风险**：极低

### P2-4 KPI 卡片加 tooltip 解释

**修改方案**：给每个 KPI 卡片加 `title` 属性。

| KPI | title 解释 |
|-----|-----------|
| 累计收益 | 从初始资金到最终的涨跌比例 |
| 年化收益 | 按复利折算的年化收益率 |
| 最大回撤 | 从最高点到最低点的最大跌幅 |
| 夏普比率 | 每承担1份风险获得的超额收益，>1 为优 |
| 胜率 | 盈利交易笔数 / 总交易笔数 |
| 盈亏比 | 平均每笔盈利 / 平均每笔亏损 |
| 交易笔数 | 回测期间的总交易次数 |
| 卡玛比率 | 年化收益 / 最大回撤，衡量风险调整后收益 |
| 盈利因子 | 总盈利金额 / 总亏损金额，>1 为盈利 |
| 最大单笔盈 | 所有交易中单笔最大收益率 |

```html
<div class="kpi-card" title="从初始资金到最终的涨跌比例">
  <div class="kpi-label">累计收益</div>
  <div class="kpi-value" id="kpiCumRet">--</div>
</div>
```

**风险**：极低
**依赖**：无（审计 M4 修正 — P3-1 不依赖 P2-4）

---

## P3 — 视觉与布局升级

### P3-1 KPI 分级布局

**现状**：10 张卡片 5×2 等宽排列，没有主次。

**修改方案**（审计 H4 修正 — 补 HTML diff）：

布局示意图：
```
┌──────────────────────────────────────────────────┐
│  ┌─────────┐ ┌─────────┐ ┌─────────┐            │  ← 核心指标（3列，大号）
│  │累计收益  │ │最大回撤  │ │夏普比率  │            │
│  │  +28.5%  │ │ -12.3%  │ │  1.85   │            │
│  └─────────┘ └─────────┘ └─────────┘            │
│  ┌──────┐┌──────┐┌──────┐┌──────┐┌──────┐       │  ← 收益指标（5列，中号）
│  │年化收益││胜率  ││盈亏比 ││盈利因子││卡玛比率│       │
│  └──────┘└──────┘└──────┘└──────┘└──────┘       │
│  ┌──────────┐ ┌──────────┐                       │  ← 交易统计（2列，小号）
│  │ 交易笔数  │ │最大单笔盈 │                       │
│  └──────────┘ └──────────┘                       │
└──────────────────────────────────────────────────┘
```

**HTML 改动**（`index.html:308-319`）：

原代码：
```html
<div class="kpi-grid" id="kpiGrid">
  <div class="kpi-card"><div class="kpi-label">累计收益</div><div class="kpi-value" id="kpiCumRet">--</div></div>
  <div class="kpi-card"><div class="kpi-label">年化收益</div><div class="kpi-value" id="kpiAnnRet">--</div></div>
  <div class="kpi-card"><div class="kpi-label">最大回撤</div><div class="kpi-value" id="kpiMaxDD">--</div></div>
  <div class="kpi-card"><div class="kpi-label">夏普比率</div><div class="kpi-value" id="kpiSharpe">--</div></div>
  <div class="kpi-card"><div class="kpi-label">胜率</div><div class="kpi-value" id="kpiWinRate">--</div></div>
  <div class="kpi-card"><div class="kpi-label">盈亏比</div><div class="kpi-value" id="kpiPLR">--</div></div>
  <div class="kpi-card"><div class="kpi-label">交易笔数</div><div class="kpi-value" id="kpiTrades">--</div></div>
  <div class="kpi-card"><div class="kpi-label">卡玛比率</div><div class="kpi-value" id="kpiCalmar">--</div></div>
  <div class="kpi-card"><div class="kpi-label">盈利因子</div><div class="kpi-value" id="kpiProfitF">--</div></div>
  <div class="kpi-card"><div class="kpi-label">最大单笔盈</div><div class="kpi-value" id="kpiBest">--</div></div>
</div>
```

改为：
```html
<div class="kpi-grid" id="kpiGrid">
  <!-- 核心指标：3列大号 -->
  <div class="kpi-row kpi-row-primary">
    <div class="kpi-card" title="从初始资金到最终的涨跌比例"><div class="kpi-label">累计收益</div><div class="kpi-value" id="kpiCumRet">--</div></div>
    <div class="kpi-card" title="从最高点到最低点的最大跌幅"><div class="kpi-label">最大回撤</div><div class="kpi-value" id="kpiMaxDD">--</div></div>
    <div class="kpi-card" title="每承担1份风险获得的超额收益，>1 为优"><div class="kpi-label">夏普比率</div><div class="kpi-value" id="kpiSharpe">--</div></div>
  </div>
  <!-- 收益指标：5列中号 -->
  <div class="kpi-row kpi-row-secondary">
    <div class="kpi-card" title="按复利折算的年化收益率"><div class="kpi-label">年化收益</div><div class="kpi-value" id="kpiAnnRet">--</div></div>
    <div class="kpi-card" title="盈利交易笔数 / 总交易笔数"><div class="kpi-label">胜率</div><div class="kpi-value" id="kpiWinRate">--</div></div>
    <div class="kpi-card" title="平均每笔盈利 / 平均每笔亏损"><div class="kpi-label">盈亏比</div><div class="kpi-value" id="kpiPLR">--</div></div>
    <div class="kpi-card" title="总盈利金额 / 总亏损金额，>1 为盈利"><div class="kpi-label">盈利因子</div><div class="kpi-value" id="kpiProfitF">--</div></div>
    <div class="kpi-card" title="年化收益 / 最大回撤，衡量风险调整后收益"><div class="kpi-label">卡玛比率</div><div class="kpi-value" id="kpiCalmar">--</div></div>
  </div>
  <!-- 交易统计：2列小号 -->
  <div class="kpi-row kpi-row-tertiary">
    <div class="kpi-card" title="回测期间的总交易次数"><div class="kpi-label">交易笔数</div><div class="kpi-value" id="kpiTrades">--</div></div>
    <div class="kpi-card" title="所有交易中单笔最大收益率"><div class="kpi-label">最大单笔盈</div><div class="kpi-value" id="kpiBest">--</div></div>
  </div>
</div>
```

CSS（替换原 `.kpi-grid` 规则，第 80 行）：
```css
.kpi-grid { display: flex; flex-direction: column; gap: 10px; }
.kpi-row { display: grid; gap: 10px; }
.kpi-row-primary { grid-template-columns: repeat(3, 1fr); }
.kpi-row-secondary { grid-template-columns: repeat(5, 1fr); gap: 8px; }
.kpi-row-tertiary { grid-template-columns: repeat(2, 1fr); gap: 8px; max-width: 50%; }

/* 核心指标放大 */
.kpi-row-primary .kpi-card { padding: 20px 22px; }
.kpi-row-primary .kpi-value { font-size: 28px; }
.kpi-row-primary .kpi-label { font-size: 12px; }

/* 交易统计缩小 */
.kpi-row-tertiary .kpi-card { padding: 12px 14px; }
.kpi-row-tertiary .kpi-value { font-size: 18px; }
```

**涉及行**：第 80 行 CSS + 第 308-319 行 HTML（精确 diff 见上）
**风险**：中

### P3-2 主题切换重定位

**现状**：主题按钮藏在状态栏里，14px 图标 + 11px 文字。

**修改方案**：移到 sidebar-header 右上角，做成显眼的图标按钮。

```html
<div class="sidebar-header">
  <div class="logo">
    <div class="logo-icon">V</div>
    <div class="logo-text"><h1>VERA</h1><span>量化回测系统 v1.0</span></div>
    <button class="theme-btn" onclick="toggleTheme()" title="切换明暗主题">
      <svg id="themeIcon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--text2)" stroke-width="2"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>
    </button>
  </div>
  <div class="status-bar">
    <span class="status-dot" id="statusDot"></span>
    <span id="statusText">就绪</span>
  </div>
</div>
```

```css
.theme-btn {
  margin-left: auto; width: 32px; height: 32px;
  border-radius: 50%; border: 1px solid var(--border);
  background: var(--bg); cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  transition: all .2s;
}
.theme-btn:hover { border-color: var(--accent); background: var(--card); }
```

**涉及行**：第 111-113 行 CSS + 第 152-155 行 HTML
**风险**：低

### P3-3 默认主题改为 light 优先 + 主题持久化

**现状**：`:root` 写了暗色值，`[data-theme="light"]` 覆盖为亮色。`toggleTheme` 不写 localStorage。

**修改方案**（审计 C3 三步修复）：

**第一步**：`toggleTheme` 加入 localStorage 持久化：
```javascript
function toggleTheme() {
  const next = getTheme() === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('vera_theme', next);  // ← 新增：持久化主题选择
  themeIcon.innerHTML = next === 'dark' ? sunIcon : moonIcon;
  if (lastResult) renderAllCharts(lastResult);
}
```

**第二步**：在 `<head>` CSS 之前加内联 JS，消除 FOUC：
```html
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=1280">
<script>
  // 在 CSS 解析前设置主题，消除白屏闪烁
  (function() {
    var t = localStorage.getItem('vera_theme') || 'light';
    document.documentElement.setAttribute('data-theme', t);
  })();
</script>
<title>VERA — 量化回测系统</title>
<style>
```

**第三步**：`:root` 改为 light 色值，`[data-theme="dark"]` 覆盖暗色：
```css
:root {
  --bg: #ffffff; --surface: #f6f8fa; --card: #f6f8fa;
  --border: #d0d7de; --text: #24292f; --text2: #656d76;
  --up: #d1242f; --down: #1a7f37; --accent: #0969da;
  --accent2: #8250df; --warn: #bf4b00;
}
[data-theme="dark"] {
  --bg: #0d1117; --surface: #161b22; --card: #21262d;
  --border: #30363d; --text: #e6edf3; --text2: #8b949e;
  --up: #ef4444; --down: #22c55e; --accent: #58a6ff;
  --accent2: #d2a8ff; --warn: #f0883e;
}
```

**涉及行**：第 9-22 行 CSS + 第 392-397 行 JS + `<head>` 新增内联 script
**验收标准补充**（审计）：刷新页面后主题保持用户上次选择，无 FOUC 闪屏
**风险**：低

### P3-4 字体加载

**修改方案**（审计 M3 修正 — 非阻塞加载 + dns-prefetch）：

```html
<link rel="dns-prefetch" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.googleapis.com" crossorigin>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap"
      rel="stylesheet" media="print" onload="this.media='all'">
```

> **注意**：如果 VERA 在内网环境无法访问 Google Fonts，则跳过此项，继续使用系统字体 fallback。`media="print" onload` 确保字体加载不阻塞渲染。

**风险**：极低

### P3-5 "已保存"徽章状态化

**修改方案**：
- 编辑时显示"未保存"（橙色）
- 保存后显示"已保存"（绿色）
- summary 显示时展示对应状态

```javascript
function toggleEdit(blockId) {
  const block = document.getElementById(blockId);
  block.classList.add('editing');
  const badge = block.querySelector('.saved-badge');
  if (badge) {
    badge.textContent = '未保存';
    badge.style.background = 'color-mix(in srgb, var(--warn) 20%, transparent)';
    badge.style.color = 'var(--warn)';
  }
}

function saveBlock(blockId) {
  const block = document.getElementById(blockId);
  block.classList.remove('editing');
  const badge = block.querySelector('.saved-badge');
  if (badge) {
    badge.textContent = '已保存';
    badge.style.background = '';  // 恢复 CSS 默认
    badge.style.color = '';
  }
  saveAllConfig();
  refreshAllSummaries();
  addLog('全部配置已保存到浏览器', 'ok');
}
```

**风险**：极低

---

## P4 — 高级功能（锦上添花）

### P4-1 交易明细表搜索与筛选

**修改方案**：在交易表头上方加一行筛选工具栏。

```html
<div class="trade-toolbar" style="display:flex;gap:8px;padding:8px 16px;align-items:center;">
  <input id="tradeSearch" placeholder="搜索代码/名称" style="width:160px;font-size:11px;padding:4px 8px;">
  <select id="tradeFilter" style="font-size:11px;padding:4px 8px;">
    <option value="all">全部交易</option>
    <option value="win">仅盈利</option>
    <option value="loss">仅亏损</option>
  </select>
  <select id="tradeReason" style="font-size:11px;padding:4px 8px;">
    <option value="">所有退出原因</option>
  </select>
  <span id="tradeFiltered" style="font-size:10px;color:var(--text2);margin-left:auto;"></span>
</div>
```

**风险**：中

### P4-2 历史回测对比

**修改方案**：
1. 历史下拉改为多选，或加"与此结果对比"按钮
2. KPI 卡片旁显示 delta 值（本次 vs 上次）
3. 权益曲线叠加两条策略线

> 此功能需要新增后端 API 或前端缓存机制。审计 M1 修正：**P4-2 单项 6.5h+**（历史多选 UI 1h + KPI delta 1h + 双策略叠加 1.5h + 后端对比 API 2h + 联调测试 1h）。建议后续单独排期。

**风险**：高（需后端配合）
**工时**：6.5h（原计划低估）

### P4-3 图表导出功能

**修改方案**：利用 ECharts 内置的 `toolbox` 功能。

```javascript
chart.setOption({
  toolbox: {
    feature: {
      saveAsImage: { title: '保存图片', pixelRatio: 2 },
      dataZoom: { title: { zoom: '区域缩放', back: '还原' } },
      restore: { title: '还原' },
    }
  },
});
```

**风险**：低（ECharts 原生支持）

---

## 实施顺序与依赖关系（审计 M4 修正）

```
P0-1 月度收益修复 ─────┐
P0-2 基准线断点修复 ───┤── 无依赖，可并行
                       ├──→ P2-1 dataZoom（依赖 P0-2 的基准线修复）
P1-1 取消按钮 ─────────┤
P1-2 恢复默认 ─────────┤
P1-3 日志滚动 ─────────┤── 无依赖，可并行
P1-4 toast 通知 ───────┤
P1-5 表单校验 ─────────┘
                       │
P2-2 图表颜色 ─────────┤── 无依赖，可与 P1 并行（审计 M4 修正）
P2-3 图表标题 ─────────┤
P2-4 KPI tooltip ──────┘
                       │
P3-1 KPI 分级布局 ─────┤── 不依赖 P2-4（审计 M4 修正），仅依赖 KPI HTML 结构存在
P3-2 主题切换重定位 ───┤
P3-3 默认主题 + 持久化 ┤
P3-4 字体加载 ─────────┤
P3-5 徽章状态化 ────────┘
                       │
P4-1 交易筛选 ─────────┤
P4-2 历史对比 ─────────┤── 6.5h+，建议单独排期
P4-3 图表导出 ─────────┘
```

## 工时估算（审计 M1 修正）

| 阶段 | 任务数 | 预估工时 | 依赖后端改动 |
|------|--------|----------|-------------|
| P0 数据准确性 | 2 | 45 min | 否（但见前置声明） |
| P1 交互补全 | 5 | 2.5 h | P1-2 调后端已有 API |
| P2 图表增强 | 4 | 1.75 h | 否 |
| P3 视觉布局 | 5 | 2.5 h | 否 |
| P4 高级功能 | 3 | 8-9 h | P4-2 需后端新 API |
| **合计** | **19** | **~15-16 h** | 1 项需新后端 API |

## 不做的事（排除项）

| 原设想 | 排除理由 |
|--------|----------|
| 完整响应式（移动端适配） | VERA 是实盘工具，用户只用桌面；做移动端投入大收益低 |
| Firefox 滚动条适配 | 用户群体 95%+ 用 Chrome/Edge，投入产出比太低 |
| WebSocket 替代轮询 | 轮询 800ms 对当前场景够用，改动成本不值得 |
| 国际化 (i18n) | 目标用户全中文，无此需求 |

**审计追加排除项**：

| 追加排除 | 理由 |
|----------|------|
| 不在后端 bfill 未删时宣称"数据已修正" | 诚实标注比假修复安全 |
| 不引入新的硬编码默认值 | 所有默认值走后端 API |
| 达到 1000 行时必须拆 JS | 当前 877 行 + 19 项改动会膨胀到 1200+，届时拆为 `web/vera-ui.js` |

---

## 验收标准

### 原有标准
- [ ] P0-1：月度收益用复合公式，与手动计算对比误差 < 0.01%
- [ ] P0-1：月内交易日 ≥10 天的月份，复合收益与简单加总的差值 > 0
- [ ] P1-1：每个配置块有"编辑 / 保存 / 取消"三个按钮
- [ ] P1-1：二次编辑不覆盖首次快照，取消恢复原始值
- [ ] P1-3：日志新增条目后自动滚到底（用户在底部时）
- [ ] P1-4：错误提示不再用 alert，改 toast 3 秒消失
- [ ] P1-5：日期/阶梯格式等关键字段有即时校验红框
- [ ] P2-1：权益曲线支持鼠标滚轮缩放和底部拖拽条
- [ ] P2-2：图表颜色跟主题联动（切暗色后饼图/柱图颜色正确）
- [ ] P3-1：KPI 核心指标（累计收益、最大回撤、夏普）比其他指标视觉更大
- [ ] P3-5："已保存"/"未保存"徽章状态正确

### 审计追加标准
- [ ] P0-2：基准线无空白断裂，且基准起点值与后端返回的归一化值一致（不得强制归零）
- [ ] P1-2：恢复默认后，前端各字段值与 `/api/config/defaults` 返回值一致
- [ ] P3-3：刷新页面后主题保持用户上次选择，无 FOUC 闪屏
- [ ] P0 验证脚本：浏览器 console 中执行最小测试通过
- [ ] 所有改动已 `git add` 并提交
- [ ] 页面在 Chrome 120+ 和 Edge 120+ 下功能正常
- [ ] 修改后文件总行数 < 1000（否则拆分 JS 为 `web/vera-ui.js`）
