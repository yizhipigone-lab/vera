# CHANGELOG — VERA 质量演进基线

> 记录每次系统性迭代的基线变化、关键改动、测试增量、剩余风险。
> 用户/审计员可凭此追溯"7.5 → 8.3 → 9.0"演进路径。

---

## 2026-09-25 — 持仓页「同一票两轮买卖揉成一行」修复（518880 持仓天数 26 天事件）

**一句话（大白话）**：黄金ETF华安（代码 518880.SH）9月23日重新买入、9月24日卖出，页面却显示「入场 8月19日、持仓 26 天、亏 7,187.50 元」——因为它把 8月那轮早就平仓的老账和 9月这轮新账**缝成了一条**。根因是统计函数把这只代码**有史以来**的成交一锅端：入场取「有史以来第一笔买入」，盈亏全加总。修完按「轮」切开（清仓后再买入 = 新的一轮），页面如实显示：最新一轮 9月23日→9月24日、**持仓 1 天**、81,800 股、亏 **8,998.00 元（-1.24%）**；已平仓列表里 8月那轮单独一条（5,800 股、+1,810.50 元、10 天）。

- **报障与取证（用户指出「8月19日买入的早就平过了，9月23日买入两份才有仓位」）**：只读查 `data/trade/trade.db`，该票共 26 笔成交——第一轮 8月19~21日（+1,869.10 元）加 9月2日遗产仓尾货 200 股（-58.60 元），第二轮 9月23日 14:54 买入两笔各 40,900 股、9月24日 14:54 全卖（-8,998.00 元）。诊断脚本 `research/2026-09-24_518880持仓天数诊断_脚本.py`。
- **修法**：`trade/analysis.py::entry_and_closed` 由「GROUP BY 全历史」改为**按轮次切分**——买入时若上一轮已闭环（累计卖出量≥买入量）则开新一轮、入场时间=本轮首笔买入；卖出总是归入最近一轮（闭环后才到的遗产仓尾货也归上一轮，与旧口径一致）；首笔就是卖出（系统上线前的遗产仓）开「遗产轮」，入场时间如实为 None。`summary[code]`（持仓幽灵行用）= 最近一轮；`closed`（已平仓列表）= 每轮一条、最新在前；`entry_map`（持仓票入场时间）= 当前未闭环轮的首笔买入。159290 遗产仓成本倒推口径（pnl_qty/pnl_turnover）逐轮保留。
- **刻意不动**：`trade_main.py:384` 的 `_hold_days_for_code`（监控器时间止损的持仓天数）仍用「有史以来首笔买入」旧口径——这是 2026-08-06 的有记录拍板（同位置注释：「多算天数只会让时间止损提前，方向安全，不动」）。**注意该拍板对「清仓后重新买入的股票」意味着时间止损可能提前触发**，本次只修展示层，交易行为口径要不要对齐由用户另行拍板。
- **测试（TDD，先红后绿）**：`tests/trade/test_analysis.py` 新增 3 例——两轮切分（518880 复刻：summary=最新一轮/持仓 1 天/closed 两条）、清仓再买入的持仓票入场时间=新一轮首笔、同轮内多次买入不拆轮；3 个既有锁（159290 遗产仓、表内平仓、无 pnl 历史行）原样全绿。全量 `pytest tests/`：与本改动无关的既有失败（test_rotation/test_rotation_lots/test_notifier 等，stash 对照实验确认改动前后失败集合相同、且这些文件不调用本函数）之外全绿。
- **真实库验证（只读）**：`research/2026-09-24_518880轮次切分验证_脚本.py` 对生产 `trade.db` 跑新函数——518880 两行（9/23→9/24 持仓 1 天 -8,998.00；8/19→9/2 持仓 10 天 +1,810.50），159290 主行 229,200 股 -46,579.53 与历史数字一致未被切碎。
- **生效条件**：改的是 `trade/analysis.py`，**热重载端点 `reload_view` 只重载 `view_calc` 不覆盖它 → 需重启 trade_main（8081）**，重启遵守既有纪律（不在 14:54~15:00 窗口、先等 QMT 就绪）。纯展示口径，重启前旧显示不影响任何交易行为。

---

## 2026-09-22 — 公式农场达标线年化下限 15% → 10%（用户拍板）+ 历史判定全量重算

**一句话（大白话）**：公式农场的「及格线」原来是年化 15%，用户拍板降到 10%。真正要改的只有一处——`core/farm_rules.py` 里的 `TARGET_ANN`，因为全系统（粗扫报告、达标榜、各扫描器）都现读这一个数。改完把已扫过的 1173 条历史公式按新线**重新判了一遍**（只重算判定、不回跑回测），达标池从 8 条涨到 19 条；剔除已作废（踩未来函数黑名单）的之后，页面上能看到的达标公式从 2 条变成 **10 条**。

- **口径改动**：`core/farm_rules.py` `TARGET_ANN = 0.15 → 0.10`（|最大回撤|≤15%、笔数≥20 两条腿不动）。docstring 记沿革：09-10 聚焦三公式研究已预演过 10% 门槛（`docs/2026-09-10_公式农场聚焦三公式_最终结论报告.md`），09-22 正式落为农场口径。
- **历史判定重算（不重跑回测）**：判定是 (年化, 回撤, 笔数) 三个数的纯函数、三数已存在档案里，且「选最优组合只在笔数≥20 里选」的规则本次未变 → 直接对 `data/formula_farm/archive.json` 逐条重算 verdict，与 `--rebuild-archive` 全量重建等价，但不依赖 sweep CSV 是否还在。旧档备份 `data/formula_farm/archive_backup_20260922_阈值下调前.json`。
- **结果**：档案判定分布 未达标 976→965 / 达标 8→**19**（样本不足 148、无效 41 不变）；页面可见达标池（作废组已摘出）2 → **10 条**，新进 8 条：GS1514 双底定乾坤(年化11.55%)、GS1459 妖股收割机(11.52%)、GS0920 尖角买(11.27%)、GS0516 抄底建仓(10.90%)、GS1555 小阳共振启动(10.76%)、GS1042 抄底短线(10.67%)、GS0980 第一次多头(10.52%)、GS0675 快速拉伸(10.46%)。另有 3 条新线下达标但已踩黑名单作废（GS1311/GS0681/GS0806），按规矩仍不进榜、禁止回填复跑。
- **顺手修会说错话的文案**（判定数字全走 farm_rules 本就不受影响，但写死「15%」的字会撒谎）：`tools/gs_top9_batch.py` 统计行标签与 `tools/formula_farm/farm_batch_sweep.py` winners 卡片标题改动态读 `farm_rules.TARGET_ANN`；6 处注释/docstring 同步（quantqq_5m_sweep/quantqq_1m_sweep/quantqq_5m_sweep_2010、gs_5m_sweep、gs_make_report、farm_backtest）。
- **测试**：`tests/test_farm_rules.py` 边界锁改 9.99%/10.00%（0.1499 在新线下是达标，旧断言必红）、`test_gs_5m_sweep_report.py` / `test_formula_farm_report.py` 抬头断言改 10%；农场相关 8 个测试文件 **89 例全绿**。
- **生效条件**：改了 `core/` 下 Python → 重启 server.py（重启时四个闸门全空闲、8080 无回测在跑；trade_main 与 scheduler 不 import farm_rules，未动）。

---

## 2026-09-22 — 回测深挖图：滚动指标「左右两条零线错开 20% 图高」修复

**一句话（大白话）**：回测结果页那张「滚动指标（60 日窗口）」图有两条纵轴——左边是夏普、右边是收益/波动率（%）——两个 **0 却画在不同高度**（左在 40%、右在 60%），而图上横贯全图的那条虚线只代表「夏普=0」。于是 15.2% 的滚动波动率看着像贴着 0、甚至像负的。根因：两条轴都交给画图库按各自数据自动取刻度，**从没有任何机制让两个 0 对齐**；改成「以 0 为中心对称」后，两个 0 永远落在正中、永远重合。

- **报障与实测（用户看截图指出「两边轴的 0 是错开的」）**：拿 `output/last_result.json`（2024-01-01~2025-06-30、3374 笔）复算——左轴数据 [−4.79, +3.79] → 自动刻度 [−6, 4]（0 在图高 **40.0%**）；右轴数据 [−70.3%, +134.3%] → 自动刻度 [−100%, 150%]（0 在 **60.0%**）→ **零点相差 20.0% 个图高**，与截图上的刻度标签逐一对上。同图 tooltip 三个数（夏普 2.24 / 年化收益 35.4% / 年化波动率 15.2%）与后端 `rolling_metrics` 完全一致 → 问题只在「画得容易误读」，不在取数。
- **修法（最小改动）**：`web/js/charts_deep.js:55-62` 新增共用常量 `SYM_ZERO_AXIS`（ECharts 的 min/max 支持传函数：`min: v => -Math.max(|v.min|, |v.max|)`），`renderRolling` 两条 y 轴都套它。**一处定义两轴共用**，防「同一条规则写两份」漂移（本项目的老坑类）。修后复算：零点高度差 20.0% → **0.0%**；刻度仍好读（左 −4/−2/0/2/4，右 −100/−50/0/50/100%）。
- **顺带堵一个会画崩的空轴**：修复时发现，净值全平的回测（`roll_std` 恒 0）会让 `rolling_sharpe` **全 None**，而收益/波动率仍有 0 值 → 图照画、左轴却一个数都没有，ECharts 给该轴的端点是 `[Infinity, -Infinity]`，对称化后会算出 NaN 坐标把图整张画崩（违背本文件"数据异常降级不崩"的约定，`web/js/charts_deep.js:7`）。故 `_symSpan` 对"无数据/全 0/NaN"回退成 ±1。取证：`backtest/metrics.py:120` 的 `sharpe.where(roll_std > 0)`。实测四种空/边轴（无数据、全 0、只有负值、NaN）都返回有限范围。
- **刻意不动的**：`web/js/market_dashboard.js`、`web/js/market_position.js`、`web/js/charts.js` 里也有「双轴都不给 min/max」的写法，但那几张两条轴的 0 含义不同（例如净值「元」与回撤「%」），**不该对齐**；本次只修 0 含义相同（都表示"不赚不亏"）的那张。
- **生效条件**：纯前端静态文件，**不用重启 `server.py`**；`index.html` 的 `analysis.js`/`vera-ui.js` 与两个 JS 内的 `charts_deep.js` 引用版本号同步 bump 到 `?v=20260922a`（共 4 处，防浏览器拿旧文件），**刷新页面即生效**。
- **验证（三层，都不靠肉眼）**：①`node --check`（按 ES module 解析）通过；②`tests/js/audit_css_vars.mjs` 退出码 0（185 个变量引用、未定义 0 个）；③**端到端真渲染**——用项目自带的 `web/echarts.min.js`（5.5.0）以 SSR 无头模式跑同一份数据与同一份轴配置，直接量「值为 0 在轴上的像素位置」：修复前 左 141.6px / 右 194.4px（**相差 52.8px = 绘图区高度的 20.0%**），修复后 左 168.0px / 右 168.0px（**相差 0.0px，双双落在 50%**）。临时脚本从 `charts_deep.js` 源码里抠出 `SYM_ZERO_AXIS` 原样代入（不是另抄一份配置）。

---

## 2026-09-21 — ETF 轮动「换腿把旧腿卡死」修复（改 risk_etf2 后旧持仓卖不掉、也不受止损保护）

**一句话（大白话）**：想在设置面板把纳指那条腿从「纳指ETF国泰(513100.SH)」换成「纳指科技ETF景顺(159509.SZ)」，结果**换不了**：收盘后台账明明白白写着「目标 159509」，账户里 98 万的 513100 一股没动，而且从那天起它再也不受 15% 移动止损保护、也不再进对账。根因不是卖出逻辑坏了（对照组实测它一次能卖 43 万股），是**「可卖名单」只按配置算**——配置一改，旧腿就从名单上掉下去，系统连「你手上有没有这个仓」都不看，就当它不存在。修完，改配置等于一次正常换腿，系统自己在下一个信号日卖旧买新。

- **根因（用户质疑「明明持仓为什么会卖不掉」驱动核实，台架实测复现）**：`trade/rotation.py::_execute` 的 working 代码集原为 `codes = sorted(self._rotation_codes(cfg))` —— **只由配置算出**。改 `risk_etf2`/`cyb_etf` 后旧代码掉出该集合，而 `self._lots` 里旧腿仓位还在，同一处失守三连：①卖出量上限 `can_use_rem` 只按 `codes` 建字典，`.get(旧码, 0)` 取到默认 0 → `min(持仓, 0) = 0` → 卖出循环 `continue`，**连下单函数都没进**；②`_fetch_quotes(codes)` 不给旧腿拉行情 → 即便放行也是无价 fail-closed；③QMT `can_use` 回填循环只遍历 `codes` → 风控闸4（T+1 可卖）还会再拦一道。**且旧腿从此不受移动止损**（止损循环带 `c in risk_legs` 过滤）、**也不再进份簿记对账**（`_reconcile_lots(qmt_pos, codes)`）—— 静默且永不自愈（`_migrate` 同样只认 `codes`，重启也没用）。
- **实测证据（用户真实账户数字：总资产 108万8733 元 / 513100 持 43万200 股 / 现金 10万6586 元 / `etf_ratio` 0.9 / 三份锚定周三·周四·周五）**：对照组（配置不改、信号选中创业板50ETF）→ 三份各卖 143,400 股，**430,200 股全卖掉**，证明卖出路径本身完好；改配置组（`risk_etf2=159509.SZ`）→ **三份目标都算对、台账都写了、成交为零**。audit 里**没有** `rotation_fail_closed`（无买一价），说明它在第一道 `qty=0` 就被跳过，没走到第二道。
- **修法（最小改动）**：working 代码集并入本份簿记当前持有的代码 —— `held_codes = {c for (_i, c), v in self._lots.items() if v["qty"] > 0}` → `codes = sorted(self._rotation_codes(cfg) | held_codes)`。**正常运行簿记代码恒为配置代码的子集 → 集合与旧行为逐位一致**（现有换档/止损/对账用例全绿即是证明），只在「配置被改、旧腿还在手上」这一条路上生效。
- **刻意不动的**：`pool_money.rotation_codes` 保持「只按配置」—— 它是「哪些代码不属于股票池」的单一真相源（`trade/pool_money.py:47`），两个用途混一份就是又造一块手表。
- **测试**：`tests/trade/test_rotation.py` 新增 `test_leg_code_change_still_sells_old_leg`，**先红后绿**（修复前 `assert [] == ['513100.SH']` 失败；修复后三份旧腿全卖）。
- **生效条件**：改的是 `trade/` 包 → **需重启 `trade_main`，且必须 ≥15:05**（14:54–15:00 重启会触发启动补偿补跑一轮调仓）。改完配置本身是热生效（`PUT /api/trade/config` → `update_config`），无需二次重启。
- **顺带修复（非本次改动引入）**：`research/pool_matrix.py`（修复前第 65 行）在 f-string 的**表达式**里写了反斜杠字面量（3.12 才允许），让仓级 lint 门禁（`ruff check . --select F821,F822,F823`，目标 3.10）判 `invalid-syntax` 变红 —— 这正是 `tests/test_lint_gate.py` 守护的东西。改为 `lbl = "腿A " + chr(92) * 2 + " 腿B"`，**打印输出逐字符不变**（已比对 repr），全仓门禁转绿。
- **遗留（未修，如实记录）**：①改配置后若旧腿**已不在簿记里**（人工买入的轮动代码、或簿记被手工清过再重启），本次修复覆盖不到 —— 那条路仍会孤儿化，走对账告警人工核对；**因此不要用「清 `rotation_lots` + 清 `lots_initialized` 再重启」来换腿**（那会让旧腿彻底脱离簿记），正确做法就是直接改配置。②台架 `FakeGateway` **下单时不冻结 `can_use`**（只在成交时扣减），所以多轮复现里会重复挂同样的卖单；真实 QMT 下单即冻结 `can_use`，`min(份内, can_use)` 这道兜底会拦住重复 —— **这条只核实到「台架与真实口径的差异」，未在实盘验证**。③本仓另有 1 个既有失败 `tests/trade/test_mobile_contract.py::test_mobile_contract_snapshot`（`/api/trade/analysis/daily_pnl`、`/api/trade/analysis/summary` 响应多了字段），属今日分析页那条工作流，**不在本次改动范围**，需人工核对后决定是否更新快照。④**修复改变了「腿代码填错」的失败模式**（质检补测，台架实测）：填成不存在的代码（如 `159509.SH`）时，修复前是「目标算成避险、但旧腿卖不掉 → 永久卡死」；修复后旧腿能卖了 → 会按「只剩一条有数据的腿」的**降级信号**把旧腿清仓进避险（实测三份各卖 143,400 股，台账写「两腿动量均≤0 (159949 −1.7% / 159509.SH —)」—— 破折号可见，但「两腿都跌」这句原因文案有误导）。清仓方向是避险（安全向），但根子问题仍在：配置校验只查格式不查存在、`compute_momentum_signal` 把「配置里的腿取不到数」当「不参与择腿」而非「数据不足 fail-safe」。**建议后续加守卫**（未实施，待拍板）：配置腿取数为零根时按数据不足处理（维持现状），或保存配置时校验代码可解析。

---

## 2026-09-20 — 公式农场：改编选股栏目「墙误判」修复（栏目被误杀一个半月后恢复收货）

**一句话（大白话）**：闸门①两个栏目每天都在查，但改编选股栏目（用户指定的心水货源）一篇都收不进来——它的文章页带验证码墙标记，爬虫见「验证码」三个字就整篇丢弃；实测墙只挡原著公式，改编后的选股源码在墙下完整可见。修复后该栏目恢复收货，并按用户拍板补抓近一年（约 700 篇）。

- **根因（用户报障驱动核实）**：用户报「检查增量应该是查改编选股，现在查的是通达信栏目」。核实：`state.json` 证明两个栏目**都在查**（改编选股已读位置推进到 9月18日），但 `intake/gaibianxuangu` 只有 **5 篇**（通达信栏目 1232 篇）——详情页结构是「本文选股公式根据以下股票公式改编：【原著被验证码墙挡】+ 墙下完整可见的改编选股源码」，旧墙判定（整页搜「验证码/此处内容已被隐藏」）把整篇判死。实测 70732/70733 号文章：wall=True 但源码完整提取（14/9 行，XG: 收尾）。
- **修法一（墙判定）**：改为「有墙标记 **且** 提取源码里没有选股输出行（`XG:`）才算被墙」——改编选股页（XG 可见）正常入仓；通达信栏目主图墙文（可见的是指标码、无 XG）照旧跳过，保护不削弱；判墙页仍清空 `code` 防误用（旧测试契约保留）。
- **修法二（标题过滤收口）**：「标题含主图直接跳过」也是墙时代的错误「实测」——改编选股栏目**每篇都是选股公式**（标题里的「主图」指被改编的原著，实测 70733 源码完整）。抽成唯一实现 `_keep_article(feed, title)`：改编选股栏目保留主图标题（`keep_zhutu`），通达信栏目照旧排除（那真的是指标不是选股）。
- **修法三（删抽样整栏目跳过的鲁莽闸）**：首次补抓撞出第三层问题——「抽样 2 篇全墙就整栏目跳过」用 2 篇判定 579 篇的命运；实测该栏目**墙分布不均匀**（9月文章可见、1月文章真被墙），且高负载会触发**临时墙**（同一篇 70739 探针时判墙、几分钟后实测可见）。删掉该闸，全量模式与增量一致**逐篇判定**；被临时墙误伤的篇目本轮不进仓，下次全量跑按「归档里没有就是新」自动重试，自愈。
- **规模实测定调（用户拍板）**：该栏目共 111 页 **9,300+ 篇**（约 15 年存量，每天约 2 篇）——全量补抓爬取 4~5 小时、粗扫按月计才能消化，用户拍板**只补近一年（约 700 篇，`--max-pages 7`）**。
- **测试**：`test_formula_farm_crawler.py` 新增 2 例（墙标记+可见 XG 源码不判墙 / 栏目化标题过滤），既有 4 例（含真墙页判墙）全绿；全量 `pytest tests/` 绿（退出码 0）。
- **生效条件**：爬虫是闸门①的子进程脚本，**无需重启**，下次检查增量即生效。
- **顺带发现（已另报用户）**：生产尾盘自动买入的 QUANTQQ 公式在当前通达信里已不存在（旧安装 `E:\NEW_TDX` 删除时未迁移），因择时闸拦截一直未触发，需手工补回。

---

## 2026-09-20 — 舆情页"看不见"三连修（L1 心跳 / L2 扫到什么 / L3 健康度）+ 关注范围接 128 行业表 + 取数换大水管

**一句话（大白话）**：此前这台机器**没有仪表盘**——每轮扫描的统计只写日志、扫过的 360 条新闻页面上一条看不见、榜单上全是指数（因为新闻那条腿五周来一条没触发）。现在页头有一句人话的健康度 + 每轮心跳，多了一张「扫到的新闻」卡片（每条的判定与"为什么没触发"都摆出来），关注范围从"手写 3 个概念"扩到"**你 review 定稿的 128 行业表**"（P1+P2 共 47 个词），取数从"一个词一次请求"换成"**一次拉大水管 + 本地筛**"。

用户拍板（2026-09-20，三轮讨论）：L1/L2/L3 都做；关注范围选 **B**（接 `policy_kb/tongdaxin_priority.json`，P1 重点盯/P2 次重点/AVOID 不盯）；取数**换**大水管；明细**完整版**（两张表都建）。

- **L1 心跳**：`news_dedup` 新增 `tick_log` 表 + `log_tick`/`recent_ticks`（`SCHEMA_VERSION` 2→3，`purge_old` 一并清理）；`sentiment_pipeline` 每轮恰落一行，并记**失败原因**（拉新闻/去重/打分/快照各自打标）+ 大水管吞吐（`源 110 条→命中 29`）。页头显示"今天已扫 N 轮，最近一轮 HH:MM:SS（扫/新增/打分/触发）"。
- **L3 健康度**：`sentiment_api._health_text` 是**纯函数**，四态分开说 —— 非交易日（**不冤枉调度器**）/ 今天 0 轮疑似停机 / 没有新新闻（源里还是同一批）/ 有新闻但没过线。依赖 `utils.trading_calendar.is_trading_day`（读不到时按交易日对待，宁提示勿掩盖）。周一实测前它已如实写出"今天是非交易日"。
- **L2 新闻台账**：`news_log` 表 + `log_news_item`/`news_between`；每条新闻五态判定 `pass`/`weak`（带"情绪分 +0.42 未达 0.60"这类差多少）/`error`/`missing`/`truncated`；新端点 `GET /api/sentiment/news`；页面新增「扫到的新闻」卡片（**无条件渲染**，无告警时也显示）。**关键回归锁**：`missing`（打分器没返回）**绝不标记 `news_seen`** —— 否则那条新闻永久丢失，正是"polarity=NULL 静默失效五周"的同族坑。
- **C1 关注范围**：`policy_kb.policy_tagger.sector_names_by_priority(tiers)` 新公开出口（与 `tag_stock` 共用 `_load_priority_map`，不写第二个解析器）；`sentiment_pipeline.watch_keywords` = 手写概念词 + P1/P2 行业名（去重，AVOID 排除）；**删掉 `concepts[:3]` 截断**。实测词表 47 个（P1×12+P2×27+概念，重叠去重后），AVOID（煤炭开采）未混入。
- **C2 取数两腿**：腿A **大水管 + 本地筛** —— 复用 `core.market_event_sources.fetch_all_candidates`（财联社电报/同花顺/新浪/华尔街见闻/美联储 RSS 五源），命中关注词才进流水线（**筛在前、打分在后**，LLM 预算只花在该看的东西上）；腿B 对 **P1 按词补搜**（东财 `stock_news_em`）并**轮转**覆盖全部 P1（旧代码永远问同样 3 个词）。新增配置 `watch.tiers / feed_days / keyword_fetch_per_tick`。两腿互相独立 fail-soft。
- **实测（真实网络，只取数不调 LLM）**：大水管 **110 条 → 本地筛命中 29 条**（财联社 6 / 华尔街见闻 5 / 新浪 2 / 同花顺 1 / 按词补搜 15），命中内容确实相关（长鑫半导体设备扩产、Anthropic 算力/光互联、海光信息芯片）。
- **测试**：新增 `tests/brain/test_news_dedup_tick_log.py`（9 例，含 fail-soft）、`tests/brain/test_sentiment_watch.py`（10 例，含轮转与"不命中就筛掉"）、`tests/js/test_sentiment_heartbeat.js`（19 例，含"后端老版本无 ticks 不许渲染 undefined"）；`tests/test_sentiment_api.py` +9 例（健康度文案 7 + 端点 2）。全部先红后绿；相关套件 **119 passed**、Node **19 passed**。
- **生效条件**：`sentiment_api.py`/`sentiment_pipeline.py` 改动需**重启 `server.py` 与 `scheduler`**（用户已于 15:25 重启，实测 `/summary` 已带 `ticks`/`health`、`/news` 通）。前端 `sentiment.js` 版本号已 bump `20260920a→b`（防浏览器缓存旧 JS）。
- **排查中发现（未处理，留给用户决定）**：同日 15:25 重启后 **scheduler 有两个实例在跑**（12:19 旧实例未退 + 15:25 新实例）——重复实例会双跑 job（双份 tick 与推送），建议确认后杀掉旧的那个。

---

## 2026-09-20 — 公式农场粗扫：增量落盘与中断自愈（+ pytest 落盘竞态根治）

**一句话（大白话）**：粗扫报告/成绩单/达标榜档案原来只在**整轮跑完那一刻**才写盘，中途重启 = 已扫部分全被吞（9/20 实测中断三轮，~60 条已扫公式连达标榜都进不去）。现在**扫一条落一条**，启动时自动把中断残留补登进档案；顺带根治了 pytest 把假状态写进生产 `last_status.json` 的竞态。

计划书 `docs/plan/2026-09-20_公式农场粗扫增量落盘与中断自愈_计划书.md`（含实测证据：今日四轮运行时间线 / archive.json 缺失实测）。

- **根因链**：sweep CSV 本来就逐条落盘（中断不丢、不重扫），但三样"给人看的产物"——报告 md、`backtest_summary.json`、`archive.json`——只在 `farm_backtest.main()` 终点一次性写；且档案更新只收「本轮启动时的待扫清单」，中断轮扫完的公式已有结果 → 下轮不在待扫清单 → **永远进不了档案**（实测 GS1352~GS1418 均不在 822 条档案中）。
- **工作项 A（farm_backtest.py）**：装配抽成 `_assemble_batch`/`_checkpoint` 唯一实现（循环与终点共用，防口径漂移）；每条公式四种结局（达标/未达标/停牌/作废）扫完即 `_archive_one` + `_checkpoint`；报告/成绩单改 tmp+replace 原子写；写失败 fail-soft 不杀整轮；运行日期开头取一次（防跨午夜劈叉）；新增 `_sync_archive_orphans` 启动孤儿对账（有结果/停牌但不在档案的整批一次补登，无需人工 `--rebuild-archive`）；终点非 rebuild 的整批入档删除（循环已逐条做）。
- **工作项 B（前端）**：④卡片成绩单前缀按状态切换——闸门运行中且日期是今天 →「**本轮已扫:** 」，否则「上次成绩: 」。纯函数 `farmSumPrefix` 落 `web/js/farm_util.mjs`（仿 decision_util.mjs 先例，node 直测 6 项）；不改 farm_summary.py（避开 server 重启）。
- **工作项 C（farm_runner.py）**：`_work` 改为全程局部变量算定终态，**`_save_last()` 完成后才赋值 `run.status`/`_current`**——旧版状态先翻 done、落盘在后，测试 `_wait_idle` 一返回 monkeypatch 拆除路径补丁，落盘就写进生产文件（今晨 09:05 实测投毒）。确定性测试锁：慢速 save 放大竞态窗口，修复前必红修复后必绿。
- **测试**：新增 `tests/test_farm_backtest_incremental.py` 4 例（部分进度落盘 / **中断不吞已扫**核心回归 / 孤儿对账补登且不重扫 / 原子写无 .tmp 残留）+ `test_farm_api.py` 竞态 1 例 + web 6 项；全部先红后绿；全量 `pytest tests/` 绿。
- **生效条件**：A+B 无需重启（闸门④每轮是新子进程，页面刷新即生效）；C 需**闸门空闲后重启 server.py**。重启前先把 `data/formula_farm/last_status.json` 里 09:05 那条假 check 记录删掉（它还在运行中 server 的内存里，直接改文件会在下次落盘时复活）。**本次实施未代重启任何进程、未打断正在运行的粗扫。**
- **已知余留**：当前正在跑的轮次仍是旧代码（进程 13:41 加载），它终点只入档本轮 34 条；中断残留的 ~60 条由**下一轮**（新代码）启动时孤儿对账自动补全。

---

## 2026-09-20 — 脆弱期 P0 修复五项：门禁转绿 + raw 底账去 tick + 看门狗 + 交易文件日志 + 部署快照

**一句话（大白话）**：唯一门禁红了半个多月没人看（里面还藏着一个真 bug）；行情 tick 把审计底账淹到 99.99%；交易进程死了没人知道、也没日志文件。这次把五件事修掉，全部先红后绿。

计划书 `docs/plan/2026-09-20_脆弱期P0修复_计划书.md`（含 item 6「成交先落库再改内存」复核推翻记录：现有"先 book 后 DB + reconciler A3 补写网"已自愈，倒置反而开一个不自愈的洞 → 撤销）；逐行审查 `docs/audit/2026-09-20_脆弱期P0修复计划书_逐行审查.md`（41/41 断言属实，4 条 P1 已补入计划书）。

- **item 7 门禁转绿**：`trade_main.py:906` F821 真 bug（`logger`→`_logger`，0561824 引入——降级路径的诊断日志会自炸成 NameError 被事件引擎吞掉）；`core/market_position_runner.py` 的 10 条 F822 是 `__getattr__` 动态转发的静态误报（逐名运行时验证可取），加 noqa 并写明"不许改回按值 import"。新增 `tests/test_lint_gate.py` 把 CI 门禁命令原样搬进 pytest（ruff 缺失则 skip）——门禁再红会拉着全量测试一起红。**注意**：CI 的 Test 步骤历史上从未执行（历次红全是 lint 短路 skipped，GitHub API job 步骤实测），修绿后它是史上首跑，ubuntu 侧大概率有新红要修。
- **item 1 raw 底账去 tick**：`_wire` 加 keyword-only `raw`（trade_main.py），tick 接线传 `raw=False`——实测 8 月归档 3,740,113 行里 tick 占 99.99%，真实回报仅 482 行。tick 仍照常入引擎（monitor 心跳/止损靠它），只是不落盘。测试锁"tick 不落 raw + 仍到引擎"（tests/trade/test_raw_log_no_tick.py）。历史文件不删。
- **item 5 交易进程文件日志**：`main()` 里 `attach_file_logger(output/logs/trade_main.log, max_mb=50)`。**必须只在 main()**——模块级挂 = 测试 import trade_main 就污染生产 output/logs/（2026-07-27 投毒事故同类；tests/trade/test_trade_main_logging.py 锁）。
- **item 4 看门狗**：scheduler 增 1 分钟 interval job（仅交易日+交易时段，复用 `TRADING_HOURS`），探 8081/8080（强制 3s 超时），连续 2 次失败推飞书（红）/恢复推绿。判定是纯函数 `scheduler/health.py::evaluate_watchdog`（`monitor_healthy` 三态 None 不误报；`last_tick_age_s` 缺失走回退）；状态存模块级 dict；人工停机（`.vera_stopped` 标记）整轮跳过。配套 **4b**：`TradeApp.last_tick_age_s` property + `/api/trade/status` 增同名字段（手机契约快照已按流程同步，diff 只此一个字段）。
- **item 9 部署快照**：`tools/deploy_snapshot.py`——`output/deploy_snapshots/<ts>/` 存 changes.patch（git diff HEAD）+ new_files/（未跟踪文件，>10MB 只记哈希）+ manifest.txt（HEAD/分支/status 全文/全源文件 sha256），自带四项链式自验；`--tag` 打 `deploy-<ts>` 附注 tag（message 写明 dirty 状态）。上线三步：跑快照 → 重启 → --tag。

**生效条件**：item 1/4b/5/7a 需重启 trade_main（≥15:05，且农场闸门空闲）；item 4a 需重启 scheduler；item 7b 需重启 server.py；item 9 立即生效。**本次实施未代重启任何进程。**

---

## 2026-09-20 — AI 叙事扩容两节（三大指数 + 避险腿黄金）+ 重出失败不再抹掉好叙事

**一句话（大白话）**：体温表叙事原来只讲沪深300 一个指数、完全不提避险腿 —— 而 9/18 实测
三大指数十年位置是 **上证 92.7% / 沪深300 74.0% / 创业板指 93.6%**（差 19.6 个百分点，结构分化），
轮动 10 年回测又证明「真正的刹车是双腿双负→黄金」。现在这两块都进叙事（用户拍板）。

- **三指数并列**（`market_narrative._facts_block`）：沪深300 那行（四格结论的锚，口径不动）之外
  新增「三大指数十年位置」一行，算出最高最低差 + 人话判定（`_spread_note`：≥15pp 记
  "分歧大（结构分化）"、≤5pp 记"位置接近"），阈值只写一处。
- **避险腿**（`_gold_facts`）：黄金ETF **518880.SH**（与 trade 轮动默认 `gold_etf` **同码**；
  代码登记在 `core/market_position_io.GOLD_ETF` —— 叙事模块不许 import trade，业务铁律 1）：
  收盘价 + 近 20 交易日涨跌 + 同期沪深300 涨跌 + 领先/落后几个百分点，**零联网**（读本地日线缓存）。
  两条纪律：两条腿**末根日期不同就整节不写**（宁缺勿混 —— 混日期正是当天那个 bug）；序列缺失/
  异常一律 fail-soft 返空，绝不打印 None。
- **黄金也纳入日常补拉**：`kline_cache_maintenance._index_pool()` 由"三大指数"扩为
  "非个股跟随池"（`INDEX_SPECS` + `GOLD_ETF`，同一真相源）—— 否则黄金缓存停更时那节会静默消失。
- **重出失败不再抹掉好叙事**（`_narrative_rewrite` 第三道守卫）：实测快速档偶发"只回 reasoning、
  content 全空"（重试 3 次仍空）→ 兜底草稿被机检拦 → 状态 rejected、文本清空，把几分钟前
  **同一交易日**的 ok 叙事顺手覆盖丢了。现在同交易日已有 ok 文本就保留 + 记 `refresh_failed`；
  只有"当天从来没有 ok 版本"才把失败如实落盘（不许静默）。
- **token 口径按实测重校, 最终按用户拍板「不设上限」**：`llm.providers.LLMClient.chat` 支持
  `max_tokens=None` = **该字段不发给服务端**（不是发 `null` —— 部分网关对 null 直接 400，
  会被松耦合吞成"LLM 没回话"，叙事静默消失），由 API/模型自身上限兜底；默认仍是 1024，
  既有调用方（复盘/政策提取/快速对话）行为不变（有测试锁三条）。叙事侧先经历 600 → 1400 →
  2600 三档**都被 deepseek 推理吃光**（content 全空 → reasoning 草稿兜底 → 被机检拦 →
  当天没有叙事），改不设上限后首次调用即出稿；`timeout` 同步放宽到 120s。
  机检长度上限 600 → **1200**（实测同类 facts 正常输出 **609~822 字**，600 会误杀正常叙事 ——
  容忍带只该拦几千字的跑飞），提示词写 200~520（最多 700）。
- **实测线上文案**：重出后 **797 字、机检通过**，含「上证 92.7%、沪深300 74.0%、创业板指 93.6%，
  最高最低差 19.6 个百分点，分歧大」「黄金 ETF 收 9.009 元，近 20 个交易日 -4.04%，同期沪深300
  -2.41%，黄金落后 1.62 个百分点」。
- **测试**：新增 8 例（三指数分歧 / 黄金节 / 两腿日期不一致宁缺勿混 / 缺缓存不打印 None /
  token 下限锁 / 长度带 / 重出失败保留 + 无旧版仍可见），全量 pytest 全绿。

---

## 2026-09-20 — 大盘位置指数缓存停更根治：指数池纳入日常补拉 + 两处静默缺口守卫

**一句话（大白话）**：AI 叙事里「沪深300 十年位置 73.4%（截至 2026-09-16）」把 9/16 的数
说成"今天" —— 根因是**指数的日线缓存从来没有任何自动化在刷**（日常补拉只认 `--universe 50`
沪深A股，31 个指数/ETF 代码 **0/31** 在里头），停更两天无人知。现已把指数池纳入补拉判据与
补拉命令，并给"整段空却自称不滞后""叙事混日期"各加一道守卫。

- **根因链（逐环实测）**：`market_narrative._facts_block` 逐字段回退 → `data/market_position/daily.jsonl`
  9/17、9/18 的 `indices` 段 3 指数 × 7 字段**全 null** → `mpio._index_series` 直读的
  `data/kline_cache/1d/{000001.SH,000300.SH,399006.SZ}.parquet` 末根停在 **2026-09-16**
  （`manifest.db` 显示这 31 个非个股代码最后一次 fetch 就是 9/16，此前无任何日常记录）→
  `tools/backfill_kline_cache.py` 只按 `get_stock_universe("50")` 出代码（实测 0/31 命中）。
  **TDX 直取实测有 9/17、9/18** —— 不是源的问题，是没人去拿。
- **治本（A）**：`core/kline_cache_maintenance.py` 新增 `_index_pool()`（唯一真相源 =
  `market_position_io.INDEX_SPECS`，**不写第二份代码表**）+ `_index_pool_stale()`（末根 < 应有交易日）；
  `ensure_cache_fresh` 判据纳入指数池（原判据只看 5m/1d/1m 股票周期 → "股票新鲜"时秒回 no-op，
  指数永远没人补）；`_run_refresh` 末尾多跑一段 `--codes <指数>` 且 **`--end` 给"应有交易日"而不是
  "今天"**（否则非交易日/收盘前跑会因"尾段拉不到新 bar"被记成停滞冷却 72h —— 手跑实测抓到并修）；
  `tools/backfill_kline_cache.py` 新增 `--codes`（显式代码清单，给了就忽略 `--universe`）。
- **守卫（C1，记录侧）**：`_build_record` 新增 `indices_missing` 字段 —— 记录的 `stale` 只比日期
  （`d < expected_date`），指数整段空时它照样说"不滞后"（实测 9/17、9/18 就是这样）；体温表在指数
  缺失时先印一行红字点明「**缺的是缓存不是行情**」，旧记录无此字段时按"真缺"处理（向后兼容）。
- **守卫（C2，叙事侧）**：`_facts_block` 逐字段记数据日期，与快照日期不一致时**每个数字都带自己的
  日期**并追加一行「⚠ 数据日期不一致」（原实现只有位置那一行带日期，叙事把两句都说成"今天"）。
- **历史数据修复（B）**：31 个停更代码（3 指数 + 28 ETF）补到 9/18（每文件 +2 根、首根不变、
  历史不截断）；重跑 `collect` 补上 9/17、9/18 的 indices 段，**跑前有值的字段跑后 0 条变 null**
  （逐字段回归对比）；`refresh_fill` 重出叙事，位置数已回到 9/18（74.0%）。
- **测试**：新增 12 例（先红后绿：`TestIndexPoolFreshness` 5 / `TestIndicesMissingGuard` 3 /
  叙事混日期 2 / 同日不误报 1 等）；全量 `pytest tests/` **exit 0**。
- **留档两个副作用**：①手跑 CLI 曾把 3 个指数写进 `backfill_state` 的 `stalled`，已清理，并由
  `--end` 修法从源头堵住；②全量 pytest 在**与 TDX 取数并发**时出现过一个 `tests/test_market_events.py`
  的 `os.replace` **WinError 5** 抖动（单跑必过、无并发复跑 exit 0），是 Windows 文件锁抖动而非逻辑错，
  以后跑全量别同时开取数工具。

---

## 2026-09-20 — 公式农场「说运行中却没进度」三层病因全修（进度可见性）

**一句话（大白话）**：点④开始粗扫后页面只显示最后一行原始输出，一个公式跑十几分钟
那行字一动不动，停在上一阶段的诊断行「TQ数据连接已关闭」上——看着像报错。现在显示
「进度 13/94 · GS1369 · 已 44 分钟」，实时输出贴在正在跑的那张卡里。

- **病因一（信息被中间层吞掉）**：`tools/formula_farm/farm_backtest.py::_sweep` 用
  `subprocess.run(capture_output=True)` 调 `gs_5m_sweep`，而后者每 25 组就 flush 一次
  的 `[GS1369 shard 0] 25/36 (0.05/s, ETA 7min)` **正是唯一的"还活着"信号**——全攒在
  内存里，只在每个阶段结束回放最后 2 行。→ 新增 `_run_stream()`（Popen + 中继线程，
  stderr 合流，超时语义与 `subprocess.run` 一致：杀子进程再抛 `TimeoutExpired`），
  三段输出边跑边转；中继线程内的打屏失败吞掉（线程死掉 = 管道没人读 = 子进程写满
  缓冲区后永久卡死）。
- **病因二（进度从来没被解析）**：`core/farm_runner.py` 只把**最后一行原文**当 `stage`
  ——而 `farm_check.py` 头注释白纸黑字写着「阶段行 [1/3].. 供 FarmRunner 解析进度」，
  这个解析**从来没实现**。→ 新增 `_progress_of`（阶段行 `[i/N]` → progress，
  坏值/非进度行一律 None 不编数字）+ `progress`/`elapsed_s` 字段（已跑秒数在服务端算，
  不受浏览器时钟影响）；`stage` 顺手去掉前导空格。
- **病因三（进度日志贴在另一张卡里）**：唯一的进度线索 `log_tail` 被写死在闸门①
  「检查增量」卡片的 `farmNewList` 里，而用户在④卡片看「运行中」——等于不存在。
  → 四张卡片各有 `farmTail{Check,Onboard,Verify,Backtest}` 容器，只往**正在跑的那张**
  写；文案 `_farmRunningText`（`运行中 · 进度 13/94 · GS1369 · 已 44 分钟`，
  无进度标记的闸门如③复核回落 stage，后端老版本无字段则只写「运行中」）。
- **顺手**：成绩单加「上次成绩:」前缀——「运行中 + 9月16日 达标 0」原样摆着会被读成
  本轮跑完的结论。`vera-ui.js` 版本号 bump `20260919b → 20260920a`（防浏览器缓存旧 JS）。
- **测试**：`tests/test_farm_api.py`（`_progress_of`/`_elapsed_since` 纯函数 + 真子进程
  端到端进度）、`tests/test_farm_backtest_stream.py`（**实时性**：子进程还没退出第一行
  就已转出，这是旧代码必红的判据）、`tests/js/test_farm_progress.js`（21 例：四卡容器
  接线 + 按标记抠出纯函数**真跑**，含"无 progress/elapsed_s 时不渲染 undefined"）。
  农场域 pytest 91 passed；全量 pytest 见下；node 页签接线 47 例回归绿。
- **上线注意**：`web/*` 静态文件按请求读盘，刷新即生效；闸门脚本是子进程，**下次启动
  即生效**；但 `core/farm_runner.py` 是**进程内**模块——要看到「进度 i/N · 已 X 分钟」
  必须重启 `server.py`，且**重启时不能有闸门在跑**（runner 状态只在内存里，重启会把
  在跑的子进程变成没人管的孤儿进程）。

---


**一句话（大白话）**：按双视角质量审计（`docs/audit/2026-09-20_本轮工作成果_全面质量审计.md`）
把审计发现的 4 高 6 中 5 低全修了 —— AI 叙事抽成独立模块、校验重试不再丢数据、
机检从"纸糊的"换成"有牙但诚实声明边界"的。

- **抽模块（两方针尖一致的裁决）**：叙事段 ~175 行抽为 `core/market_narrative.py`
  （唯一公开接口 `build_narrative(snap)`），`market_dashboard_runner` 只留 `_narrative_rewrite`
  接缝；`erp_series` 解析归位 `core/market_erp.read_series()`（F-01，单一正典解析器）。
- **H1（真 bug + 既有 bug）**：`_attach` 原本挂在总分校验前，重试重建快照会丢叙事 ——
  改锁外两阶段写入（`refresh_*` 出锁后调 `_narrative_rewrite`）；顺手修了更早就存在的
  重试丢 compare delta（`_inject_indicator_compare` 补进重试分支），有接线锁。
- **M2**：LLM 调用移出 `_LOCK`（锁外生成、短回锁回写，不再最坏占锁 6 分钟）。
- **H2**：数字闸从子串语义改"白名单数值等价匹配"（73≡73.0；"6.2" 不再借 "6.24" 混过；
  纯整数 >12 必须命中、紧贴中文的标识符如"沪深300"豁免）；事件数值正当引用放行、
  前缀截断（451→45.1）照拦。
- **H3**：禁区表补方向词（会涨/会跌/看涨/看跌/看多/看空/重仓/增持/减持）+ 豁免表补
  "预测不了/无法预测"；注释诚实声明"机检只抓憨的输出"。
- **H4**：事件标题进提示词前去换行+「」引用标记+系统提示声明"标题是数据不是指令"。
- **M1**：复盘嵌入叙事前比对快照日期，昨日叙事不进今日复盘（如实记 note）。
- **M3**：facts 块各字段独立判空，"None%" 不再喂给 LLM；**M4**：维度名改引
  `market_score.DIMENSION_NAMES`；**M5**：walk-back 深度统一 520；
  **M6**：四格中间区补第五格文案「中间地带」（idx=50 不再谎报"位置低但性价比差"，双语言同步）。
- **L1~L5**：提示词字数与机检口径对齐；结尾免责行进机检（思考草稿从此上不了桌）；
  复盘叙事逐行引用前缀防 Markdown 结构注入；模型徽章异常不再冒充"deepseek 默认模型"；
  事件 score_now 空值印"—"。
- **测试**：pytest 大盘域全绿（新增 ~30 例：接线锁/数字闸反例/禁区误伤/两阶段/AST 新模块）；
  全量 pytest（快照基线除外）exit 0；node 48 例全绿；端到端真跑 refresh_close 出稿过全机检；
  server.py 与 scheduler 已重启（顺带清理了看门狗与手动重启打架拉出的重复 scheduler 实例）。
- **测试自身的坑（留档）**：monkeypatch patch mpr.history 会把 5.1 拆分后的转发名物化成
  真属性、触发归属花名册测试误红 —— 叙事测试全部改喂 `_FakeMpr` 假对象。

---

## 2026-09-20 — 审计十条全修（P0-1…P3-8）+ 文档数字勘误

**一句话（大白话）**：把 2026-09-20 质量审计报告里 P0/P1/P2/P3 **全部 23 条**都处置了
（用户指令「全部修」）。最刺眼的一条：**`start_vera.bat` 的 QMT 等待块在 cmd 解析期就
崩，三个进程一个都起不来** —— 它此前只有"字节级检查 + 探针单跑"，从没被真正执行过。

- **P0-1（致命，自复现）**：`start_vera.bat` 块内 echo 含未转义 ASCII 圆括号 → cmd 解析期
  提前闭合 `for` 块，报 `... was unexpected at this time.` 并中止（实测停在 `[2/3]`，
  交易与调度都没起）。重写：提示语无括号且整行移出块外 + label 流
  (`:qmt_ready/:qmt_config_error/:qmt_wait_done/:qmt_start_trade/:qmt_after_trade`)、
  退出码 0/1/2 语义分流。新增 `tests/test_start_vera_bat.py` **5 例**（字节不变量
  GBK/CRLF/无 BOM + 三场景桩化执行）；把带括号的 echo 注回 → **4 例红**。
  **教训**：字节检查 ≠ 可执行正确性；`.bat` 必须**真跑**才算验过。
- **P1-4/P3-7（口径守卫 + 第 5 份复刻）**：口径校验收口成 `engine.check_prep_caliber`
  （fail-closed），四个 tools sweep **加上此前漏掉的第 5 份复刻
  `research/gupiao_stability_sweep.py`**（新收编到 `prepare_matrices` 接缝）。
  **顺手自捕获一个真 bug**：四个 sweep 的守卫调用写在模块级 `_load_cache` 里，而
  `check_prep_caliber` 只在 `do_prep` 内**局部 import** → 一调用就 `NameError`
  （"实现搬了、消费点没搬"的又一例）。改模块级导入 + 5 例**功能**测试（真调
  `_load_cache`，不是读源码文本）；改回局部导入 → 红在 `NameError`。
- **P1-1/P2-10（前端错误契约）**：`mobile.html` 新增 `tradeParse`（查 `r.ok`、读
  `detail‖error`）—— 此前 POST 不查 `r.ok`，急停/解除急停遇 500 会按"成功"走；
  `api.js`/`trade.js`/`decision_util.mjs` 统一认两种形状（server 的业务软失败是
  `{success:false,error}`，没有 `detail`）。`tests/web/test_error_contract.mjs` 13 例
  （真抽 `tradeParse` 跑假响应）。
- **P2-1/P2-2/P2-3（事件引擎三处）**：①`read_via_consumer` 的 `timeout` 变成**整件事**的
  预算（入队+等结果）—— 此前入队走关键事件 5s 超时，HTTP 线程最坏卡 ~7s；超时后
  `fut.cancel()` 让消费者线程**不再白打一次柜台**。②`pump` 截止判定移到循环入口，
  docstring 改成诚实的"duration 是**至少**不是至多"。③补 pump **接线**测试：
  把 executor/rotation 的等待改回 `time.sleep` → 两条测试分别红（原先 104/33 例全绿）。
- **P2-4（测试有效性）**：`test_prepare_matrices_parity.py` 增"脏数据"用例 ——
  `end_time` 透传、停牌 NaN 的 ffill 与 tradable、low 列交集、非标准 bar 过滤。
  **四个突变全部红**（此前这四个改动都是绿的）。
- **P2-5/P2-6/P2-7（拆包引入的 patch 面，本批最重）**：六个块模块 + runner 原先各自
  `from core.market_position_io import _f, history, _upsert…` —— 拿到的是 **import 时刻的
  值副本**，于是 **patch 基座（测试隔离/参数扫描）是静默 no-op**。现在全部改**调用期
  取值**（`mpio.X` / `market_xxx.X` / `mpp.X`），`_FORWARD_TABLE` 扩成**归属名册**（含
  纯数学层 12 名，旧入口 `mpr.similar_days` 不再依赖"碰巧还有显式 import 活着"），
  `_IO_STATE_NAMES` 补 `INDEX_SPECS`。新增 `TestOwnershipRoster` **4 例**；三个突变红
  （含"模块级按值别名 `_upsert`"→ 回填/预热测试红，证明"patch 基座"是真的生效路径）。
  **两处测试 patch 点跟着实现搬到 `mpio._upsert`**（不搬就是静默 no-op）。
- **P2-8（隔离面）**：conftest 补 `DASHBOARD_PATH`/`EVENTS_PATH`/`kline_cache_maintenance`
  缓存根（含锁与日志）—— 同目录两条真落盘路径此前漏隔离（latent 投毒面）；
  隔离守卫扩到**五条**落盘路径，去掉新隔离即红。
- **P2-9（tools 清单判据）**：`tools/` 列入生产目录、`.bat` **递归**扫、根级 `.md` 只算
  文档引用。重跑实测 **129 个脚本 → 生产 74 / 仅文档 38 / 零引用 17**；审计点名的
  `gs_top10_prep_parallel`（只被 `tools/gs_launch_prep.bat` 拉起）与
  `formula_pipeline/verify_parse`（被同目录 run_pipeline 调用）已正确判为生产。
- **P3 全清**：runner 删 7 个死 import + 六个**孤儿注释横幅**（109 行，内容随实现搬进
  属主模块 docstring）→ 557 → 430 行；删 `scheduler/trading_calendar.py` 兼容 shim
  （grep 零引用）；回填守卫探活地址改**解析链**（`--api-base` > env > `.env` > 8081，
  治"`trade_main --api-port` 一改守卫就 fail-open 照写 trade.db"）；探针加 `--fake`
  对齐三条 fake 来源，`start_vera.bat` 加 `VERA_TRADE_FAKE=1` 跳过等待；
  `shadow_compare.py` 文档改指 `trade.rotation_feed.IndexFeed.closes`；
  `trade/rotation.py` 标注**三个日志通道**（按通道查告警别漏）。
- **数字勘误（P3-1，按 `git show <rev> | wc -l / 字节数` 重测）**：五刀累计
  **113.6KiB → 25.4KiB（-77%，2166 → 606 行）**；第三刀末 **1294 行**（原写 1273）；
  thermometer **644 行**（原写 641）；新模块合计 **99.9KiB**（原写 96KB）；
  raw 轮转 **7 例**（原写 8 例）；"新增 3 个测试文件"实列 4 个；
  计划书 §1.3 标注"按月分文件"改法**未按原样落地**（实际是启动时归档非当月行、读写侧零改动）。
- **验证**：全量 pytest **3108 例通过 / 7 skip / 0 失败**
  （排除并行会话在研的 `tests/test_market_dashboard.py`；该文件 3 例失败来自那份
  未提交的 `core/market_narrative.py` 新校验，与本次无关）；
  前端 15 套件仍只 `test_brain_viz.mjs` 1 例历史遗留红；生产
  `data/market_position/*` 四个文件 mtime 未变（隔离实测；另有
  `data/scheduler_heartbeat.json` 被既有调度器健康测试刷新，与本次改动无关）。
- **重启要求**：`trade/events.py`/`trade_main.py` 改动 → **trade_main 需重启且 ≥15:05**；
  conftest/tests/docs/tools 改动无重启要求（用户尚未部署）。

---

## 2026-09-20 — 大盘仪表盘「AI 叙事解读」上线（用户拍板三条）

**一句话（大白话）**：「合起来读」模板卡下面多了一段大模型写的人话叙事 —— 把温度、五维、
事件分（主观解读）串成一段"今天到底怎么回事"，**随仪表盘刷新烤进快照**，页面和飞书复盘
都只是读；模板卡是锚，AI 只做解说员。

- 用户拍板：① 随刷新生成不随页面现调；② 顺手进 15:55 飞书复盘推送；③ 机检作废/未生成
  **显示一行原因，不许静默**。
- **双保险守门**（计划书 `docs/plan/2026-09-19_大盘仪表盘AI叙事解读_计划书.md`）：提示词禁区
  （不预测/不指挥）+ 输出三道机检（禁区词 / 编造数字 / 长度 80~600 字）。**真实冒烟机检立功**：
  第一次实测 deepseek 三次只回思考草稿（reasoning_content），草稿里的「预测」二字被机检
  拦下判 rejected——思考草稿本来就不该上桌。修复：max_tokens 600→1400（推理也吃额度，
  600 被吃光才导致 content 全空）+ 禁区检查先豁免「不预测/不是预测」等合规否定句
  （否则模板自己的免责语都会被误伤）。修复后实测出稿 465 字，全机检通过。
- 四格结论锚：Python 侧 `_ai_cell_verdict` 与 JS synthReading 同阈值（ERP ≥60/≤40、
  位置 ≥70/≤30），双语言口径锁（同一组 fixtures 两侧各测一遍）。
- 挂点：`_finish`（三版刷新唯一收口）→ `snap["ai_narrative"]`；任何异常落 failed 状态，
  绝不阻断落库。前端三态渲染（ok 带徽章 / 未生成一行 / 老快照整块不出现）；
  notes_gen 复盘在盘面段后插节。
- **测试**：pytest +14（test_market_dashboard +11 / test_daily_review +3）；
  node 45 例照绿；server.py 与 scheduler 均已重启生效（trade_main 未动）。

---

## 2026-09-19 — 架构审查修订批次 5.2：API 层位置统一；core 收包按证据改判"不硬搬"

**一句话（大白话）**：把唯一一个放错地方的接口文件挪回它该在的位置；至于
"给 core/ 分家（market_/farm_/数据访问 三个包）"——量化完改动面后决定**不硬搬**：
要改 100 多处引用、动到测试里的路径锚点与 patch 点，而收益只是"文件夹好看"。
理由与数据留在下面，你要是觉得该搬，说一声我照搬。

- **做了（低风险、审计明确点名的口径不一）**：`core/farm_api.py` → 根目录
  `farm_api.py`（`git mv`），与其余 5 个 `*_api.py` 位置口径统一；同步改
  `server.py` + `tests/test_farm_api.py` 共 10 处引用；**搬家必改点**：文件里
  `ROOT = Path(__file__).resolve().parent.parent` 少一层 → 改 `.parent`（否则指向
  项目外）。验证：`ROOT` 实测正确、`test_farm_api` 等 68 例绿、全量 3028 例绿。
  （插曲：冒烟检查一度以为 farm 路由没挂上 —— 实为 FastAPI 新版把 include 的路由
  存成 `_IncludedRouter` 不再摊平，**是检查方法错了，不是代码错了**。）
- **没做（附量化证据）**：`market_*` 一族（10 个模块）、`farm_*`（5 个）、数据访问
  一族（`data_fetcher`/`kline_cache`/`data_cache`/`tdx_tq`）收进子包。实测改动面：
  `core.data_fetcher` 被 **39 个生产 + 20 个测试文件**引用、`core.market_position*`
  约 **25 + 11**、`core.farm_rules` **12 + 3**；另有测试用**文件路径字符串**做锚点
  （`test_market_position.py::_POSITION_MODULES`、`test_daily_review.py` 的 AST 守卫）。
  改名还会牵动 logger 名（`get_logger(__name__)`）与各处 patch 点 —— 今天已两次踩到
  "实现搬了、patch 没搬"的坑。**收益仅是组织性，代价是 100+ 引用点 + 锚点重写 +
  patch/logger 风险** → 决定：**存量不搬**，新代码继续用 `market_*`/`farm_*` 前缀
  （事实上已按族分名），需要分家时再按"先抽共享底座"的成功路径做。
- **终态体检（计划书 §六 四指标）**：① 最大文件 < 60KB —— **未达成**：`trade_main.py`
  84.6KB / `trade/rotation.py` 73.5KB / `backtest/engine.py` 72.9KB / `trade/store.py`
  63.9KB（这四个都不在本计划书条目内，列为后续候选）；② tools 零引用清单机制 —— ✅
  `tools/tools_inventory.py`；③ 8081 写端点鉴权 —— 用户拍板不做（已记档）；④ 跨进程写
  全部有锁 —— ✅（`daily.jsonl` 文件锁 + raw 日志按月轮转 + kline_cache 原子写）。

---

## 2026-09-19 — 架构审查修订批次 5.1 收尾（第四/五刀）：照镜子 + 体温表文案层，5.1 全部完成

**一句话（大白话）**：把最后两块也搬出去了 —— 照镜子和体温表文案（那篇"大白话
报告"）。**主文件从 113.6KiB 瘦到 25.4KiB（-77%）**，八种职责拆成六个小模块 +
一个共享底座。搬迁途中我自己踩了一个"碰生产数据"的坑，如实记在下面。

- **新增两个模块**：`core/market_mirror.py`（6.4KB / 137 行，`mirror` + 四个照镜子
  常量）、`core/market_thermometer.py`（**38.4KiB / 644 行**，`thermometer_md` +
  10 个人话模板 + `push_thermometer` + `CALIBER_FOOTER`）。文案层是依赖链最上层：
  单向 import 底面五块（底座/照镜子/牛熊/体检/影子），无环。
- **实测数字（5.1 五刀累计）**：`market_position_runner.py` **113.6KiB → 25.4KiB
  （-77%，2166 → 606 行）**；新模块合计 **99.9KiB**：底座 11.1 + erp 7.2 +
  regime 5.3 + validity 13.4 + shadow_replay 18.3 + mirror 6.2 + thermometer 38.4。
  剩余 runner = 数据准备 + 记录组装 + `collect` + 分桶 + 旧入口转发层。
- **实测坑（两处 patch 位置）**：①测试用 `monkeypatch.setattr(mpr, "similar_days", …)`
  造分位带 —— mirror 搬走后该 patch 不再影响实现（症状: "六特征齐全 0 条"），
  改打 `core.market_mirror.similar_days`；②`market_thermometer.py` 漏了
  `pandas` 与 `INDEX_SPECS` 导入（由"未解析名 AST 扫描"查出补齐）。
- **⚠️ 我自己的工作失误（如实记录）**：排查上面两例时写的**临时诊断脚本只隔离了
  `DAILY_PATH`/`KLINE_1D_DIR`，没隔离 `ERP_PATH`、也没设 `VERA_MP_NO_ERP_FETCH`**
  → 脚本里的 `collect()` 联网刷新并**写了生产 `data/market_position/erp.jsonl`**。
  事后逐项核对：**5209 行 / 无重复日期 / 最大日期 2026-09-18（最后交易日）**，
  新增的 2 行正是 9/17、9/18 两天的真实 ERP —— 每日 15:50 job 本来也会写，**结果无害
  但流程违规**。教训：**临时脚本必须照 conftest 隔离全部落盘路径 + 关联网取数**，
  不能只隔离"当前正在改的那条"。
- **验收**：大盘域 126 例绿；全量 **3028 例绿 / 7 skip**；`daily.jsonl` mtime 未变
  （9/18 15:55），`erp.jsonl` 经逐行核对内容正确。**5.1 全部完成**；仅剩 5.2（core 收包）。

---

## 2026-09-19 — 架构审查修订批次 5.1 第三刀：拆出影子回放 + 转发改按归属表

**一句话（大白话）**：把"择时影子回放"这块（约 360 行）也搬出大盘主文件，
主文件从 113.6KiB 瘦到 **67.5KiB**。过程中发现旧入口转发只认基座一家 —— 搬走
名字一多就会漏，改成了**按归属表转发**。

- **新增 `core/market_shadow_replay.py`（18.7KB / 363 行）**：`_cost_params` /
  `_hac_tstat` / `_holding_segments` / `_segment_stats` / `shadow_replay` +
  `MIN_SEGMENTS_FOR_T` / `SHADOW_RULES` / `WINDOW_SPLIT` / `_COST_CACHE`；
  `_year_breakdown`（照镜子与影子共用）进共享底座。
- **转发机制修好**：原来 `__getattr__` 只往基座转发 → 第三刀的名字住在影子模块，
  测试按 `mpr._hac_tstat` 调就炸（`AttributeError`）。改为 `_FORWARD_TABLE`
  **按归属表转发**（每批搬走的名字登记归属模块）—— 确定性、不会把某模块的
  import 名（pd/dt）漏出去，也不会因同名转发到错家。
- **实测数字**：`market_position_runner.py` **84.8KiB → 67.5KiB**（1610 → **1294** 行）；
  三刀累计 **113.6KiB → 67.5KiB（-41%）**，新模块合计：io 11.1 + erp 7.2 +
  regime 5.3 + validity 13.4 + shadow_replay 18.3 = 55.3KiB。
- **验收**：大盘域 126 例绿；全量 **3028 例绿 / 7 skip**；生产
  `daily.jsonl`/`erp.jsonl` mtime 仍是 9/18 15:55。**剩最后一刀 = 体温表文案层**
  （+ 推送），之后是 5.2（core 收包）。

---

## 2026-09-19 — 大盘总览页首加「合起来读」综合解读卡（用户拍板）

**一句话（大白话）**：七张图看不懂先看它 —— 总览页第一张卡把温度、五维结构、股债性价比、
指数位置、人气量能揉成一段人话，最后落进 2×2 四格结论（划算但位置高 / 划算且位置低 /
贵且位置高 / 位置低但性价比差）。

- **解读由数字模板生成**（`synthReading` 纯函数，tests/js/test_market_dashboard.js 锁 12 例），
  不写死结论、不接 LLM——数字变话才变；数据不全时明说"数据不全"不硬凑。
- **边界**：只描述现状与历史赔率，必带「不预测涨跌、不联动任何仓位」（业务铁律 1）；
  阈值口径（ERP 十年分位 ≥60/≤40、位置百分位 ≥70/≤30、冰点 <30%/<20%）印在卡片底部。
- 顺手收编：维度中文名表原来在 market_dashboard.js 里写了两份 → 模块级唯一一份。
- 纯前端改动（index.html + market_dashboard.js），刷新页面即生效；45 个 node 用例全绿。
- 插曲：验收时发现 server.py 进程不知何时退出（8080 拉不通），已重启并复核新端点正常。

---

## 2026-09-19 — 架构审查修订批次 5.1 第二刀：拆出 regime / 指标体检 / ERP 三个分析模块

**一句话（大白话）**：把大盘文件里的三块"算给人看"的逻辑各自独立成小文件 ——
牛熊区间、指标体检、股债性价比。它们原本全挤在一个 111KB 的文件里，现在各自
5~14KB，主文件瘦到 86KB。

- **新增三个模块**（均只依赖共享底座，单向）：
  - `core/market_erp.py`（7.4KB）：ERP 股债性价比 —— 取数/读缓存/表/快照 + `ERP_*` 常量；
  - `core/market_regime.py`（5.4KB）：牛熊区间与时长（`_regime_episodes`/`_summary`/`_all`）；
  - `core/market_validity.py`（13.7KB）：指标体检（`_spearman`/`_quintile_spread`/`_dimension_validity`）
    —— 单向 import `market_erp._erp_table`（估值的证据维度要它）。
- **底座再進两批原语**：`ERP_PATH`（路径单一所有者再扩一条）+ 格式化原语
  `_num`/`_pct`/`_rat`/`_yi` + `_features_frame`（照镜子与体检共用，放底座避免环）。
- **实测数字**：`market_position_runner.py` **109.4KiB → 84.8KiB**（2051 → 1610 行，
  本刀搬走 441 行）；底座 11.1KiB。三块合计 25.9KiB。
- **踩坑记录（都当场修掉）**：①提取脚本把上轮改过的 `mpio._ROOT` 一起搬进了基座
  自己（基座里没有 `mpio`）→ 手工改正；②新模块缺 `os`/`json`/`warnings` 标准库导入
  → 由"未解析名 AST 扫描"逐模块查出补齐（这个方法以后每次搬运都该跑）。
- **测试隔离继续跟着搬**：`ERP_PATH` 也进了基座，conftest 改为 patch 基座
  （`_IO_STATE_NAMES` 守卫同步加 `ERP_PATH`）。
- **验收**：大盘域 126 例绿；全量 **3028 例绿 / 7 skip**；生产
  `daily.jsonl`/`erp.jsonl` mtime 仍是 9/18 15:55。

---

## 2026-09-19 — 架构审查修订批次 5.1 第一刀：抽出共享底座 `market_position_io`

**一句话（大白话）**：本想直接拆最大的那个大盘文件，动手前先用工具扫了一遍
依赖，发现四块全都挂在同一组共享函数上、先拆谁都会撞循环 import —— 于是先抽
**共享底座**（路径常量 + 取数/落盘/读取原语），为后面几刀铺路。这一刀顺带
把一个"测试写生产数据"的隐患改成了机器守卫。

- **新增 `core/market_position_io.py`（216 行）**：`_ROOT` / `KLINE_1D_DIR` /
  `DAILY_PATH` / `INDEX_SPECS` / `_UPSERT_LOCK_TIMEOUT` + `_index_series` / `_f` /
  `_expected_trading_day` / `_upsert`(+`_upsert_locked`/`_cross_process_lock`/平台锁原语) /
  `history` / `latest`。runner 2166 → 2051 行（搬走 157 行 + 新增转发层约 42 行）。
- **路径常量单一所有者（关键设计）**：这三个路径只在基座定义，runner **不留副本**，
  旧入口 `mpr.DAILY_PATH` / `mpr.KLINE_1D_DIR` 由**模块级 `__getattr__`（PEP 562）
  动态转发** —— 不做 `X = mpio.X` 快照（快照会在基座被 patch 后变陈旧，谁读它谁写
  生产路径）。runner 内部一律 `mpio.X` **调用期取值**。
- **测试隔离 patch 点跟着搬**：conftest 改 patch 基座（`ERP_PATH` 仍在 runner）；
  `tests/test_market_position_api.py` 的 `setattr(mpr, "DAILY_PATH", …)` 改 patch 基座。
- **实测踩坑并加了守卫**：`monkeypatch.setattr(mpr, "DAILY_PATH", …)` 撤销时会把它
  **实体化成真实属性**，从此永久 shadow `__getattr__` 转发 → 之后"写 tmp、读生产"
  （全量跑出 4 例失败，正是这个机制）。处置：①改打基座；②新增 `TestIsolationGuard`
  三条守卫 —— 路径必须落在 tmp、runner 不许有 `_IO_STATE_NAMES`（路径/阈值）的实体
  副本、runner 的 `history()` 必须跟着基座走（证明是调用期取值而非快照）。
- **module 内裸全局名不走 `__getattr__`**：runner 内部还在用的 `_upsert` 必须显式
  import（只靠转发会在运行期 `NameError` —— 实测踩中）。
- **测试**：全量 **3028 例绿 / 7 skip**；生产 `data/market_position/daily.jsonl` 与
  `erp.jsonl` 的 mtime 仍是 9/18 15:55（**测试没碰生产数据**，直接证据）。

---

## 2026-09-19 — 架构审查修订批次 5（5.3 完成；5.1/5.2 勘误后延后）

**一句话（大白话）**：把"模块公开方法 ≤8"这条规矩从**数个数**改成**看本质** ——
配置类和账本类模块的字段/状态机本身就是契约，不该按个数卡；薄薄的 API 路由层
按端点数算。另外，本来打算拆最大的那个大盘文件，动手前用 AST 扫了一遍依赖，
发现**四块全都挂在同一组共享函数上，先拆哪块都会撞循环 import** —— 于是把
正确顺序写进计划书，不硬拆。

- **5.3 铁律 8 口径修订（审查 P2-18，已完成）**：CLAUDE.md 实盘铁律 8 按
  2026-07-26 M7 先例扩围 —— `config`（配置模型：字段即契约）与 `book`（订单
  状态机+常量）一并豁免；`*_api.py` 薄路由层按 HTTP 端点计、不按方法数计；
  其余超标模块（实测 executor 21 / monitor 12 / channel_manager 12 / analysis 10 /
  risk 9）**计入待拆清单但不为拆而拆**（下次动该模块时顺手分内聚块，新代码不得
  再增公开面）。判据从"数方法个数"改为"能不能一口气读完 + 公开面是不是契约本身"。
- **5.1 market_position_runner 四刀（勘误后延后）**：AST 依赖实测推翻了计划书
  "每块自包含"的假设 —— ERP / 指标体检 / 牛熊 regime / 影子回放 / 体温表文案
  **五块全部依赖同一组共享原语**（`_f`/`_index_series`/`_history`/`_upsert`/
  配置常量），而 runner 的公开面也要这些块 → 先拆任何一块都会形成
  **runner ↔ 新模块循环 import**。正确顺序改为：① 先抽共享底座
  `core/market_position_io.py`，② 再拆 regime/体检/ERP，③ 影子回放，
  ④ **文案层最后**（它依赖前三者全部）。完整依赖矩阵与顺序已写进计划书 §五，
  下轮照做即可。**当前不留半拆中间态**。
- **5.2 core/ 按族收包（延后）**：动 import 面太大，留到实盘平静周单独做，且需
  先跑全仓 AST 依赖扫描确认无环。

---

## 2026-09-19 — 架构审查修订批次 4（后半）：等成交不再死等 + rotation 拆两块

**一句话（大白话）**：交易线程"等成交/等撤单回执"的那几秒原来是真的干等 ——
整个事件队列跟着停摆；现在等待窗口里顺手把回报和行情处理掉（队列继续流动）。
另外把 89KB 的轮动主文件拆掉了两块自包含的东西：取数降级链与在途台账。

- **4.2 消费者线程墙钟阻塞（审查 P0-4）**：新增 `EventEngine.pump(types, duration)`
  —— 在 handler 内部就地消费**叶子类**事件（回报类 + 行情类：`PUMPABLE_WAIT_TYPES`），
  其余（命令/信号/轮动/扫描/对账/EOD）原样放回队列（它们会重入特性层，等待中间
  插进来会打断状态机）。`rotation._wait_fills` 与 `executor._wait_terminal` 的
  `sleep` 改为 pump（窗口时长不变，判据仍是查网关真相源）。
  三个安全阀：**只许消费者线程调用**（`can_pump()` 先问，错线程 pump 直接抛
  RuntimeError —— 防"两个写者"）；types 白名单由调用方给；`engine=None`/非消费者
  线程（单测直构）自动退回纯 sleep，零行为变化。
  **专项测试** `tests/trade/test_events_pump.py` 5 例直接证明：等待期间到达的
  委托回报与 tick **在等待结束前**就被处理（顺序断言），非回报事件留队列但一条不丢，
  错线程被拒，窗口时长有上限。
- **4.5 rotation.py 拆两块（审查 P1-9）**：
  ① **取数降级链** → 新模块 `trade/rotation_feed.py`（`IndexFeed.closes()`；
  QMT→TDX→腾讯 三级降级 + 陈旧标记 + fail-closed 全挂返 `([], "none")`），
  调用点改 `self._feed.closes(...)`；独立测试 6 例（含 min_bars 阈值与全挂）。
  ② **在途买卖台账** → 新模块 `trade/rotation_ledger.py`（`RotationLedgerMixin`
  + 7 个方法：`_clear_open_ledgers`/`_settle_open_sells`/`_reduce_lot`/
  `_save_open_ledger`/`_read_open_ledger`/`_ledger_corrupt`/`_settle_open_buys`），
  `RotationFeature` 改为继承它。**为什么用 mixin**：这些方法读写的全是特性内部
  状态，抽独立类要先定义 8+ 个接口 —— 那是在实盘台账逻辑上做手术（台账有
  "恰好一次扣减""失败方向=账本偏多"两条硬不变量）；mixin 让方法体**逐字不动**
  达成"端出文件"的目标，风险面为零。
- **实测体积**：`rotation.py` 89KB → **73.5KB**（另两块 4.5KB + 16.5KB）；
  剪裁脚本带断言（剪掉的必须正好是那 7 个方法）。
- **测试**：新增 11 例（pump 5 + feed 6）；trade 全域 **983 例绿**；全量
  **3025 例绿 / 7 skip**。
- **上线约束**：动 trade 包 → **trade_main 需重启生效，且必须 ≥15:05 窗口**。

---

## 2026-09-19 — 架构审查修订批次 4（前半）：HTTP 线程零接触 QMT + 第二写者守卫 + 跨进程写锁

**一句话（大白话）**：把三条"两个线程/两个进程抢同一份数据"的路给堵了 ——
网页查资产不再自己伸手问柜台（改成排队让交易线程去问）、回填脚本不再和
运行中的交易进程抢着写库（活着就拒）、大盘录像的写入加了跨进程锁
（原来两个进程同时读改写会丢记录）。**4.2（消费者线程阻塞）与 4.5
（rotation 拆包）留待下一轮**：它们要动实盘状态机与 89KB 主文件，单独做。

- **4.1 HTTP 线程零接触 QMT（审查 P0-2）**：新增事件 `EVENT_READ_QUERY`
  （按关键事件对待，被丢会让等待方白等超时）+ `TradeApp._on_read_query`
  （消费者线程执行只读查询，结果经 `concurrent.futures.Future` 回传）+
  公开缝 `read_via_consumer(fn, timeout)` / `read_asset(timeout)`。
  `trade/api.py` 的 `/api/trade/asset` 与 `trade/analysis_api.py` 的资产对账
  改走该缝，超时转人话 503「资产查询超时 —— 交易进程忙」。
  **专项测试直接证明查询发生在消费者线程**（`test_api_no_direct_gateway.py`：
  探针记录 `threading.current_thread().name == "trade-event-consumer"`，
  外加超时/异常回传 2 例）—— 不是"接口还返回 200"就算过。
- **4.3 trade.db 第二写者守卫（审查 P0-5）**：`tools/backfill_daily_decision.py`
  直写 `daily_decision` 表, 是 trade.db 的第二写者, 与运行中的 trade_main 并发
  原来只靠 WAL busy 重试兜底。现在**交易进程活着就拒绝执行**（TCP 探活
  8081，退出码 3 + 三条处置指引），`--force` 可自担风险绕过，`--dry-run` 不拦。
  **坑**：探活最初用 `urlopen`，但 `tests/conftest.py` session 级焊死 urllib
  （假 200），测试里死端口被判成"活着" → 改用 socket TCP 连通性判据（更简单
  也更可靠，且测得出真实行为）。
- **4.4 daily.jsonl 跨进程写锁（审查 P0-6）**：`core/market_position_runner._upsert`
  加**跨进程文件锁**（同目录 `.lock` + Windows `msvcrt.locking` / POSIX `fcntl`，
  非阻塞轮询到 `_UPSERT_LOCK_TIMEOUT` 默认 30 秒），超时抛 `TimeoutError`，
  `collect()` 转成"另一个进程正在采集"的明确错误（fail-closed，不静默丢一半）。
  原来 `_COLLECT_LOCK` 只是 threading.Lock，跨进程毫无作用（scheduler 三个 job
  与 server 手动采集并发时靠 last-write-wins 兜底会丢记录）。
- **测试**：新增 3 个测试文件（4.1 专项 3 例 / 4.3 守卫 5 例 / 4.4 锁 3 例）；
  trade 全域 **969 例全绿**，大盘域 123 例全绿。
- **上线约束**：4.1/4.3/4.4 都动 trade 包或 trade 侧工具 → **trade_main 需重启
  生效，且必须 ≥15:05 窗口**；scheduler 侧（写 daily.jsonl 的三个 job）也要重启
  才用上新锁。

---

**一句话（大白话）**：把"同一段配方抄四份"和"校验只在一条路上做"两个静默漂移
温床拆了 —— 4 个扫描脚本改走引擎公开接缝、口径校验搬到引擎里（直调也管）、
缓存 key 的"缺省 True 键"名单由 selector 声明（新增键不会再漏）、tools/ 有了
分级清单工具（不删，只分类给人看）。

- **3.1 sweep 复刻收口（审查 P1-7，本批大头）**：`backtest/engine.py` 新增公开
  接缝 **`prepare_matrices()`**（run() 准备段的对外出口，返回既有 dict 契约；
  取数为空返回 None）；4 个 sweep（`gs_5m_sweep`、`quantqq_5m_sweep`、
  `quantqq_1m_sweep`、`quantqq_5m_sweep_2010`）各自那段"取数→非标准 bar 过滤→
  列对齐→ffill→tradable"复刻全部删除，改调接缝（涨停预过滤是 sweep 特有步骤，
  在接缝之后补，行为不变）；`tools/attr_gp1014.py` 的
  `BacktestEngine._filter_limit_up` **运行时 monkeypatch 改为显式配置**
  `filter_limit_up: False`（新增引擎配置键，默认 True 零行为变化）。
  **口径变化（有意）**：接缝会把窗口终点截断到请求区间终点（2026-07-21 引擎口径），
  旧复刻段有的没截断 → sweep 缓存 meta 加 `prep_seam` 标记，加载旧缓存时告警。
  **parity 锁**：`tests/test_prepare_matrices_parity.py` 用合成 5m 数据把"接缝产物"
  与"旧复刻配方（参考实现内联在测试里）"逐字段对拍（close/entries/high/low/open/
  tradable/last_tradable_idx/idx/cols），3 例全绿。
- **3.2 口径校验下沉（审查 P1-14）**：复权一致性 + period 一致性校验自
  `pipeline.step2_backtest` 下沉到 `engine._validate_caliber`，`engine.run()` 新增
  `selection_caliber` 参数（pipeline 如实传）；**直调 run() 不再静默绕过** ——
  传了就校验（复权不一致直接抛），不传就打 `caliber_unverified` WARNING 明示。
  校验先于"空 selections 早退"（空信号也照样抛坏口径）。`tests/test_engine_caliber.py`
  5 例 + 既有 period_mismatch 两例改 logger 出处（pipeline→backtest.engine）。
- **3.3 缓存缺省键声明式导出（审查 P1-15）**：`UNIVERSE_TRUE_DEFAULT_KEYS`
  唯一声明搬到 `selection/selector.py`（语义归属地，真正决定缺省值的那行旁边），
  `selection_cache` 函数内引用（不在模块级拖 selector 的 TDX 依赖）；
  `_normalize_universe` 提升为公开名 **`normalize_universe`**（跨模块走私私有名正名，
  `universe_cache` 与测试同步改）；新增 **AST 防漂移锁**
  `tests/test_universe_key_spec.py` —— 扫描 selector 里真实的 `u.get(k, True)`
  调用与声明比对，新增同类键忘登记立刻红（P0-1 复发防护）。L0/L2 缓存口径
  不对称的取舍理由写进 docstring（不是 bug，别"统一"）。
- **3.4 tools/ 淤积治理机制（审查 P2-16）**：新增 `tools/tools_inventory.py`
  —— 三级分类（被生产引用/仅文档引用=研究证据/全仓零引用），**只分类不删除**，
  每季度人工过目后处理孤儿。首跑实测：**128 个脚本 → 生产引用 57 / 仅文档 51 /
  零引用 20**（**2026-09-20 判据修正后重跑：129 个脚本 → 生产引用 74 /
  仅文档 38 / 零引用 17** —— 原判据把 tools/ 互引与嵌套 .bat 漏成「孤儿」、
  又把根级 .md 的一句提及算成生产引用，两类误判方向相反）（此前粗估"82 个零引用"是没算研究引用，工具证明了一刀切删会毁证据链）。
  9 例测试锁分类判据（含"运行时产物目录不算引用来源"）。
- **测试**：批次新增 4 个测试文件（parity 3 例 / caliber 5 例 / universe 键 2 例 /
  tools 清单 4 例）；全量 pytest 见提交时的基线。

---

**一句话（大白话）**：治"同一个地址抄四遍"和"报错格式四种写法" —— 9 月 19 日
"404 被报成 8081 不可达"事故和"_month 幽灵行"事故的同源温床，这次拆掉了。
（批次 2.4 的 8081 鉴权由用户拍板不做：都是自己用，无需密钥。）

- **2.1 8081 基址唯一出口**：`decision_util.mjs` 的 `tradeApiBase` 本就是唯一
  实现；`trade.js:19`、`analysis.js:8-9`（两份常量）改为 import 它；
  `mobile.html` 加 module 桥挂 `window.tradeApiBase`（主脚本包 DOMContentLoaded
  等桥 —— module 是 deferred 先于 DOMContentLoaded，时序有保），`?trade=`
  覆盖功能保留。新增接线守卫 `tests/web/test_trade_base_wiring.mjs`（13 例，
  文本断言四个消费方零 `:8081` 硬编码）。
- **esc() 收编勘察后放弃**：`charts.js` 的 esc 走 DOM（`createElement`），与
  node 可测文件的 regex 版语义不同源也不同实现，合并会破坏 node 测试 ——
  正确的决定是不合，留档备查。
- **2.2 错误契约统一**：两台服务（server.py / trade/api.py）各加一个全局
  exception handler，未捕获异常统一 JSON `{"detail": ...}`，不再回 Starlette
  纯文本 500（前端 `r.json()` 会炸成 SyntaxError）。契约成文：**传输错误 =
  非 200 + {detail}；业务软失败 = 200 + {success:false}**，写在 handler 注释里，
  谁也不许发明第三种。坑：Starlette 的 ServerErrorMiddleware 发完 500 还会
  再 raise 一次，TestClient 必须 `raise_server_exceptions=False` 才测得到响应形状。
- **2.3 手机版契约快照**：`tests/trade/test_mobile_contract.py` +
  `tests/web/mobile_contract_snapshot.json` —— 把 mobile.html 实际消费的 9 个
  端点（grep 实证清单）的响应形状（字段名+类型递归）锁死；后端改字段即红，
  并打印逐端点 diff。快照更新走 `VERA_UPDATE_MOBILE_SNAPSHOT=1` 环境变量
  （pytest_addoption 在非 conftest 模块不生效，实测踩坑）。
- **测试**：契约测试 3 例 + 手机快照 1 例 + 接线守卫 13 例新增；trade 全域 +
  server 相关 981 例全绿；前端 15 套件 14 绿（test_brain_viz 历史遗留红除外）。

---

**一句话（大白话）**：按当天架构审查报告（`docs/audit/2026-09-19_代码库架构审查报告.md`）
的计划书（`docs/plan/2026-09-19_架构审查修订计划书.md`）执行批次 1 —— 全是"纯卫生、
零行为变化"的项：启动脚本学会等 QMT 就绪、放错位置的日历工具搬回工具层、
752MB 的原始回报日志开始按月归档、git 积压清成零。

- **1.1 QMT 就绪等待**：新增 `tools/qmt_ready_check.py` 探针（与 trade_main 同一份
  config，只 connect/disconnect，stdout 全 ASCII 防 GBK 主窗口乱码）；`start_vera.bat`
  启动交易进程前每 20 秒探一次、最多 10 次，仍不就绪则**跳过交易进程**（回测/调度照起，
  fail-closed）—— 治本 9 月 2 日冷启动 rc=-1 事故（探针已真机实测 READY/exit=0）。
  .bat 保持 GBK+CRLF（补丁经 Python 脚本写入，逐字节校验零纯 LF）。
- **1.2 trading_calendar 搬家**：`scheduler/trading_calendar.py` →
  `utils/trading_calendar.py`（它零 scheduler 依赖却被 core 3 处 + trade 6 处向上
  import，层级倒挂）；原位留 re-export shim（一版本后删）；10 个生产文件 + 2 个测试
  文件共 15 处引用全部改指 utils（带计数断言的替换脚本，零残留 grep 验证）。
- **1.3 raw 审计日志按月轮转**：`trade/raw_log.py` 新增 `rotate_raw_log_monthly` ——
  非当月行切到 `raw_reports_YYYYMM.jsonl` 归档（追加不覆盖），**先写归档→行数对账→
  原子重写 live** 的 fail-closed 顺序，解析不出的行一律留 live 不丢；append-only
  时序文件用"首行即当月"快路径免每次启动全量扫。启动钩子接在 trade_main 构造
  TradeStore **之前**（Windows 被占用文件无法 os.replace）。性能实测：21MB/20 万行
  1.23 秒 → 752MB 历史存量切分约 45 秒，**在 trade_main 下次重启时自动完成**
  （当前 trade_main 运行中，不重启不切）。离线 CLI：`python -m trade.raw_log <path>`。
- **1.4 仓库卫生**：删 `web/` 两个 .bak 与 0 字节散文件 `data/trade.db`；.gitignore
  收口 `data/formula_farm/`、`data/daily_review/`、`data/morning_brief/`、`logs/`、
  `.agent-teams/`（**坑**：gitignore 不支持行内注释，首版五条规则全没生效，已改
  独立行）；61 项积压改动按主题分 6 个提交全部入库（git status 清零）。
- **测试**：新增 `tests/trade/test_raw_log_rotate.py` 7 例；全量 pytest（除快照
  parity）exit=0；前端 15 个 node 套件 14 绿 —— `test_brain_viz.mjs` 1 红经 git
  worktree 对 HEAD 复跑确认**是历史遗留红**（与今日改动无关，留档待查）。
- **遗留**：`scheduler/trading_calendar.py` shim 一版本后删；raw 历史切分随
  trade_main 下次重启自动发生（需 ≥15:05 窗口）。

---

**一句话（大白话）**：事件扫描原来只盯"三家通讯社的电报"（财联社/同花顺/新浪），
海外事只能靠它们转述；本次加了两个海外直通信道（美联储公告 RSS、华尔街见闻快讯），
并把美债 2 年收益率从"转载"升级到"发行方本尊"（美财政部 CSV）——海外事件从
"等二手转述"变成"看一手原文"。计划书 `docs/plan/2026-09-19_事件跟踪数据源扩充_计划书.md`
（含 15 个候选源的本机连通性实测矩阵：FRED/GDELT/BLS 被墙、金十 502、RSSHub 403，一律不接）。

- **P0-① 新取数层 `core/market_event_sources.py`（新模块，公开接口仅 3 个）**：
  五源候选池（akshare 三源自 scan 迁入 + **美联储 RSS** + **华尔街见闻快讯**）+
  美财政部 2 年收益率序列。候选新增 `fact_level`（primary=一手可指原文 /
  secondary=转述须复核）与 `hint` 字段；`_SOURCES` 注册表，加新源 = 加一行，
  公开接口不涨。美联储 RSS 两个坑被 fixture 锁死：**UTF-8 BOM**（实测 EF BB BF，
  必须 utf-8-sig）与 **GMT→北京日换算**（FOMC 声明 18:00 GMT = 北京次日凌晨，
  不换算事件日期整体错一天，测试锁：9/16 18:00 GMT → 2026-09-17）。
- **P0-② fed_rate 跟踪器升级一手源**：`fetch_fed_rate_proxy` 改"美财政部主源 →
  akshare 兜底 → 双源同日偏差 >5bp 写 cross_check 告警"。实测两源同为 4.76
  （2026-09-18），分毫不差。财政部 CSV 最新行在最上、年度文件，年初交界当年
  <25 行才补拉上年（一年省 ~360 次请求）。
- **P0-③ RUBRIC 加「溯源终点清单」**：政策类→央行/统计局/证监会/政府网原文页；
  美联储类→federalreserve.gov 原文；secondary 源（华尔街见闻）录普通重大及以上档
  前必须复核到一手终点 —— 把 9月19日 事实溯源铁律落到具体 URL 级别。
- **P1 fed_rate 挂调度器**：`_job_market_dashboard_fill`（每交易日 08:30）开头
  先 `update_fed_rate_event()` 再补齐快照（财政部数据美东傍晚发布≈北京清晨，
  08:30 必取到 T-1；fail-soft 失败不阻塞补齐）。不加新 job 不加进程；
  **scheduler 需重启生效**（core 层改动零重启：scan/sources 不被 server/trade_main 引用）。
- **两遍全面自检抓到并修掉 2 个真问题**：①交叉校验原按"两源最新值"直接比，
  一方滞后一天就误报（实测相邻日差 9bp > 5bp 阈值）→ 改为**只在同日比数**，
  日期不齐不告警，测试锁；②akshare 兜底取数起初留在判定层 scan 里（分层漏洞，
  scan 还 import akshare）→ 挪进取数层为第 3 个公开函数，scan 不再 import akshare。
- **深模块浅模块审查（照 research/2026-08-16 框架）**：sources = 深模块（3 接口藏
  5 源 HTTP/BOM/时区/年度拼接，删除测试通过）；scan 变薄属"有意的接缝"
  （RUBRIC/关键词/主备策略是它的领域知识）；接口数 scan=7 不变 / sources=3，
  铁律 8 内。
- **测试**：新增 `tests/test_market_event_sources.py` 16 例（fixture =
  2026-09-19 真实响应落 `tests/fixtures/market_event_sources/`，**零联网**：
  BOM/GMT/解析/隔离/fallback/交叉校验/AST 不 import trade）；大盘域四套件 82 例全绿；
  真实联网冒烟：候选池 26 条五源齐（财联社4/同花顺1/新浪7/华尔街见闻10/美联储4），
  FOMC 声明正确标高优先，fed_rate proxy 双源一致无告警。
- **剩余风险**：华尔街见闻/美联储为公网免费端点，稳定性无担保（fail-soft 已兜：
  挂了只是少一路候选）；scheduler 的 fed_rate 行要重启后 08:30 才首次出现。

---

## 2026-09-19 — 事件跟踪「台账有货、页面为空」排障 + 两条刷新约定落地

**一句话（大白话）**：事件跟踪页像饭店出菜窗口 —— 事件先记在点菜台账
（`events.jsonl`），但窗口只摆"烤好的菜"（`dashboard.jsonl` 快照）；9 月 19 日
下午录了 5 条事件，可最后一次快照是 9 月 18 日晚生成的，周末调度器又不跑，
页面自然空。本次把两条"让菜及时上桌"的约定落地，并顺手修了一个会被周末
刷新每周触发的分数放大 bug。

- **根因**：数据流是单向的 —— 事件落台账 → 仪表盘刷新时才"烤进"快照 →
  页面只读快照。三个定时刷新 job（每交易日 16:30/08:30/09:30）走 `add_daily`，
  有交易日门槛，周末一次都不跑；事件扫描与快照刷新两条链之间没有钩子。
- **必要性结论（用户拍板按此执行）**：交易决策链无盲区 —— 周一 08:30 补齐版
  会在开盘前把事件烤进快照；要补的只是"周末看页面新鲜"，因此不做事件驱动
  即时重刷（违反勿增实体 + 削弱可复现性），只做下面两条轻量约定。
- **约定①扫描收尾必刷新（零代码）**：`core/market_event_scan.py` 用法说明新增
  第 5 步 —— 事件落库后顺手触发一次刷新（POST
  `/api/market_position/dashboard/refresh` 或本地 `mdr.refresh_close(write=True)`）；
  `core/event_cli.py` 文档字符串同步提醒"录的是台账、页面读的是快照"。
- **约定②周末兜底 job**：`scheduler/__main__.py` 新增
  `market_dashboard_weekend` —— `add_weekly(weekday=5, hhmm="18:00")`
  （weekly 语义不看交易日，同 weekly_evolution 的 P0-2 修复），每周六 18:00
  跑一次 `refresh_close`（内部自动回退到最近交易日落账，不落"周六快照"）。
  周日录入的事件仍等周一 08:30 补齐版或页面手动刷新。**scheduler 需重启生效。**
- **顺手修 bug**：周末刷新用"最近交易日"做衰减基准，晚于基准日录入的事件
  `elapsed` 为负，剩余天数曾会超过满额、把分数放大到初始分之上（如 -0.4 变
  -0.44）——`_days_left` 已 clamp 到满额，`days_left` 不再超过 `expire_days`。
- **测试**：`tests/test_market_events.py` 新增
  `test_future_start_event_score_capped_at_full`（未来起始日事件分数封顶满额）；
  大盘域回归全绿。
- **登记**：CLAUDE.md 架构骨架新增「大盘环境仪表盘·事件跟踪」行（数据流铁律
  + 两条约定 + 衰减口径 + 本坑）。

---

## 2026-09-19 — 大盘仪表盘多维图表上线（设计预览 → 计划书 → 实施）

**一句话（大白话）**：大盘位置页签从"只有一张只有 1 天数据的总分图"变成七张图 ——
温度仪表、五维雷达、股债性价比 22 年长卷、三大指数十年百分位+牛熊背景带、市场温度带
热力图，外加收进「只说现状」折叠区的成交额/市场宽度/涨跌停三张。

- 依据：设计预览 `docs/2026-09-19_大盘仪表盘多维图表设计预览.html` → 计划书
  `docs/plan/2026-09-19_大盘仪表盘多维图表_计划书.md`（两轮自审后实施）。
- **证据分层**：ERP（12 个月相关性 +0.55）与十年百分位（−0.57 反向）是唯一通过预测
  检验的两个维度 → 画成主角挂「有预测证据」徽章；成交/宽度/涨跌停挂「仅描述现状」徽章
  收折叠区（延续 9 月 17 日"降级保留"裁决）。
- **后端**：`market_dashboard_runner.erp_series()`（第 7 个公开函数，铁律 8 上限内）+
  路由 `GET /api/market_position/dashboard/erp_series`（只读本地 erp.jsonl，fail-soft）；
  日线序列零新增 —— 复用既有 `/api/market_position/history?limit=520`。
  **server.py 已重启**（仅它，trade_main 未动）。
- **前端**：market_dashboard.js 加图表层 —— 全令牌取色（getColors）、echartsInit 共享
  注册表（resize 全站覆盖）、主题切换经 MutationObserver 七图重上色、折叠区首开才画
  （0 宽容器坑，模式出处注明）、erp_series 404 专门识别为"server.py 需要重启"。
- **测试**：tests/js/test_market_dashboard.js 33 例（+14：radarFromDims/buildRegimeBands/
  buildHeatRows 三个纯函数）；pytest 大盘域 116 例全绿；前端 13 套件全绿。
- **未做（计划内）**：五维堆叠面积图、事件甘特图 —— dashboard.jsonl 只有 1 天、
  events 0 条，攒够 30 天再做。

---

## 2026-09-19 — UI/UX 综合改造（UI/UX Pro Max 技能评估 → 计划书 → 落地）

**一句话（大白话）**：按 9 月 19 日的界面评估报告把界面欠账集中还了一遍 —— 新 TAB（大盘环境
仪表盘）修了一个真 bug 和一堆"不守图纸"，全站把"危险红"和"涨红"分开，暗色模式下原本
看不清的字全部修到达标，PC 端页面切后台不再空跑轮询。

依据：`docs/audit/2026-09-19_UIUX综合评估报告_UI-UX-PRO-MAX.html`（81 条发现）→
计划书 `docs/plan/2026-09-19_UIUX综合改造_计划书.md`（两轮自审后实施）→
实施报告 `docs/audit/2026-09-19_UIUX综合改造_实施报告.md`。

### 关键改动

- **新 TAB 真 bug（P0）**：`market_dashboard.js` 历史走势摘要的运算符优先级错误 ——
  `+` 先于 `===` 执行，"近N个交易日：总分从 X 到 Y（"整句被吞、永远显示"变化"。
  Node 实测复现后修复，并有 `tests/js/test_market_dashboard.js`（19 例）锁死。
- **新 TAB 重构**：整段 CSS 从 JS 字符串注入迁回 index.html 走令牌；转 ES module 复用
  charts.js 的 getColors/echartsInit（图表随主题换血，原写死 #378add）；补
  `marketPageLeave` 离场钩子 + visibilitychange（倒计时不再切走后空跑）；温度分着色
  与涨跌红绿脱钩（热=警示黄/温=中性/冷=信息蓝，估值维度"分高=便宜"不再刷红）；手动刷新
  按钮带已耗时秒数；子页签补 tablist/tab/aria-selected 语义。
- **交易页（P0）**：`trade.js` `get()` 补 r.ok 检查（后端 500 带 JSON body 原会被渲染成
  "该日无成交"）；六处"交易服务 (8081) 不可达"统一改 `describeTradeError`（连不上 /
  服务端报错 / 超时三种说法分开，超时分支为新增）；trade.js 转 ES module。
- **红绿铁律收口（用户拍板"全部照建议改"）**：tokens.css 新增 `--danger`/`--on-danger`/
  `--danger-text`/`--on-accent`/`--on-warn`/`--on-ok`/`--down-strong`/`--on-down` 与浅色
  文字变体 `--ok-text/--pending-text/--info-text/--fail-text/--warn-text`；卖出按钮改绿
  （btn-sell，卖出=卖出方向色）；急停按钮按真实语义两级化（激活=危险红，待命=警示黄 ——
  实施期发现评估报告把 .armed 语义读反，按 trade.js 第 133 行代码真相落地）；危险/错误/
  删除全站改走 --danger 系，不再占用涨红 --up。
- **对比度（WCAG 实算）**：浅色 --text2 #6b7280→#5b6472（on surface 4.02→4.98:1）；
  原不及格的 10 组文字组合全部修到 ≥4.5:1（复算表见实施报告）。
- **健壮性**：analysis.js 全模块裸 fetch 收口到 fetchT（10s 超时+r.ok）；成交加载失败
  不再伪装成"共 0 笔"；日历 daily_pnl 失败时格子显示"加载失败"而非全灰"未归档"；
  decision.js 补请求序号守卫（防慢响应覆盖新响应）+ role=button 键盘激活（同一 bug
  第三次出现，连根修）；vera-ui.js 五处静默吞错改显形；板块加载失败文案补 esc 转义。
- **无障碍与一致性**：AI 设置页 8 个输入补 aria-label；新增 .btn-xs 收编 9 处内联小按钮；
  表单即时校验扩面 8 个数字字段 + 买卖上下限交叉校验；页签覆盖层 top:41px 魔法数改
  --tabbar-h 实测回写；页签栏加横向滚动兜底；交易页宽表加 .td-scroll。
- **卫生**：polish.css 农场段写死 hex 全部收编回令牌；audit_css_vars.mjs 巡逻清单补
  market_dashboard.js/decision.js/decision_util.mjs；market_position.js 保留并加注
  （评估报告误判为孤儿文件 —— 实为跨语言文案反向锁的一半，勿删）。

### 测试增量

- 新增 `tests/js/test_market_dashboard.js`（19 例：摘要文案三档 + 温度着色边界 + 方向三态）。
- `tests/web/test_decision_util.mjs` +5 例（describeFetchError 泛化 + describeTradeError
  默认行为逐字不变）。
- `tests/web/test_data_cache.js` 断言随新红绿口径更新（过期=--danger-text，新鲜=--ok-text）。
- 基线全绿；`test_brain_viz.mjs` 有 1 个**既有**失败（"集成: marked 渲染出 h1"，
  开工前就在，与本批改动无关，未触碰）。

### 剩余风险 / 明确未做

- 徽章六合一、表格二合一、echarts 懒加载、图表高度令牌化、emoji 图标替换 —— 纯视觉
  重构，留待带截图验收的专项（理由见计划书 §1）。
- trade.js 第 891/923 行"目标腿"仍用涨红做强调色（计划内明确不动，后续可改 --link）。
- 改动全是前端静态文件，server.py/trade_main 均无需重启；浏览器需硬刷新一次拿新资源
  （版本号已全部 bump 到 20260919b，正常刷新即可）。

---

## 2026-09-19 — 决策台账上线当天修一个「谎报军情」的前端 bug：8081 明明是好的

**一句话（大白话）**：重启交易进程后，页面上那张卡报**「查询失败: 交易服务 (8081) 不可达」**，
但 8081 其实**好得很** —— 直接 curl `http://127.0.0.1:8081/api/trade/decisions` 返回 200。
**真正的错因和报出来的话完全不是一回事**，于是白查了一轮。根因是前端把请求**发错了地方**。

### 根因（一行代码）

```js
// 修复前 —— decision.js
if (typeof window.get === 'function') return window.get(path);
return fetch(path)…          // ← 每次都走到这里
```

`decision.js` 想复用 `trade.js` 里那个统一的 `get()`。但 `trade.js` 整个被包在
`(function () { … })()` 里，它的 `get` 是**函数内部**的局部函数，**从来没挂到 `window` 上**
（`trade.js` 只暴露了 `recordsPageEnter` / `recordsPageLeave` / `tradePageEnter` /
`tradePageLeave` 四个钩子）。于是每一次都静默退化成 fallback 的 `fetch(path)` ——
那是**同源**相对路径，打到页面的 **8080** 上，而 8080 根本没有 `/api/trade/*` 这些路由
（连原有的 `/api/trade/deals` 实测也是 404）→ 404 → 被 `catch` 吞掉 → 统一报成"8081 不可达"。

**为什么上一轮没抓到**：只测了两头 —— `test_decision_util.mjs` 测"文案怎么生成"，
`test_decision_api.py` 测"后端接口对不对"；中间这段**「请求到底发去了哪个地址」谁都没管**。

### 修法

| 项 | 做法 |
|---|---|
| 地址 | **算出来，不猜**。新增 `tradeApiBase(location.hostname)` 明确拼 `http://<hostname>:8081`；删掉"先试全局函数、不行再退化"那条投机分支 —— 那种分支会安静地走错路 |
| 传输 | 新增 `fetchJson(fetchImpl, base, path)`，`fetch` 由调用方注入（工具模块因此仍不碰全局 `fetch`，可在 node 直测）；抛错时**带上完整 URL**，否则排查时看不出请求打去了哪 |
| 报错口径 | 新增 `describeTradeError(err)`：**「连不上」和「服务端报错」必须说成两句不同的话** —— 一个要去看进程，一个要去看日志。混成"不可达"会让人去查错的东西 |
| 防缓存 | `index.html` 里 `decision.js?v=20260918a` → `v=20260919a`（`/web` 是静态目录，不推版本号浏览器会用旧的） |

用 `location.hostname` 而不是写死 `127.0.0.1`：手机走局域网 IP 打开页面时，
写死回环地址会连到手机自己身上。

### 测试（本次的重点）

新增 `tests/web/test_decision_wiring.mjs` —— **16 项**。它把 `decision.js` **真加载起来**
（假 DOM + 假 fetch + 假 `location`），走页面真实入口 `window.recordsPageEnter()`，
然后断言它请求的 URL。不联网、不起服务、不改仓库文件（`.js` 复制到系统临时目录再以 `.mjs` 加载）。

**并且证明过它不是摆设**：用环境变量 `VERA_DECISION_SRC` 指向一份"修复前"的副本重跑，
**9 项转红**，其中两条正是铁证 ——

```
[FAIL] 请求 1 打到 8081 的当日端点
  got:  "/api/trade/decisions"                    ← 裸相对路径 = 打到 8080 = 就是这个 bug
  want: "http://127.0.0.1:8081/api/trade/decisions"
```

断言覆盖：URL 必须带 8081 / 不许是裸相对路径 / 局域网场景跟着页面 IP 走 /
正常响应不报错 / 404 说"交易服务报错"且带状态码 / 真连不上才说"连不上"并告诉他去开哪个进程。

**顺带**：`tests/web/test_decision_util.mjs` 102 → **120 passed**（补 18 项，覆盖
`tradeApiBase` / `fetchJson` / `describeTradeError` 三个新函数）。决策模块 Python 侧
**302 passed** 不变（本次只动前端）。

### 上一轮那条"上线硬约束"已执行

交易进程 `trade_main`（8081）已由用户重启，`/api/trade/decisions` 实测 **404 → 200**。
（重启窗口：**交易日 14:54–15:00 之间不要重启**，会触发启动补偿补跑一轮调仓；
本次是 2026 年 9 月 19 日星期六、非交易日，重启安全。）

### 剩余风险（已记录，未改）

`trade.js` 里存在**同一类**的报错口径问题：第 141 / 454 / 464 / 473 / 481 / 636 行等
把任何失败都写成「交易服务 (8081) 不可达」。它不像本次这个会**必然**误报（那里多数情况
确实是连不上才报），但一旦端点改名（404）或返回体畸形（JSON 解析失败），同样会指错方向。
**未动它** —— 那是实盘页面，改动要用户点头。建议后续统一走 `describeTradeError`。

---

## 2026-09-18 — 交易记录「当日决策台账」：让每一天都能回答"为什么动了 / 为什么没动"

**一句话（大白话）**：以前点开交易记录，只能看到**真成交了**的那些单子；没成交的日子
（没信号、没站上均线、止盈线没跌破、程序压根没开机）是一片空白 —— 用户只能自己猜。
现在交易记录页**顶部**多两张卡：一张说"今天为什么动 / 为什么没动"（缺省看当天），
一张是"决策日历"（翻到 8 月，点任意一天看那天的原因）。**2026 年 7 月 27 日以来的
40 个交易日已全部回填**，8 月每一天点进去都有据可查。

### 用户需求原话（留证）

> 「在交易记录，如果当天没有记录，希望能够说明原因，如果有记录也说明为什么。主要是要知道
> 当天的情况，例如因为没有站上均线，所以没有触发，因为动量变了，所以etf切换，等等。
> 并且可以比较方便地看到历史原因。」

计划书 + 落地记录（含与计划的四处偏离、实施中钓出的三个真缺陷）：
`docs/plan/2026-09-18_交易记录当日决策台账_实施计划书.md`（§十一）。

### 关键落地

| 层 | 内容 |
|---|---|
| 数据表 | `daily_decision`：**一天 × 一条策略 × 一个对象 = 一行**，固定回答六件事（谁/哪天/做了什么/为什么/凭什么/关联哪几笔单）。主键 `(trade_date, strategy, subject)` **天然幂等**（定时跑一次 + 人工再跑一次只覆盖，不堆重复行） |
| 三态分离 | 有动作 / 无动作但有原因 / **根本没运行**（`NO_RUN`）。第三态是这次最容易漏的：程序没开机与"跑了但没信号"在页面上必须长得不一样，否则真故障会被一句"没触发"藏起来 |
| 原因码唯一真相源 | `trade/decision_codes.py`：24 个原因码，动作收敛 5 种（BUY/SELL/HOLD/FAIL/INFO），色调 **买=红 / 卖=绿 / 没动=灰 / 该做没做成=黄 / 告知=蓝**。`classify_rotation` / `classify_rotation_skip` / `classify_auto_buy` 三个**纯函数实时与回填共用**（不写第二份） |
| 唯一写入口 | `DailyDecisionStore.log(rows)` 一个人管四件事：①**行自己的日期**判非交易日拒写（守卫收口在一处，四个写入点不必各自记牢）②动作强弱合并 `SELL>BUY>FAIL>INFO>HOLD`，**「更弱的那次不许丢」双向成立**（先失败后成交、先成交后失败都留）③**事件保留与写入顺序无关**（见下）④出错吞掉 + 重试一次 + 写 `decision_log_fail` 审计 —— **台账写不进去绝不影响交易** |
| 来源可信度 | `source` 四级 `live`(3) > `backfill_exact`(2) > `backfill_text`(1) > `inferred`(0)，**低可信度不许覆盖高可信度**。判据**不能**简化成「是 live 就不许覆盖」——那会把实时重跑自己拦掉（施工中冒烟实测抓到）。页面用小角标标出"这条不是当场记录的" |
| 历史回填 | `trade/decision_backfill.py` + `tools/backfill_daily_decision.py`（离线 CLI，**不做页面按钮**）。从 `audit` 表还原 **40 个交易日 / 592 行还原 / 423 格落库**（`backfill_exact` 373 · `inferred` 47 · `backfill_text` 3 · `live` 0）。**老明细三种历史形态**都吃下了（08-17~08-20 单篮子只有 `state`/`changed`；08-21 动量嵌在 `signal.momentum`；08-24 起顶层 `momentum`）—— 判据是**「键缺失」而不是「值为 None」**（后者会把"今天没重算"误判成老格式） |
| 前端 | 交易记录页**顶部**两张卡。卡片一「今天为什么动 / 没动」缺省当天；卡片二「决策日历」按月翻（休市 / 没运行 / 有记录**三态颜色与文字都不同**）；**「近30天原因分布」统计卡按用户拍板砍掉**。纯函数抽到 `web/js/decision_util.mjs` 可在 node 下直测；`decision.js` 是**包装** `window.recordsPageEnter`（进了该页才挂卡），所以脚本引入放在 `trade.js` **之后** |
| 防漂移 | 收盘汇总的"命中判定"必须**复用** `monitor` 现有的 `_hit_cost_stop`/`_hit_trailing`/`_hit_ladder`/`_hit_time_stop`/`_hit_cond_time`/`_hit_first_day`，只新增"显示用距离"算术 —— 有一条用例 monkeypatch 这几个函数来证明判定确实被委派出去了 |

### 实施中钓出的三个真缺陷（都是**测试全绿之后**拿真实数据端到端核对才浮出来的）

| # | 缺陷 | 根因 | 修法与实测 |
|---|---|---|---|
| ① | 回填后台账行的**时间戳全失真**：2026-07-30 那行的 14 条重试全被标成"2026-09-18 23:34" | `updated_ts` 直接取"写库那一刻"。实时写入时两者天然等价，回填时能差两个月 —— 计划书完全没预料到 | `_load_audit` 把审计表原始 `ts` 带着走 → 写入口优先用 `event_ts`。**实时四个写入点一个都不用改**（不传就退回原行为），风险面为零。修后 `updated_ts` 散落回 2026-07-27~09-18，2026-09-18 那几行显示 `09:15:00`（预埋单）/`14:54:01`（轮动）/`14:54:31`（尾盘选股） |
| ② | 明细里"当天还发生过"**保留几条竟然取决于写入顺序**，且大多数日子只剩 1 条 | 写入口碰到"同强度、同一对象、又来一条"时**整份覆盖 evidence**；那一刻新行手里没有 `events`，于是把之前攒的轨迹清空一次。**先成交后失败**（07-30）恰好留住 14 条，**先失败后成交**（08-04，也是更常见的顺序）只剩 1 条 | 新增 `_merge_events()` 把旧行 events 接到新行前面；同强度但原话不同的主结论也进 events；同一条只存一份 + 封顶 `_EVENTS_MAX = 50` 保**最早**那批。实测：events 非空 **10 行/26 条 → 90 行/169 条**；`002039.SZ` 那天 **1 条 → 12 条**，与审计里 12 种文案**数量吻合**。有对拍用例锁死"两种顺序结果必须一样" |
| ③ | `events` 那一栏在页面上是**一大坨原始 JSON** | 走了 `JSON.stringify` 兜底 | 新增 `fmtEvents()`：一行一条流水（`HH:MM:SS 动作中文：原话`），按时间升序，缺时间戳排最前（不显示 `NaN`），超 12 条只说"还有几条没展开"；值单元格加 `white-space: pre-line`。**这条最要命** —— ②修好之后 events 成了台账里信息量最大的地方：2026-08-04 那天真正的故事是"移动止盈触发了，但 09:31 风控急停把单子拒了，之后每 10 秒重试一次，试到第 12 次才以 20.21 元卖掉"，全在 events 里；渲染成 JSON 等于**修好了数据却看不见** |

### 顺带修掉的小口子

| 项 | 内容 |
|---|---|
| `auto_buy.start()` 缺交易日守卫 | 定时入口只判了"开关开着吗"，**没判"今天是交易日吗"** → 周末/节假日 14:54 理论上也会跑一遍选股。已补（与 `rotation.start` 同口径），`manual` 不受影响（人工命令任何时段放行，2026-07-27 裁决①）；静默丢弃不写审计（休市日本就不该有"决策"，页面由日历标「休市」） |
| `label_of` 收口 | 决策文案要写"简称(代码)"（2026-09-07 用户反馈过满屏裸代码看不懂）。原本轮动有一份 `_etf_label`、台账又要在三处各来一份 → 提到 `trade/book.label_of` 做**唯一实现**，四处改引 |
| 日历读库失败会**谎报** | 原实现一次瞬时读失败会让整月每个交易日都显示"没运行" → 改成返回空格子 + `note` |
| 日历格摘要被不重要的事占掉 | `INFO` 会压过 `NO_RUN`（2026-09-11 那天只剩"阶梯止盈开关关着"，完全看不出程序挂了）→ 加显式优先级 |
| 前端两个静默失败 | `dominantTone` 让表外色调胜出 → 格子失去底色（无效 CSS）→ 忽略表外色调；`parseIso` 对畸形月份返回 NaN → 日历画出 42 个 NaN 格子 → 严格正则 + 返回 `null` |
| 回填指错库会**编数据** | 没有 `audit` 表时一次都查不到，程序会老实给每个交易日写一行"程序没开机" —— 一库假数据还看着挺像真的 → `_require_audit_table` 直接抛错 |
| 回填 CLI 新增 `--reset` | 实测发现"重跑一遍"**不一定能修正旧结果**（合并会继承旧 events，错时间戳跟着留下）→ 新增 `--reset`，**只删 `source != 'live'` 的行**，当场记录的一根头发都不动 |

### 测试与验收

- **新增 404 项断言**：`tests/trade/` 六个文件 **302 passed**（`test_decision_codes` / `test_decision_log` /
  `test_decision_digest` / `test_eod_decisions` / `test_decision_api` / `test_decision_backfill`）+
  `tests/web/test_decision_util.mjs` **102 passed**（node 直跑，零框架依赖）。
- **全量回归**：`pytest tests -q` → **2959 项 / 2952 通过 / 7 跳过 / 0 失败 / 0 错误，退出码 0**。
- **回填实跑**（2026-09-18 23:45，非交易时段）：逐日核对 §7 指定的四处边界 —— 2026-08-17 有轮动行、
  2026-08-31 有"程序没开机"、2026-09-18 有 3 份轮动行、2026-09-11 有"跑了但没到收盘就退了"，**四项全对**。
- **页面**：卡片一 / 卡片二与脚本引入三处一次落位；`pageRecords` 区块 div 配平 25/25、11 个元素 id 各出现 1 次。

### 剩余风险与**唯一未完成项**

- ⚠️ **需要重启交易进程 `trade_main`（8081）本改动才生效**（`/api/trade/decisions` 现在仍是 **404**）。
  **必须 ≥15:05** 重启（14:54–15:00 之间会触发启动补偿补跑一轮调仓，撞收盘集合竞价 —— 与
  2026-09-17 买侧在途台账同一条硬约束）。会话内起的后台进程会在回合结束时一并退出，会把交易系统
  留在停机状态，所以这一步**不由助手代劳**，按项目既有方式 `stop_vera.bat` → `start_vera.bat` 执行。
- ✅ **Web 后端（8080）不需要重启**：`server.py` 的 `/` 路由每次请求都 `read_text()` 读磁盘上的
  `web/index.html`，`/web` 是 `StaticFiles` 目录挂载，JS 改动刷新页面即生效。
- 重启后验收：`curl http://127.0.0.1:8081/api/trade/decisions` 应从 **404 变 200**，交易记录页顶部出现两张卡。
- 已知边界（如实记）：`inferred` 行的 `updated_ts` 是"写库时刻"（推断行没有"当时"可言，退回写库时刻是
  诚实的），当前前端**不显示**该字段，故无用户影响；将来若要显示需单独想一个口径。

---

## 2026-09-17 — 大盘位置页签分区：体检不合格的指标降级保留（用户拍板）

**一句话（大白话）**：指标体检已经判定宽度类指标（站上20日均线占比、新高新低、成交额）
**不能预测未来** —— 但它们能描述现状（"指数高位+宽度冰点"的背离预警有实战价值），
所以用户拍板**不删、降级**：挪进一个默认折叠的「这一区只说现状、不作预测」区。

### 关键落地

| 类 | 内容 |
|---|---|
| 页面结构 | 「位置趋势」+「箱线图」两卡从上半区挪到分桶图之后，包进 `<details id="mpLowEvidence">` 默认折叠；折叠区开头红字写明体检结论与"只能当体温计"的边界 |
| 上半区（有证据） | 位置表（十年百分位=强负相关）/ 估值 ERP（唯一又强又稳的正向维度）/ 照镜子 / 分桶图 / 候选择时规则 |
| 前端坑 | 折叠时图表容器尺寸为 0，echarts.init 会画空白 → `drawTrend`/`drawBox` 加 `_lowEvidenceOpen()` 守卫（折叠就跳过），`<details>` 的 `toggle` 事件里展开才画；版本号 → `20260917f` |
| 验证 | node 单测 88/88 + 页签接线 43/43；HTTP 实测页面含折叠区、JS 含守卫 |

---

## 2026-09-17 — 大盘位置新增「前期12月涨跌 → 未来12月收益」分桶图 + 复盘/体温表渲染修复

**一句话（大白话）**：用户看中外部研究报告（926 号回测）里"前期 12 个月涨跌幅分档 vs
未来一年收益"的图 —— **只借图的形式，数字全部用本机数据重算**。复核发现原报告
"跌0~10% 是最差档（-5.3%/胜率29.6%）"的核心结论在本机三个指数口径下**复算不出来**
（该桶实际 -0.3%~+3.9%），只有"跌透了会弹、涨疯了会落"这两头的方向稳定，
所以图旁必须带警告、且三指数并排不合并口径。

### 关键落地

| 层 | 内容 |
|---|---|
| 纯数学 | `core/market_position.py` 加 `_momentum_bucket_stats`（**私有接缝**：公开函数已顶到铁律 8 上限，唯一生产消费者是 runner）+ `MOMENTUM_BUCKETS` 桶边常量（与 926 号回测同 6 桶便于对照）；**分桶前 prior 舍入到 1e-6**（实测浮点把 -20% 整算成 -19.999…% 会滑进隔壁桶，边界测试抓出来的） |
| IO | `core/market_position_runner.py::momentum_buckets`（runner 第 8 个公开接口，满员；三指数各算各的不合并——中间桶排名对指数口径敏感，合并会假装有唯一答案）+ `MOMENTUM_BUCKET_WARNING` 大白话警告（测试锁黑话） |
| API | `GET /api/market_position/momentum_buckets`（薄层转发，只读本地指数日线缓存，不联网） |
| 前端 | 大盘位置页签「照镜子」下新增分桶卡片（柱=之后一年均值、虚线=胜率、柱顶 n=样本月数）+「当前落在哪一档」一行话；`market_position.js` 版本号 → `20260917e` |
| 顺手修复 | **今日复盘/完整体温表原来用 `textContent` 直贴 Markdown 原文**（用户看到"乱乱的"），改走与体检报告同款的 marked + DOMPurify + h1 降级渲染路径 |
| 测试 | python：`TestMomentumBucketStats`（6 桶边界手算锁 / 无未来函数两道锁 / 短样本 fail-soft）+ runner 侧 `TestMomentumBuckets`（三指数并排/空缓存 fail-soft/警告无黑话）；node：`momentumChartData`/`momentumNowHtml` 15 条 |

### 实测定论（本机数据，141 个月样本，n_eff≈11.7，数据日 2026-09-16）

- 上证指数/沪深300：「涨超40%」桶稳定最差（均值 -18.7% / -16.9%，胜率约 10%）；「跌超20%」桶全正
- 创业板指完全不同（涨0~15% 桶均值 +38.4%）——**中间桶排名对指数口径敏感**，这是警告文案的实证依据
- 当前位置：上证 +0.2%（涨0~15%档）、沪深300 -3.5%（跌0~10%档）、创业板 +2.3%（涨0~15%档）

---

## 2026-09-17 — 「大盘位置」页签点了没反应：漏的是第 4 处接线

**一句话（大白话）**：页签要能用，得做四件事 —— ①页面上有个按钮 ②**点它的代码**
③刷新后能回到这一页 ④按钮对应的内容区。**前三件都做了，第二件（点它的代码）漏了**，
所以点上去毫无反应。从代码上看"像是做完了"，因此前面几轮验收都没发现：
我当时只验了"接口挂在服务上"，**没验"点一下会怎么样"**。

### 关键落地

| 类 | 内容 |
|---|---|
| 缺的那一行 | `web/js/vera-ui.js` 补 `tabBtnMarket` 的 click 监听（与其余 9 个页签同一写法） |
| 缓存串 | `index.html` 里 `vera-ui.js`/`market_position.js` 的 `?v=` 提到 `20260917b` —— JS 是静态文件，**不改版本号浏览器可能继续用旧副本**，改了也白改 |
| 回归锁（新） | `tests/js/test_tab_wiring.js`：A 每个 `tabBtnXxx` 必须有点击监听 / B 每个页签要有内容容器（`backtest` 是主界面、例外）/ C 每个页签名必须在 hash 白名单里 / D `switchTab` 必须真的处理它。**43/43 通过**，并验了**反锁**（把那行删掉测试必须红 → 实测"抓到了"） |

### 验证（走 HTTP 验，不看磁盘）

- `GET /` 里有 `id="tabBtnMarket"`、版本号已是 `20260917b`
- `GET /web/js/vera-ui.js` 里有那一行 click 监听
- `GET /api/market_position/latest` → **HTTP 200 / 12,115 字节**（后端路由本来就通，问题纯在前端接线）
- 全量 `pytest tests/` **EXIT=0**；`node tests/js/test_tab_wiring.js` 43/43；
  `node tests/js/test_market_position.js` 73/73；`node tests/js/test_recover_integration.js` 13/13

### 教训

**纯函数全绿 ≠ 页签能用**。前端当时的 Node 单测测的全是 `market_position.js` 的纯函数，
**DOM 接线一处都没测**；而"接口返回 200"也只证明后端通，证明不了按钮接上了。

---

## 2026-09-18 — 公式农场达标榜卡片改说人话 + 补口径披露

**依据**: 用户原话「看不懂最优组合的黑话表述 c-0.2_a0.08_d0.005_Loff_t20_cd0_cp0.0」「粗扫的股票池、回测周期，在卡片上要写清楚」

**一句话（大白话）**: 榜单上那串密码一样的参数组合，现在直接显示「硬止损20% + 移动止盈激活8%/回撤0.5% + 时间止损20天」；表格上方多一行「用什么股票池、几分钟线、多少钱」的说明；每条公式回测的是哪段时间，单独一列写清。

### 关键落地

| 类 | 内容 |
|---|---|
| 人话翻译唯一实现 | 新增 `core/farm_summary.py::_combo_plain` —— 达标榜卡片与「→ 回测页」回填横幅**共用同一个生成器**（原回填横幅手写一份，再抄一份给榜单必然漂移，AI 设置三档合并的教训）；阶梯止盈开启时必须出现在文案里（漏掉 = 卡片少报一条真实规则） |
| 榜单行新字段 | `_board_row` 增 `combo_text`（人话）+ `window`（回测区间）；原始 key 仍下发，前端收进鼠标悬停提示供排查；老档案无 params 时 `combo_text` 留空、前端回落显示 key —— **后端绝不编文案** |
| 口径抬头 | `overview` 增 `caliber_text`（后端唯一生成，LOW-8 纪律：前端不手写第二份）：股票池=沪深300 · 5分线 · 前复权 · 信号日收盘买入 · 300万本金 · 单票上限2万（轻仓） · 移动止盈优先 · 达标线 —— 数字全部现读 `farm_rules.SWEEP_CALIBER` / `describe()`，不抄死 |
| 前端表格 | `web/js/vera-ui.js` 最优组合列摆人话 + 新增「回测区间」列（区间逐条公式不同，不能只在抬头说一次）+ 表头改「最优组合(卖出纪律)」 |
| 测试 | `tests/test_farm_summary.py` 新增 3 条：人话文案与区间逐字段锁 / 阶梯止盈必须出现 / 无 params 回落 key 不编文案；连带回填既有断言共 **47 passed**；真实档案实测 GS1075 输出正确 |

**生效条件**: 改了 `core/` 下 Python，**必须重启 Web 服务**（`python server.py`）后页面才显示新文案（2026-09-17 教训：进程比代码旧 = 接口 404）；前端 js 直接读磁盘，刷新页面即新。

---




**依据**: 用户两次截图（先 `Not Found`，后 `Cannot read properties of null (reading 'style')`）→ 审计报告 [公式农场回填回测页前端抛错](docs/audit/2026-09-17_公式农场回填回测页前端抛错_审计报告.md)（引用对象为 .js/.html，不在 `_verify_references.py` 覆盖范围，行号已逐条核对 + 附真浏览器实测）

**一句话（大白话）**: 回填按钮点了会报错，而且错误**卡在抄写的中途** —— 公式名抄进去了，本金/止损/区间全没抄，留在页面默认值上。**页面看着"填好了"，实际跑的是另一套口径**（本金 100万 vs 粗扫 300万）。

### 关键落地

| 类 | 内容 |
|---|---|
| 第一种"旧" | 回填接口 `/api/farm/prefill` 返回 404 —— **服务进程启动时间(2026-09-16 23:08:49)比接口代码修改时间(23:58:21)早 50 分钟**，接口根本不在进程里。只重启 Web 后端（`python server.py`），**交易进程 trade_main 全程未动**；重启前先确认无任务在跑（`running:false`），未打断任何回测 |
| 前端 `null.style` 根因 | `sectorEmpty` 是 `#sectorGrid` 的**子节点**，而 `renderSectors` 用 `grid.innerHTML=` 整块重画 → **第一次渲染就把占位元素自己抹掉**，第二次访问即 null。触发需两个条件同时满足：通达信开着（板块加载成功，实测 128 个板块）+ 再有任意一次重画（农场回填会先清板块） |
| 修复①自愈 | 新增 `_sectorEmptyBox`（`web/js/vera-ui.js:221`）按需重建占位元素；`renderSectors`（`:249`）与 `loadSectors`（`:228`）两处**同一根因**都改走它，不写第二份 |
| 修复②收口重复 | `loadSectors` 里两处近乎逐行重复的失败提示合并为 `fail()`（`web/js/vera-ui.js:231`）—— 重复即漂移 |
| 修复③附带动作不许打断核心 | 农场回填"清板块"整段套 `try/catch`（`web/js/vera-ui.js:687` 起，catch 在 `:695`）：清板块是附带动作，参数回填是核心，附带动作出错绝不能让它半途而废（**这正是本次事故的放大器**） |
| 真浏览器验证 | 修复前 toast=`null.style`、hash 停在 `#farm`、本金 100万；修复后 **0 报错 / 切到 `#backtest` / 横幅显示 / 本金 300万 / 止盈激活 8% / 回撤 0.5% / 硬止损 20% / 区间 2024-09-02~2026-09-03** 全部与粗扫口径一致 |
| 顺手修一个"一直红"的测试 | `tests/web/test_data_cache.js:21` 断言找 `var(--red)`，而 2026-09-07 的 tokens.css 令牌收口已改为 `--up/--down`（全项目 0 处 `var(--red)`）→ **该测试自 09-07 起一直失败**（与本次改动无关，git 状态可证）。按当前令牌修正断言，语义不变，两个前端测试现已全绿 |
| 排除的环境噪音 | 抓到的 2 条 `console.verbose` 是 Chrome 自带"密码框不在 form 里"提示；farm 页轮询无未捕获异常（pageerror=0） |

**剩余风险**: ①**前端 DOM 层无自动化护栏** —— 现有前端测试是"纯函数 + node 直跑"，`renderSectors` 这类需要 DOM 的函数测不到，本机无 jsdom，**不为 5 行修复引新依赖**；建议加"轻量浏览器冒烟脚本"进发版前手工清单（待拍板）。②GS 编号撞号存量 35 条仍未补扫（另见公式农场条目）。

---

## 2026-09-17 — M1–M6 三轮审计发现处置（M7）：把所有"会说反话"的地方修掉

**依据**: 三轮独立审计（[口径与正确性](docs/audit/2026-09-17_M1-M6_口径与正确性_审计报告.md) /
[工程与架构](docs/audit/2026-09-17_M1-M6_工程与架构_审计报告.md) /
[交付与端到端](docs/audit/2026-09-17_M1-M6_交付与端到端_审计报告.md)，
引用机器校验 28/28、94/94、66/66 全 PASS）→ **处置报告**：[M1–M6 审计发现处置报告](docs/audit/2026-09-17_M1-M6_审计发现处置报告.md)

**一句话（大白话）**: 审计翻出 15 条问题，**其中三条会让报告"说反话"** ——
最狠的一条是：复盘报告判断"这是买入还是卖出"用的是自己编的一套代码，
跟生产库对不上，于是**每一笔买入都判不出来**，报告会一本正经地写「今日无买入成交」。
三条全修完，并且顺手抓到**审计没发现的 1 个 HIGH**：有个模块**整个不能 import**，
而它的失败被 `except` 吞成了「日历不可用」—— 不报错、只是悄悄不再检查数据新不新鲜。

### 关键落地

| 类 | 内容 |
|---|---|
| 买卖方向（HIGH） | `notes_gen/daily.py` 改为**引 `trade/book.py` 的唯一真相源**（23=买 / 24=卖），判定收进 `_is_buy`/`_is_sell`；测试夹具原来自己编 `0/1`，一并改成引生产常量（**测试和生产用两套口径=双双自洽**，这才是它没被抓到的原因） |
| 胜率分母（HIGH） | 两个坑叠加：①「最近 200 笔卖出」把**买入**算进分母（该列 `NOT NULL DEFAULT 0`，`IS NOT NULL` 过滤等于没写）；②**没有盈亏记录的卖出被当成"没赚钱"**（实测 105 笔，日期全在 2026-07-30~08-07，正是 `trades` 还没有这两列的时候）。修正后生产库实测 **20.5% → 38.3%**，并**明写被排除的笔数** |
| 模块不能 import（**新发现 HIGH**） | `core/kline_cache_maintenance.py` 模块级用 `STUB_TRADED_RATIO` 却只在函数里惰性 import → 整个模块 `NameError`，而 `_expected_trading_day` 的 `try` 把它吞成「交易日历不可用，按今天处理」→ **新鲜度检查永远宽松、补拉入口一并失效**。常量提到顶层 + 模块级契约测试 |
| 标的名称取不到（**新发现 MEDIUM**） | `_stock_label` 写的是 `from core.data_fetcher import get_name_map`，而它是 `DataFetcher` 的**类方法** → `ImportError` 被 `except Exception` **无声吞掉** → **每只标的都印「（查不到中文简称）」**，而名称表里 7300 条、代码全在。改成 `DataFetcher.get_name_map()` + 失败要打 WARNING。**这两个是同一类病**：`except Exception` 把"我坏了"伪装成"数据里没有"，测试（只断言不崩）永远抓不到 —— 靠**把最终报告读一遍**才发现 |
| 隔夜简报把机器数据包倒给用户（**新发现 MEDIUM**） | 09:05 那张卡原来直接转 `brain/market_panel.market_snapshot()` 的返回文本，而那是**给大脑看的原始数据包**：卡片里出现错位的列、`NaN`、`133558676`（没单位的封板资金）、甚至一行被截断的「福莱蒽特 10.011」。改成 `market_panel.overnight_facts()`（**结构化数字**，新出口）+ `morning.py::_overnight_plain()`（报告层组织人话）；**机器数据包逐字节不变**（冻基线比对 2692 字符 → 2692 字符）；新增反向断言：`NaN`/`数据包`/`最新价` 出现在卡片里即判红。另修：简报里的「昨天」改为**由数据生成**（实测今天 9月17日、缓存最新只到 9月15日，写"昨天"是错的） |
| 飞书推送验收（**原计划一直没执行**） | 离线过完 V2~V7（**表格没被 lark_md 打散**、数据滞后抬头、占比不带正号、照镜子警告整句、三条已知偏差、铁律提示）→ 真推一次：`ok: true`、`cards: 4`、`codes: [0,0,0,0]`；同时验了落盘 `data/daily_review/2026-09-16.md`（27,720 字节）且该目录**不在 RAG 语料内** |
| 硬编码统计结论 | 维度体检正文的「rho +0.55 / −0.57」「各指标 7.6~12.3」「4 个族」全部改成**由数据现算**；新增**注入式回归锁**（把数据改成 +0.99/88.8，正文必须跟着变）+ 一条反锁 |
| 预热余量 | **冻结数据实测**（只让预热变）：漂移天数 0→243、60→187、120→126、180→67、240→10、**250 起→0** → 下限就是整窗 250（`min_periods=120` 是陷阱：有数但是错的）。`WARMUP_BARS = AMOUNT_RANK_WINDOW + 50` 单源推导 + 「数值不许漂移」回归锁 + 反锁 |
| 影子回放的样本口径 | 双窗口**每段报持有段数**，段数 < 30 标「样本不足，不算数」；日频 `n_eff` 明写**按天算**，并点名「真正独立的下注 = 建仓次数」为保守下界 |
| 静默边界 | 删掉「一个参数同时管两条数据窥探纪律」的 `gap`（误传现在直接 `TypeError`）；构建自检改为在**磁盘侧**直接查 source 重（原来"两文件撞同一 source 且其一失败"会漏判） |
| RAG：解自相矛盾 | 提示词要求"审计/根因类必须先搜库"，而 `docs/audit` 被排除在索引外 → **移出排除名单**；补 4 例**真正指向 `docs/audit`** 的评测（原来那 4 例挂名"审计报告"、路径全在别处，**等于没测过**）→ 4/4 命中 |
| RAG：扩语料代价（反事实实测） | 同一套 44 例、只切换 `docs/audit` 进不进索引，各全量重建：命中率 **都是 95.5%**，但 **MRR 0.9015 → 0.8447**（≈5 个原本第 1 名掉到第 2 名）。**"没有挤占 top-k"只在命中率口径下成立** —— 以后扩语料必须两种口径一起报 |
| 邮箱语料默认入口（HIGH） | 技能脚本在 `<技能目录>/scripts/` 而工具只在根目录平铺 glob → 默认入口 **EXIT=3**；修完又撞上本机只有 `powershell` 没有 `pwsh` → `[WinError 2]`。两处都修，端到端复跑 **EXIT=0**，重跑幂等（19→19 文件） |
| 前端/体温表用词不一致 | 同一条分档规则的第二份实现（页面"高位/中位" vs 体温表"偏贵区/中间"）→ 逐字对齐，两侧各加锁 |
| 交付数字勘误 | `breadth50` 47.1%/+0.3%/−1.1% → **48.0%/+0.5%/−0.9%**（并写明这是**活读数**：缓存一变动就动）；"118 条被劣化"三个口径（单次 72 / 连续 119 / M2 记 118）全部列出并说明差别 |

### 验证

- 全量 `pytest tests/` **EXIT=0**；`node tests/js/test_market_position.js` **73 通过 / 0 失败**
- 生产库**只读**取证：9月2日那笔 `513100.SH` 买入现在能正确识别；胜率 38.3%
- RAG：**4912 文件 / 12481 块**（`skip 0`、`problems 0`），盘符开头 source **0**、踩排除目录 **0**，
  `fresh` = `stale: false`；评测 **48 例 46/48 = 95.8%**（原 44 例保持 42/44 可比）
- 处置报告引用机器校验 **8/8 PASS**（并记录一个校验器盲区：**"总引用 0"也会 PASS**）

### 剩余风险（已登记，非本次范围）

`docs/audit` 里 10 篇按需生成的因子体检报告也进了索引（排除机制只支持目录前缀）；
`breadth` 有 **0.02 个百分点**的 pandas 浮点尾差（根因已定位，不修）；
飞书卡片仍未人工眼验；`data/daily_review/` 尚未产出过 —— **那些会说反话的段落一条都还没发出去**。

---

## 2026-09-17 — ETF 轮动买侧在途台账 + 隔夜核销（买入废单/部成不再留幻影持仓）

**依据**: [计划书 v2](docs/plan/2026-09-17_ETF轮动买侧在途台账与隔夜核销_计划书.md)（经一轮独立审阅迭代：13 处问题全并入）→ [实施报告](docs/audit/2026-09-17_ETF轮动买侧在途台账与隔夜核销_实施报告.md)
**用户拍板**: 出计划书 → 审阅迭代一次 → 开工 → **完工后两轮独立质量审计**
**一句话（大白话）**: 以前买 ETF 是"委托一发出就在账本上写：我买到了"，券商废单或只成交一部分时，账本上会留下**根本没买到的"幽灵份额"**；现在给买单也记一本"在途台账"，**当场或第二天拿 QMT 的委托回报核对，按真成交的数量把账本改回来**。方向**只减不增**——宁可账本多留一点（少赚），也绝不会少记（少记会触发重复买入=超仓）。

### 关键落地

| 类 | 内容 |
|---|---|
| 台账 | 复用 `rotation_meta` 新键 `rotation_open_buys`（**零 DDL**）；条目 = `[份, 代码, 记录量, 已核销量, 委托时间, 查不到轮次]` |
| 两个核销点 | ① pass 内买入后等一轮回报（买侧独立 1 秒短超时，不堵唯一写者）；② 每 pass 开场隔夜核销；**核销的唯一扣减点** |
| 恰好一次 | `reduce_by = 记录量 − 实际成交量 − 已核销量`；台账条目**移除与扣减同步**，pass 末整表替换（三轮 pass 用例锁死） |
| 失败方向 | **先写台账成功、再改内存**；台账写失败 = 整条跳过不扣；任何崩溃窗口都只让"账本 ≥ 真实" |
| 查询异常 | **立即返回、台账不动**（一次超时绝不误扣；卖侧同步加固这一条，其余卖侧语义零变化） |
| 安全闸 | 新增 `rotation_lots_understated`：`Σ份簿记 < QMT 持仓` → 该代码本轮**不补买**（拦"少记→补买→超仓"一整类），连续 3 轮命中升级告警 + 给出两条人工修账入口与后果警示 |
| 对账口径 | 期望值 = QMT 持仓 + **我方非终态买单的未成交量**（用当次委托回报的 `qty − filled_qty`）→ 修掉"部成仍在途"导致的**假漂移** |
| 迁移 | 顺序改「**先核销（买+卖）→ 后迁移**」；迁移清两本台账 + 逐条审计被丢弃的在途条目 |
| 测试接缝 | `FakeGateway.simulate_partial_cancel`（部撤状态 53 原先测不到） |

### 验证

- 全量 `pytest tests` **EXIT=0**（2435 条）；轮动两文件 66 passed；工作树仅 4 个文件（`trade/rotation.py`、`trade/gateway.py`、`tests/trade/test_rotation_lots.py`、`tests/trade/test_rotation.py`）。
- **两轮独立质量审计**（+ 修复后窄范围独立验证）：第一轮"有条件通过"（3 项必修：对账假漂移、闸的误伤面、两条分支无用例 → 全修）；第二轮由另一名审查员独立复审"有条件通过"（1 项必修：**新增的两条"人工修账入口"文案照字面做不生效** —— 进程运行中改库会被内存镜像整表覆盖、表里还有行时清标志不会迁移 → 已改为"先停 trade_main / 清空全部行"的可执行指引）；第三名审查员窄范围验证确认两处修复正确，并揪出**同一份修账指引手写三份且漂移**（升级告警那份漏改）→ 收口为单一真相源常量 `_LOTS_REPAIR_HINT`，三处文案各加字面断言锁死。

### 上线时序（硬约束）

2026-09-17 **14:54 错峰代码首次真实执行**（跑现有代码）→ **≥15:05 重启 `trade_main` 装载本改动**（**绝不在 14:54–15:00 之间重启**：启动补偿会在该窗口补跑一轮调仓，撞收盘集合竞价）→ 2026-09-18 14:54 首个带买侧核销的 pass → 盘后比对"份簿记 vs QMT 持仓"验收。

### 遗留（已登记，非本次范围）

`<key>.bak` 损坏备份键不清理（同键覆盖写，最多 2 行、无读取路径，属有界死行）；卖侧读侧损坏仍静默跳过（本计划把卖侧改动限定为"查询异常早退"）；`entry_high` 仍用委托价而非实际成交价。

---

## 2026-09-17 — 盘后复盘报告：一条消息看完「大盘 + 账户 + 留意清单」（M6）

**依据**: [计划书](docs/plan/2026-09-17_盘后复盘报告_计划书.md)（§3 架构裁决 → §5 留意清单 → §12 外部最佳实践 → §13 深度审查修订）+ 实施记录 §14
**一句话（大白话）**: 以前收盘后只有"我的账户"那一半（飞书日报），**大盘那一半完全没有**。现在 15:55 推一条消息：**大盘在什么位置 + 我的账户怎么样 + 有什么需要我留意 + 今天做对了什么 + 我的交易习惯 + 累计统计**。

### 关键落地

| 类 | 内容 |
|---|---|
| 模块 | 新增 `notes_gen/daily.py`（编排器，4 个公开接口：`build_review` / `review_md` / `push_review` / `run_daily_review`），与既有 `notes_gen/monthly.py` 同族 |
| **依赖单向朝下** | 编排器在**更外层**同时 import 两边；盘面段**复用** `thermometer_md()`、账户段**复用** `daily_report.payload_json`（**不重算**）、月度口径**复用** `trade.analysis.build_daily_pnl_view`、止损阈值**复用** `trade.Config.CostStopConfig` —— **一处都没写第二份** |
| **双向 AST 守护** | 正向：`core/market_position*.py` 绝不 import trade（既有）；**反向**：`trade_main.py` 与 `trade/` 不许 import `notes_gen`（§13.3 M8，否则「报告→交易」反馈环就通了） |
| 落盘 | `data/daily_review/<日期>.md`（**`data/` 已被 RAG 排除 → 天然不会自我污染检索库**；绝不落 `docs/`）；有测试与 RAG §12.6 联验 |
| 调度合并 | 15:55 的 job 由「推体温表」改为「**先补采 → 再推复盘**」—— 体温表是复盘的盘面段，两个 job 各推一次会让用户同时收到两条重叠卡（测试断言 `push_thermometer` 不许再出现） |
| 停机纪律 | 当日无 EOD 归档 → 账户段明写「今日没有交易归档」，**绝不拿昨天数字冒充今天**；空壳 bar（`volume=0`）与一字板（`high==low`）→ 写「当日无有效振幅」，**不算出一个数**（§13.1 MED-2 防除零） |
| 大白话 | 每个数字后跟一句解释；留意清单**不许出现评价性措辞**（`过于/太频繁/你应该` 有反向断言）；止损只报**硬止损**那一条（移动止盈要峰值、阶梯要分档记录，本报告不复原 —— **硬凑就是编**） |
| 页签 | 大盘位置页签新增「今日复盘」折叠块（新端点 `/api/market_position/review`，薄层只转发） |

### 实测抓到并修一个单位错

payload 里的 **`turnover` 是「成交额（元）」，不是百分比** —— 写方 `trade/daily_report.py:68` 存的是 `Σ|amount|`，`trade/notifier.py:282` 也按「成交额 {x:,.2f}」打印。第一版按百分比打印，**空成交时显示 0.0 看不出错，一旦有成交额就会把「12 万元」印成「+0.00%」**。对写方核实后改正，并加测试断言「成交额 120,000 元」。**教训**：跨模块用别人的 payload，单位必须对着**写方**核，不许猜（同类：`win_rate` 是比率、`day_pnl_pct` 是百分数，两个单位不一样）。

### 验证

- `tests/test_daily_review.py` **20 条**新增全绿：停机不冒充 / 复用 payload / 除零与空壳 bar / 月度口径**真的被复用**（spy 断言）/ 留意清单无评价词 / 落盘路径不在 RAG 语料范围 / push fail-soft / **双向 AST 守护**。
- `tests/test_scheduler_main.py` 更新：15:55 必须**先 collect 再推复盘**，且**不许**再单独 `push_thermometer`。
- 端到端实跑：`python -m notes_gen.daily --asof 2026-09-16 --stdout` 输出账户段数字与真实 payload 逐项一致（总资产 1,061,192 元 / 当日 +0.50% / 成交额 0 元）。
- 全量 `pytest tests/` **EXIT=0**；JS 单测 70 条全绿。

---

## 2026-09-17 — 研究库检索增强（RAG）：source 口径收口 + 语料扩到 research/docs + 邮箱语料入库（M5）

**依据**: [计划书](docs/plan/2026-09-17_研究库检索增强RAG_计划书.md)（§3.1 顺序铁律 → §4 要改什么 → §11 外部最佳实践 → §12 深度审查修订）+ 实施记录 §13
**一句话（大白话）**: 以前大脑只能在自己公司的档案里搜；现在**我们写过的研究报告、计划书、方法论**它也能搜到了，而且**邮箱收到的外部研报**也进了库（会标清"这是外部来的、没复核过"）。同时修掉一个会让"同一篇文章在库里出现两次"的隐患。

### 关键落地

| 类 | 内容 |
|---|---|
| **顺序铁律** | 先只做 `source` 收口（**索引范围一个字不改**）→ 量基线 → 再扩语料。两件事分开做，出问题才查得清是"口径变了"还是"新语料稀释了 top-k" |
| source 唯一出口 | `_to_source()`；**全模块只剩一处 `relative_to(`**（有测试断言）；`source` 由「vault 相对」改成「**项目根相对**」，大脑拿到就能直接读 |
| 三重防线 | ①唯一出口 ②项目根之外**抛错**不退化 ③构建后自检（不同 source 数 == 处理文件数） |
| 失败语义 | 读不到/嵌入失败 → 记 `skipped` **不算失败**；真漏/旧文件残留 → **拒绝落盘**；嵌入失败**重试一次** |
| 版本闸门 | `SOURCE_FORMAT = 2`；`update_file` 版本不匹配**拒绝写入**（防迁移窗口内同一文件两个 key） |
| 语料范围 | `config/brain_index.json`（读不到 = 老行为）；递归遍历；**排掉 `docs/brief`/`docs/report`/`docs/audit`**（机器生成物，不排就会被自己的 RAG 索引进去） |
| 新鲜度 | 新增 `index_freshness()` + CLI `fresh`：从 `meta.jsonl` **现算**（不新建 stamp 文件），给出新增/更新/已删/版本四类差异 |
| 邮箱语料 | 新增 `tools/mail_to_corpus.py`：调 qqmail 技能收信 + **自己解 zip 取 `.md`** → 按内容哈希去重 → 落 `docs/research_inbox/`（已 gitignore），每篇注入来源 + 「**外部产物，未经本系统复核**」警告 |
| 提示词 | 模式 A 扩成「公司档案 + 我们自己的研究报告 + 项目文档」，三类问题**必须先搜再答**，引用外部产物必须标出处 |

### 实测

| 项 | 收口前 | 收口 + 扩范围后 |
|---|---|---|
| 索引 | 4625 文件 / 5353 块 | **4799 文件 / 9842 块**，skip 0，problems 0 |
| 评测集 | 12 例（**全是公司档案**） | **44 例**（公司 12 / 研究报告 16 / 计划与方法论 12 / 审计 4） |
| 命中率 | 12/12 = 100% | **42/44 = 95.5%，MRR 0.9015** |
| 分类 | — | 公司档案 **12/12**（与基线一致）、研究报告 14/16、计划与方法论 **12/12**、审计 **4/4** |
| 邮箱语料 | — | 9 封邮件 → 21 篇 `.md` → 落盘 **19 篇**（去重 2 篇） |
| 索引干净度 | — | **盘符开头的 source 0 个**、**踩排除目录的 source 0 个** |
| 新鲜度 | — | `fresh` = **「索引与磁盘一致」** |

**别拿 95.5% 去和基线的 100% 直接比**：基线 12 例全是公司档案问题，研究类问题在扩范围之前**没有语料可命中**（结构性 0%）。干净的对照是**同类语料前后**：公司档案 **12/12 前后一致**（收口没破坏老语料），研究类从「不可检索」变成 **30/32 = 93.8%**。

**两个未命中逐条查过**：`r02` 返回的全是**同主题的兄弟报告**（语料里有 6 份 ETF 轮动报告，语义高度重叠）→ 主题命中、具体文件不对；`r13` 返回的只是**共享词汇**的文档 → 真失败（目标文件是内容偏薄的「研究补遗」）。**没有为了让数字好看去改查询。**

### 验证

- `tests/test_brain_search_engine.py` **36 条** + `tests/test_mail_to_corpus.py` **10 条** + `tests/test_brain_prompts.py` 全绿；全量 `pytest tests/` **EXIT=0**。
- 新增测试逐条对应计划书条款：三重防线、§12.1 四行失败语义、§12.2 版本闸门、§12.3「早退不改版本号」、§12.6 排除目录、§11.2 新鲜度四类差异、§12.4 技能缺失非零退出。

---

## 2026-09-17 — 大盘位置与趋势研判子系统（连续录像 + 历史照镜子 + 择时影子）

**入口**: 用户系统优化评估中点名「大盘所处位置 + 趋势研判（提升空间最大的方向）」，拍板「这个可以做」

### 关键结论

| 内容 | 结果 |
|---|---|
| 首跑实测位置（数据日 2026-09-15） | 上证十年百分位 90.9% / 沪深300 72.9% / 创业板指 88.4%；但站上 20 日均线仅 **21.2%**、创 60 日新高 51 家 vs 新低 **275** 家、全A成交额 16,037 亿元处于一年 **1.6% 分位** —— 指数高位、宽度与量能冰点，实证「只看指数会得出相反结论」 |
| 历史照镜子（**2026-09-17 已修订**） | 结论**不再基于「最像的 5 天」的中位数**（5 个样本不构成统计量），改为**距离最近的一档**（`similar_days(quantile=0.05)`，实测 **110 天**）：之后 20 个交易日沪深300 涨跌中位 **+1.5%**、平均 −0.1%、中间一半落在 −3.2%~+3.3%、上涨占比 57%；**但必须同时报「有效独立样本」—— 这 110 天挨得很近、涨跌高度重叠，真正独立的信息只有约 5.5 份（60 日约 1.8 份）**。top-5 明细（2022-01-24 / 2016-12-26 / 2017-12-08 / 2017-01-25 / 2023-03-08）降级为展示 |
| 照镜子按年份拆解（新增） | 2016 年 45 天 / 2017 年 34 天 / 2021 年 2 天 / 2022 年 24 天 / 2023 年 5 天 —— **2016+2017 合计占 71.8%**；而 2022 年那 24 天之后**一个上涨的都没有**（中位 −6.9%），2017 年那 34 天之后 94% 上涨。**读法**：同一批「位置很像」的日子落在不同年份结果完全相反，「总体中位 +1.5%」必须配这张表一起看 |
| 择时规则影子回放（2013-01-04~2026-09-16，3329 个交易日 / 13.7 年，T+1 口径，**毛/净并列**） | 沪深300>MA20 毛 +2.6%/**净 +1.0%**（建仓 196 次、持仓中位 4 日）；宽度≥50% 毛 +0.3%/**净 −1.1%**（179 次、5 日；**毛口径看着还行，扣费后转负**）；**牛熊口径=牛 毛 +4.3%/净 +4.2%**（仅 15 次、28 日，段数 <30 **不给 t 值**）；买入持有（对照）+4.3%。**成本口径**：现读 `backtest/engine.py` 默认值（`_cost_params()`，不写第二份），净 = 佣金×2 + 印花税 = **0.11%/次往返，未计滑点**（含滑点则 0.31%，报告明示「净口径是乐观下限」） |
| 回放结论（**措辞已按纪律改口径**） | 三条规则净口径 95% 区间**全部跨过 0** → 只能写「**看不出显著的优势或劣势**」，**不再写「明显输给买入持有」、更不写「无效」**（旧写法属过度解读）。双窗口（时间对半）**两条均线类规则前后半段不同向 → 标「待复核」**，只有牛熊口径与买入持有同向。牛熊口径与买入持有年化基本持平（+4.2% vs +4.3%）但**回撤浅 7.3 个百分点** —— 仍是唯一值得继续跟踪的候选，但它本身就是项目既有牛熊口径（与 ETF 轮动大势过滤同源，有「同一逻辑被同一份数据反复证明」的嫌疑），且仅 13.7 年单一样本，不足以支撑改规则 |

### 关键改动

- 新增 `core/market_position.py`（纯数学，8 个公开函数 = 铁律 8 上限）、`core/market_position_runner.py`（IO，7 个接口）、`market_position_api.py`（薄路由）、`tools/market_position_collect.py`（CLI）、`web/js/market_position.js`（第 10 个页签「大盘位置」）
- `core/index_regime.py` 加只读出口 `ma_and_slope`（纯加性）—— 防「偏离年线」各处重算 MA250 造出第二份口径
- `scheduler/__main__.py` 加三个 job：每交易日 15:50 采集（排在 15:45 缓存补尾段之后）、**当天 15:55 采集后再推飞书「大盘体温表」**（2026-09-17 用户纠错后改；原设计是「次日 09:05 补采并推」，实测 15:50 与次日 09:05 算出的数据日期是同一天、记录逐字段相同 → 早上那张卡一个新数字都没有）、次日 09:05 只补采不推送（留作**隔夜简报**的挂点，见下「遗留」）
- `tests/conftest.py` 隔离 `data/market_position/` 与 runner 的日线缓存目录（防 2026-07-27 投毒事件重演）
- `.gitignore` 加 `data/market_position/`（可由 `--backfill` 重建）

### 首跑抓到并修掉的四个坑

1. **空壳 bar**：日线缓存盘前抓数会造出「有日期、无成交」的假 bar（2026-09-16 09:16 写入的 000001.SZ 那根 open=high=low=close 且 volume=0）→ 有效交易日判据 + `stale` 标记，实测正确回退到 9 月 15 日
2. **年化分母**：影子回放原用录像条数（5501）当分母，其中 2174 天没有指数数据 → 买入持有被压成 +2.6%（真实 +4.3%）。改为按真实日历跨度折算
3. **规则状态口径**：`regime` 规则原落 `'bull'/'range'`，而回放按 `=='on'` 判定 → 持仓占比恒为 0%
4. **接口可用性连坐**：牛熊（需 220 根）与十年百分位（需 750 根）门槛不同，首版共用一个「样本不足」掩码，导致 400 根数据判不出牛熊

### 验证

- `pytest tests/test_market_position.py tests/test_market_position_runner.py` **50 项全绿**，含 AST 铁律守护（三模块均无 `import trade`）
- 全量回填 **5501 条**录像（2004-02-03 ~ 2026-09-15；指数数据起于 2013-01，故回放窗口 13.7 年）
- 幂等实测：同区间连跑两遍，JSONL 行数与内容逐字节不变

### 剩余风险

- 宽度与相似日的历史序列只用「今天仍在缓存里」的股票 → **生存者偏差**（早年缓存覆盖股票数明显偏少）；已写入 `CALIBER_FOOTER` 常量随报告带出
- 涨停家数按 10%/20% 幅度本地推导，ST 股（±5%）会漏计 = 低估
- 2016~2019 年的百分位窗口不足十年（指数缓存最早 2013），不可与今天完全并排比较
- **Web 页签待人工确认**：需重启 server 后刷新 `http://127.0.0.1:8080/#market` 才可见，本次未重启

### 追加（同日 M1/M2）—— 缓存空壳 bar 修复 + 照镜子/影子回放审查修订

**用户拍板**：出计划书 → 从网上找最佳业务实践迭代一次 → 逐行验证 + 深度审查 → **按四份计划书直接开工，每个里程碑自检，全部做完做三次独立审计 + 逐行验证 + 端到端测试**

| 里程碑 | 内容 |
|---|---|
| **M1** | `core/kline_cache.py` 新增只读出口 `last_bar_traded_ratio()`；`core/kline_cache_maintenance.stale_periods()` 对 **1d** 增加「末根成交量占比 < 50% 也算 stale」；5m/1m 不加（避开盘中天然无量的假告警）。**实测**：`last_bar_traded_ratio('1d') = 0.0426`、`stale_periods() = ['5m','1d','1m']` —— 修之前 1d **不在**这个名单里 |
| **M2·照镜子** | ①排除最近 **252** 个交易日（原 20 = 拿上个月当「历史」，是数据窥探），并拆成 `exclude_recent`/`min_gap` 两参数、旧 `gap` 留兼容；②结论改基于**分位带**而非 top-5 中位数；③新增**按年份拆解** + 单一年份 >50% **强制点名**；④分位带**同时报 N_eff**；⑤报告与页签新增「命中日最集中的两年合计占 X%」（无阈值的描述，让人自己看见集中度） |
| **M2·影子回放** | ①**毛/净并列**（成本现读 `backtest/engine.py` 默认值，不写第二份）；②**HAC（Newey-West）t 值 + 95% 区间 + 有效样本量 N_eff**；③**按持有段**做检验，且段收益**不做长度归一化**；④**双窗口**（时间对半）分别出净年化 + 同向判定；⑤**措辞纪律写进代码判据**（区间跨 0 → 只写「看不出显著的优势或劣势」） |
| **M3·牛熊区间** | `POSITION_COLUMNS` 增第 9 列 `regime_20`（**20% 法则**口径，与既有年线斜率口径**并列**）；体温表新增「牛熊区间（这轮走了多久）」一节。**实测两条口径分歧巨大**：年线口径说「震荡」（本轮 0.5~0.9 个月），20% 法则说「牛」（上证/沪深300 已走 **23.5 个月**、起点 2024年9月30日）、创业板指说「熊」。**并实测发现年线口径日频抖动太勤**（沪深300 历史 71 段、5.2 段/年）→ 输出里明写「这个口径的『走了多久』不可用」，而不是偷偷加去抖 |
| **M3·指标体检** | 私有 `_dimension_validity`：7 个指标 × 1/3/6/12 个月 → Spearman rho + p + 五分位差 + 有效独立样本（**月频采样**）。**实测三件事**：①**ERP 是唯一又强又稳的正向维度**（12 个月 rho **+0.55**、五分位差 **+37.4 个百分点**，**独立复现外部研究的 +0.48 / +26.7pp**）；②**十年百分位是强负相关**（上证 12 个月 rho **−0.57**、五分位差 **−32.2 个百分点**）；③**宽度类四个持有期全部看不出相关性** → 标「仅描述现状，不作预测依据」。遵守《公式因子体检方法论》纪律 2/3/5；**不做** DSR/PBO，只披露「共检验 28 个组合」 |
| **M3·图表** | 趋势图加 regime 背景着色带（牛绿/震荡灰/熊红）；新增按牛/震荡/熊分组的箱线图；前端纯函数 `regimeBands`/`boxStats`/`boxByRegime`（Node 单测 64 条） |
| **M4·大白话落地** | 用户明确要求（「我是小白，所有给我的数据、表述都要用大白话，可以打比方」）。**关键设计：解释由数字生成，不写死** —— 新增 5 个模板函数 `_width_plain`/`_hl_plain`/`_amount_plain`/`_zdt_plain`/`_position_plain`，输入数字、输出人话，并有单测锁边界（85% 时不许说「八成」）。体温表开头改成「先说人话再给数字」；位置表加「（偏贵区）」标签 + 逐列白话词典；影子回放口径删掉 `T+1`/`Newey-West (HAC) t 值` 并补「t 值／95% 区间／有效独立样本」三条白话；**`CALIBER_FOOTER` 三条已知偏差整段重写**（删掉「生存者偏差」这类黑话）；页签新增估值卡片、删掉 `MA250`；`trade/llm_review.py::_PROMPT` 加第 7 条（每个数字后必须跟一句「这意味着什么」，含正反例）。**反向锁**：黑话不许再出现在报告里（有测试） |
| **M3·估值** | 新增 ERP（股债性价比）维度：数据源 `akshare stock_ebs_lg`（乐咕乐股），**口径已用算术核实**（1/PE-TTM − 10年期国债 = 源值，相对误差 0.0006%），日频 5207 条（2005-04-08 ~ 2026-09-16）。落 `data/market_position/erp.jsonl`，体温表新增「估值（贵不贵，跟位置是两回事）」一节：**实测 ERP 6.22%、处于过去十年 72.4% 分位**（越高越划算），十年中位 5.53%、区间 2.47%~7.75%。**口径如实标注为「沪深300」，不是「全市场」**；联网取数 fail-soft + 一天最多一次 + env 可关（测试默认关） |
| 接口预算 | `core/market_position.py` 公开函数仍 **8 个**、`core/market_position_runner.py` 仍 **7 个**（新逻辑全在 `_` 私有接缝），`test_public_interface_within_limit` 保持绿 |

**实施中发现的两个真问题（都已修 + 留证）**：

1. **段收益归一化会把结论符号弄反**：计划书曾建议「段内日均收益」或「折算年化」。实测 `ma20` 的 196 段 —— 每笔平均盈亏 **+0.28%**，而「段内日均收益」是 **−0.39%/天**，与按天数加权的真实日均收益 **+0.028%/天** 反号。原因：除以段长给长赢家（最长 56 天）打折、却不给短输家（最短 1 天）打折。**处置**：段收益定义改为「段内复合收益，不做长度归一化」，检验的是「每开一次仓平均是赚还是亏」。
2. **计划期估的数总体是准的，但 M2 自检时一度读到偏低的数**：六特征全非空的日频观测读到 2461（计划期 2579）、排除 252 天后的可选池读到 2210（计划期 2327）。**追下去发现是一个静默数据劣化 bug**：日常采集 `collect(bars=300)` 把日线截断成 300 根，`amount_pct_1y` 用的 `rolling(250, min_periods=120)` 在窗口头部 120 天必然算不出，而这些记录被 upsert **覆盖**回录像 → 实测 **118 条历史记录（2025-04-17 ~ 2025-10-13）的 `amount_pct_1y` 被改成 null**，且**只坏不好、0 条被修好**。记录看起来是完整的，只有那一格悄悄变空 —— 比缺行更阴。**修法**：新增 `WARMUP_BARS = 300`，日常采集多读 300 根做预热、只输出窗口内最后 `bars` 天；修完复测**差值 0、被劣化 0 条**（回归测试 `TestWarmup`）。修正后交付值：**2579 / 2328 / 分位带 116 天 / N_eff 5.8 与 1.9**。`breadth50` 的 181→179 与这个 bug 无关（回放不读该字段），以 **179 次 / 47.1% / +0.3%** 为准。

**验证**：`tests/test_market_position.py` 34 条 + `tests/test_market_position_runner.py` 33 条 + `tests/js/test_market_position.js` 51 条全绿；全量 `pytest tests` 收集 **2527 条 EXIT=0**（本仓库 pytest 不打印汇总行，用 `--collect-only -q` 按文件计数求和报数）。新增测试含反向断言（措辞纪律不许出现「无效」「跑输」、HAC 对 AR(1) 序列的方差膨胀必须 > 2、成本参数必须等于 `BacktestEngine` 默认值）。

### 遗留（已登记，非本次范围）

- **隔夜简报**（次日 09:05 那个触点的真正内容：美股隔夜收盘 / 港股 / 南向资金 / 隔夜消息 + 昨日位置一句话背景）：只补采不推送的挂点已就位，**简报本身待做**；硬规则是「没有隔夜新信息就不发空卡」「昨天 15:55 没推成时附带补发」
- `CALIBER_FOOTER` 三条已知偏差**仍是大白话未改**（「生存者偏差」「ST 股 (±5%) 会漏计」用户看不懂）→ 归入 M4「大白话文案落地」
- ~~牛熊区间与时长、维度体检（月频 + N_eff）、regime 背景带与箱线图~~ → **M3 已全部完成**（见上表）
- `CALIBER_FOOTER` 的估值口径**已补**「ERP 是沪深300 口径，不是全市场」（M4）
- **大白话只覆盖了大盘位置子系统 + 交易复盘提示词**：`brain/` 对话、政策/舆情报告、公式农场日报文案尚未逐句审过 → 后续项

---

## 2026-09-16 — ETF 轮动改良研究·第三轮: 主旋钮全网格 + 13 年稳健性 (三轮总账 244 组)

**入口**: 用户两问「回测周期多久?」「组合数不多,有没有竭尽全力?」——前两轮 ~80 组是单变量对照,本轮把旋钮组合网格跑满

### 关键结论

| 内容 | 结果 |
|---|---|
| G1 窗口×阈值全网格 (64 格) | **20 日窗口整行是高原**(全部 8 个阈值列上最高, 28.5%~35.8%); 卡玛榜首 = 1.50, 只有 (20 日, 阈值 0) **基线本身**与 (20 日, 1.5%) 达到 —— 64 格无一格卡玛超基线 |
| G2/G3 网格冠军 | 止损 12%+两份(周三+周五) 卡玛 1.64、半导体第三腿+阈 3% 卡玛 1.51 —— 均超基线, 但属 ~550 组合搜索的**多重比较最大值**, 且各叠两层事后选择 (weekday 运气/特定腿×特定阈值), **不采信**, 仅留档 |
| G4 13 年稳健性 (2013-08-01→2026-09-15) | **20 日窗口在 13.1 年仍是明确峰值** (26.1%, 邻居 15~19%) → 窗口可信度大升; **阈值 1.5% 在 13 年反拖累** (23.6% vs 26.0%) → 证实阈值增强是近 10 年特定产物, 样本外不稳定; 红利+纳指 13 年回撤 -52.3% (2015 股灾) 反证黄金避险的关键性 |
| 回测周期说明 | 主窗口 10 年 (2016-09-16→2026-09-15) 是基线双腿的最长共同窗口 (创业板50ETF 2016 年 7 月才上市); 更长用长历史品种另做 13 年复核 |

### 验证

- 总账 244 组 (12+67+165), 报告新增 §八 (组合数总账/热力表/冠军警告/13 年复核/最终判断)
- 未跑数万种全排列的两条如实理由: 主旋钮交互已覆盖 + 搜索越大冠军越假 (项目"多重检验校正不做"的边界内)

---

## 2026-09-16 — ETF 轮动改良研究·第二轮补测 (67 组, 覆盖允许改动清单全维度)

**入口**: 第一轮交付后用户指出覆盖不全 (资金分配比例/候选池扩充/窗口阈值网格/调仓触发/分片其他形态未测)

### 关键结论

| 维度 | 结果 |
|---|---|
| 资金分配比例 (50~100%) | 卡玛随占比单调升 (1.40→1.50), **无甜点**; 80% 账户口径年化约 27% (余量按现金计, 实盘余量是股票池) |
| 分片机制 | **两份(周三+周五) 卡玛 1.57 唯一超基线** (年化 35.17%/换手 280), weekday 强弱两轮口径高度重合 (周三恒强/周一恒弱) 但属事后选择 → **生产维持三份**, 两份列为需用户拍板的备选 |
| 风险腿候选池 | 11 单腿诊断 (纳指 22.9%/创业板50 21.0% < 双腿 34.0% → 协同价值反证) + 替换/三腿/四腿/10 行业腿 **全部更差**; 半导体 39.7% 与酒 39.7% 账面亮眼但回撤 -27.7%/-42.4%; 豆粕 +7.3pp 但样本仅 5.75 年 → 留档观察 |
| 动量窗口 (10~60 日) | **20 日明确峰值** (25 日 28.7% / 60 日 9.6%) |
| 动量阈值 (0~5%) | 3% 年化最高 35.78% 但卡玛 1.34 仍低于基线 1.50; 曲线锯齿 → 维持"1.5% 为卡玛不亏的边际增强"结论 |
| 避险篮子 | 纯国债 25.6% / 纯城投 25.7% / 三债等权 28.5% / 纯货币 25.3% —— **单黄金全胜**; 十年国债 2017 年才上市, 10 年窗数据无效已标注 |
| 大势过滤变体 | 创业板指 200 线 24.8% / 300+120 线 27.7% / 300+250 线 30.9% —— 全部不如基线 |
| 止损规则 | 周频检查 33.7% / 12% 34.2% vs 基线 34.03% —— 差异 <0.5pp, 维持 15% |
| 仓位梯度 | **top2 各半全场最浅回撤 -18.76%** (卡玛 1.43, 年化 26.8%) → 深度保守留档 |
| 调仓触发 | 每日 29.4% (换手 987) / 双周 26.4% / 月频 10.2% —— **周频是甜点**, 与生产一致 |

### 验证

- 引擎扩展 (调仓频率/择腿名次/止损检查/账户比例) 默认值零行为变化, 基线回归与第一轮逐位一致 (34.03%/431 换手)
- 报告 `research/2026-09-16_ETF轮动基线诊断与改良迭代_研究报告.md` 新增 §七 覆盖度自查表 + §7.8 推荐清单更新; 数据 `output/etf_rotation_iter2/report.json` (79 行)

---

## 2026-09-16 — ETF 轮动改良迭代研究 (18 组回测: 基线已是局部最优)

**入口**: 用户委托「以三份错峰基线 (周三/四/五 + 双腿 20 日动量 + 黄金避险 + 日频 15% 移动止损) 为基准做迭代优化探索」

### 关键改动

| 类别 | 内容 |
|---|---|
| 新回测引擎 | `tools/etf_rotation_iter_research.py` — 口径 = T 收盘信号 / **T+1 开盘价成交** (用户指定), 前复权数据; 与上轮 T 收盘口径的 `tools/etf_rotation_freq_backtest.py` 互不混用 |
| 基线复测 | 10 年年化 **34.03%** / 回撤 -22.68% / 夏普 1.27 / 卡玛 1.50 / 换手 431 次 (止损仅触发 9 次) |
| 口径纠错 | 发现上轮脚本把「双腿动量≤0」处理成**空仓(现金)** 而非生产规则的**满仓黄金** → 上轮基线被低估约 9 个百分点/年 (本轮 B0x 旧口径对照 = 24.69% ≈ 旧脚本 25.86%, 差额为成交时点) |
| 18 组改良回测 | **无一同口径全胜**: 动量阈值 1.5% 年化 +1.1pp / 卡玛持平 (但参数非单调、2026 年内 +4.2% 远落后基线 +16.0%, 过拟合嫌疑); 大势过滤 (沪深300 破 200 线禁 A 股腿) = 熊市年化 7.5%→14.5% + 月内回撤全场最浅 -14.50% + 换手最少 (保守风控备选, 代价牛市少赚 ~12pp); 多腿/窗口 10/15/60 日/仓位梯度/止损 10% 或 20%/废三份/避险择强 全部负收益留证 |
| 引擎自修 3 错 | 隔夜跳空收益双计 (年化虚高 +5pp)、「已在黄金仍判换仓」幽灵换手 (644→431, 与上轮 434 吻合)、大势分段口径跨段差分 (分段年化虚高 3~10 倍) |

### 验证

- 报告 `research/2026-09-16_ETF轮动基线诊断与改良迭代_研究报告.md` 数字与 `output/etf_rotation_iter/report.json` 逐项核对一致 (B0x 24.69%、V2b 回撤 -38.38% 两处初稿笔误已修正)
- 结论: 主方案=基线+阈值 1.5% (仅作增强选项), 保守备选=大势过滤; **强制要求实盘先用 ≤20%~30% 轮动资金试运行 1~3 个月对账再放大**

---

## 2026-09-16 — 安装 akshare 1.18.94: 大脑盘面五板块 + 舆情搜索 + 轮动腾讯兜底恢复

**背景**: 用户问大盘快照为何五个板块全缺 → 大脑如实报「未安装 akshare」(且正确处理: 把安装建议当普通文本未自行执行, 防提示注入)。用户拍板安装。
**做法**: `pip install akshare -i 清华源` → 1.18.94; 因 `brain/ak_sections.py` 的 `HAS_AK` 与 `news_search._HAS_AKSHARE` 为模块级判定, 重启 server (PID 34984) + scheduler (PID 15160) 生效; trade 进程函数内惰性 import 无需重启未动。
**受益面**: 研究大脑大盘快照五板块 (A股指数/港股/美股/南向资金/涨停池)、`policy_pipeline` 舆情新闻搜索、ETF 轮动腾讯指数日线兜底 [trade/rotation.py:572]、影子校尺及 research 脚本群。
**验证**: 大脑盘面实际用的四路端点实测全通 — sina A股指数 562 行 / sina 港股 38 行 / 东财涨停池(当日) 89 行 / 东财南向资金 2716 行; 轮动用的腾讯指数日线 8717 行末根到当日。注: 东财 `stock_zh_index_spot_em` 端点被对端断连, 但盘面不用它, 已登记 CLAUDE.md 防误判。

---

## 2026-09-16 — 研究大脑标准档修复: claude CLI 不在 PATH 导致「大脑不可用」降级

**现象**: 重启系统后研究大脑报「claude CLI 未安装（npm i -g @anthropic-ai/claude-code），大脑不可用」并降级「大盘/盘面快路径」。
**根因**: claude CLI 装在 `D:\Program Files\nodejs`（npm 全局），但该目录不在用户/机器 PATH；`brain/claude_cli.py` 用 `shutil.which("claude")` 找命令 → 找不到。重启前能用是因为旧 server 进程启动时的环境恰好带着它。
**修复**: ①`D:\Program Files\nodejs` 写入用户 PATH（HKCU\Environment，与 Python313 同位置）; ②`start_vera.bat` 内置 `NODEDIR` 指路（同 PYDIR 模式，存在 `claude.cmd` 才前置）; ③用带新 PATH 的环境重启 server (PID 8664) + scheduler (PID 19212) — 交易进程不依赖 claude 未动。
**验证**: `claude --version` → 2.1.271; `shutil.which('claude')` → `D:\Program Files\nodejs\claude.CMD`; 端到端 `POST /api/research/chat/stream` (mode=standard) 返回 `success: true`，无「claude CLI 未安装」字样（测试请求中文乱码是 PowerShell 不发 UTF-8 的测试侧问题，浏览器路径无此问题，测试会话已清理）。
**连带坑登记 (CLAUDE.md)**: PowerShell 直接敲 `claude` 命中 `claude.ps1` 被执行策略拦，cmd/Python 无此问题。

---

## 2026-09-16 — 手机版 /m 六条优化落地 (纯前端, 后端零改动)

**背景**: 用户问「/m 有没有存在必要/会不会随 PC 自动变化/优化空间」→ 排查结论: PC 页写死 `min-width:1280px` 完全不适配手机, /m 是手机端唯一可用入口必须保留; 数据与样式与 PC 同源 (同一批接口 + tokens.css), 功能独立维护不自动跟随。**用户拍板: 六条优化全做。**

### 落地清单 (均在 `web/mobile.html`, 第 6 条在 `web/index.html`)

| 项 | 做法 |
|---|---|
| 后台轮询白烧电量 | 5 秒轮询加 `document.hidden` 暂停 + `visibilitychange` 回前台立即补刷 (交易页刷交易, 分析页刷分析) |
| 状态行漏通道信息 | 补读 2026-09-07 T4 新增字段: 通道名 (QMT/THS)、`channel_down` → 「通道异常」且状态点变红、`channel==='ths'` 且 `armed_effective===false` → 「未武装」 |
| 分析页只看旧数 | `analysisLoaded` 一次性闸门改为: 首次切页签自动加载一次 + 「每日盈亏」标题旁加「刷新」按钮 (`loadAnalysis`) + 回前台自动重拉 |
| 交易服务地址写死 | `TRADE_BASE` 支持 `?trade=<base>` URL 覆盖, 覆盖值记 localStorage 下次免带, `?trade=reset` 清除回落默认 `http://<主机名>:8081` |
| 手机看不到 ETF 轮动 | 交易页持仓与成交之间新增「ETF 轮动」区: 读 `/api/trade/rotation/last`, 逐份渲染 份号+锚定日(周一~周五)/目标腿/各腿 20 日动量/决策人话; 未启用/未执行/获取失败三态空态; 渲染放在主状态早退判断之前, 主接口挂了也出「获取失败」而非停在「加载中…」 |
| /m 无入口 | PC 页签栏右侧加「手机版 /m」链接 (新窗口打开, title 注明局域网/tailscale 访问方式) |

### 验证
- 内联 JS 抽取后 `node --check` 通过 (exit=0); JS 引用的 18 个元素 id 与 HTML 定义逐一交叉核对无缺失
- 轮动渲染结构对照后端真相源 `trade/rotation.py` (`last` 属性注释结构 + `_pick_decision` 人话决策 + `_WD_CN` 锚定日映射)
- 后端零改动, 未跑 pytest (纯前端变更); CLAUDE.md 架构骨架已加「手机版页面」行

---

## 2026-09-16 — 全系统代码质量审计 + 修复: P0×8/P1×39/P2×22 落地, 两轮独立自查通过 (全量测试绿)

**依据**: [审计报告](docs/audit/2026-09-16_全系统代码质量_审计报告.md)（P0×9/P1×39/P2×24, 133 处引用机器校验 100%）→ [修复实施报告](docs/audit/2026-09-16_全系统代码质量修复_实施报告.md)（96 处引用机器校验 100%）
**用户拍板**: 「除了 8081 鉴权那条（P0-4, 手机访问拍板的设计）不用修, 其他全修」; 范围=P0×8+P1 全修+P2 一行式小项; T1 保守方案（只改告警文案）; 重构类（prep 四合一/接口拆分）不动
**方法**: 10 审查代理分模块扫 7 万行 → 主会话逐条 read 核对 → 5 修复代理并行施工（78 文件 +962/-638）→ 主会话全量 diff 逐行复审 → **两轮独立自查**（第一轮有条件通过: 抓到第六份黑名单漏网副本, 当场收口; 第二轮通过可放行）→ 全量 pytest 4 轮 EXIT=0, 快照基线字节级零变化

### 关键落地（全清单见实施报告）

| 类 | 代表项 |
|---|---|
| 实盘安全 | 阶梯止盈兜底标档补 `tier_state.save` 双写（盘中重启不再同档双卖）; QMT 对账键名 `totalAsset`→`total_asset`（对账告警不再形同虚设）; `avg_cost<=0` 不再发 0 元卖单; `_sync_tranches` 归位消费者线程 |
| 数据正确性 | K 线缓存 F6 取当日末根（5m/1m 不再每日误报分红全量重拉）; 选股缓存 `exclude_quit: False` 撞 key 修复（缺省表驱动+回归锁）; 拉取失败不再盖 24h 冷却戳 |
| 口径收口 | QUANTQQ 三套达标线归一 farm_rules（15%/15%+补回撤腿）; 未来函数黑名单 6 份手写副本全部收口 `tools/future_tokens.py`（27 token 并集, 新文件）; 涨停判定公式 3 份收口 `detect_limit_up`（浮点顺序不变）; push_feishu 3 份合一; judge 子进程执行收口 `_run_cli_oneshot`（AI 设置标准档对 judge 生效） |
| 静默失效→有声 | 流动性约束两条失效路径修复+缺数据告警; ST 过滤缺失占比超 5% 告警; L0/L2 缓存两条安全线加固（当日不落盘/异常返 None 保守校验）; 公式卖出失败结果带 `formula_sell_failed` 标记 |
| 测试防线 | conftest 隔离补 kline_cache 接缝+影子日志路径; 网络焊死补 requests/smtplib; 真实管线测试显式关 K 线缓存; 名不副实测试整改 |
| 清理 | 删除 `tools/backfill_daily_asset.py`（会把推算资产行打上 'eod' 实测戳的旧脚本） |

### 有意未动（拍板/登记）
P0-4（8081 零鉴权+CORS*, 2026-08-18 手机访问拍板）; G4（prep 四份合一）; 接口拆分三项（DataFetcher/DataCache/PositionBook）; tdx_tq 双引导。

---

## 2026-09-16 — QMT 日线新鲜度: 审计两条 + 日历三条 + 择时闸门双计 全部落地 (全量测试绿)

**依据**: [审计报告](docs/audit/2026-09-16_QMT日线新鲜度判定_审计报告.md) → [修复实施报告](docs/audit/2026-09-16_QMT日线新鲜度判定修复_实施报告.md)
**用户拍板**: 「三条日历项 + 审计那两条一并修掉」→ 追加「顺手修掉」择时闸门双计

### 落地清单 (行号为**修复后**, 已机器校验)

| 项 | 做法 |
|---|---|
| 补下载后不复检新鲜度 (审计①, 高) | 新增复检口 `_note_history_result` [trade/gateway.py:514-530]: 仍陈旧 → WARN + 落 `history_stale` 标记 (取到新鲜时清除); 两个取数口各复检一次 [trade/gateway.py:569-570]、[trade/gateway.py:725-726]; 轮动侧把「陈旧」写进数据来源名让页面看得见 [trade/rotation.py:518-523] |
| 防抖戳写在下载之前 (审计②, 中高) | 失败码改按 **60 秒**重试 (成功仍 600 秒): 间隔选择 [trade/gateway.py:487-490] + 成败记录口 `_download_history` [trade/gateway.py:495-512] |
| 降级历下放宽判据 | `_expected_last_bar_day` 按日历可信度分两档 [trade/gateway.py:78-105]; 每天一条告警 [trade/gateway.py:58-66] |
| 装精确历库 | `exchange-calendars 4.13.2` 已装 (清华镜像); 2026 年内精度提升 (国庆/调休认得出) |
| 自带假日表 | **没有编造 2027 年日期** (国务院尚未公告, 编就是错): 把「只覆盖 2026 年」变成代码里的显式事实 [scheduler/trading_calendar.py:59-72] + 覆盖区间常量 [scheduler/trading_calendar.py:83-84] |
| 可信度判断口 | `calendar_covers` 按**精确历实际覆盖区间**判断 [scheduler/trading_calendar.py:87-92]、[scheduler/trading_calendar.py:105-117]; `is_trading_day` 先判覆盖再查历 [scheduler/trading_calendar.py:120-133] (表外日期不再每次刷 WARNING) |
| 弱市择时闸门当天计两次 (审计第四条发现, 收尾) | 网关新增只读观测「实际返回序列的末根日期」[trade/gateway.py:132-140] + 写入点 [trade/gateway.py:736]、[trade/gateway.py:751]; 闸门判据 [trade/auto_buy.py:139-153]、[trade/auto_buy.py:164]: 当日已在序列里 → **不再追加实时价**, 不在 (盘中当日 bar 被裁 / 数据源滞后) → 照旧追加 |

### 两条与原审计建议不同 (理由见实施报告第二节)

1. 失败后不是「立刻重试」而是「60 秒后重试」—— 原设计的阻塞上限 (下载超时 = 4 倍接口超时) 也要保住, 立刻重试会让每次调用都真阻塞一遍。
2. 区间口**没有**因陈旧拒收 —— 第一版改成返回空字典时 pytest 立刻打脸: 那正是 2026-09-16 那次修复要救的场景 (QMT 只有 9 月15日 而「应有一根」是 9 月16日, 拒收会把 9 月15日 收盘价一起丢掉, 停机日补算又回到拒写); 最终「照给 + WARN + 标记」。

### 实测发现 (本轮最值钱的一条)

**装库 ≠ 覆盖未来**: `exchange_calendars 4.13.2` 的 XSHG 历**只到 2026-12-31** —— 显式 `end=2030` 直接报错 `The XSHG holidays are only recorded to the year 2026`, 即 2027 年谁都还没有数据。
第一版 `calendar_covers` 只看「库加载成功」就报「可信」→ 2027 年会被误标可信、严格判据又被启用; 已改为按实际覆盖区间判断。

**收尾择时闸门时差点埋的第二个坑**: 观测值第一版写成「原始日线 DataFrame 的末根日期」, 但盘中 (15:05 前) 当日那根会被网关按无未来函数裁掉 —— 此时原始末根**就是今天**, 闸门会判「当日已在序列里」→ 不追加实时价 → **14:52 那条尾盘生产路径反而看不到当天最新价**。已改为记「实际返回序列的末根」(被裁后取前一根) [trade/gateway.py:751], 并补测试直接锁这个语义 [tests/trade/test_regime.py:245-281]。
**教训**: 凡「记一个事实供他人判断」的代码, 都要问——这个事实是我**取到的**, 还是我**交付出去的**?

### 验证

- 全量 `pytest tests -q` **退出码 0**; `pytest tests/trade -q` 退出码 0; 日历相关三文件 78 passed; `tests/trade/test_regime.py` 17 passed
- 新增 11 个回归测试 [tests/trade/test_gateway_history_fresh.py:196-296] + [tests/test_scheduler.py:62-75] + [tests/trade/test_regime.py:197-281]
- 两份文档引用机器校验 100% (审计报告 36 处 / 实施报告 23 处)

### 遗留 (2026 年 12 月待办)

2027 年放假安排公告后二选一即可恢复严格覆盖: 升级 `exchange-calendars` 或补 `_HOLIDAYS_2027` 并顺延覆盖区间常量。
在那之前系统「知道自己不可信」: 每天一条 WARNING、盘后只要求上一交易日、不再每 600 秒空补。
（`auto_buy` 实时价与当日日线双计已在本条一并修掉 —— 见落地清单最后一行。）

---

## 2026-09-16 — QMT 日线新鲜度改动 审计报告 (并行派发实验副产物, 9 条线索核出 4 条需修正)

**入口**: 用户问「多智能体能力够不够、会更快还是更慢」→ 拿真活做了一次「并行 vs 串行」派发实验
(同一批提示词: 串行 117.6 秒 → 并行 73.9 秒 = 1.59 倍; 先并行后串行, 缓存/预热的便宜全归串行臂的保守设计),
实验的副产物 = 对 2026-09-16 当天「日线新鲜度」改动的三角度审计 (边界条件 / 调用方一致性 / 失败路径)。

**产物**: [docs/audit/2026-09-16_QMT日线新鲜度判定_审计报告.md](docs/audit/2026-09-16_QMT日线新鲜度判定_审计报告.md)
(36 处引用机器校验 100% PASS, 退出码 0)

### 关键发现 (2 条建议尽快修)

| 级别 | 发现 |
|---|---|
| 🔴 高 | **补下载后不复检新鲜度**: 重取之后只判「空不空」`[trade/gateway.py:473-476]`、`[trade/gateway.py:626-628]`, 陈旧序列原样返回且不给调用方任何标记; 下游动量**只数根数不看日期** `[trade/rotation.py:122-125]` → 这次改动只修了一半, 但说明文字读起来像「已修好」 |
| 🟠 中高 | **防抖时间戳写在下载之前** `[trade/gateway.py:440]`, 下载失败只记日志不回滚 `[trade/gateway.py:467-468]` → 一次失败压住 600 秒; 轮动当天只跑一次的话等于当天不补了 |
| 🟡 中 | 本机**未装 `exchange_calendars`**(实测 ImportError, 而交易进程正是这个解释器) → 降级历只含 2026 年假日表 `[scheduler/trading_calendar.py:47-60]`; **2027 年起**落在工作日的法定假日会被误判成交易日, 盘后「应有一根」永远不成立, 每 600 秒空补一次 |
| 🟡 中 | `auto_buy` 无条件追加实时价 `[trade/auto_buy.py:146-148]` → 修复后 15:05 后当日日线已保留, **弱市择时闸门的指数均线**会把当天计两次(注: 影响对象是**指数均线**, 不是 ETF 轮动的动量参照点 —— 口头转述时我曾说错, 报告里已更正) |

### 方法与教训

- 9 条线索里 **4 条需修正**: 1 条引用失真(`cap_day` 封顶分支实为**无人调用**, 子智能体说「只被测试覆盖」不准确)、
  1 条定级偏高(Fake 是测试替身, 不算生产风险)、1 条未证实(轮动「每天只调用一次」的引用不足以支撑论断)、
  1 条夸大测试覆盖 → **子智能体的审计产出只能当线索, 逐条 read 原文这一步不能省**
- 机器校验的边界: `_verify_references` 只查「文件存在 + 行号在文件行数内」,
  **查不出「行号落在范围内但语义张冠李戴」**(本次 B2 就是实例: 引用 `[trade/gateway.py:793]` 说区间查询, 实际那行是 `query_daily_closes`, 区间查询在 `[trade/gateway.py:799-803]`)——已写入报告第六节
- 测试现状: `tests/trade/test_gateway_history_fresh.py` 16 个用例全绿(实跑退出码 0), 但**缺**「补下载后仍陈旧」的用例(该行为目前不存在, 修完第一条应补)

---

## 2026-09-16 — 手机(Tailscale)访问 DSH 对话界面 3080 (信任栅栏 + cookie 十年)

**入口**: 用户问"已连 Tailscale, 为什么手机打不开 100.94.120.22:3080"

### 关键改动 (DSH 侧, 仓库外)

| 类别 | 改动 |
|---|---|
| 根因 | 3080 只绑回环 `127.0.0.1`(官方明确不支持 `dsh web --host 0.0.0.0`); 另有 `/api` Host/Origin 信任栅栏 + 启动时打印的一次性 `?token=` cookie 两道门 |
| 实测证据 | `/api/status`: `127.0.0.1:3080`→401(可信未认证)、`desktop-9r6m55v.tail2f41cc.ts.net:3080`→403(栅栏拒)、`100.94.120.22:3080`→404(tailscale serve 按主机名路由, 纯 IP 不认); 对照: VERA 8080 绑 0.0.0.0, `http://100.94.120.22:8080/m` 手机版实测 200 |
| 通道 | 先试 `tailscale serve --bg --http=3080 http://127.0.0.1:3080`(HTTP 反代, **按主机名路由, 纯 IP 实测 404**), 用户追问"IP 不行吗"后改用 `tailscale serve --bg --tcp=3080 tcp://127.0.0.1:3080`(**原始 TCP 转发, 不认主机名, 域名与纯 IP 都放行**) |
| 配置 | `~/.dsh/profiles/web/cordis.patch.yml` 覆盖 `connection` 行: `trustedHosts: ['desktop-9r6m55v.tail2f41cc.ts.net','desktop-9r6m55v','100.94.120.22']` + `cookieMaxAgeDays: 3650`(登录 cookie 30 天 → 10 年, 手机贴一次 token 长期免贴) |
| 校验 | `node apps/cli/lib/bin.js --profile web --dump-config` **不启动**验 compose: EXIT=0, 输出标明该行 "patched by ...cordis.patch.yml" |
| IP 可达性实测 | 换 TCP 转发后(重启前)纯 IP `http://100.94.120.22:3080/` 由 404 变 **401**(已打到 DSH), `http://100.94.120.22:3080/api/status` 仍 **403**(白名单待重启生效); 改用 TCP 后域名同样通 |

**踩坑 (已写入 CLAUDE.md)**: 用户 patch 层**不支持 `!!js`** — 首版照 bundle 写法用 `!!js [...ctx.webRuntime.trustedHosts, 'x']`, dump 直接报 `unknown tag !<tag:yaml.org,2002:js>`(文件头注释却写"`!!js` expressions allowed", 文档与实现不符), 若直接重启会**启动失败**; 改字面量列表后通过。另 patch 是整块替换 config 不深合并。

**未做/边界**: token 无法取消(该 plugin 无关闭认证开关, token 只是首次种 cookie 的引导); 生效需重启 `dsh web`(会中断当前会话, 由用户自己挑时机); 撤销 = `tailscale serve --tcp=3080 off` + 还原 `cordis.patch.yml.bak-20260916`。手机端用域名或纯 IP 均可(已把两种写法都列进白名单), 但**必须带 token 访问一次**。

---

## 2026-09-16 — QMT 本地日线"有数据但陈旧"也补下载 (轮动动量参照点 / 停机日补算)

**入口**: 用户「检查当前 ETF 轮动的规则」→ 顺查取数链, 实测 QMT 本地日线滞后于 TDX

### 关键改动

| 类别 | 改动 |
|---|---|
| 根因 | `RealGateway.query_daily_closes` / `query_daily_closes_range` 只在**取空**时 `download_history_data`; 本机 9/16 盘后实测 513100 末根仍停 9/14、159949 停 9/15 (TDX 已有 9/16), 属"有数据但陈旧" |
| 影响面 | ① 轮动动量: 尾部缺 9/15, 14:54 追加实时价后参照点由「20 个交易日前」变成「21 个交易日前」; ② 停机日补算: 缺 9/15 收盘价 → fail-closed 拒写 9/11、9/14 (审计 `gapfill_reject` 实证) |
| 修法 | 「何时该补下载」收成**单一判定** `_should_download_history` (空 或 末根 < 应有一根), 两个取数口共用 (原为两处各写一份同语义条件); 应有一根 = `_expected_last_bar_day` (≥15:05 且当日为交易日→当日, 否则上一交易日; 日历不可用→不猜不下载); 同码防抖 `_REFRESH_MIN_INTERVAL_SEC`=600s (数据当天可能晚到, 失败一次不该把每个调用都拖成 20s 阻塞); 历史区间查询按右端 `cap_day` 封顶 (end 在过去不该被判陈旧) |
| 顺带收口 | 丢「当日未收盘 bar」的 15:05 时点与新鲜度判定同源 (`_CLOSE_READY_HM`); 日期解析 `_last_index_day` 一处实现 |

### 验证

- 新增 `tests/trade/test_gateway_history_fresh.py` **16 例** (时点语义 4 + 纯函数 2 + 陈旧/防抖 5 + 两口接缝 5), `pytest tests/trade` **全绿** (exit 0)
- **生产端到端**: 重启 trade_main 后日志出现 `query_daily_closes_range(513100.SH) 末根 20260914 落后于应有 20260916, 补下载历史`; 原先 fail-closed 拒写的停机日补算转为 `gapfill_write 补算停机日 2026-09-11、2026-09-14 → 两端夹逼一致 (右端 2026-09-15 分毫不差)`, `daily_asset` 新增 9/11 (1,057,104.92) 与 9/14 (1,054,700.92), `source='derived'`
- 复核 QMT 本地库: 513100 / 518880 末根已到 9/16 (2.218 / 8.905, 与 TDX 一致)

### 剩余风险

- 159949 未被本轮查询触发 → 仍停 9/15; 周五 14:54 轮动取它时会自动补下载 (同一判定)
- 若 QMT 当天迟迟不给当日 bar, 15:05 后同一代码最多每 10 分钟重试一次 (有界, 数据落地即停)

---

## 2026-09-16 — 本机 Python 定位修复 (商店占位符占名)

**入口**: 用户敲 `python` 报 `Python was not found; run without arguments to install from the Microsoft Store`

### 关键改动

| 类别 | 改动 |
|---|---|
| 根因 | `python` / `python3` 只解析到 `%LOCALAPPDATA%\Microsoft\WindowsApps\` 的 0 字节商店占位符; 真实解释器 `D:\Program Files\Python313\python.exe` (3.13.15, pandas 2.3.3) 从未进 PATH |
| 系统 | 该目录 + `Scripts` 写入**用户** PATH (HKCU\Environment, 原值 `C:\Users\Administrator\AppData\Local\Microsoft\WindowsApps;` 保留在后); 复核: 新终端 `where python` → 真解释器优先, `python -V` → 3.13.15 |
| 启动脚本 | `start_vera.bat` / `p0_tick_watch.bat` 顶部加 `PYDIR` 指路 (存在性检查 + 找不到即早退), 不再依赖系统 PATH; 其余 `data/formula_farm/runs/*.bat` 原本就是全路径写法 |
| 附带 | 8080 回测 Web 因该坑停摆 (8081 交易进程正常), 已用真解释器单独拉起, **未重启交易/调度进程** |

### 验证

- `python -V` → `Python 3.13.15`; `sys.executable` 指向真解释器, pandas import 正常 (CRLF 探针脚本实跑, 探针已删)
- `http://127.0.0.1:8080/` → 200, title `VERA — 量化回测系统`; 8081 仍返回 404 (交易进程未受影响)

**坑 (写入 CLAUDE.md)**: `.bat` 必须 CRLF 且无 BOM — 纯 LF 时 cmd 把 `set` / `if (` 解析成乱码 (实测 `'ogram' is not recognized`), 同一条命令换 CRLF 即通过。

---

## 2026-09-16 — 深模块浅模块审计修复波 (13/14 项落地, 两轮自查通过)

**入口**: [docs/audit/2026-09-15_深模块浅模块复查_审计报告.md](docs/audit/2026-09-15_深模块浅模块复查_审计报告.md) (59 引用机器校验 100%) +
[docs/audit/2026-09-15_深模块浅模块审计修复_实施报告.md](docs/audit/2026-09-15_深模块浅模块审计修复_实施报告.md) (含第二轮独立交叉审计结论)

### 关键改动

| 类别 | 改动 |
|---|---|
| 🔴 农场达标线 | `farm_batch_sweep` 手写门槛删除, 收口 `core/farm_rules` (is_pass/pick_best/describe) —— 白捡笔数≥20 守卫, GS1292 噪声当结论的形态彻底封死 |
| 🔴 夏普口径 | 分析页 summary 改调 `MetricsCalculator` 单一实现 (扣 1.5% 无风险利率, 与回测页同尺); `metrics.sharpe_ratio` 委托 `_sharpe` 消同文件双实现。**口径纠正: 分析页夏普/索提诺/卡玛/年化数字会略变, 不是业绩变化** |
| 路由埋计算×6 | 下沉纯函数: `build_daily_pnl_view`/`build_summary_view` (trade/analysis.py), `core/kline_view.py` (新, 同一变形家族两份收口), `scheduler.trading_calendar.month_grid`, `core/lab_runner.history_items` —— server.py/analysis_api.py/lab_api.py 回归薄路由 |
| 收盘竞价双胞胎 | 新 `trade/closing_auction.py` 单一实现 (买侧笼子上限/挂涨停, 卖侧跌停价); executor 深沪两个逐字节相同分支合并; auto_buy 补 st 参数 (ST 股 5% 板口径纠正) |
| 规则副本 | claude_cli `_classify_recovery` 消文件内双写; research 牛熊残份委托 `core.index_regime`; `brain/ak_sections.py` (新) 收编分家复制助手 + re-export 删除; kg 补 `get_all_companies`/`get_link_nodes` 门面消三处直写 SQL |
| 组合根 | gapfill 编排下沉 `asset_gapfill.orchestrate_gapfill`, trade_main 只留装配 |
| 小项 | shadow 别名删/ai_api 双 load/dsh_channel 注释漂移/_period_stats 降私有/gs_top9 常量/send_report_feishu 用 load_dotenv/builder keyword 白名单守卫 (tests/test_engine_run_path.py) |

**延期**: rotation 执行段拆分 (审计原判"单独立项", 实盘重区不在马拉松尾声动刀)。

### 测试增量与验证

- 新增 `tests/test_kline_view.py`; 3 个环境敏感测试改环境免疫 (test_brain/test_tdx_path/test_fastpath mock 迁移)
- 全量 `pytest tests/` EXIT=0 (含 TDX 真实链路); 第二轮独立交叉审计 12/12 通过 0 不通过, 结论"可合入"

### 剩余风险

- 分析页指标口径纠正后, 与历史截图/旧报告数字不可直接比 (口径变了, 不是业绩变了)
- rotation 拆分欠债一笔 (立项时配套 lots 全量测试 + 影子跑一周)

---

## 2026-09-16 — ETF 轮动资金三份错峰改造 (Phase 0-3 全部落地)

**入口**: [docs/plan/2026-09-16_ETF轮动资金三份错峰改造_计划书.md](docs/plan/2026-09-16_ETF轮动资金三份错峰改造_计划书.md) (两轮计划审阅 16 处修订) +
[docs/plan/2026-09-16_ETF轮动资金三份错峰改造_实施总结.md](docs/plan/2026-09-16_ETF轮动资金三份错峰改造_实施总结.md) (三轮实现自审)
**依据**: 10 年回测 ([research/2026-09-16_ETF轮动周频vs每日双负保险丝_10年回测研究报告.md](research/2026-09-16_ETF轮动周频vs每日双负保险丝_10年回测研究报告.md)) —— 三份错峰 年化 25.86%/回撤 -22.4%/Calmar 1.16, 全面优于全押周五的 23.08%/-20.6%/1.12, 且总交易金额不增加。

### 关键改动

| 文件 | 改动 |
|---|---|
| `trade/rotation.py` | 重写为多份架构 (单一代码路径, N=1 退化): 份运行时状态 + 份内簿记内存镜像; 统一执行 pass (一次运行算 N 遍、下一遍单, 分单不并单); 逐份移动止损 (持仓判定以 lots 为准); 迁移初始化 (按手轮转/碎股归尾/entry_high 继承→20日高→实时价/已初始化标志闸); 在途卖单台账隔夜核销 (rotation_open_sells, 自愈); D7 状态统一注入 (9-10 失忆事故根治); 漂移只告警不改账 |
| `trade/store.py` | 三张新表 + 三个表域内聚子 store: `rotation_lots` (份内虚拟持仓) / `rotation_tranche_state` (逐份信号) / `rotation_meta` (初始化标志+在途台账); 旧单行 `rotation_state` 停写保留 (供迁移继承) |
| `trade/config.py` | `rotation.signal_day` str→`tuple[str,...]` (str/列表皆收, 1~5 项不重复, 归一 tuple); `to_dict` 输出 list |
| `trade_main.py` | 日报 `payload["rotation"]` 逐份列表 (单份仍为单 dict) |
| `trade/api.py` / `trade/llm_review.py` | rotation/last config signal_day 输出 list; AI 复盘逐份一句话 (份N(anchor) 前缀) |
| `web/index.html` / `web/js/trade.js` | 设置面板信号日改多选 (多选=错峰, 提示改份数需重启); 轮动卡逐份渲染 (份N·anchor/目标/动量/止损基准), 配置行显示「N份错峰」 |

### 测试增量

- `tests/trade/test_rotation_lots.py` (新, 15 例): 三表存取 / signal_day 列表校验 / 迁移 (轮转分份·碎股·entry_high 继承·标志闸) / 只卖自己那份 / 止损按份 / clear_external_sells 单次 / 现金按份序 / 隔夜核销 / 漂移只告警 / D7 跳过路径状态注入回归 / 多锚定信号日 / llm_review 逐份。
- `tests/trade/test_rotation.py`: 34 例语义 parity 全绿 (注入点随簿记迁移: `_entry_high`→lots、`_pending_target`→`_tranches[0]`)。
- 全量 `tests/trade/` + web 提示: **613 过**; 两份 rotation 文件 **49 过**。

### 剩余风险 / 环境说明

- 全量 `tests/` 有 `test_sector_selection` 等**既有环境型红** (TDX 可用性/顺序依赖, 与本改动无触及; 单跑及与本改动测试连跑均绿)。
- 上线步骤: 非交易时段部署重启; 设置面板信号日多选周三/四/五 → 重启 → 首个执行 pass 自动迁移 (现有 513100 持仓按手三等分, entry_high 推算 ≈ 近 20 日高)。
- 上线前人工核对券商 ETF 佣金有无最低 5 元/笔 (逐份分单, 有则改并单, 计划书 §六)。
- 本机无 git 可执行文件, commit 由用户侧完成。

---

## 2026-09-11 — 公式农场粗扫报告修复 (5 缺陷) + 达标线统一 15% + 第四闸门

**入口**: [docs/plan/2026-09-11_公式农场粗扫报告修复_计划书.md](docs/plan/2026-09-11_公式农场粗扫报告修复_计划书.md)
**触发**: 用户对当日粗扫报告做端到端检查。结论: **流水线健康, 报告不合格**。

### 审计发现 (证据可复验)

| # | 缺陷 | 证据 |
|---|---|---|
| F1 | **静默漏扫一半**: 本批入库 20 条只粗扫前 10 条, 报告一字未提, 且无补扫机制 (新批次一来剩下 10 条永远轮不到) | 日志「批次 **20** 条」+`[10/20]` 收工; 旧 `farm_backtest.py:61` 默认 `--max-formulas 10` + `:75 items[:10]` |
| F2 | **缺达标判定与淘汰原因** (计划书 §8.5 / §4 步骤 7 明文要求) | 规则所有者自跑: `gs_5m_sweep report GS1285/GS1294/GS1292` → 三例「达标 0」 |
| F3 | **「最优组合」无最小样本门槛** → 3 笔 100% 胜率、盈利因子 ∞ 的 GS1292(卡玛 8.23) 被当最优 | 原始 CSV `trades=3` |
| F4 | **达标线两套口径**: gs 系三份各写 `0.30/0.15/1000`, 而 09-09 批实际在用 **≥15%** | grep `TARGET_ANN`; 09-09 结论报告第 15 行 |
| F5 | **计划书 §4 步骤 6「上架后定量复核」未接入生产闸门** | `core/farm_runner.py` GATES 只有 check/onboard/backtest |

### 关键改动

| 文件 | 改动 |
|---|---|
| `core/farm_rules.py` (新) | 达标口径**唯一真相源**: 年化≥15% 且 回撤≤15% 且 笔数≥20; `verdict()` 四态+人话原因 / `pick_best()` (最优只在笔数≥20 里选) / `is_pass()` (dict/pandas Series 通吃) / `describe()` |
| `tools/formula_farm/farm_backtest.py` | ①目标集改**跨批次补扫**(所有 onboard ok 且无结果, 旧批次优先防饿死; `--max-formulas 0`=全部); ②纯函数 `build_report()`: 全口径+达标线+**声明区间 vs 实测窗口**+本批/本轮/余量, 逐行判定+原因, 未扫清单; ③`no_signals` 短路停牌(不再拿缺失 cache 去跑 run 记成"闸门失败"), 停牌标记 `NO_SIGNALS.txt`; ④飞书结果如实; ⑤子进程诊断留摘要 |
| `tools/formula_farm/farm_verify.py` (新) | 第四闸门: 批量跑 repaint_check + future_func_check, 按**数字**(不一致率/保留率)判通过; 工具跑失败→「复核失败」、输出没匹配上→「未解析」, **两者都不当通过** |
| `core/farm_runner.py` / `farm_api.py` / `web/*` | 第四闸门接线 (GATES + `POST /api/farm/verify` + 前端按钮/状态位; 前端版本号统一 20260911a) |
| `tools/gs_5m_sweep.py` / `gs_make_report.py` / `gs_1d_detail_report.py` | 三处硬编码阈值收口到 farm_rules; `do_report` 改报「达标/样本不足」两栏 |

### 实测结果

- **补扫生效**: 本批 20 条首次全部跑完 (旧版只有 10 条); 顺带把 09-06 批次遗留的 38 条「TDX 无此公式/零信号」停牌, 不再每天白烧 prep。
- **被漏掉的 10 条里有本批最好的候选**: GS1301(强势底分型) 年化 **9.13%** / 卡玛 2.01 / 1792 笔, GS1300 2.61%/1070 笔, GS1304 2.42%/1140 笔 —— 旧报告把它们整批藏了。
- **判定**: 达标 0 · 未达标 14 · 样本不足 6 (GS1292/GS1293 的 3 笔已被正确降级)。
- **修复过程中自己踩的坑 (已补回归测试)**: 收口阈值时删了 `tgt = ...` 赋值却漏改下游 `if len(tgt):` → 每次 report 步 NameError rc=1 (sweep CSV 其实已写好); 新增 `tests/test_gs_5m_sweep_report.py` 直接调 `do_report` 锁死。另: 「本批」一度按"每公式最早入库日"归类, 把 20 条缩成 2 条 → 改为按最新 onboard.json 的 mtime (与旧语义一致)。

### 测试增量

新增 31 例: `tests/test_farm_rules.py` (阈值边界 14.99/15.00、回撤 15.00/15.01、19/20 笔、None/NaN/0 笔、四态标签、选优、describe) · `tests/test_formula_farm_report.py` (抬头口径/声明 vs 实测窗口/覆盖度/四态计数/样本不足降级/纯 Markdown) · `tests/test_farm_verify.py` (重画与未来函数解析、"未通过" vs "复核失败" vs "未解析"、汇总) · `tests/test_gs_5m_sweep_report.py` (do_report 回归锁)。全量: **2265 例 / 1 失败(环境红: AI 设置标准档使旧 provider 告警测试失效) / 0 错误 / 6 跳过**。

### 剩余风险

- 粗扫仍是 300 万/单票 2 万的**轻仓**口径, 账户年化主要在排"出票多不多"; 夏普在轻仓下结构性为负、卡玛在 回撤<0.01% 被死区归零 —— 报告已把这句提醒写进抬头, 但**引擎口径本身没动** (要改需另立计划)。
- 第四闸门需 TDX 在运行且较慢 (repaint_check 每公式 2 次全A选股, 20 条约半小时); 已用 2 条公式冒烟验证全流程 (通过 2 · 未通过 0 · 无法判定 0, 报告落 `reports/2026-09-11_定量复核_公式农场.md`), 全批 20 条的正式复核建议人工点一次。**实测坑**: 两个复核工具连跑时, 第二个偶尔撞上 TDX 连接刚被前一个关掉的窗口 ("连接路径为空, 请先调用 tq.initialize(path)"), 单独重跑就正常 → 已加 `--retry`(默认 1 次, 间隔 20s), 重试仍失败则如实写「复核失败」, 绝不当通过。
- 停牌标记 (`NO_SIGNALS.txt`) 是静态的: 若 TDX 后来补上了该公式, 需删标记或 `--include-done` 重扫。

---

## 2026-09-10 — 停机日资产补算 (9/9 日历缺格事件)

**入口**: [docs/plan/2026-09-10_停机日资产补算_计划书.md](docs/plan/2026-09-10_停机日资产补算_计划书.md)
**用户问题**: "为何 9/9 那格显示无成交、没有盈亏比例? 日历今日卡片说亏 3366, 交易页 513100 却显示 3606, 是不是昨天没开程序?"

### 查到的三件事 (证据链完整可复验)

1. **9/9 那一行根本没写**: `daily_asset` 表缺 8/31、9/1、9/9 三个交易日。前端在没有资产行时把该格显示成"无成交"
   —— 把"没采到数据"当成了"当天没成交" (`web/js/analysis.js:225` 旧逻辑)。
2. **为什么没写**: 9/9 机器反复意外断电重启 (06:55 起→07:28 挂; 07:28 起→**11:19:53 断电**→19:28 才恢复),
   trade_main 只在 06:57/07:29 起来对过账就没了, **15:05 的收盘归档没跑成**; 三层证据一致 (audit 表、
   reconcile_log、raw_reports.jsonl 最后一条停在 9/8 15:30)。
3. **为什么差 240**: 逐日盈亏算法用"本行 − 上一行", 认不出缺口 → 9/10 那格的基准被静默换成 9/8,
   显示 -3,365.60 = 9/9 的 **+240.40** 与 9/10 的 **-3,606.00** 之和; 交易页 -3,606.00 才是 9/10 单日真值。
   校验: `-3,606.00 + 240.40 = -3,365.60` 分毫不差。**两个数都是亏, 不存在一赚一亏。**

### 关键改动

| 文件 | 改动 |
|---|---|
| `trade/asset_gapfill.py` (新) | 停机日补算深模块: 纯函数 `plan_gaps` (缺口发现+成交回放+逐日计价+两端夹逼) / 编排 `fill_gaps` / 取价链 `make_close_source` (QMT 主 + TDX 兜底, 不复权) |
| `trade/store.py` | `daily_asset` 加 `source` 列 ('eod' 实测 / 'derived' 推算) + 幂等迁移; `save()` 返回 False = **实测行永不被推算覆盖** (存储层最后一道保险); `get()` 透出 source |
| `trade/gateway.py` | 新增 `query_daily_closes_range` → `{日期: 不复权收盘价}` (补算要按"哪一天"取价, 原 `query_daily_closes` 只返无日期序列) |
| `trade_main.py` | `_startup_catchup` 在 15:05 补偿归档后自动补一轮 (+ `gapfill` 人工命令, 连续竞价时段守护); 全程 fail-soft |
| `trade/analysis_api.py` | `daily_pnl` 每天带 `source` + `_missing` (没补上的日子, 界面显示"未归档·差¥X") + `_gapfill` (最近一次补算留痕) + `_month.derived_days`; 新增 `POST /gapfill` (入队) 与 `GET /gapfill_last` |
| `web/index.html` + `web/js/analysis.js` | 日历四态 (实测/推算/未归档/待归档), 推算日加"推算"徽标, 月汇总标"含 N 天推算", 推算日/未归档日点开有口径说明, 新增「补算停机日」按钮 (先预览再写) |
| `tools/gapfill_daily_asset.py` (新) | 离线补算入口 (trade_main 没跑也能补): `--apply` 才写入, 默认预览 |

### 实测结果 (真实库已写入)

| 日期 | 结果 | 口径 |
|---|---|---|
| 2026-09-09 | 1,065,038.12 (**+240.40 / +0.02%**) | 推算, 两端夹逼**分毫不差** |
| 2026-09-10 | **-3,606.00** (原误显 -3,365.60) | 实测, 与交易页 513100 `day_chg_amt` 完全一致 |
| 2026-08-31 / 09-01 | 1,063,317.12 / 1,065,699.72 | 推算, 两端差 ¥1,847.20 (0.17% < 0.5% 容差) 已按日均摊并标注 |

**8/31-9/1 那 1,847.20 是什么**: 两天停机期间在券商端手工买过 200 股 518880 (约 9.236 元/股 = 1,847.20),
持仓重启后从 QMT 读回来了, 但成交表里没有这一笔、现金却真实少了 —— 这正是"两端夹逼"存在的意义。
补录这笔成交后重跑补算即可精确到分。

### 测试增量

`tests/trade/test_asset_gapfill.py` 新增 **29 例**: 纯函数 (单日/多日缺口、缺口内成交、残差均摊与余数、
超容差拒写、容差可调、缺收盘价 fail-closed、尾部缺口 skip、无锚点、非交易日、推算行重算、负持仓诊断)
+ 编排 (写 derived / 幂等 / dry-run / 实测行保护 / 存储层拒写 / 未补齐上报 / audit 留痕 / 异常 fail-soft)
+ API 接线 (source 与 `_missing`/`_gapfill`、触发端点) + trade_main 接线 (开机自动补、人工命令、时段守护)
+ 旧库迁移。接线测试当场抓出一个真 bug: 交易日历异常被静默当成"全不是交易日" → 补算一声不响什么都没干
(已改为与 `monitor.is_trading_day_cached` 同款 fail-open 回落周末判定)。

### 剩余风险

- **取价口径**: 补算用不复权收盘价 (与实盘市值同维度), 但**未计交易费用与分红** → 缺口内有成交时会有几元级偏差
  (P2 税费回溯的已知口径, 与 trade/ 模块一致)。
- **停机期间的手工成交若没进成交表, 推算值会带上那笔现金/持仓误差** (8/31-9/1 实证 0.17%); 已按容差写入并标注,
  补录成交后重跑即可精确 —— 不自动改账 (守住"对账只告警不改账"铁律)。
- 尾部缺口 (最后一行到今天之间) 暂不补: 今天的实测由 15:05 EOD 负责, 没有右端尺子不猜。
- **未重启 trade_main 前**: 旧进程不返回 `source`, 界面上推算日不会显"推算"徽标 (数字已正确);
  重启后自动补算 + 徽标 + 补算按钮全部生效 (注意先等 QMT 就绪, 冷启动 -1 坑)。

---

## 2026-07-15 — 迭代 1/2/3/4：基线 → 9.0/10

**审计入口**: [2026-07-15_全项目质量检查审计报告_前置.md](docs/audit/2026-07-15_全项目质量检查审计报告_前置.md)
**审计员立场**: 严苛挑刺、不讲好话、逐项验证 → 推翻报告 C1 假阳性 + 修订 C2 失真 + 补回漏报真 CRITICAL。

### 关键改动一览

| 迭代 | 主题 | 关键改动 | 影响 |
|---|---|---|---|
| **1** | 实盘偏差验证 | 新建 `backtest/_entry_basis.py` (EntryPath/LiveBiasEstimate/assert_single_path) + entry.py 显式声明 BACKTEST_T_CLOSE 路径 + 业务铁律 2+3 守卫测试 15 个 | 把"两套口径别混"从 CLAUDE.md 口头规范升级为代码事实 |
| **2** | 审计纪律 | 新建 `docs/audit/_verify_references.py` (extract/verify/audit CLI) + CLAUDE.md 写入"审计铁律" + 15 个测试含 C1 反向防回归 `test_metrics_67_actual_code_has_as_e` | 把"审计员错引代码"事件永久防回归 |
| **3** | 测试密度 | 新建 7 个测试文件 + 79 测试 (`test_exit_strategies_full` 29 / `test_safe_serialize` 22 / `test_backtest_state` 27 / `test_metrics_full` 35 / `test_exit_dispatcher_full` 16 / `test_ladder_tp_pure` 18 / 加 `test_entry_basis` 15) | **322 → 502 测试 (+56%)** |
| **4** | 技术债 | `.gitignore` 增 `.coverage` / `htmlcov/` / `.pytest_cache/` 防止覆盖率文件误入 git | 防"覆盖率文件污染 git 历史"复发 |

### 文件改动统计

| 类型 | 数量 |
|---|---|
| 新增源文件 | 3 (`_entry_basis.py`, `_constants.py`, `_verify_references.py`) |
| 新增测试文件 | 7 |
| 修改源文件 | 8 (`metrics.py`, `stop_config.py`, `result_writer.py`, `pipeline.py`, `benchmark.py`, `engine.py`, `loop/entry.py`, `server.py`) |
| 修改配置/文档 | 2 (`CLAUDE.md`, `.gitignore`) |
| **总计** | **20 个文件** |

### 测试基线演进

| 日期 | 测试数 | 增量 | 备注 |
|---|---|---|---|
| 2026-07-13 | ~270 | — | 候选 A 阶段 1 后基线 |
| 2026-07-14 | 302 | +32 | 候选 A 阶段 2 (loop refactor) |
| 2026-07-15 (修复后) | 322 | +20 | 本次审计建议的小改 |
| 2026-07-15 (迭代 3 末) | **502** | **+180** | 突破 500 测试大关 |

### 分数演进

| 阶段 | 综合分 | 关键变化 |
|---|---|---|
| 2026-07-13 | 7.5/10 | run_cached 加厚前门 + 锁私有 + 复权口径统一 |
| 2026-07-14 | 7.5/10 | loop refactor + ENGINE_VERSION v3.4 |
| 2026-07-15 (审计前) | 5.5/10 (本次 diff 评级) | 报告自身错引代码 |
| 2026-07-15 (修复后) | 8.3/10 (本次 diff) | 10 项 P0/P1/P2/P3 修复 |
| **2026-07-15 (迭代 4 末)** | **9.0/10 (系统综合)** | 实盘偏差 + 审计纪律 + 测试密度 + gitignore |

### 剩余风险 / 已知债

1. **sim_trader 实盘路径未实现** — CLAUDE.md 自承"实盘走 sim_trader T+1 开盘路径"但代码层不存在。
   - **状态**: 已建 `EntryPath.LIVE_T_PLUS_1_OPEN` 枚举 + 路径冲突守卫 + 偏差估算工具
   - **下一步**: 等用户需要实盘时再实现 sim_trader 引擎

2. **`_simulate_core_v3_legacy` 527 行甲骨文** — 实存 parity oracle,3 个测试 + 1 perf 工具使用,**不删**
   - **删除时机**: 见 [loop.md §7.7](docs/architecture/loop.md) 的"发版 → 观察 1-2 周 → 转快照 parity → 删"流程

3. **engine.py 1190 行 / server.py 452 行** — 超过 800 行红线
   - **状态**: 已知,未拆分 (本次范围外)
   - **下一步**: 候选 E 阶段可考虑

4. **coverage 文件** — 已 gitignore,但若用户之前误 git add 过,需手动 `git rm --cached .coverage` 清理

### 审计纪律反向防回归

`test_metrics_67_actual_code_has_as_e` 是 C1 假阳性事件的永久反向防回归:

```python
def test_metrics_67_actual_code_has_as_e():
    """C1 假阳性防回归: metrics.py:67 必须有 'as e' (报告错引 = 审计失败)."""
    ref = Reference(file="backtest/metrics.py", start=67, end=68, raw="[backtest/metrics.py:67-68]")
    results = verify_references([ref], PROJECT_ROOT)
    assert results[0].is_valid
    assert "as e" in (results[0].content_snippet or "")
```

未来任何审计 agent 错引这段代码,CI 立刻失败。

### 移动止损止盈默认口径收口

- `config/default.yaml` 的权威默认保持为：盈利 3.5% 激活、峰值回撤 1% 退出。
- 引擎 `run()` / `run_cached()`、Web 请求模型、配置摘要和页面首次访问值全部对齐该口径。
- 用户在策略 YAML、`config/current.yaml` 或浏览器 localStorage 中明确保存的 8%/5% 及其他值保持原样，不自动迁移。
- 老用户如需采用新默认值，应在页面点击"恢复默认配置"；系统不会猜测 8%/5% 是旧默认还是用户主动选择。
- 依赖旧 Web/引擎缺字段兜底生成的历史回测，与修复后的默认回测不可直接比较；显式配置的历史回测不受影响。

---

## 2026-07-21 — 回测区间精确化 + 5m 缺失降级日线 + 期末未平仓市值计价

**触发**: 用户复核「黑马选股1」2024-01-01~2025-01-01 5m 回测: 权益曲线显示 2024-06-27~2025-04-25
(起点被 TDX 5m 深度截断、终点含 +75 交易日窗口尾巴)、基准曲线 2024-12-31 后缺失。
**计划书**: [2026-07-18_5m数据层降级与降级影响报告_计划书.md](docs/plan/2026-07-18_5m数据层降级与降级影响报告_计划书.md) 实施状态追加条目。

### 用户三条语义决策 → 落地

| 决策 | 落地 | 关键文件 |
|---|---|---|
| 回测哪个区间图表就显示哪个区间, 不延长 | `compute_window_bounds`/`get_kline_windowed` 新增 `end_time` 截断, engine 透传 | `core/data_fetcher.py`, `backtest/engine.py` |
| 没有 5M 线的时段降级为日线 (默认开) | 降级网格起止 = 请求区间 (不再从 5m 首 bar 起); `default.yaml` + `server.py` 默认开启 | `backtest/engine.py`, `config/default.yaml`, `server.py` |
| 期末未平仓按市值统计, 不做退市强平 | loop 新增 `final_positions` 快照 → `open_positions` 明细全链路输出 | `backtest/loop/loop.py`, `backtest/engine.py`, `backtest/result.py`, `pipeline/result_writer.py`, `web/index.html`, `web/vera-ui.js` |
| (附) 基准曲线缺失修复 | `step3_benchmark` 拉取区间 = equity 实际首尾 (非请求区间); 基准在日粒度回退 | `pipeline/pipeline.py`, `backtest/benchmark.py` |

### 测试

新增 `tests/test_window_end_clip.py` (7) + `tests/test_open_positions.py` (3) + `test_degrade_5m` (+1)
+ `test_benchmark` (+2 含 step3 区间修复回归) + `test_result_writer` (+1);
`test_engine_5m_window`/`test_matrix_cache` mock 签名补 `end_time`。全量套件零回归。

### 行为变更提示 (语义修正, 非回归)

- 5m 回测执行窗口从此 = 请求区间: 期末仍持仓的不再获得窗口尾巴自然平仓, 改按市值计入权益
  (trades 不再出现期末强平记录, win_rate 等按已平仓交易统计)。
- `degrade_5m` 默认开 (config 置 false 回退丢信号旧行为); 开启时 `matrix_cache` 对 5m 自动跳过。
- 降级占比在长区间会显著变大 (如半年无 5m 数据), degradation 报告数字变大属预期。
- run_cached/批量优化路径不支持降级, 也不导出 open_positions (与既有 LOW-3 限制一致)。

### 质量审计 (2026-07-22, [报告](docs/audit/2026-07-22_区间精确化迭代_质量审计报告.md))

- **F1 HIGH 已修**: 请求终点晚于数据末端时, 降级网格尾部全 NaN 日会把期末持仓误判退市强平
  (reason=11) → `_apply_5m_degradation` 网格裁到最后有数据的交易日 + 探针测试。
- **F2 LOW 已修**: end_time 早于信号日 (异常输入) 窗口倒置 → `compute_window_bounds` 钳制
  win_end ≥ win_start + 测试。
- MEDIUM 记录: 长区间网格内存 (计划书既有标注); 5m/1d 复权口径漂移 — 已专项定位:
  TDX K线前复权只应用请求窗口内除权事件 + TDX 5m 数据滞后 (只到 07-17, 其后的
  除权进不了 5m 复权) + 缓存逐股锚定不一, 全量扫描 5517 只中 231 只漂移 (4.2%,
  清单 output/f4_adjust_drift_scan.csv, 工具 tools/scan_adjust_drift.py);
  修复路径 = 终端同步 5m 数据到最新 → --probe 验证 → 删漂移股 parquet 重建 → 复扫。

---

## 2026-09-05 — 研究大脑 DSH 深度思考通道

**决策入口**: [docs/adr/0002-dsh-deep-thinking-channel.md](docs/adr/0002-dsh-deep-thinking-channel.md)
**计划书**: [docs/plan/2026-09-04_研究大脑DSH深度思考通道_计划书.md](docs/plan/2026-09-04_研究大脑DSH深度思考通道_计划书.md)

### 关键改动一览

| 主题 | 关键改动 | 影响 |
|---|---|---|
| DSH 深度思考通道 | 新建 `brain/dsh_channel.py` (run_dsh/stop_dsh/scan_leak) + 便携运行时 `dsh-runtime/` (不入 git) | 研究 TAB 勾选「🧠 深度思考」走独立 DSH 子进程 (deepseek-v4-flash, 全工具面) |
| 路由 + 停止 | `research_api.py` stream 端点 deep 分流 + `/api/research/chat/stop` | 未勾选路径零改动; 跑飞了可手动杀进程树 |
| 检测型控制 | 每问全量留档 `data/brain_dsh_runs.db` (会话日志 SHA-256 + 泄漏关键词扫描), 留档失败=显性失败 | 出网上下文全部记账, 敏感信息出网即告警 |
| 对话沉淀 | run_dsh 传 channel 时调 archive_exchange | DSH 回答同样进 vault, 不破 2026-07-28 铁律 |
| 防答非所问 | `dsh-runtime/workspace/CLAUDE.md` 岗前手册 + 任务指令前缀 (IRX 会话实测教训: 出厂程序员人设遇裸问题会聊环境) | 深度思考直接答题, 结论先行, 大白话 |
| 防挂死 | 子进程输出走日志文件不用 PIPE (IRX 实测 64KB 管道缓冲挂死) + 直调 node 绕批处理截断 + server.py reload 排除 dsh-runtime | 长答案不挂死; reload 模式不再 wedge |
| 测试 | +16 (tests/brain/test_dsh_channel.py 13 + tests/test_research_chat_api.py 3) | 全离线, 不碰真 DSH/真 DB |

### 剩余风险 / 已知债

- `dsh-runtime/` 含 DeepSeek API 凭据 (home/.credentials.yaml), 绝不外发/上传; .gitignore 整目录兜底。
- 多轮记忆靠打包最近 5 轮对话进任务文本 (headless 无会话续接), 超长上下文有 CreateProcess 32767 字符天花板。
- server.py / web/index.html / web/js/brain_chat.js 与前序在途改动 (BrainViz/热加载/proactor 循环) 同文件交织, 待用户确认后一并入库。

---

## 2026-09-05 — 深模块浅模块治理 III · Wave 1 止血包

**计划书**: [docs/plan/2026-09-05_深模块浅模块治理III_增补计划书.md](docs/plan/2026-09-05_深模块浅模块治理III_增补计划书.md)
**背景**: 2026-09-05 逐项核实治理 II 13 项中 12 项未动; 同期产生牛熊口径分叉 (250 vs 60) 新债。四波重排 (止血/实盘正确性/接口债/结构), 本条为 Wave 1。

### 关键改动一览

| 主题 | 关键改动 | 影响 |
|---|---|---|
| W1-a 清理闸 | 删根目录 17 个 .log + _tmp 垃圾; 寻优 json×3/预览 txt/一次性脚本移 `scratch/` (gitignored); PS1 助手 (kill/tun helper) 归 `tools/`; `.gitignore` 增 `/_tmp_*`+`scratch/` | 根目录恢复可导航; 防堆积机制建立 |
| W1-b 牛熊口径收口 | 新建 `core/index_regime.py` 单一真相源 (MA250/门槛200/斜率20 用户拍板) + 7 测试; `brain/data_tools.market_health` 内联数学换真调用 (含 09-04 体检表本体入库); `research/index_regime.py` 改委托 (年头不足段标 range) | 消灭"同一件事两块手表"口径分叉; 门槛常量有测试锚定, 不许悄悄漂移 |
| W1-c 常量/舍入收口 | `DIRECTION_BUY` 两处副本 (notifier/llm_review) 改 import book 唯一真相源 ("防环"注释理由经核实不成立); `round_price` 迁 `trade/book.py` (executor/auto_buy 经模块属性无感切换) | 全库方向常量与股票价格档位各只剩一份 |

### 测试

- 新增 `tests/test_index_regime.py` 7 个 (先红后绿); 回归 tests/brain/ 144 绿 + tests/trade/ 相关 109 绿。
- 验收 grep: `DIRECTION_BUY = 23` 全库只剩 book.py; `def round_price` 只剩 book.py (ETF 版随 Wave 2 rotation 动刀迁入)。

### 剩余风险 / 已知债

- Wave 1 未覆盖: data_tools 杂物间分家 / api.py 拆分 / api.js 前端收口 等结构任务, 按治理 III 计划书 Wave 4 条件触发。
- (2026-09-05 同日补记) Wave 2 实盘正确性三件套已执行完毕, 见下条; 在途改动已分 9 组提交, 工作区恢复干净。

---

## 2026-09-05 — 深模块浅模块治理 III · Wave 2 实盘正确性

**计划书**: [docs/plan/2026-09-05_深模块浅模块治理III_增补计划书.md](docs/plan/2026-09-05_深模块浅模块治理III_增补计划书.md)（引用 08-28 治理 II P0 原卡）

### 关键改动一览

| 主题 | 关键改动 | 影响 |
|---|---|---|
| W2-④ rotation 三态迁出 | deprecated 三态整体搬 `trade/legacy_three_state.py` (零依赖纯函数); rotation.py 811→约 720 行只留动量规则; shadow.py 惰性 import 改顶层 import **破 rotation↔shadow 循环**; research×10 + tools/shadow_compare + 测试改指新位置 | 生产文件不再误导; 规则源放对地方 |
| W2-① 资金口径下沉 | 新建 `trade/pool_money.py` 纯函数层 (pool_split/position_value/stock_pool_value/stock_budget_cap/in_flight_sell_returns) + 13 单测 (含 8-21 现场 orders-领先场景); trade_main 三件套删除改委托 (1314→1279 行); rotation._etf_value 委托消灭 D1 市值重复; **热更改统一 apply 契约** (monitor/executor/timer/risk 各加 apply(), _apply_config 不再直改 4 模块私有字段) | 三道保命闸读同一份口径; 8-21 型 bug 进测试网当场抓住 |
| W2-⑤ 卖单登记收口 | rotation 直连卖单改走 executor 共享槽 (register_external_sell / clear_external_sells, 不进 _pending 防追价双卖); executor.in_flight_sells 单源含两路; trade_main 组合根不再手拼 executor+rotation | 对账隐性契约消除, reconciler 读单一真相源 |

### 测试

- 全量 `pytest tests/` 绿 (退出码 0); tests/trade/ 覆盖 rotation/executor/reconciler/api/auto_buy/monitor/risk 热更契约回归。
- 验收 grep: trade_main 无 `def _stock_budget/_stock_pool_value/_in_flight_sell_returns`; rotation 无 `_pending_sells` 代码引用与 deprecated 定义; rotation.in_flight_sells 已删、组合根单源读。

### 剩余风险 / 已知债

- `round_price_etf` (ETF 0.001 档) 仍留在 rotation.py:102 —— W1-c 原计划随 W2 rotation 动刀迁入 book.py, 因动刀时点该文件改动面已大, 迁入顺延 Wave 3 (低风险纯搬家)。
- rotation 外部卖单登记在"卖出已成交→次日"窗口仍保留在 executor._external_sells (与旧 _pending_sells 语义一致), 由每次调仓起手 clear —— 行为不变, 不引入新债。
- Wave 3/4 未动: data_fetcher 双胞胎 / BatchResult / get_or / schema 上移 / api 拆分 / 前端收口 (计划书 §五)。

---

## 2026-09-05 — 深模块浅模块治理 III · Wave 3 接口债

**计划书**: [docs/plan/2026-09-05_深模块浅模块治理III_增补计划书.md](docs/plan/2026-09-05_深模块浅模块治理III_增补计划书.md)

### 关键改动一览

| 主题 | 关键改动 | 影响 |
|---|---|---|
| W3-rpe (审计顺延清零) | `round_price_etf` (ETF 0.001 档) 迁 `trade/book.py`, 与股票 0.01 档同居; rotation import + 删本地 def; 测试改指 book | 价格档位口径 100% 单点 |
| W3-③ data_fetcher 反义双胞胎 | raw 版 `get_trading_dates` 更名 `get_calendar_days` (点名"字符串日历", 行为零变); robust 版 `get_trading_days` 定为回测窗口数学唯一入口; 8 工具/缓存/测试调用方改指, 双方法 docstring 交叉讲分工。08-16 "不合并" 拦阻已随 09-04 server 切精确历解除 | 09-04 交易日历事故类陷阱的命名根除 |
| W3-BatchResult | 新建 `SelectionBatchResult` (df + batch_errors + failed_all, DataFrame 透明委托老调用方零改动); 删 `FormulaRunner.last_batch_errors` 类属性 (并发选股写-读竞态); L2 缓存改读返回值区分真空/失败空 | "失败空不缓存"正确性随结果走, 不会漏判 |
| W3-get_or | DataCache 增 sector_list/sector_stocks/name_map 的 `*_or` 单方法 (判过期→回源→回填, TTL 语义一处); DataFetcher 三处手动 has/get/set 折叠 (失败不缓存防毒化); stock_filter `_INFO_CACHE` 补 24h TTL (常量 import data_cache 同源, auto_iter 预填兼容) | 样板消灭; ST 判定不再进程内永不过期 |
| W3-schema | KlineCache 增 `cached_last_date`/`manifest_stats` 只读方法; kline_cache_maintenance 不再裸开 manifest.db (schema/列名知识单点, db 不存在仍返 None 无副作用) | schema 双写点合一 |

### 测试

- 全量 `pytest tests/` 绿 (退出码 0); 定向覆盖 data_cache/kline_cache(+maintenance)/signal_day_cache/sector/concept/filter_limit_up。
- 验收 grep: `DataFetcher.get_trading_dates` 0 引用; `last_batch_errors` 仅 docstring 提及; maintenance 无 `sqlite3.connect`。

### 剩余风险 / 已知债

- `compute_window_bounds` 薄委托保留 (core/window + calendar_fetcher 注入契约), 未做"折叠"——因两个生产调用方依赖它喂入日历注入, 折叠会把该知识散回调用方; 本次以消灭事故源 (双胞胎) 为实, 不为了接口计数而移动真接缝。
- stock_filter 缓存收编为"TTL 同源"而非并入 DataCache 实例 (auto_iter 直接写 `_INFO_CACHE` 的预填 hack 兼容考虑), 记录为可选后续。
- Wave 4 未动: data_tools 杂物间分家 / api.py 拆分 / api.js 前端收口 / P2-3/4/5 (计划书 §五)。

---

## 2026-09-05 — 深模块浅模块治理 III · Wave 4 结构

**计划书**: [docs/plan/2026-09-05_深模块浅模块治理III_增补计划书.md](docs/plan/2026-09-05_深模块浅模块治理III_增补计划书.md)

### 关键改动一览

| 主题 | 关键改动 | 影响 |
|---|---|---|
| W4-a regime 文档修正 | 声明"回测与实盘共用"不实 (生产唯一调用方 auto_buy); 回测对照走独立研究脚本如实登记 | 双实现假设解除 |
| W4-b executor FillContext | fill_context 三件套 (register/peek/discard) 收 `FillContext` 小对象 (executor.fill_ctx), 公开方法预算释放; rotation/auto_buy/trade_main/测试改指, 语义零变 | Executor 公开面收窄 |
| W4-c data_tools 分家 | 市场面 (zt_pool/market_health/market_snapshot) 原样搬 brain/market_panel.py (789→628 行); 私有助手复制标注来源; data_tools 兼容 re-export 旧引用零改动 | 找"市场体检"不再翻 789 行杂物间 |
| W4-d api.py 分析域拆分 | 5 个 /api/trade/analysis/* 端点抽 `trade/analysis_api.py` (APIRouter), api.py 729→424 行只留交易/持仓/配置; 路径不变, include_router 注入 | 改图不再碰下单代码 |
| W4-e api.js 补 client | 数据准备/分析 (calendar/benchmark/kline) + 研究对话 (chat/reset/stop) 域 client 增量加入 | 收敛起点 (页面迁移见下) |

### 已知债 / 诚实边界

- **api.js 页面 fetch 收敛（2026-09-05 二次推进后）**：client 能力已齐全（benchmark 带参/chat body/data_cache 三端点/put 助手/跨源 8081 `createTradeClient` 工厂）；**module 页 analysis.js 三处同源 fetch 已切统一 client**（calendar×2 + benchmark，SVR_BASE 死常量删除，node --check 绿）。仍留待 **UI 人工回归** 的一组：classic 页（data_cache.js 3 点 / brain_chat.js 普通 POST 4 点——需经 vera-ui 全局桥）、跨源 8081 大组（trade.js 助手层语义已自洽、analysis.js 8081 端点、mobile.html）、研究 SSE 流式（brain_chat:221）、charts_replay 422/502 状态分支——这些在无浏览器环境盲改违背项目 web 区审计纪律，保持不动并记录。
- **TradeStore 续拆（2026-09-05 完成）**：`_RawLogWriter` 端出独立 `trade/raw_log.py`（re-export 兼容）+ tier_state 表域拆 `TierStateStore` 子 store（写方 executor/读方 trade_main 改指 `store.tier_state`）→ store.py 769→678 行。orders/trades/audit"承重墙"按侦察结论不动（不为行数把 769 行风险换成 600 行风险）。
- tdx_tq 悬空: 维持"预留"标注, 待市场面下一需求消费。

### 测试

- 全量 `pytest tests/` 绿 (退出码 0); W4 定向覆盖 test_regime/test_executor/test_rotation/test_api/test_analysis/brain 全部绿; 路由冒烟确认 5 个 analysis 端点注册。

---

## 2026-09-05 — 舆情/公告采集链路 DNS 故障加固（11001 事件修复）

**背景**: 2026-09-02/03 五只票"公告源+互动易源"齐报 `[Errno 11001] getaddrinfo failed` —— 环境性 DNS 对巨潮 cninfo.com.cn 主域瞬时解析失败（Windows WSAHOST_NOT_FOUND），与个股无关。原链路无 DNS 预检、无重试、失败即标【缺】且报错不落盘。

### 关键改动一览

- **brain/data_tools.py** 新增 `_fetch_resilient(ak_call, hosts, timeout, what)`：① `socket.gethostbyname` 预检（DNS 挂则秒败，不等 akshare 内部无超时请求干等）；② 解析/调用失败重试 3 次（间隔递增 0.5/1.5/2.5s）；③ 全败抛 RuntimeError（含 host 与末次原因），调用方渲染可诊断的【缺】文案；④ 落一行 `data/brain_model_cache/_collect_errors.log`（此前采集错误只进控制台窗口，关窗即丢）。
- **接入三处**：公告源 `www.cninfo.com.cn` / 互动易源 `irm.cninfo.com.cn`（均 timeout=40）/ 快讯新浪主源 + 同花顺兜底（timeout=20）。
- **tests/brain/test_data_tools_resilience.py**（新增 4 测）：DNS 一次成功 / gaierror 前两次失败第三次自愈 / 全败抛 RuntimeError 含 host 并落盘 / 调用异常重试后成功。
- 实现期顺手修复：`time` 缺失导入（重试路径原会 NameError）+ 落盘块缩进损坏 + 未用 `Path` 导入。

### 测试

- `pytest tests/brain/` 全绿（含新增 4 测），提交 2be1f5f。

### 剩余风险 / 已知债

- **M18 告警归属未决**：`fetch_documents.py` 09-04 02:01 退出码 1 的日志与仓库内任何任务/脚本不对应，主机与命名来源待向用户核实后再排查，不臆断。

---

## 2026-09-05 — 实盘稳健性体检 + 调度 P0 修复（日志落盘 / weekly 语义）

**体检报告**: [docs/audit/2026-09-05_实盘稳健性_体检报告.md](docs/audit/2026-09-05_实盘稳健性_体检报告.md)（只读，三轴：告警现状 / 调度与采集 / 对账链路）
**体检结论**: 交易主链路健康（对账每日"三方一致"、无 CRITICAL、急停未触发）；调度层两个大窟窿已按 P0 修复如下。

### P0-1：调度进程日志落盘

- **背景**: `python -m scheduler` 独立进程日志只进控制台窗口，关窗即丢 → 8/29~9/1 静默窗口（含 9/1 整日漏跑、9 月月度笔记永久错过）根因无从复查，11001 事件也同源同类。
- **改动**: `utils/logger.py` 新增幂等 `attach_file_logger()`（给 root 挂 RotatingFileHandler，各子 logger 沿 propagate 落盘，同文件不双写）；`scheduler/__main__.py` 启动即挂 `output/logs/scheduler.log`。
- **测试**: `tests/test_logger_attach.py`（落盘 / 幂等 / 多子 logger 不重复）。

### P0-2：weekly job 语义（修"周日 job 结构性死锁"）

- **背景**: `weekly_evolution`/`sgpjbg_weekly` 原注册为 daily + 函数内判周日 —— daily 强制交易日而周日休市，周日分支**代码上不可达**，周报从未自动跑成。
- **改动**: `vera_scheduler` 新增 `add_weekly()`（按星期几触发、不看交易日，ISO 周防重，状态持久化同 daily/monthly）；`__main__` 两个周度 job 改 weekly（周日 18:00/18:30），删函数内冗余 weekday 守卫。
- **测试**: `tests/test_scheduler.py` 新增 TestWeeklyJob（周日触发/其他日不触发/同周防重跨周再触发/参数校验）。

### 测试

- `tests/test_scheduler.py` + `tests/test_logger_attach.py` + `tests/brain/test_data_tools_resilience.py` 全绿；`import scheduler.__main__` 冒烟 OK。提交 f5ea331 / 5a117af。

### 剩余风险 / 已知债（见体检报告）

- P1：K 线补拉 7250 条 ERROR 根因（异常吞成"未知错误"）+ refresh.lock 超时清理。
- P1：结果文件替换失败兜底（8/27 GS 批跑中断同类）。
- P2：monitor_no_quote 高频审计降噪；月度笔记补发语义。
- 未决：M18 `fetch_documents.py` 退出码 1 告警归属（仓库内无对应任务，来源待用户确认）。
- 待观察：sgpjbg_fetch/sentiment_tick 需在下一交易日确认当前调度进程已重启到新代码。

---

## 2026-09-05 — 体检 P1 修复（K线回填失败治理 / 错误留痕 / 锁归属）

**体检报告**: [docs/audit/2026-09-05_实盘稳健性_体检报告.md](docs/audit/2026-09-05_实盘稳健性_体检报告.md)
**背景**: refresh.log 近两周 7250 条 "获取K线数据失败: 未知错误" + 8/26 一轮 5303 条源站故障硬刷 + refresh.lock 归属不可见。

### 根因（取证）

- TDX 对次新股/停牌/无该区间请求返回 `ErrorId≠0` 但常**不带错误文本** → 一律记"未知错误"（明细被吞）。
- 回填工具判定 bug：**有旧缓存的公司延伸拉取无进展时，`manifest` 存在即算成功** → 永不进 no_data，每轮调度刷新重复打同一批失败。
- 源站整体故障（8/26 03:20 全池 5303 次/37 分钟）无熔断，硬刷到底。

### 改动

- **utils/kline_backfill_policy.py（新增，纯函数可单测）** + **tools/backfill_kline_cache.py**：停滞（延伸无进展）记 `stalled{code: ts, err}`，72h 冷却内跳过、到期自动再试；无记录却拉空仍走 no_data；最近 80 次判定失败占比 ≥60% → 熔断中止并**回滚本轮新增停滞**（不把源站故障误记成个股停滞）；补拉子进程每段进度心跳续命 refresh.lock mtime（长任务不会被 TTL 误收尸 → 双补拉）。
- **core/data_fetcher.py**：错误渲染 `_fmt_tdx_error` —— 带出 ErrorId + Error/ErrorMsg/Message 文本；确无文本时列出返回键名；日志附带 codes/period/窗口。
- **core/kline_cache_maintenance.py**：refresh.lock 改 JSON `{pid, trigger, ts}`；跳过原因带持有者描述（谁在拉、何时开始）；`_lock_held_by()`/`_lock_owner()` 归属查询，旧版纯文本锁读不出返回 None 不炸。
- **已核实无需改动**：8/27 GS 批跑 `tmp.replace` 崩溃与 L2 保存 WinError5 噪声 —— 源码事故后已改用 `pcu.atomic_replace`（退避重试），当前代码即修复态。

### 测试

- 新增 tests/test_kline_backfill_policy.py（15 例：有进展判定/冷却/熔断）+ test_kline_cache_maintenance.py TestLockOwnership（4 例）+ data_fetcher fmt 3 例；全量 `pytest tests/` 绿。提交 e5bf66b / e2bceff / f6fb8ec。

### 剩余风险 / 已知债

- P2：monitor_no_quote 高频审计降噪；月度笔记补发语义。
- 未决：M18 `fetch_documents.py` 告警归属（仓库外来源，待用户确认）。

---

## 2026-09-05 — 体检 P2 修复（监控审计降噪 / 月度笔记补发）

**体检报告**: [docs/audit/2026-09-05_实盘稳健性_体检报告.md](docs/audit/2026-09-05_实盘稳健性_体检报告.md)
**背景**: monitor_no_quote 14 天 7070 条刷库淹没真告警；9 月月度笔记因 09-01 断档错过触发日即永久丢失。

### 改动

- **trade/monitor.py（P2-1 降噪）**: `monitor_no_quote` / `monitor_stale_quote` 改走 `_write_throttled` —— 同 code 同类 15 分钟最多落一条，首现立即写；持续无价/陈旧是**稳态**不是新事件，恢复/触发等真状态翻转不经过此口，信号不丢。预计从约 505 条/日降到单票稳态至多 ~16 条/日。
- **scheduler/vera_scheduler.py + __main__.py（P2-2 补发）**: `add_monthly` 新增 `catchup_days` 参数 —— 触发日错过（机器/调度断档）后，触发日起 N 天内本周期仍未触发则补发一次；超窗/已发不补。`monthly_note` 注册 `catchup_days=7`（9/1 断档丢失教训的直接对策）。默认 0 = 旧行为不变。

### 测试

- test_monitor.py 新增节流用例（首现写/窗口内跳/过窗再记）；test_scheduler.py TestMonthlyJob 新增 4 例（宽限内补发、窗前不提前/超窗不补、0 不补、参数校验）；全量 `pytest tests/` 绿。提交 77bf143 / 2a91770。

### 生效提醒 / 剩余

- 生效：P2-1 需**重启交易进程**（trade_main，8081 窗口）；P2-2 需**重启调度进程**（start_vera.bat 一键全启即可）。
- 剩余：M18 `fetch_documents.py` 告警归属仍待用户确认（仓库外来源）；至此体检 P0/P1/P2 建议已全部落地。

---

## 2026-09-06 — AI 设置独立页签（对话大脑三档接入配置）

**审计**: [docs/audit/2026-09-06_AI设置页签_审计报告.md](docs/audit/2026-09-06_AI设置页签_审计报告.md)（深模块/浅模块审计，发现 deep 档合并 Bug + 测试假断言，已修）
**背景**: 换模型要改 .env / ~/.claude / DSH settings 三处，用户要求界面化；三档各按各的协议（OpenAI 兼容 / Anthropic 兼容 / DSH 适配器），存 `config/ai.json`（不入 git，Key 打码回传）保存即热生效。

### 改动一览

| 模块 | 改动 | 影响 |
|---|---|---|
| `llm/ai_config.py`(新) | config/ai.json 读写 + Key 打码 + `merge_patch` 合并语义唯一实现（`_FIELDS` 三档共用字段形状，`__clear__` 整档清空） | 消灭"三份手写合并漂移"；深模块收口 |
| `llm/providers.py` | LLMClient 每次 chat 现读 fast 段（显式 > ai_config > .env > 默认） | 快速档/政策提取/交易复盘换 Key 即热生效；无配置零行为变化 |
| `ai_api.py`(新) | /api/ai/config(打码读) / save(合并) / test(连通)；合并语义只调 merge_patch 一行 | 路由回归薄层 165→138 行 |
| `brain/claude_cli.py` | standard 档 spawn 注入 ANTHROPIC_* env（不碰 ~/.claude 原文件）；配置后不弹 provider 软告警 | 标准大脑可界面换 Anthropic 兼容端点 |
| `brain/dsh_channel.py` | deep 档 spawn 前改写 dsh-runtime settings 的 agent-default-model（幂等，失败按现状运行） | 深度思考可界面换适配器/模型 |
| web 前端 | 新增第 9 页签「AI 设置」+ ai_settings.js（三档表单/保存/测试连接/清空本档，纯函数可 node 测） | 界面配置，留空=保留旧值 |

### 审计发现的坑（防再犯）

- **deep 档合并语义曾用 `is not None`**（fast/standard 用 truthy）：空串会把已配置清空——合并逻辑手写三份必然漂移，收口 merge_patch 后同因杜绝。
- 测试曾留 `assert ... or True` 恒真断言——假绿灯；已删改真检查。
- `__clear__` 仅严格 `is True` 触发，防 `"false"` 字符串误清。

### 测试基线

新增 tests/test_ai_config.py + test_ai_api.py + test_ai_brain_wiring.py + tests/web/test_ai_settings.js（红→绿）；全量 `pytest tests/` exit 0。

---

## 2026-09-05 — 唯一下单口收口（Executor.place_order，深模块评估候选①高危项）

**计划书**: [docs/plan/2026-09-05_唯一下单口收口_计划书.md](docs/plan/2026-09-05_唯一下单口收口_计划书.md)（两轮审读: 作者逐行 + 独立对抗审计, 应修 P1-P5 全部吸收）
**背景**: 下单七步曲（风控→取号→发单→登记成交原因→订单簿→落库→审计）在 5 处克隆且已分叉；泰山石油式修复（created_ts）被迫抄 4 份。

### 改动（T1→T6 六提交，先红后绿）

- **T2 `trade/executor.py`**: 新增 `PlaceRequest` 数据类 + `Executor.place_order` 唯一下单口（七步脊柱）；风控价口径 `risk_price`（审计 P1：auto_buy 风控吃参考价、发单/入账用委托价）；审计 extra 的 `order_id` 占位回填（审计 P2：键序与收口前逐字节一致）；`created_ts` 单点收口；风控拒**静默**返回（防双倍告警）。place_ladder/_sell 内部克隆切换；`next_remark` 转私有 `_next_remark`（公开面 8+1−1=8 不破顶）。
- **T3 `trade/rotation.py`**: `_place_order` 走 place_order（rotation 标记入 intent_flags），卖单登记尾巴留本模块；清 3 个死 import；补 in_flight 直测（审计 P8）。
- **T4 `trade/auto_buy.py`**: 走 place_order（`price=order_price` 委托价 P0-6 口径收编 + `risk_price=price` 参考价），定价策略不动；清 2 个死 import。
- **T5 `trade_main.py`**: 人工买走 place_order（`manual` 标记 + `account_immediately=False` 不入账等事件链——行为保持）；audit extra 的 price 保原始值（审计 P11）；清 2 个死 import。
- **T6 `trade/monitor.py` + `trade_main.py`**: `Monitor.clear_trigger` 公开，组合根 :417 的 lambda 摸私有接线转正为公开方法引用；补当日重触发直测。

### 测试

- 新增 `tests/trade/test_place_order.py` 6 例（脊柱副作用/风控拒静默/人工买不入账/risk_price 口径/order_id 占位回填/created_ts 不继承）+ test_rotation in_flight 直测 + test_monitor clear_trigger 直测。
- 回归: test_executor 33 例（place_ladder 12）零断言修改全绿；test_rotation 34 / test_auto_buy 21 / test_api+e2e+notifier 82 / test_monitor 全绿；**全量 `pytest tests/` exit=0**。
- 验收 grep: created_ts 赋值单点（reconciler.py:441 对账例外）；rotation/auto_buy/trade_main 生产路径零下单直调；`next_remark` 外部调用清零。

### 行为口径（§五 三处微统一，已在计划书论证等价）

1. created_ts 时钟源统一到 Executor clock（生产三路本就同一 `time.time` 对象）；
2. 审计 extra 的 order_id 构造时机提前（占位回填，键序逐字节一致，无观察者）；
3. 语句顺序：_sell 尾巴（_pending/锁/回填）与 ladder 的 mark_tier 移到 place_order 之后（毫秒级崩溃窗口变化已论证登记）。

### 剩余风险 / 已知债

- **[已知债·单独立项] 人工买入入账时机分叉**: `_cmd_buy` 走 `account_immediately=False`（回报经事件链入账），与四路"立即入账"并存——同系统两种答案。本卡按用户拍板保持现状不统一，统一需先确认事件链回报与立即入账不重复。
- ladder mark_tier 崩溃窗口从≈0 扩到毫秒级（含 2 次 sqlite 写）；概率极低已登记，实盘出现即按计划书预案改预标记。

---

## 格式约定

每次重大迭代新增一条顶级条目,包含:
1. 审计入口链接
2. 关键改动表 (迭代 → 主题 → 改动 → 影响)
3. 文件改动统计
4. 测试基线演进表
5. 分数演进表
6. 剩余风险 / 已知债