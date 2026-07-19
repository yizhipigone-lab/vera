# trend_radar/regime 移植计划书 审计报告

> **审计对象**:`docs/plan/2026-07-19_trend_radar_regime移植_计划书.md`
> **审计类型**:**计划书质量审计**(非代码审计)
> **审计日期**:2026-07-19
> **审计员**:code-reviewer agent
> **审计纪律**:来源函数/参数/Notes 已用 gh api repos/HKUDS/Vibe-Trading/contents/.../SKILL.md 机器校验;VERA 接缝代码已 Read 实际文件(tools/combo_filter_test.py / tools/factor_score.py / backtest/result.py / pipeline/pipeline.py)。

---

## 0. 机器校验结论(审计纪律第一步)

### 0.1 函数名校验(源 SKILL.md vs 计划书)

| 计划书引用 | SKILL.md 中位置 | 状态 |
|---|---|---|
| compute_edge_density | Mode 1 | PASS |
| detect_regimes | Mode 1 | PASS |
| regime_exposure_context | Mode 2 | PASS |
| first_mover_attribution | Mode 3 | PASS |
| rewiring_leaderboard | Mode 4 | PASS |

**结论**:5 函数名全部命中。

### 0.2 参数校验(计划书 §1 / §2 vs SKILL.md 函数签名默认值)

| 参数 | 计划书取值 | SKILL.md 默认 | 状态 |
|---|---|---|---|
| corr_window | 60 | 60 | PASS |
| edge_threshold | 0.5 | 0.5 | PASS |
| enter_threshold | 0.65 | 0.65 | PASS |
| exit_threshold | 0.45 | 0.45 | PASS |
| smooth_window | 5 | 5 | PASS |
| baseline_window (M3) | 120 | 120 | PASS |
| move_window (M3) | 3 | 3 | PASS |
| alarm_z (M3) | 8.0 | 8.0 | PASS |
| watch_z (M3) | 3.0 | 3.0 | PASS |
| lead_gap / macro_span / macro_fraction | 2 / 1 / 0.6 | 2 / 1 / 0.6 | PASS |

**结论**:11 个参数全部与源一致,无失真。

### 0.3 8 Notes 校验(计划书 §5 vs SKILL.md Notes)

| 计划书 Note | SKILL.md Note | 语义一致 |
|---|---|---|
| 1 因果是全部 | Causality is the whole game | PASS |
| 2 非交易信号 | Not a trading signal | PASS |
| 3 阈值不跨估计器 | Thresholds do not port across correlation estimators | PASS |
| 4 事件窗口左删失 | Event-window scans are left-censored | PASS |
| 5 幸存者偏差 | Survivor bias truncates attribution | PASS |
| 6 归因范围 + calm 误报率 | Scope of the attribution claim | PASS |
| 7 标签 walk-forward | Labels are human, walk-forward | PASS |
| 8 MAD=0 守分母 | MAD can be zero | PASS |

**结论**:8 Notes 完整且语义一致。

### 0.4 机器校验总判定:**PASS**(无 CRITICAL 级失真)

---

## 1. 六维度结论

| 维度 | 结论 | 关键发现 |
|---|---|---|
| 1 完整性 | **WARNING** | 4 模式全、8 Notes 全、3 接缝全;但 M3 baseline 源(H2)、M4 mask 定义(L2)、4.3 接缝过略(L3)有缺 |
| 2 一致性 | **WARNING** | 参数/Notes 一致;但 §2 表内 alarm_z 论据不对等(M2)、涨跌停行不适用板块(M3)、enter/exit "待校准"与默认值并存(LOW) |
| 3 可行性 | **WARNING** | 算法可移植、tushare token 已有;但数据接口名歧义(M1)、申万分类调整风险(M1 子项)、5m 铁律边界未声明(M5) |
| 4 风险识别 | **WARNING** | 8 Notes + A 股特有风险列全;**但 4.2 减仓语境与 CLAUDE.md 业务铁律 1 边界模糊(H1)** |
| 5 优先级 | **PASS** | 6 步骤顺序合理(校准→M1→M2/3→M4→接缝→验证),与规则 3 "独立新文件" 一致 |
| 6 可测试性 | **WARNING** | M1 标准清晰(误报率<1%/天);但验证事件不足(M4)、calm 期定义未明(M4 子项)、M3/M4 命中判定标准缺失 |

---

## 2. 问题清单(按严重度)

### CRITICAL — 0

(无。机器校验全 PASS,无算法/参数失真。)

### HIGH — 2

#### H1: 4.2 减仓语境与 CLAUDE.md 业务铁律 1 边界模糊

- **定位**:计划书 §4.2 + §10 风险控制表;CLAUDE.md "业务铁律 1"
- **问题**:计划书 §4.2 写 "接 server.py Pipeline.run 之后的仓位层:PipelineResult 加 regime_gross 字段"——把 regime 状态变成 gross exposure 建议接进仓位层。CLAUDE.md 业务铁律 1 原文:**"宏观/地缘事件:只生成报告提示,绝不联入仓位调度——用户保留人工把关"**。regime(市场融合度)处于 "宏观系统性风险" 与 "市场结构状态" 的灰色地带。
- **后果**:若 regime 被认定为 "半个宏观",自动减仓语境直接违反铁律 1。这是会触发用户 "四禁" 止损条款的边界冲突。
- **修复**:计划书必须二选一明确,且写入 §0 / §4.2:
  - 选项 A(推荐,与铁律 1 同口径):声明 "regime 减仓语境只生成报告提示(同宏观铁律口径),不自动改仓位;PipelineResult.regime_gross 仅作监控字段,实盘减仓需人工确认"。
  - 选项 B:声明 "regime 不属宏观/地缘事件(它是市场结构状态,类似波动率),可联自动减仓"——但这要在 §0 显式声明并和用户确认。

#### H2: M3 baseline 源(blind trailing vs Mode 1 calm mask)未决策

- **定位**:计划书 §1 Mode 3 描述 + §6 步骤 3
- **问题**:源 SKILL.md "Calibration Discipline" 原文强调 "Baseline from detected calm, not from a blind trailing window, in production: source the median/MAD baseline from Mode 1 defused regimes. Trailing windows that overlap a previous crisis produce contaminated baselines and inflated bars — this was a real failure class in validation (post-crisis calm that wasn't)"。SKILL.md 代码本身用 intensity.rolling(baseline_window).median().shift(1) (blind trailing),但生产指南明确要求用 Mode 1 的 calm mask。计划书第 39-43 行只列算法签名,§6 步骤 3 只说 "M3 walk-forward 校准 alarm_z",**没决策 baseline 用哪种**。
- **后果**:实施时若按代码字面用 blind rolling,会落入源文档警告的 "post-crisis contaminated baseline" 陷阱(2024-02 微盘股崩盘后 2024-03~04 的 trailing 窗口被危机数据污染,alarm bar 失真)。
- **修复**:计划书 §1 Mode 3 + §6 步骤 3 加一行:"baseline 用 M1 fused==0 子集算 median/MAD,不用 blind trailing window(源 SKILL.md Calibration Discipline 强制要求);shift(1) 防自污染保留"。

---

### MEDIUM — 5

#### M1: 数据接口名歧义(tushare index_daily 不含申万行业指数)

- **定位**:计划书 §2 "数据源" 行 + §6 步骤 1
- **问题**:计划书写 "tushare index_daily / akshare 板块"。**tushare 标准 index_daily 接口不含申万行业指数**(申万不是交易所发布的标准指数),需要的是 sw_index_daily (积分要求更高) 或 index_hist_sw (老接口名);akshare 对应 sw_index_daily_ins 或 index_hist_sw。计划书没指明接口名,实施时直接套 index_daily 会拿到空数据或上证/沪深系列。
- **修复**:§2 + §6 步骤 1 加 "先验通数据源:pro.sw_index_daily(ts_code='801010.SI', start_date=..., end_date=...) 或 ak.index_hist_sw(symbol='801010');若 tushare 积分不足降级 akshare;申万一级 28 行业 ts_code 为 801010-801280.SI"。

#### M2: alarm_z 8→10-12 论据不对等

- **定位**:计划书 §2 第 67 行
- **问题**:论据 "A 股涨跌停 ±10/20/30%,波动大 → alarm_z 8→10-12" 有三处不对等:
  - 板块指数本身**无涨跌停限制**(只有个股有),用个股涨跌停论证板块 alarm_z 不对等
  - z-score 是 robust 统计(median/MAD),已经吸收了波动量级差异
  - 比较不对等:源 SKILL.md 用加密 **1 分钟 bar**,计划书用 A 股**日级** bar,日级波动天然小一个量级
- **后果**:论据误导,可能让读者以为必须调高;实际上 walk-forward 校准后可能 8.0 就够,10-12 起步值偏高。
- **修复**:论据改为 "A 股日级板块 returns 的 z-score 尾部更厚于加密 1 分钟级,walk-forward 校准起步值 10-12(最终值由步骤 3 校准定)",删掉 "涨跌停 ±10/20/30%" 误导。

#### M3: §2 第 70 行 "涨跌停" 行不适用板块级

- **定位**:计划书 §2 第 70 行
- **问题**:写 "涨跌停:日级 returns 已含涨跌停,无需额外处理"。板块指数没有涨跌停限制(它是 28 行业加权指数,不是个股),这一行对当前板块级方案无意义。
- **修复**:改为 "板块指数无涨跌停限制,无需处理;个股级 M3/M4(本计划不做)才需处理涨跌停截断"。

#### M4: 验证事件不足 + calm 期定义未明

- **定位**:计划书 §7
- **问题**:两个独立子问题:
  - **事件数不足**:§7 只列 2 个明确事件(2024-01~02 微盘股崩盘、2024-09 逼空大涨),第 3 个 "2025 某事件(待选)" 留白。源 SKILL.md 用 **8 事件**回归。事件少 → 统计不显著 + 过拟合到 2 个事件上。
  - **calm 期定义未明**:"calm 期每日误报率<1%/天" 的 calm 期怎么定?是 M1 fused==0 的所有 bar,还是手动挑的平稳期?分母不同误报率差几倍。
- **修复**:§7 补足 4-6 个事件(建议加:2024-04 新国九条、2025-04 关税风波、2024-10 逼空回调、2026-XX 任一近期事件),明确 "calm 期 = M1 标 fused==0 的所有 bar(与 H2 baseline 源一致)"。

#### M5: 5m 回测铁律边界未声明

- **定位**:计划书 §9 + CLAUDE.md 业务铁律 2/3
- **问题**:CLAUDE.md 业务铁律 2 "回测买入价:尾盘选股→信号日 T 收盘价买入"。regime 状态是**日级**(T-1 收盘算 density,T 日判定)。计划书没声明 regime 状态在 5m 回测买入时刻的边界——若 regime 用 T 日盘中 5m 数据更新,会和 T 收盘买入铁律冲突。
- **修复**:§9 加一行 "regime 状态以日级 T-1 收盘 density 判定,T 日开盘前确定,T 日 5m 选股时刻不更新 regime(与 T 收盘买入铁律不冲突)"。

---

### LOW — 3

#### L1: 附录 "4 个函数" 实为 5 个(文字 bug)

- **定位**:计划书附录第 184 行
- **问题**:写 "4 个函数(compute_edge_density / detect_regimes / regime_exposure_context / first_mover_attribution / rewiring_leaderboard)"——括号里列了 **5 个**。
- **修复**:改 "4 模式 5 函数" 或 "5 个函数(分属 4 模式)"。

#### L2: M4 的 calm_mask/event_mask 定义未明

- **定位**:计划书 §1 Mode 4 + §6 步骤 4
- **问题**:rewiring_leaderboard(returns, calm_mask, event_mask, min_bars=40) 需要两个 mask 输入,§6 步骤 4 只说 "M4 事件窗口选择 + 和 M3 互补验证",没说 calm_mask 怎么定(是用 M1 fused==0 还是另定)。
- **修复**:加 "calm_mask = M1 fused==0 子集;event_mask = run-up 窗口(regime onset 前 20-60 bar)或怀疑段(步骤 4 实施时定)"。

#### L3: §4.3 选股碰撞接缝过略

- **定位**:计划书 §4.3
- **问题**:一句话 "FUSED 时趋势排名加权收紧",没指明接哪个函数(selection/selector.py 的输出?还是 tools/factor_score.py 的 total_score 加权?)。
- **修复**:补 "FUSED 时对 factor_score.score_selections 输出的 total_score 加 regime 惩罚项(或声明二期再做,当前计划只接 4.1/4.2)"。

---

## 3. VERA 接缝可行性核查(已 Read 实际代码)

### 3.1 §4.1 动态避雷阈值 → tools/combo_filter_test.py

- **现状**(combo_filter_test.py:67):ap.add_argument("--threshold", type=int, default=-2) 固定阈值
- **计划书接法**:把固定 --threshold 换成 regime 驱动的动态阈值(DEFUSED→-2,FUSED→0)
- **可行性**:**PASS**。combo_filter_test.py 第 93 行 filtered = scored[scored["total_score"] > args.threshold] 只用一处阈值,改成从 regime 状态查表即可。factor_score.py 的 total_score 范围 [-4, +4],FUSED 收紧到 0 合理(剔除 total≤0 即 mf+dragon+block 三因子至少一项偏空)。
- **注意**:combo_filter_test.py 是 tools/ 下的批量验证脚本,不是生产路径。计划书应声明 "4.1 接缝先在 tools/ 验证脚本验证 delta,生产路径(server.py Pipeline)另案"。

### 3.2 §4.2 减仓语境 → PipelineResult

- **现状**:PipelineResult 定义在 pipeline/result_writer.py(已确认 pipeline/pipeline.py:12 import)。backtest/result.py 的 BacktestResult 是 frozen dataclass,加字段不破坏(规则 3 兼容)。
- **可行性**:**PASS(机制层面)**。但与 H1 冲突——加字段本身可行,但是否自动改仓位违反铁律 1,必须先解决 H1。

### 3.3 §4.3 选股碰撞 → selection/selector.py / factor_score.py

- **现状**:factor_score.py 的 score_selections 已有 total_score 输出。
- **可行性**:**PASS**,但接缝过略(见 L3)。

### 3.4 §9 不破坏现有

- **核查**:trend_radar/ 目录尚不存在(Grep 全项目,除计划书和无关参数优化报告外无 regime 引用),完全是新建。
- **5m 铁律**:§9 明确 "5m 回测 + 优化止损绝对不动"——符合 CLAUDE.md 业务铁律 2/3 和 MEMORY 中的 "5m 涨停过滤修复" 留档。
- **结论**:**PASS**。

---

## 4. 总体判断

| 项 | 结论 |
|---|---|
| 机器校验(函数/参数/Notes vs SKILL.md) | **PASS** |
| 4 模式完整性 | PASS |
| 8 Notes 完整性 | PASS |
| 不破坏现有 | PASS |
| 接缝可行性(机制层) | PASS |
| **铁律 1 边界(H1)** | **FAIL — 必须用户拍板** |
| **M3 baseline 源(H2)** | **FAIL — 必须补决策** |
| 6 维度合计 | 1 PASS + 5 WARNING + 0 FAIL |

### 审计结论:**需修订(2 个 HIGH 解决后可合入)**

**Verdict: WARNING — 2 个 HIGH 必须解决后再合入**

- **H1(铁律 1 边界)**:必须用户拍板,选 "只生成报告" 还是 "自动减仓"。建议选前者(与铁律 1 同口径,最稳)。
- **H2(baseline 源)**:必须补决策 "用 Mode 1 calm mask,不用 blind trailing"。
- 5 个 MEDIUM 建议合入前一并修(数据接口名、alarm_z 论据、涨跌停行、验证事件、5m 边界)。
- 3 个 LOW 可实施时随手修。

### 计划书优点(客观记录)

- 4 模式 5 函数 + 11 参数 + 8 Notes **机器校验全部 PASS**,来源扒取无失真
- 独立新模块 trend_radar/,严守 "不破坏现有"(规则 3)
- 6 步骤优先级合理(校准先于实现,与规则 3 测试已有路径精神一致)
- A 股适配点都想到了(停牌剔除、复权口径复用 core/dividend_type.py、退市个股级另案)
- regime 不作交易信号(Note 2)与 CLAUDE.md 铁律 1 "宏观只报告" 在意图上一致——只是 4.2 减仓语境落地时边界没讲清(H1)

---

## 附录:机器校验命令(可复现)

    gh api repos/HKUDS/Vibe-Trading/contents/agent/src/skills/correlation-regime/SKILL.md \
      -H "Accept: application/vnd.github.raw" > /tmp/skill.md
    # 校验 5 函数名 + 11 参数 + 8 Notes 全在

报告结束。
