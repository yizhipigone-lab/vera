# 前端交互与体验审计报告：vera-ui.js / trade.js / analysis.js / decision.js(+decision_util.mjs)

审计日期：2026-09-19。审计方式：逐行通读四个目标文件全文（889 + 1386 + 905 + 287 + 319 行），并以 grep 交叉验证 visibilitychange、hex 硬编码、定时器配对。market_dashboard / market_position / charts* / brain_* 不在本次范围。

---

## 维度 1：加载与反馈

**[高] trade.js:12 —— `get()` 不检查 `r.ok`，服务端 500 会被当成"无数据"渲染**
`get` 只有 `fetch(...).then(r => r.json())`。POST 路径在 2026-08-13 修过（L17-35 查 `r.ok` 并拼 `detail`），GET 路径没修：后端返回 500/422 且带 JSON body 时，`renderDeals(d)` 拿到的是错误对象，`d.deals` 为 undefined → 页面显示"该日无成交"。错因（服务端报错）被表现成"本来就没数据"，比报错更危险。
建议：`get` 与 `post` 共用同一段 `r.ok` 检查（`detail` 拼接待 `err.serverMsg`），让下游 catch 能区分。

**[高] trade.js:454 / 464 / 473 / 481 —— 查询失败一律硬编码"交易服务 (8081) 不可达"，不遵守 describeTradeError 口径**
四个查询函数（loadDeals / loadHistoryOrders / loadReconciles / loadAudits）的 catch 全都写死 `'查询失败: 交易服务 (8081) 不可达'`。decision_util.mjs:309-318 的 `describeTradeError` 已经把「连不上（TypeError）→ 去看进程」与「服务端报错（HTTP xxx）→ 去看日志」分开，decision.js 全面遵守（L65、L176），而 trade.js 各写各的——这正是 2026-09-19 "404 被误报为不可达"教训的复发面（HTTP 错误、JSON 解析失败、4 秒超时全被报成"不可达"）。
建议：trade.js 引入 decision_util 的 `describeTradeError`（或把该函数挪到公共模块），四处 catch 统一改走它。

**[中] trade.js:1104 / 1136 —— 设置面板加载/保存失败同样一律报"不可达"**
`配置加载失败: 交易服务不可达`、`保存失败: 交易服务不可达或超时 (4s)`。且 L1099 的 `get('/api/trade/config')` 在 500 JSON 时会走进 `fillSettings(错误对象)` 抛 TypeError，再被 catch 报成"不可达"。同维度 1 第二条口径问题。

**[中] vera-ui.js:326 —— loadFactorRules 网络失败被显示成"该公式暂无体检规则"**
`catch(e) { _factorRulesData = { exists: false, rules: [] } }`，随后 renderFactorRules 渲染"该公式暂无体检规则 — 先跑 formula_lab 生成"。网络抖动会被读成"这公式没体检过"，引导用户去白跑一轮体检。
建议：catch 分支渲染"规则加载失败（网络），重试"而不是落入 exists:false 分支。

**[中] analysis.js:815-821 —— loadDeals 失败静默退化成"共 0 笔"空表**
`fetch(...).then(r => r.json()).catch(() => null)` → `_allDeals = []` → 表格空 + "共 0 笔"。服务挂掉时用户看到的是一个"干干净净但没有数据"的页面，无任何失败提示。且此 fetch 不查 `r.ok`。
建议：失败时在表体放一行"成交加载失败：<describeTradeError 口径>"。

**[中] analysis.js 全文 fetch 无 AbortController / 无超时 —— 挂起即永远"加载中…"**
L48-50（summary/equity/daily_pnl）、L106（attribution）、L349（gapfill_last）、L366（gapfill POST）、L545（daily_report）、L604（daily_pnl 切月）、L815（deals）全是裸 `fetch`。对照：trade.js 的 refresh/cmd/设置保存都有 AbortController + 4s 超时（trade.js:573-574、626-627、1119-1120），vera-ui.js 回测提交有 2 小时超时（L173）。analysis.js 是四个文件里唯一完全没有超时的。`showDailyReport` 的"加载中…"（L542）在后端半死时会永远停在那里。
建议：抽一个 `fetchWithTimeout(url, ms)` 工具函数，全文件统一。

**[低] decision.js:166-178 —— 决策日历加载无 loading 态、切月按钮不防连点**
`loadCalendar` 只在失败时写 hint，成功前没有任何占位；decPrevBtn/decNextBtn 点击后按钮不禁用、无在途标志。卡片一（loadDecisions，L60-61）有按钮禁用 + "查询…"文案，两张卡口径不一致。

**[低] vera-ui.js:167 —— 回测状态轮询 `catch(e) {}` 完全静默**
800ms 一次的状态轮询连续失败时（如 server.py 重启中），进度条就地冻住、无任何"连接中断"提示。设计上容忍瞬时失败合理，但连续 N 次失败应给出提示。

---

## 维度 2：轮询与定时器

**[高] 全 web/js 无 visibilitychange 处理 —— 标签页隐藏后 trade.js 仍以 1s × 6 接口轮询**
grep 确认 `web/js/*.js` 中 `visibilitychange` / `document.hidden` 零匹配。mobile.html 已修过此问题（切后台暂停、回前台即刷），PC 端四个文件全没处理：trade.js:604 的 `setInterval(refresh, 1000)`（6 个接口 × 1Hz）在标签页隐藏、电脑锁屏后照跑；vera-ui.js:408 的 lab 2s 轮询、L505 的 farm 2s 轮询同理。
建议：在 vera-ui.js 统一挂一个 visibilitychange 监听，hidden 时调各页的 Leave 钩子、visible 时调 Enter 钩子（mobile.html 的实现可直接搬）。

**[中] trade.js:662-683 / 1199-1212 —— watchCmdResult 与 _chPollUntil 的 setInterval 页签切走后继续跑**
`watchCmdResult`（1s × 10 次）和 `_chPollUntil`（1s × 8~15 次）是独立计时器，不归 `tradePageLeave` 管（L606-608 只清 pollTimer）。有界（最多 10-15 次），不算泄漏，但切走页签后仍在后台打请求，且 `_chPollUntil` 在向导已关闭时仍会跑完。
建议：计时器句柄纳入 pageLeave 清理，或 tick 开头检查 `document.hidden` / `_chOverlay`。

**[低] vera-ui.js:455-460 —— lab 每 2 秒轮询都附带 loadLabHistory 全量重建 DOM**
`refreshLabStatus` 每次执行末尾 `loadLabHistory()`，即每 2 秒一次 fetch + innerHTML 重建历史列表。farm 侧有签名脏检查防重绘（L580-582 `_farmOvSig`），lab 侧没有，滚动位置和选中态每 2 秒被冲一次。

定时器配对本身做得规范：`_labPollTimer`/`_farmPollTimer`（L408-409、505-506）、trade.js pollTimer（L601-608）、回测 poll 与 timeout（L166-188）均有成对 clear，未发现裸泄漏。

---

## 维度 3：颜色与样式硬编码

**[低] trade.js:1356 / 1362 —— 通道向导写死 rgba 与 border-radius:10px**
`background:rgba(0,0,0,.55)`（遮罩）、`box-shadow:0 8px 32px rgba(0,0,0,.4)`、`border-radius:10px`（tokens 有 `--radius` / `--radius-sm`，L1256 的输入框倒是用了 `var(--radius-sm)`——同一个弹窗内两种口径）。
建议：遮罩/阴影颜色可以接受写死（双主题语义一致），border-radius 应改 `var(--radius)`。

**[低] 8081 端口三处独立硬编码 —— tradeApiBase 已是唯一真相源但没人引用**
trade.js:9 `':'8081'`、analysis.js:8-9 两处拼接、decision_util.mjs:264 `TRADE_API_PORT = 8081` 并带完整注释。三份各自为政，改端口要改三处（analysis.js 还拼了两次）。decision_util 的 `tradeApiBase(hostname)` 本该被 trade.js / analysis.js 复用。

正面结论：四个文件 **没有任何写死的 hex 颜色**（grep 验证）。红绿盈亏全部 `var(--up)`/`var(--down)`；echarts 配色全部走 `getColors()` + `hexToRgba()`（analysis.js:613、661、711-712、741-760），即 CSS 变量 → getComputedStyle 链路。analysis.js:229 的日历格渐变用 `color-mix(in srgb, var(--up) 22%, transparent)`，双主题安全。这一项是干净的。

---

## 维度 4：DOM 安全与渲染

**[中] vera-ui.js:234 / 240 / 243 —— 板块加载失败提示把未转义的服务端 error / e.message 拼进 innerHTML**
`fail()` 里 `e.innerHTML = msg + '<br><button …>'`，msg 来自 `result.error`（服务端字符串）或 `e.message`。同文件其他地方（L267-269 板块名、L452 体检错误）都走 `esc()`，唯独这个错误分支没有。服务端 error 若含 `<` 会被当 HTML 解析。
建议：msg 部分套 `esc()`，按钮 HTML 保持原样。

**[低] trade.js:224 —— `p.tiers_done.join(',')` 未转义直插 innerHTML**
服务端数据（预埋档号数组）。同文件 L317-325 有"审计L12修复：qty/filled_qty/status 统一 Number() 强转——纵深防御缺口"的先例，这一格漏了。

**[低] trade.js:797-807 —— auto_buy 卡片的 selected / bought / dp.qty 直插 innerHTML，未 Number() 强转**
与 L317 的 L12 修复口径不一致：同一文件委托表修了强转，尾盘选股卡片没修。`dp.price` 有 `Number()`（L806），`dp.qty` 没有。

**[低] analysis.js:173 / 305 —— tip 文案字符串拼接进 title 属性**
`title="' + c.tip + '"`、`title="' + tip + '"`。当前 tip 全是文件内硬编码常量（安全），但模式本身不防回归——将来 tip 改成动态数据就是属性注入。建议套 `escAttr`（charts.js 已导出）或改 `el.title =` 赋值。

esc 使用一致性总体良好：trade.js / decision.js 各自的 esc 覆盖绝大多数动态字段；decision.js 全部输出路径（L86-148）均 esc；vera-ui.js 农场榜对来源 URL 有 `^https?://` 白名单防 `javascript:` 协议（L609-611，含注释说明），报告渲染走 DOMPurify（L467-469、798-800）。

---

## 维度 5：错误处理

**[中] vera-ui.js:455 / 460 / 804 / 866 / 879 —— 五处 `catch(() => {})` 静默吞错，列表区永远显示旧数据**
refreshLabStatus、loadLabHistory、loadFarmReports、fetchSavedConfig、fetchResults 失败时页面无任何迹象，用户分不清"没数据"和"刷新失败"。对照 trade.js 的 `markOffline()` + stale 角标（L118-144）是同问题的正确解法。
建议：至少在对应容器角落写一行"刷新失败，显示的是旧数据"。

**[中] analysis.js:52-58 —— 三路请求部分失败时静默缺图**
`Promise.allSettled` 后 `fetchError=true` 只在 `summary` 缺失或"无错误但全空"时才改提示文案；equity 单独失败 → 权益曲线区域空白无说明；dailyPnl 单独失败 → 日历渲染成全灰"未归档"样子（`pnl` 全 undefined → 每格显示"未归档/休市"），这比报错更误导。
建议：逐路失败时在对应容器内写失败提示（chartsShowLoading 的容器可以复用）。

**[低] analysis.js:94-95 / 100-101 / 111-112 —— 深挖图表失败只 console.warn**
renderUnderwater / renderRolling / renderAttribution 失败仅打日志（注释说明是刻意的 fail-soft 隔离，方向正确），但用户面对一块永远空白的图区没有任何解释。建议失败时在容器内写"该图暂不可用"。

**[低] vera-ui.js:120 —— `stopBacktest` 失败 `catch (e) {}` 静默**
停止请求失败用户无感知，只能靠后续状态轮询发现还在跑。建议失败时 addLog 一条"停止请求发送失败"。

---

## 维度 6：状态一致性

**[中] decision.js:50-72 / 166-178 —— 卡片一/日历请求无序号守卫，慢响应可覆盖新响应**
analysis.js 有现成范式：`_dailyReportSeq`（L21、537、546）与 `_calLoadSeq`（L22、593、605）防"快连点慢响应覆盖快响应"，注释里写明这是 2026-08-07 审计 HIGH#2 和 2026-09-04 对手审计修过的真问题。decision.js 的 loadDecisions（Enter 连发、gotoDay 连点）与 loadCalendar（切月连点）都没有同类守卫，旧月响应后到会把日历渲染回旧月。
建议：照抄 analysis.js 的序号守卫模式（两处各加一个 `_seq`）。

**[低] decision.js:233-235 —— 日历格 click 绑给所有 `[data-date]`，role/tabindex 只给有 rows 的**
`data-date` 每格都有（L223），所以"没运行/休市"格也可点（会触发 gotoDay 查一个大概率空的日期），但键盘焦点却进不去（无 tabindex）——鼠标和键盘看到两套可点集合，行为不一致。

**[低] trade.js:226 / 328 —— `cmdInFlight` 在声明（L614）之前被引用**
依赖 `var` 提升才能工作（当前不出错），但 L226 的注释与声明隔了 400 行，后续若有人把 L614 改成 `let` 立即炸。建议声明挪到渲染函数之前。

**[低] analysis.js:826-828 —— filterDealsByDate 直接读 DOM 元素无 null 守卫**
`document.getElementById('analysisTradeSearch').value` 三连，与文件内其他处的防御风格（L41-43 等）不一致；元素改名即抛 TypeError。

**[低] analysis.js:893-904 —— setTimeout(100ms) 延迟接线**
module 脚本天然 defer，执行时 DOM 已就绪，100ms 延时既无必要又引入"100ms 内工具栏不响应"的窗口。

**正面**：页签切换残留控制得好——analysis.js:594 切月时作废在途日报请求 + 598-602 清选中；trade.js 切页清轮询；vera-ui.js:769-772 农场回填快照与横幅同生命周期。事件重复绑定未见问题（切月导航用 `onclick=` 赋值幂等，innerHTML 重建后旧监听器随节点回收）。

---

## 维度 7：进度与长任务反馈

**[低] analysis.js —— 切月/日报/补算请求不可取消**
只有 `_gapfillBusy` 防重入（L343、358）和序号丢弃（L605），请求本体没有 AbortController；快速切 10 个月会在后台堆 10 个在途请求。与维度 1 的超时缺失同源，一并修即可。

**正面（重点表扬）**：回测进度条是四个文件里完成度最高的交互——
- 真实刻度 + 缓动逼近（不虚构前进，`dispPct += (target - dispPct) * 0.4`，vera-ui.js:151-153）
- 新轮重置检测（L150-151）、阶段文案 + 批次 detail + 已用时间 + 速率法 ETA（L158-163）
- `aria-valuenow` 读屏可感知（L155-156）
- 日志按内容去重防刷屏（L165）
- 2 小时 AbortController 超时 + 手动停止按钮 + `tryRecoverAbortedResult` 断线恢复（L169-192）
长任务可取消：回测（L118-122）、体检（L426-429）、农场（L498）均有停止路径。

---

## 维度 8：键盘与无障碍

**[中] decision.js:117-122 —— `.dec-row` 有 role="button" tabindex="0" 却没有 keydown 处理**
行上标了 `role="button" tabindex="0"`（L140），键盘用户能 Tab 到，但 Enter/Space 展不开"凭什么"明细——只有 click 监听。trade.js:269-271（排序表头）和 analysis.js:272-274（日历格）都修过一模一样的问题并留了审计注释，decision.js 是最新的文件反而漏了。
建议：补 `keydown` → Enter/Space 触发同一展开逻辑。

**[中] decision.js:223-235 —— 决策日历格完全无键盘可达性**
有数据的格子标了 `role="button" tabindex="0"`（L224），但没有 keydown；鼠标点能 gotoDay，键盘按不动。同维度 6 的可点集合不一致问题叠加。

**[低] trade.js:1353-1375 —— 通道向导无焦点管理**
Esc 关闭（L1179）、点遮罩关闭（L1369）、`role="dialog" aria-label`（L1358-1359）、`aria-live="polite"` 的结果提示（L1260）都有了，但：打开后焦点不进弹窗（Tab 会先走页面背景元素）、无焦点圈禁（Tab 可逃出弹窗）、关闭后焦点不回到徽章。键盘用户打开向导后第一下 Tab 会"失踪"。

**[低] trade.js:722-741 —— 买入代码/数量/价格输入框无 Enter 提交**
填完价格必须伸手点按钮；对照 decision.js:273 的日期框有 Enter 查询、vera-ui.js:831 的搜索框有 Enter 立即执行。

**正面**：vera-ui.js 侧栏/板块折叠头同步 `aria-expanded`（L37-39、809-811、849-852、869-872）、进度条 role 语义（L155-156）、trade.js 排序表头 `tabindex="0" role="button"` + Enter/Space（L152、269-271）、analysis.js 日历格 `role="button" tabindex` + aria-label 读屏文案（L246-253、272-274）。

---

## 做得好的地方（带行号）

1. **describeTradeError 错误口径**（decision_util.mjs:309-318）：TypeError→"连不上，去启进程"，其他→"服务端报错，去看日志"，且 `fetchJson` 抛错带完整 URL（L290-299）、`tradeApiBase` 用传入 hostname 不写死回环（L274-277）。decision.js 全面遵守（L65、L176）。这是全项目应该抄的范式。
2. **trade.js POST 路径的 r.ok 检查 + 422 detail 逐字段拼接**（L17-35，2026-08-13 修复），及 `cmd()` 里"后端拒绝：serverMsg" vs "不可达"的区分（L633-636）。
3. **trade.js 轮询三件套**：1s 轮询带在途保护（L540、571）、4s AbortController 超时（L573-574）、`_consumed`/`_changed` 脏检查不重绘（L548-566，注释写明防响应体带 server_time 白干）、命令后 `invalidatePayload` 强制刷新（L568）。
4. **trade.js 宕机不留"看起来正常"的陈旧数据**：`markOffline` + staleTag 灰化 + "离线缓存"角标（L118-144），失败分支删脏检查 key 保证恢复后强制重绘（L580-595）。
5. **trade.js renderStatus 不覆盖命令反馈**：tdHint 只在急停状态变化时重写（L106-113，E2E 实证驱动的修复）。
6. **回测进度条全家桶**（vera-ui.js:148-192）：缓动不虚构、ETA、阶段文案、aria-valuenow、日志去重、2h 超时、断线恢复。
7. **analysis.js 双序号守卫防竞态**：`_dailyReportSeq` / `_calLoadSeq`（L21-22、537-546、589-605），切月同时作废在途日报请求（L594）——竞态处理的教科书实现。
8. **analysis.js 图表加载骨架**：`chartsShowLoading` 成对开关，finally 必收（L45、133-135）。
9. **零 hex 硬编码**：四个文件 grep 无任何写死颜色；echarts 全走 `getColors()`/`hexToRgba()`（analysis.js:613、661、711-760）；日历渐变用 `color-mix` + CSS 变量（L229）。
10. **农场回填的口径守护**（vera-ui.js:665-772）：27 字段快照→逐字段直写（不走会重置缺省的 applyConfigDict）、板块快照读磁盘不读内存、附带动作 try/catch 不打断核心动作、恢复与横幅同生命周期。
11. **XSS 纵深**：来源 URL `^https?://` 白名单（vera-ui.js:609-611）、报告渲染 marked + DOMPurify + h1 降级（L467-469、798-800）、esc/escAttr 覆盖面高。
12. **键盘可达性已修的三处**：排序表头（trade.js:269-271）、分析日历格（analysis.js:272-274）、通道徽章 Enter/Space（trade.js:1381-1383）。
13. **无障碍细节**：`aria-expanded` 同步（vera-ui.js:37-39、809-811）、`aria-live="polite"`（trade.js:1260）、`role="dialog"`（trade.js:1358）、日历格 aria-label 人话文案（analysis.js:246-253）。
14. **farm 看板签名脏检查防 2 秒轮询闪屏**（vera-ui.js:576-582）。
15. **decision.js 的防御式渲染**：`parseIso` 严格校验防画一屏 NaN（decision_util.mjs:237-241 + decision.js:187-194）、`dominantTone` 忽略表外色调防无效 CSS（decision_util.mjs:211-224）、`fmtEvents` 把原始 JSON 流水化（decision_util.mjs:128-145）。
16. **decision.js 非侵入接线**：包装而非修改 trade.js 钩子，本文件加载失败原有功能照常（L1-14、258-287）。

---

## 修复优先级建议（Top 5）

1. trade.js `get()` 补 `r.ok` 检查（维度 1 第一条）——"服务端报错被显示成无数据"是正确性问题。
2. trade.js / analysis.js 失败文案统一走 describeTradeError 口径（维度 1 第二、三、六条）——项目已有现成函数与血的教训。
3. PC 端补 visibilitychange 暂停轮询（维度 2 第一条）——mobile.html 有现成实现可搬。
4. analysis.js 全部裸 fetch 加超时 + decision.js 加请求序号守卫（维度 1 第六条、维度 6 第一条）——两处都有同文件/同项目范式可抄。
5. decision.js 补键盘 keydown（.dec-row 与日历格）（维度 8 第一、二条）——同一个 bug 在项目里已被修过两次，这是第三次。
