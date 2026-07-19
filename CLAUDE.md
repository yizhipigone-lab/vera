# VERA 项目上下文

> 跨会话持久上下文。新会话先读这里,避免重复踩坑。具体代码坑以 tests/ 和 git 历史为准,这里只放不会过时的铁律。

## 项目定位

- **VERA** = 个人实盘管理后台 + AI 驱动策略平台
- **已实盘,处于脆弱期**:v1/v2 跑通,部分策略实盘中,稳定性与工程化是当前瓶颈
- **本地部署优先**(不云端);策略与资金量级 = 高度机密;董秘/公司身份 = 公开

## 业务铁律(不可违反)

1. **宏观/地缘事件**:只生成报告提示,**绝不联入仓位调度** —— 用户保留人工把关
2. **回测买入价(铁律)**:**尾盘选股 → 信号日(T日)收盘价买入**。严禁用 T+1 开盘价做回测买入(T+1 开盘是实盘 sim_trader 口径,见第3条,不是回测口径)。优化/批量回测脚本统一遵循此口径
3. **两套买卖口径别混**:回测走信号日 T 收盘路径;实盘走 sim_trader 的 T+1 开盘路径。审计 bug 时**先确认在查哪条路径**,别审错
4. **策略评价默认同时给**:夏普、Calmar(用户口里的"夏普率")、最大回撤、胜率
5. **关注市场**:A 股 / ETF / 可转债、美股 / 港股、跨市场联动
6. **风险偏好**:中等回撤 + 多策略组合

## 架构骨架

| 模块 | 路径 | 职责 |
|---|---|---|
| 公式因子体检 | `tools/formula_lab.py` | 任意公式一条命令做因子体检(S0-S5):IC 筛选 → 族归纳 → A/B 终审 → 报告。方法论见 `docs/公式因子体检方法论.md`(五条纪律:IC 非终审/双窗口/数族不数因子/因果测试/必带警告) |
| 回测引擎 | `backtest/engine.py` | 主回测循环:信号→成交→止损止盈→权益曲线。`_simulate_core_v3` 现为兼容壳(2026-07-14 候选 A 阶段2,ENGINE_VERSION v3.4-loop-refactor),转调 `backtest/loop/BacktestLoop.run()`;旧 527 行实现保留为 `_simulate_core_v3_legacy` 作 parity 甲骨文。`run_cached` 加厚前门(2026-07-13 候选 A 阶段1,980b04f):9 旧位置参数不动 + 9 keyword-only 能力参数,能力按 `stop_config["capabilities"]` 三开关透传。`run` 走 Pipeline 收口路径。**5m 数据层降级(2026-07-18)**:`degrade_5m: true` 时缺 5m 的股-天用 1d OHLC 填满 48 根 bar 保信号(`backtest/degrade_5m.py`),降级影响报告在 `result.degradation`(`backtest/degrade_report.py`);默认关,仅 run() 路径 |
| 选股 | `selection/selector.py` | 股票池筛选(ST/涨停/停牌过滤) |
| 止损管理 | `backtest/stop_config.py` | 止损/止盈/移动止盈/阶梯止盈。`stop_config.py` 兜底含 priority + capabilities 字段(2026-07-13 修复)。stop_manager.py 已于候选 D C2 删除 |
| 复权口径 | `core/dividend_type.py` | **统一 int/str 映射(候选 D,0b47db5)**:DataFetcher/FormulaRunner 内部用 `to_tdx_str`/`to_formula_int` 归一化,允许混传。`assert_consistent` 由 pipeline.py:101 调用 |
| 公式系统 | TDX 公式翻译 + `core/formula_runner.py` | 通达信公式执行封装,统一入口。批量脚本 `batch_*.py` 大部分已删(2026-07-13 清 35 个废弃脚本) |
| Web 后端 | `server.py` | API + 进度反馈。**现状(2026-07-14 已完成)**:`/api/run` 走 `Pipeline.run` + `ResultWriter`（统一完整流程接缝，2026-07-14 372f59b）；进度回调由 `ResultWriter.on_progress` 驱动 `pipeline_status` 单例（不再手工赋值）；`PipelineResult` frozen dataclass 统一返回结构。C5 真实盘口验证通过（路径 A/B 数字字节级一致）。 |
| Web 前端 | `web/index.html` + `vera-ui.js` | 管理后台 UI |
| 测试 | `tests/` | pytest 套件,改核心函数后必跑。守卫式 + 字节级 parity + 能力透传 + 默认值锁 + 复权口径边界 + 进度回调签名 |

**历史背景**:`_simulate_core_v3`(39 参数私有函数)曾是事实公共入口,被 4 脚本 + 4 测试直调。候选 A 阶段 1 + 阶段 1.5 收编 5 脚本 + `optimize_strategies` 收编 + 清理 `optimize_full` 死 import,**生产直调完全清零**(锁私有完整达成,2026-07-13 e62e0ab)。候选 D C4 清理 34 个孤儿脚本(2026-07-14 aa54d19),根目录仅余 main.py / server.py / preprocessor.py 三入口。候选 D C2 删 `stop_manager.py`(死代码,无调用方)。**批量注意**:`Pipeline.run()` 每次执行 `initialize+close`,不适合 in-process 高频复用;批量场景用 subprocess 并行调度 `tools/gs_run_one.py`。

## 协作风格(用户四禁,违反即止损)

1. **不车轱辘话**:不要"这是一个值得深入探讨的问题"这种废话开场
2. **不过度谨慎**:不要为安全给 4 个保留意见 + 半个选项
3. **不空话**:必须给具体代码 / 具体数字 / 具体路径
4. **不拍脑袋**:不确定就明确说"这块我没把握,证据是 X"

## 审计纪律(2026-07-15 写入,源自 C1 假阳性事件)

**铁律**: 任何审计报告引用 `[file.py:line]` **必须**经过机器校验,严禁肉眼引用。

工具:
- `docs/audit/_verify_references.py` — 提取所有 `[file.py:line]` 引用,校验文件存在 + 行号在范围内
- CLI: `python -m docs.audit._verify_references <md_path>` (返回码 0=PASS, 1=FAIL)
- 测试: `pytest tests/test_audit_references.py` (含本日报告的反向防回归 `test_metrics_67_actual_code_has_as_e`)

事件回顾: 2026-07-15 审计报告 C1 错引 `[backtest/metrics.py:67-68]` 说 `except Exception:` 没有 `as e`,
实际代码第 67 行**就是** `except Exception as e:`。报告把 `as e:` 截掉伪造 CRITICAL,判"不可合入"。

教训: 审计 agent 写引用前**必须 Read 实际行**,不能凭记忆/上下文推断。机器校验是兜底,人是最后一道。

决策风格:**先跑通再优化**。方案给"推荐 + 备选 + 风险",该拍板就拍板。中文为主,专业词中英混用(T+1 / Calmar / 可转债),复杂概念先大白话再补术语。

## 术语中文化(2026-07-06 生效,强制)

跟用户对话、写文档/报告/代码注释时,**禁用**以下英文术语,一律用中文:

| 禁用英文 | 用中文 |
|---|---|
| ladder / ladder_tp / ladder levels | 阶梯止盈 / 阶梯止盈档位 |
| trailing / trailing_stop / trailing_tp | 移动止损止盈 / 移动止盈激活线 / 移动止盈回撤 |
| cost stop / cost_stop_threshold | 硬止损 / 硬止损阈值 |
| stop_first / ladder_tp_first / trailing_first | 止损优先 / 阶梯止盈优先 / 移动止盈优先 |
| time_stop / cond_time_stop | 时间止损 / 条件时间止盈 |
| formula_exit / formula_sell | 公式卖出 |
| priority | 优先级 |

- **代码标识符**(变量名/类名/字段名)保持英文不改,会破坏依赖;但**注释和对话里**用中文
- 用户专有缩写保留:TDX / A 股 / ETF / Calmar / QUANTQQ 等

## 回测可信度提示

- 历史审计(2026-07-02)发现过前视偏差、默认值漂移、ST 过滤失效、复权口径分裂、成交价乐观偏差等问题,系统综合曾评 4.5/10
- **2026-07-13 架构深化 + 修复后系统综合 7.5/10**(三角色审计,980b04f + edd84ce + ca8bc6e + 41d4e29 + 4b3f8c8 + d9e74cd + e62e0ab + 0b47db5 + 9a94d0c + 555aadd + ce0b314):run_cached 加厚前门 + 锁私有 + 复权口径统一 + 进度反馈细化 + 清 35 废弃脚本。审计报告 `docs/audit/2026-07-13_候选A阶段1_审计报告.md`;候选 B(35 脚本收口)已弃,改删 35 废弃
- **回测绝对值不可全信**(方向系统性乐观);相对排序在同口径、含停牌/ST 少的策略里尚可参考
- 给结论时**标注是否已核实尺子**,不要装作回测是准的。具体坑以最新代码 + tests 为准,别信旧 memory 里的 file:line

## 工作流约定

- 改核心函数前先 `grep` 调用方(`engine.run_cached` 等入口被大量脚本依赖,别破坏参数兼容)
- 新功能优先**独立新文件**,别塞进跑得好好的脚本
- 改完跑 `pytest tests/` 或最相近的测试用例
- 不要 `git add .` 大批量提交;先 `git diff --stat` 评估
- 核心源码 + tests 要及时入 git(历史审计发现过未入库问题)
- 接到任务先问清楚决策点(路径/数量/格式/边界),别靠猜动手
