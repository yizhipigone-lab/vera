# AutoResearch 量化增强 计划书 审计报告

**审计对象**:`docs/plan/2026-07-19_AutoResearch量化增强_计划书.md`
**审计类型**:方案/计划书质量审计(非代码审计)
**审计日期**:2026-07-19
**审计员**:code-reviewer(架构人格)
**审计纪律**:所有 `[file.py:line]` 引用均经 Read/Grep 机器校验(见 §7 校验记录)

## 0. 总体判断

**结论:需修订(Revision Required)** — 无 CRITICAL,但有 2 个 HIGH 风险遗漏必须在阶段 1 启动前补进文档。

裁剪思路清晰、复用清单扎实、6/6 个 `[file.py:line]` 引用全部通过机器校验、文本因子自动联实盘的红线守住了。但漏了 2 个 HIGH(文本因子前视偏差、git 撞车),1 个资产描述需补正,2 个交付标准需量化。修订后可合入并启动阶段 1。

| 维度 | 结论 | 说明 |
|---|---|---|
| 1. 完整性 | PASS | 用户原方案 7 大节全覆盖,仅"5 步 vs 9 步"对应关系未点明(LOW) |
| 2. 一致性 | PASS | fitness / 阶段 / 防线逻辑自洽,§2 错配③ 铁律合并措辞略糙(LOW) |
| 3. 可行性 | WARNING | 复用清单真实存在,但 sim_trader 现状未核实(MEDIUM) |
| 4. 风险识别 | WARNING | 红线守住,但漏 2 个 HIGH(前视偏差、git 撞车) |
| 5. 优先级 | PASS | 腿 A 先 / 腿 B 后依据充分,阶段划分合理 |
| 6. 可测试性 | PASS | fitness 可程序化、keep/discard 无歧义,交付标准欠量化(MEDIUM) |

## 1. 完整性 — PASS

用户原 7 大节**全覆盖**:核心架构定位(§1+§3)/ 因子(§7)/ 工作流(附录 A)/ 技术细节(§9+§10)/ 分阶段(§5)/ 核心优势(§12)/ 风险控制(§8)。

**[LOW #7] "5 步工作流" → "9 步循环"未点明对应关系**(附录 A):读者无法快速确认语义保真。建议附录开头加映射表,说明新 9 步只是多了 git commit / 崩溃处理 / results.tsv 三步,不改语义。

## 2. 一致性 — PASS

关键变量逻辑链(fitness 公式、阶段划分、风险防线、决策点)**全部自洽**,交叉引用一致。术语统一(fitness / keep/discard / harness / walk-forward 贯穿全文,与 CLAUDE.md 术语中文化表不冲突——表中只禁 ladder/trailing 等,不禁 fitness/harness)。

**[LOW #8] §2 错配③ 两条铁律合并措辞**:把"宏观/地缘只报告不调仓"和"实盘脆弱期"绑成"违反两条铁律"。严格讲,宏观铁律只限宏观/地缘类因子,文本因子未必都是(如公告因子)。§8.3 已做精确澄清,§2 措辞跟上即可。

## 3. 可行性 — WARNING

### 3.1 复用清单真实存在性核对(§9.1) — 全部 PASS

| 资产 | 计划书描述 | 实际校验 | 结论 |
|---|---|---|---|
| `engine.run_cached` 9 旧位置参数 | "参数兼容(9 旧位置参数不动)" | `engine.py:1128` 签名 `(self, close, entries, high_np, low_np, stop_config, selections, ladder_profits, ladder_ratios, n_ladder, *, ...)` — 不含 self 共 9 个位置参数 | 准确 |
| `gs_5m_sweep` nshards/shard 分片 | "分片并行" | `gs_5m_sweep.py:230` `combos = [c for i, c in enumerate(combos) if i % args.nshards == args.shard]` | 准确 |
| `gs_run_one` subprocess 入口 | "subprocess 单公式入口" | `gs_run_one.py:39` `def main():` + 文档注释明确"批量场景请用 subprocess 并行调度本脚本" | 准确 |
| `FormulaRunner` 选股/指标入口 | 注入点 `run_stock_selection_with_dates` / `run_indicator` | `formula_runner.py:48` / `:191` 均存在 | 准确 |
| K线缓存(parquet) | "K线缓存(parquet)" | `data/kline_cache/1d/*.parquet` 11057 个文件存在 | 准确 |
| `degrade_5m` 外挂层思路 | "参考其架构" | CLAUDE.md 确认 `backtest/degrade_5m.py` 存在 | 准确 |

### 3.2 [MEDIUM #3] sim_trader 现状未核实(§5 阶段 4)

阶段 4 依赖"sim_trader T+1 开盘路径模拟 1-2 个月",但计划书完全没核实 sim_trader 当前是否可用、是否需要改动、是否已接入实盘信号链路。若不可用或要大改,阶段 4 时间估计(>1 个月)失准。

**修复**:阶段 4 前置加一条"sim_trader 可用性确认",或在阶段 1 启动前先盘点 sim_trader(读代码 + 跑最小样本)作为阶段 4 前置 gate。

### 3.3 补充说明(非问题)

§3.2 把"批量循环"指到 `gs_5m_sweep.py:206 do_run`,而 `do_run` 用的是**矩阵缓存**(`_load_cache` 加载 `.npy + meta.json`,见 `gs_5m_sweep.py:187-203`),与 §9.1 列的"K线缓存(parquet)"是不同层资产。建议 §9.1 加一行区分:`kline_cache(原始 K 线,parquet)` vs `matrix_cache(预取矩阵,.npy)`,避免读者混淆。

## 4. 风险识别 — WARNING(2 个 HIGH 遗漏)

### 4.1 红线守住确认

| CLAUDE.md 铁律 | 计划书守线位置 | 守住 |
|---|---|---|
| 宏观/地缘只报告不调仓 | §8.3 "文本因子里政策/宏观类只生成报告提示,不进 selections 过滤" | 是 |
| 信号日 T 收盘买入 | §4.1 + §8.3 "腿 A 循环统一走 T 收盘口径" | 是 |
| 两套买卖口径别混 | §8.3 "循环用回测口径;sim_trader 用 T+1 开盘" | 是 |
| 评价给夏普/Calmar/回撤/胜率 | §6 fitness 设计 + §8.3 "results.tsv 全记录" | 是 |

### 4.2 [HIGH #1] 遗漏风险①:文本因子的前视偏差 / 未来函数

**定位**:§7 整章。
CLAUDE.md 明确"2026-07-02 历史审计发现过前视偏差、默认值漂移、ST 过滤失效,系统综合 4.5/10"。文本因子是前视偏差高发地带,典型陷阱:年报"发布日 vs 报告期"对齐、公告修正值回填、增减持"公告日 vs 权益登记日"、ST 摘帽历史状态回填。§7 全章讨论数据源、因子类型、LLM 层,**完全没提前视偏差**。腿 B 一旦上,这是最容易"把没校准的尺子接上真钱"的隐患。

**修复**:§7 新增 §7.6"文本因子前视偏差防护",至少覆盖:(1) 所有文本因子用**事件公告日**(非报告期)对齐交易日;(2) selections 过滤只能用 t 日及之前已公告事件,t+1 信息一律不得回填;(3) 加"前视检测单测":构造已知未来事件样本,验证因子值在事件日前为空。

### 4.3 [HIGH #2] 遗漏风险②:git 分支纪律与并行会话撞车

**定位**:§9.3。
§9.3 引入 `autoresearch/<tag>` 分支 + 无限 `git commit` + `git reset --hard`。但项目 MEMORY 记录 2026-07-18 亲历"并行会话 git 撞车"——并行会话把未提交改动卷进它的 commit,HEAD 可能出半残 commit。如果夜里循环自动 `reset --hard`,而第二天用户在 master 上工作,极易:(1) 循环误把用户未提交改动 reset 掉;(2) 循环分支与 master 分叉后合并冲突;(3) "results.tsv 不入 git"约定与项目"核心源码 + tests 要及时入 git"边界模糊。§9.3 完全没提。

**修复**:§9.3 末尾加"并行会话防撞"小节:(1) 循环跑前 `git status` 必须干净,有未提交改动跳过本轮或 stash;(2) 循环分支与 master 物理隔离,在单独 worktree 跑(`git worktree add ../vera-autoresearch`);(3) `results.tsv` 路径写入 `.gitignore` 明确,避免被批量 `git add` 误纳入。

### 4.4 [MEDIUM #4] fitness=NaN / 空输出处理路径不明

**定位**:附录 A 第 6 步与 §6 正式设计。
附录 A 说"空输出 = 崩溃,记 crash 跳过",但 §6 正式 fitness 章节没纳入 NaN/空输出。量价策略 0 信号、回测区间无交易、metrics 异常都会让 fitness=NaN,keep/discard 决策路径未明确。

**修复**:§6 加一节"异常 fitness 处理",明确 fitness=NaN 或空输出 → 默认 discard(记原因),不挂循环。

### 4.5 [LOW #9] 资金量级保密未点(§7.4)

CLAUDE.md "策略/资金量级 = 高度机密"。§7.4 强调"绝不发持仓/策略信号"到云端 LLM,但没明说资金量级也不能进 prompt。补一句即可。

## 5. 优先级 — PASS

腿 A 先 / 腿 B 后,依据四个维度(autoresearch 原生度 ★★★★★ vs ★☆☆☆☆ / 数据依赖已有 vs 缺 / 实盘风险中 vs 高 / 能否立即开工),**充分**。阶段 1→2→3→4 渐进,每阶段有明确前置和退出条件,**合理**。

**[LOW #10] 阶段时间估计无方法论**(§5 各阶段 1-2 周 / 2-3 周 / 3-4 周 / >1 个月):没给依据(类比历史?人天拆解?复杂度评分?)。加一行"时间估计基于 gs_5m_sweep 历史开发耗时类比"或人天拆解即可。

## 6. 可测试性 — PASS(交付标准欠量化)

### 6.1 fitness 程序化判定能力 — 全部无歧义

| 设计要素 | 程序化 | 无歧义 |
|---|---|---|
| fitness = 年化 - 0.5 × max(0, 回撤 - 15%) × 100(方案 A) | 单行公式 | 是 |
| 硬约束 回撤 ≤ 15% | 阈值明确 | 是 |
| 次序 夏普 > 胜率 > Calmar > 交易次数 | 字段在 metrics | 是 |
| keep/discard fitness 升 → keep / 降 → discard | 数值比较 | 是 |
| 牛熊分段 2 区间达标(§6.4) | 可程序化 | 是 |

这是本计划书最扎实的部分之一。

### 6.2 [MEDIUM #5] 阶段 1 交付标准模糊(§5)

"跑一夜验证 keep/discard 自动前进/回退是否工作"——"是否工作"无量化。

**修复**:改成"跑一夜 ≥ 50 轮,keep/discard 决策零失误(确定性逻辑),results.tsv 字段齐全且与 git log 一致"。

### 6.3 [MEDIUM #6] 交易次数下限缺区间约束(§6.2)

"交易次数下限 30 次/回测区间"——没说区间最小长度。区间 3 个月难达到,跨 5 年又过松。

**修复**:加"区间最小 1 年(约 244 交易日),30 次下限按 1 年口径,更长区间按比例放大"。

## 7. 引用机器校验记录(审计纪律)

按 CLAUDE.md 审计铁律,对计划书全部 6 条 `[file.py:line]` 引用逐一校验:

| 计划书引用 | 校验方法 | 实际行内容 | 结论 |
|---|---|---|---|
| `backtest/engine.py:1128` `run_cached` | Read 行 1100-1159 | 行 1128 = `def run_cached(self, close, entries, ...)` | PASS |
| `tools/gs_5m_sweep.py:206` `do_run` | Read 行 170-249 | 行 206 = `def do_run(args):` | PASS |
| `tools/gs_5m_sweep.py:187` `_load_cache` | Read 行 170-249 | 行 187 = `def _load_cache(formula, window_td=WINDOW_TD):` | PASS |
| `tools/gs_run_one.py:39` subprocess 入口 | Read 行 1-80 | 行 39 = `def main():` | PASS |
| `core/formula_runner.py:48` `run_stock_selection_with_dates` | Read 行 1-60 | 行 47-48 = `@classmethod` + `def run_stock_selection_with_dates(...)` | PASS |
| `core/formula_runner.py:191` `run_indicator` | Read 行 185-224 + Grep | 行 190-191 = `@classmethod` + `def run_indicator(...)` | PASS |

**6/6 通过**。计划书在引用纪律上做得非常好。

补充校验:`run_cached` 9 旧位置参数(Grep 实测签名确认);K线缓存 parquet(Glob 返回 11057 文件);矩阵缓存用 `.npy + meta.json`(Read 确认)。

## 8. 问题清单总览

| # | 级别 | 章节 | 问题摘要 |
|---|---|---|---|
| 1 | **HIGH** | §7 整章 | 文本因子前视偏差/未来函数风险未提(CLAUDE.md 历史审计铁律) |
| 2 | **HIGH** | §9.3 | git 分支纪律与并行会话/主分支撞车风险未提(MEMORY 2026-07-18 亲历事件) |
| 3 | MEDIUM | §5 阶段 4 | sim_trader 现状未核实,阶段 4 时间估计可能失准 |
| 4 | MEDIUM | 附录 A / §6 | fitness=NaN/空输出处理路径未纳入正式设计 |
| 5 | MEDIUM | §5 阶段 1 | 交付标准"是否工作"模糊,无量化阈值 |
| 6 | MEDIUM | §6.2 | 交易次数下限 30 次缺区间长度约束 |
| 7 | LOW | 附录 A | "5 步" → "9 步"未点明对应关系 |
| 8 | LOW | §2 错配③ | 两条铁律合并措辞略糙,§8.3 已澄清 |
| 9 | LOW | §7.4 | 资金量级保密未显式点 |
| 10 | LOW | §5 各阶段 | 时间估计无方法论 |

## 9. 审计结论

**Verdict:WARNING — 需修订后合入**

| Severity | Count | Status |
|---|---|---|
| CRITICAL | 0 | pass |
| HIGH | 2 | warn |
| MEDIUM | 4 | info |
| LOW | 4 | note |

**核心肯定**:
1. **裁剪思路清晰**:把"AutoResearch 是 LLM/GPU 平台"的神化剥掉,还原为"keep/discard 循环脚手架",据此切出腿 A(量价)/腿 B(文本),依据充分。
2. **复用清单扎实**:6/6 引用通过机器校验,9 旧位置参数 / parquet 缓存 / nshards 分片 / FormulaRunner 入口全部真实存在且描述准确。
3. **红线守住**:文本因子自动联实盘的红线在 §4.2、§5 阶段 4、§8.3 三处反复声明"永不自动联真实仓位"。
4. **fitness 可测试**:多目标压成单标量(年化主 + 回撤硬约束 + 次序判定),完全可程序化、无歧义。

**修订要求(启动阶段 1 前)**:
- 补 §7.6 文本因子前视偏差防护(HIGH #1)
- 补 §9.3 并行会话 git 撞车防护(HIGH #2)
- 其余 MEDIUM/LOW 可在阶段 1 编码过程中补

修订完成后,**阶段 1(腿 A MVP)可以启动**。
