# Hindsight 记忆 + modlens 视觉 本地化配置备忘 (2026-08-18)

> 换机器 / 重装时的照着做手册。踩过的坑都在里面。目标铁律：**记忆不落云端，本地持久化**。

## 一、Hindsight 记忆本地化（daemon 模式）

### 目标与原理
- Hindsight 是记忆系统，有三种模式：`cloud`（云端，欠费）、`self-hosted`（自己跑 docker）、`daemon`（本地守护进程，本机自动拉起）。
- 本项目用 **daemon 模式**：记忆数据 + 向量模型全在本地，DSH 会话启动时自动拉起本地服务（127.0.0.1:9077），空闲自退。

### 配置文件 `~/.hindsight/coding-agent.json`
```json
{
  "apiToken": "<旧云端 token, daemon 模式用不到, 保留无妨>",
  "serverMode": "daemon",
  "apiUrl": "http://127.0.0.1:9077",
  "daemonIdleTimeout": 86400
}
```
- 插件注册在 `~/.dsh/cordis.patch.yml`，指向 `~/.hindsight/coding-agents/dist/dsh.js`。
- 数据落盘：`~/.pg0/instances/hindsight-embed-coding-agent`（本地内嵌 PostgreSQL + pgvector）。
- 向量化：本地 `BAAI/bge-small-en-v1.5` + reranker `cross-encoder/ms-marco-MiniLM-L-6-v2`。
- 提炼 LLM（把 git 历史/对话生成知识页）：**deepseek-v4-flash**（用 `.env` 里的 `DEEPSEEK_API_KEY`）。

### 关键环境变量（已 `setx` 持久化到用户环境）
| 变量 | 值 | 用途 |
|---|---|---|
| `PYTHONUTF8` | `1` | 修中文 Windows GBK 编码崩 |
| `PYTHONIOENCODING` | `utf-8` | 同上 |
| `HF_ENDPOINT` | `https://hf-mirror.com` | huggingface 被墙，换国内镜像 |
| `HINDSIGHT_API_LLM_PROVIDER` | `deepseek` | 提炼 LLM 用 DeepSeek |
| `HINDSIGHT_API_LLM_MODEL` | `deepseek-v4-flash` | 模型名 |
| `HINDSIGHT_API_LLM_API_KEY` | `.env` 的 `DEEPSEEK_API_KEY` | key |

### 踩过的坑（换机器必看，否则复现报错）
1. **厂商 detectLlm 用 Unix `which` 命令**，Windows 上没有 → 误判"无 LLM" → daemon 直接不启动（`preflightDaemon` 返回 false）。**必须显式设 `HINDSIGHT_API_LLM_PROVIDER`** 绕过 `which` 检查。
2. **中文 Windows 默认 GBK 编码**，Python（hindsight-embed）读 UTF-8 数据崩 → 必须 `PYTHONUTF8=1`。
3. **huggingface.co 被墙**，首次冷启动下载 embedding/reranker 模型报 `WinError 10061` → 必须 `HF_ENDPOINT=https://hf-mirror.com`。
4. **daemon 默认 5 分钟空闲退出**（`daemonIdleTimeout: 300`），记忆总是掉线 → 改 `86400`（24h）。
5. **云端欠费**（`api.hindsight.vectorize.io` 报 402）是切 daemon 的动机，不是 bug。
6. **DSH 必须真重启**（关掉 node 后端进程再重开，不是刷浏览器），否则 `hindsight_*` 工具还攥着旧云端地址。

### 手动拉起 daemon（自动启动失败时）
```powershell
$env:PYTHONUTF8="1"; $env:PYTHONIOENCODING="utf-8"; $env:HINDSIGHT_API_LLM_PROVIDER="deepseek"; $env:HINDSIGHT_API_LLM_API_KEY=<key>; $env:HINDSIGHT_API_LLM_MODEL="deepseek-v4-flash"; $env:HF_ENDPOINT="https://hf-mirror.com"
& "D:\Program Files\DSH\.node\node.exe" "C:\Users\liuziheng\.hindsight\coding-agents\dist\daemon-start.js" --harness dsh
```
健康检查：`http://127.0.0.1:9077/health` 返回 `HTTP 200` 即就绪。日志：`~/.hindsight/profiles/coding-agent.log`。

## 二、modlens 视觉（让我能读图）

### 原理
- 我（deepseek-v4-pro）是**纯文本模型，看不了图**，靠 modlens 把图转成结构化 JSON 文字再喂给我。
- modlens 支持的视觉 provider：`antigravity-cli / gemini-api / openai / anthropic / claude-cli`。

### 配置 `~/.modlens/config.json`
```json
{
  "provider": "openai",
  "providers": {
    "openai": {
      "apiKey": "<GLM key>",
      "baseUrl": "https://api.z.ai/api/coding/paas/v4/",
      "model": "glm-4.5v"
    }
  }
}
```
- GLM key 来源：用户提供，或从 cc-switch 库 `~/.cc-switch/cc-switch.db` 的 `providers` 表 `settings_config`（JSON 的 `env.ANTHROPIC_AUTH_TOKEN`）里取。

### 踩过的坑
1. **`glm-5.2` 和 `deepseek-v4-pro` 是纯文本模型**，读图报 `messages.content.type is invalid, allowed values: ['text']`。**必须用 `glm-4.5v`（视觉模型）**，同一个 key 就能调。
2. **`claude-cli` provider 在 Windows 上 spawn `.CMD` 报 `EINVAL`**（Node 的 .cmd spawn bug），且用户没有真 Claude 账号——绕开它，直接用 `openai` provider 接 GLM 的 OpenAI 兼容接口。

### 测试
```powershell
npx --yes @liustack/modlens analyze -i <图片路径>
```
成功会返回 `{"image":..., "result":{"summary","ocr","layout",...}}`。

## 三、cc-switch 说明（背景）
- cc-switch 把 Claude Code 通过本地代理路由到第三方模型。
- 当前 Claude provider = **DeepSeek**（`deepseek-v4-pro`，文本）；另有 **Zhipu GLM**（`glm-5.2`，文本）。**两者都无视觉**，视觉要用 `glm-4.5v`。
