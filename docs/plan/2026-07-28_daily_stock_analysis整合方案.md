# daily_stock_analysis 整合 VERA 方案（3 轮迭代定稿）

> **日期**：2026-07-28
> **对象**：`E:\newprogram\daily_stock_analysis-main`（ZhuLinsen，**MIT License**，可搬可改可商用）
> **VERA 目标**：借鉴其 Agent 问股 + LLM 集成 + 定时运行（保持更新）+ 数据源路由，整合成 VERA 研究 TAB 对话大脑（P3）+ 政策流水线（P1b）保持更新
> **依据**：2 个 code-explorer agent 深读（Agent架构+问股 / LiteLLM+数据源+定时），全 file:line 实证

---

## §0 一句话结论

**定时三件套直接搬**（`trading_calendar` + `schedule` + `GracefulShutdown`，2-3 天落地"保持更新"）+ **Agent/LLM 模式借鉴自研**（不引 litellm 重依赖，VERA 只 DeepSeek 用 requests 够）+ **对话大脑用 ReAct + VERA 工具**（kg/policy/concept 做成工具，单 Agent 先行，不上多 Agent 级联）。

---

## §1 daily_stock_analysis 全面解读

### 1.1 它是什么
生产级 A股/港/美 AI 分析系统，每日 18:00 自动分析自选股 → 推送决策仪表盘（企微/飞书/Telegram/邮件）。**MIT License**（比 TradingAgents-CN 混合许可干净，无授权风险）。

### 1.2 六大机制（2 agent 实证）

| 机制 | 实现 | VERA 价值 |
|---|---|---|
| **多 Agent 架构** | `AGENT_ARCH=multi` 切 Technical→Intel→Risk→Decision 级联 + 4 道降级闸（预算15s/失败不阻断/超时合成dashboard/风控一票否决） | 中（先单 Agent，多 Agent 4 倍成本） |
| **问股（对话大脑）** | ReAct 循环（`runner.py:360`）+ SSE 流式（`/chat/stream`）+ 会话持久化（ConversationManager 内存+DB） | **高**（VERA P3 对话大脑） |
| **Skill 系统** | YAML 定义策略（缠论/波浪/均线）+ 三层激活（用户/manual/auto regime）+ specialist 模式 SkillAgent×N + Aggregator 加权 | **高**（VERA 公式策略写 skill） |
| **LiteLLM 集成** | 三层配置（YAML > LLM_CHANNELS env > Legacy）+ Router 多 key simple-shuffle + fallback（RateLimit 同 provider backoff/跨 provider 切）+ DeepSeek thinking 适配 | **高**（VERA DeepSeek 直接用） |
| **数据源路由** | BaseFetcher.name+priority 动态（凭据可用性）+ fail-open chain + (df, source_name) 元组 + 按 region 路由 | 中（VERA 已有 TDX，借鉴思路） |
| **定时运行** | GitHub Actions（cron 0 10 * * 1-5）+ 进程内 schedule + exchange-calendars 交易日检查 + 断点续传 | **极高**（VERA "保持更新"答案） |

### 1.3 关键纠错
- `AGENTS.md` = 仓库**协作规则**（目录边界/commit/AI 资产治理），**不是 Agent 设计文档**
- `SKILL.md` = 产品级"分析股票"能力说明（给外部 skill 消费方看），**不是 Agent 设计**
- 真正的 Agent 设计在 `src/agent/` 代码注释

---

## §2 可搬性（MIT License）

| 项 | 可搬性 |
|---|---|
| License | **MIT**（保留 LICENSE + 致谢即可商用/改） |
| 依赖 | litellm（重）/ exchange-calendars（轻）/ schedule（轻） |
| 定位差异 | 它是多市场推送系统，VERA 是 A 股研究平台 + 实盘——**借鉴模式为主，不整体搬** |

---

## §3 VERA 整合 3 轮迭代（用户要求"迭代到完美"）

### 🔄 第 1 轮（直接搬——列出所有可搬）

| 直接搬 | 借鉴自研 | 不要 |
|---|---|---|
| LLMToolAdapter（DeepSeek thinking + 多模型 fallback + Router） | — | — |
| ToolRegistry + @tool | — | — |
| run_agent_loop ReAct | — | — |
| SSE 流式 | — | — |
| trading_calendar.py | — | — |
| GitHub Actions | — | — |
| 11 种策略 Skill | — | — |

### 🥊 第 2 轮（挑刺第 1 轮——哪些不该搬）

| 第1轮要搬的 | 挑刺 | 调整 |
|---|---|---|
| **LLMToolAdapter（litellm）** | litellm 是重依赖（多 provider），VERA 只 DeepSeek（requests 够）。引 litellm 违反"不加中间件"铁律 | **借鉴 Router 多 key + fallback 思路，自研到 VERA `llm/providers.py`**（不引 litellm） |
| **GitHub Actions** | 云端运行（策略/资金机密外发 GitHub），违反 VERA"本地部署优先"铁律 | **不用 GH Actions**，用进程内 schedule（trade_main.py 加 schedule 入口） |
| **11 种策略 Skill** | VERA 是量化平台（公式因子），不是 LLM 推理型策略。11 种（缠论/波浪）是技术分析，VERA 公式策略形态不同 | **Skill 系统借鉴（YAML），但策略内容 VERA 自己写**（公式策略 → skill），P3 后期 |
| **多 Agent 级联** | 4 Agent × 6 步 = 30+ LLM 调用/股，VERA 个人本地 DeepSeek key 有限 | **先单 Agent + 多工具跑通**（AGENT_ARCH=single），验证后升 multi |
| **ToolRegistry（OpenAI schema）** | 借鉴模式好，但搬代码要适配 VERA 工具（kg/policy/concept） | **借鉴 ToolRegistry 模式自研**（VERA 工具注册） |
| **SSE 流式** | 体验好，但 VERA 研究 TAB 现在简单 fetch（research.js）。SSE 是升级 | **MVP 先同步 fetch，SSE 后续**（P3 大脑体验升级） |

### 🔄 第 3 轮（定稿——挑刺后的整合方案）

**直接搬（小而稳，无重依赖，2-3 天）**：
| 项 | 文件 | 为什么直接搬 |
|---|---|---|
| **trading_calendar.py + exchange-calendars** | `src/core/trading_calendar.py`（218 行） | 节假日表用库（exchange-calendars），VERA 不自己维护（坑）。A 股交易日判断直接用 |
| **GracefulShutdown** | `src/scheduler.py:27-53` | 实盘重启信号处理（SIGTERM/SIGINT 等任务完成），VERA 重启先对账再交易铁律的工程化 |
| **DeepSeek thinking extra_body** | `src/agent/llm_adapter.py:57-126` | `deepseek-chat` 开 thinking / `deepseek-reasoner` 不开（送了 400）。VERA DeepSeek 直接抄这几行 |
| **_is_within_days 新闻时效** | `src/search_service.py:1148-1162` | 政策流水线 P1b 新闻时效过滤 |

**借鉴模式自研（不引重依赖，2-4 周）**：
| 项 | 借鉴什么 | VERA 自研到哪 |
|---|---|---|
| **LLM 多 key + fallback** | Router simple-shuffle + RateLimit 跨 provider 不 backoff | 升级 `llm/providers.py`（多 key 轮询 + fallback，不引 litellm） |
| **ReAct 循环** | run_agent_loop（超时/预算/工具并行） | `brain/agent.py`（自研 ReAct，单 Agent + 多工具） |
| **工具注册** | ToolRegistry（OpenAI function schema） | `brain/tools/`（VERA kg/policy/concept 做工具） |
| **AgentContext 协议袋** | data+opinions+risk_flags 共享 | `brain/context.py`（对话大脑共享状态） |
| **Skill YAML** | SkillManager + load_skill_from_yaml | `brain/skills/`（VERA 公式策略写 YAML，P3 后期） |
| **进程内 schedule** | schedule 库 + provider 动态读取 | `trade_main.py` 或新 `scheduler.py`（VERA 每日定时） |

**不要（不适用 VERA）**：
| 项 | 理由 |
|---|---|
| litellm 重依赖 | VERA 只 DeepSeek，requests 够，不引多 provider 库 |
| GitHub Actions 云端 | 违反本地部署优先铁律（策略/资金机密不外发 GitHub） |
| 多 Agent 级联（Technical→Intel→Risk→Decision） | 4 倍 LLM 成本，先单 Agent 验证 |
| 12 通知渠道 | VERA 已有 web 后台 |
| 7 个 A 股 fetcher | VERA 已有 TDX |
| specialist + ResearchAgent + 800 行 dashboard 合成 | 过度工程，VERA 出精简版 |

---

## §4 实施路线（按价值/成本）

### 第一周（高价值低成本——"保持更新"落地）
1. **搬 trading_calendar.py + exchange-calendars** → VERA 交易日判断（淘汰手动节假日表）
2. **搬 GracefulShutdown** → trade_main.py 重启信号处理
3. **搬 DeepSeek thinking extra_body** → llm/providers.py（deepseek-chat 开 thinking）
4. **进程内 schedule** → trade_main.py 加每日定时（政策爬取 P1b 触发）

### 第二周（LLM 层升级）
5. **升级 llm/providers.py** → 多 key 轮询 + fallback（借鉴 Router 思路，不引 litellm）

### 第三-四周（P3 对话大脑 MVP）
6. **brain/agent.py** → ReAct 循环（借鉴 run_agent_loop）
7. **brain/tools/** → VERA 工具（kg.query / policy_pipeline / concept_kb / 行情）注册
8. **brain/context.py** → AgentContext 协议袋
9. **研究 TAB 升级** → 接 brain/（政策影响查询 → 对话大脑，自然语言问）

### 后续（验证后）
10. Skill YAML（VERA 公式策略写 skill）
11. SSE 流式（打字机体验）
12. multi-agent（仅当单 Agent 深度不够）

---

## §5 关键决策（为什么"借鉴模式自研"而非"搬代码"）

| 决策 | 理由 |
|---|---|
| **不引 litellm** | VERA 只 DeepSeek，requests 够；litellm 多 provider 是重依赖，违反"不加中间件"铁律 |
| **不用 GitHub Actions** | 云端运行违反"本地部署优先"（策略/资金机密不外发）；用进程内 schedule |
| **先单 Agent** | 多 Agent 4 倍成本（30+ LLM 调用/股），VERA 个人 DeepSeek key 有限；先单 Agent + 多工具验证 |
| **VERA 工具化（kg/policy/concept）** | 这些是 VERA 差异化（知识图谱 + 政策 + 概念），做成 Agent 工具而非新 Agent |
| **trading_calendar 直接搬** | exchange-calendars 库权威（节假日不自己维护），218 行无重依赖，直接搬省事 |
| **MIT 致谢** | 整合后 README/docs 注明来源（daily_stock_analysis, ZhuLinsen, MIT） |

---

## §6 与现有计划书（v5）的关系

| 计划书 v5 项 | 整合方案影响 |
|---|---|
| P0 知识图谱 | ✅ 已完成，做成 Agent 工具 `query_knowledge_graph` |
| P1 政策流水线 | ✅ P1a 完成；**P1b 政策源用进程内 schedule**（不用 RSSHub Docker，借鉴 daily_stock 定时模式） |
| P1.5 概念层 | ✅ 已完成，做成 Agent 工具 `query_concept_kb` |
| P2 Obsidian | 不变 |
| **P3 对话大脑** | **重定义**：借鉴 daily_stock ReAct + 工具模式自研 `brain/`（不用 Agent SDK，借鉴 ReAct）；单 Agent + 多工具；H-4 沙箱仍守（容器隔离 trade/） |
| 保持更新 | **trading_calendar + schedule 三件套**（替代之前 P1b RSSHub Docker 方案） |

**P1b 数据源决策更新**：之前计划书 v5 写"RSSHub Docker / gov.cn 爬"，整合方案改为**进程内 schedule + 政策爬取**（借鉴 daily_stock 定时模式，不用 RSSHub Docker 重运维）。

---

**整合方案 v1 结束。待用户 review + 拍板实施顺序。**
