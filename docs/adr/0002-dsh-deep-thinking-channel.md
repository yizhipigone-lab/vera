# ADR-0002: 研究大脑 DSH 深度思考通道（数据出网例外 + 检测型控制）

- **状态**:已接受 (Accepted) — 2026-09-04 用户拍板
- **决策者**:用户
- **关联**:复刻 IRX ADR-011（`E:\1target\IRX\docs\adr\`）/ [计划书](../plan/2026-09-04_研究大脑DSH深度思考通道_计划书.md)

## 背景

研究 TAB 对话大脑（brain/claude_cli.py）已带全工具（Bash/Write/Edit，
`--dangerously-skip-permissions`，2026-08-01 用户拍板），上下文本就发给外部 LLM
provider。本次新增 DSH 通道（DeepSeek 官方 harness，便携运行时驻留仓内
`dsh-runtime/`）作为勾选制升级路径，**不扩大出网面**，但补齐检测型控制。

**查证事实（2026-09-04，读代码确认）**：

- `archive_exchange` 只在 `ask_brain` 内部调用（claude_cli.py:133），新通道必须自补 vault 沉淀。
- VERA 无 Postgres，留档落 SQLite（stdlib，零新依赖）。
- uvicorn `--reload` 的 Selector 循环不支持 Windows asyncio 子进程（claude_cli.py 注释在案），server.py 现状 Proactor 已满足。

## 决策

1. 升级制路由：未勾选走现有大脑（零改动），勾选「🧠 深度思考」才进 DSH。
2. DSH 全工具面不裁（与现有大脑同级）。
3. 检测型控制为底线：每问全量留档 `data/brain_dsh_runs.db`（会话日志
   SHA-256 + 泄漏关键词扫描），leak_hits 非空即告警；**留档失败 = 显性失败**。
4. DSH 回答同样沉淀 vault 对话归档（archive_exchange，不破 2026-07-28 铁律）。
5. 应急开关 `brain/dsh_channel.py` 的 `DSH_CHANNEL_ENABLED=False` 一键停用。
6. `dsh-runtime/` 整个不入 git（含 node 二进制/凭据/会话日志）。
7. UI 形态：留在研究 TAB 对话卡片内做通道开关，不做独立 TAB / 全局侧边栏
   （理由见计划书 D11；抽屉 + page_context 注入列二期候选）。

## 后果

- 正面：停止按钮、便携部署、出网留痕三件套到位；成本走 deepseek-v4-flash 更低。
- 代价：每轮约 10s 冷启动 + 历史打包 token（摇醒机制，无会话续接）。
- 边界：单用户本地系统，例外成立；若未来多用户化必须重估。
