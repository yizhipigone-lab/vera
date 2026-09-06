# VERA 项目上下文

> 跨会话持久上下文。新会话先读这里,避免重复踩坑。具体代码坑以 tests/ 和 git 历史为准,这里只放不会过时的铁律。

## 项目定位

- **VERA** = 个人实盘管理后台 + AI 驱动策略平台
- **已实盘,处于脆弱期**:v1/v2 跑通,部分策略实盘中,稳定性与工程化是当前瓶颈
- **本地部署优先**(不云端);策略与资金量级 = 高度机密;董秘/公司身份 = 公开
- **记忆/知识本地持久化(铁律, 2026-08-18 用户拍板)**:项目知识与会话记忆一律本地持久化,**禁用云端记忆**。Hindsight 已切**本地 daemon 模式**(`~/.hindsight/coding-agent.json` 的 `serverMode: daemon`, 本地服务 127.0.0.1:9077, 数据在 `~/.pg0/instances/`, 插件在 `~/.dsh/cordis.patch.yml`);本地文档(`research/` `docs/` `notes/`)仍是兜底。事实抽取 LLM 走 **DeepSeek `deepseek-v4-flash`**(`.env` 的 `DEEPSEEK_API_KEY`);视觉读图走 **modlens + GLM `glm-4.5v`**(deepseek-v4-pro 是纯文本, 看不了图, 必须靠 modlens 转文字)。**坑**: 厂商 detectLlm 用 Unix `which`(Windows 误判无 LLM → daemon 不启动, 须显式设 `HINDSIGHT_API_LLM_PROVIDER`)、GBK 编码(须 `PYTHONUTF8=1`)、huggingface 被墙(须 `HF_ENDPOINT=https://hf-mirror.com`)、daemon 5 分钟空闲退出(须 `daemonIdleTimeout:86400`)。完整照着做手册: `docs/2026-08-18_Hindsight记忆与modlens视觉本地化配置_全过程记录.md`。

## 业务铁律(不可违反)

1. **宏观/地缘事件**:只生成报告提示,**绝不联入仓位调度** —— 用户保留人工把关
2. **回测买入价(铁律)**:**默认尾盘选股 → 信号日(T日)收盘价买入**。唯一例外: 显式配置 `entry_price_mode=open_t1`(2026-08-20 用户拍板, 解决 QUANTQQ 尾盘涨停拒单丢信号)时按 **T+1 开盘价**买入, T+1 一字涨停(OHLC 四价合一且达涨停价)拒买, 此模式下 T 日收盘涨停过滤自动关闭。两口径结果**不得混排比较**, 结果带 `entry_mode_info` 口径标注。计划书: `docs/plan/2026-08-20_回测次日开盘买入模式_计划书.md`
3. **两套买卖口径别混**:回测走信号日 T 收盘路径;实盘走 sim_trader 的 T+1 开盘路径。审计 bug 时**先确认在查哪条路径**,别审错
4. **策略评价默认同时给**:夏普、Calmar(用户口里的"夏普率")、最大回撤、胜率
5. **关注市场**:A 股 / ETF / 可转债、美股 / 港股、跨市场联动
6. **风险偏好**:中等回撤 + 多策略组合

## 实盘交易铁律(2026-07-26 写入,trade/ 包,源自三项目对比研究)

1. **QMT 是持仓/资产/成交的唯一真相源**;对账只告警+熔断,永不自动改账、绝不自动重发
2. **回调线程只做入队**:禁止在回调线程调 xtquant 同步接口(官方死锁坑)、写 DB
3. **交易状态唯一写者 = EventEngine 消费者线程**;业务代码零锁;清仓锁是业务防重入锁,保留
4. **回测与实盘共用同一份卖出规则与参数定义**(当前为同口径参数+parity 路径,规则核心合并为 P2 候选),禁止第二份实现
5. **原始回报先落盘(JSONL)再处理**;重启先对账再交易;行情故障 fail-closed(宁可不卖,不可瞎卖)
6. **预埋单报价锚定最新买一**,档位价超当日涨停价则今日跳过该档、不撞 2% 价格笼子
7. **kill switch 三重态(内存/DB/文件)任一生效即全面拒单**,只许人工解除
8. **如无必要勿增实体**:不加进程、不加中间件、不加转发层、不写第二份规则;trade/ 模块公开接口以"能一口气读完"为准,**gateway/store 两个基础设施模块例外**(契约面即接口面:gateway 的抽象方法就是柜台契约本身,store 的方法是持久化契约,拆成多个类只会加转发层),其余模块 ≤8 个方法(2026-07-26 审计 M7 裁决:修文档口径,不拆模块;2026-08-16 M7 重论证:允许 store 同文件内按表域内聚出子 store——共享连接、父类管 DDL,不加转发层,见 eb4cf4b)

## 架构骨架

| 模块 | 路径 | 职责 |
|---|---|---|
| 公式因子体检 | `tools/formula_lab.py` | 任意公式一条命令做因子体检(S0-S5):IC 筛选 → 族归纳 → A/B 终审 → 报告。方法论见 `docs/公式因子体检方法论.md`(五条纪律:IC 非终审/双窗口/数族不数因子/因果测试/必带警告) |
| 公式体检页面 | `core/lab_runner.py` + `/api/lab/*` | web 独立页签版体检(2026-07-20):严格串行队列 + baseline 自动串联 + 与回测不对称互斥(体检永远排队,回测提交在体检中 → 409)。规则 JSON 供回测页因子过滤区按公式动态渲染 |
| 回测引擎 | `backtest/engine.py` | 主回测循环:信号→成交→止损止盈→权益曲线。核心循环在 `backtest/loop/BacktestLoop`(2026-07-14 候选 A 阶段2);2026-08-01 批次 3b C2: run/run_cached 的 priority 校验/trailing 缺省/时间缩放/ATR/build+run 并入共享段 `_resolve_stop_and_build_loop`(防双入口漂移),`_simulate_core_v3` 兼容壳与 legacy 甲骨文均已删除,数值级回归由快照基线 `tests/test_snapshot_parity.py` 承担(生成器 `tools/gen_snapshot_parity.py`)。`run_cached` 加厚前门(2026-07-13 候选 A 阶段1,980b04f):9 旧位置参数不动 + 9 keyword-only 能力参数,能力按 `stop_config["capabilities"]` 三开关透传。`run` 走 Pipeline 收口路径。**5m 数据层降级(2026-07-18)**:`degrade_5m: true` 时缺 5m 的股-天用 1d OHLC 填满 48 根 bar 保信号(`backtest/degrade_5m.py`),降级影响报告在 `result.degradation`(`backtest/degrade_report.py`);仅 period=5m + run() 路径(2026-07-26 守卫改 `bars_per_day == 48`:原 >1 会被 1m 踩中静默全错)。**2026-07-21 区间精确化(ENGINE_VERSION v3.5)**:执行窗口=请求区间(窗口 end_time 截断,不再 +win_td 尾巴);降级网格起止=请求区间(5m 深度前也 1d 填充);degrade_5m 配置默认开;期末未平仓按市值计价不强平并导出 `open_positions`;基准对比在指数 5m 深度不足时回退日粒度。**1m 支持(2026-07-26, 计划书 `docs/plan/2026-07-26_1分钟线回测支持_计划书.md`)**:bpday=240 + `STD_1M_BAR_TIMES` + `_drop_nonstandard_intraday_bars` 泛化(旧 5m 名保留 alias,外部 2 调用方);区间硬限 ≥20260126 截断+告警;kline_cache 分钟级泛化 + `_fetch_and_store` ≤80 交易日分段(TDX 单次 ~24000 根上限;`_get_calendar()` 返回 set 必须先排序);matrix_cache 1m keep=2;degrade 对 1m 强制关+warning |
| 选股 | `selection/selector.py` | 股票池筛选(ST/退市/港股按 TDX 真实标记, 北交所口径剔除; **涨停不在选股排除**——涨停过滤在 engine 入场 `_filter_limit_up`, 默认开) |
| 选股结果缓存 | `selection/selection_cache.py` | 2026-07-24(计划书 `docs/plan/2026-07-24_选股结果缓存_计划书.md`):整段缓存 `step1_select` 输出(parquet,LRU 10)。key=公式+universe 完整配置(假值默认键归一化,web/yaml 路径收敛)+区间+period+复权+today_str(按日失效)+SCHEMA_VERSION;不纳入 universe 实际输出列表哈希(算它要先花 17s,R8 权衡),日内 ST 漂移由按日失效掩蔽+`selection_cache.force_refresh` 兜底。实测 5m 全A:选股 32.7s→0.01s,总 35.3s→2.2s。空结果不缓存;命中也写 raw CSV(R9);tools/* 直调 StockSelector 不经接缝不受益 |
| 池缓存+按日信号缓存 (二期) | `selection/universe_cache.py` + `selection/signal_day_cache.py` | 2026-07-26(计划书 `docs/plan/2026-07-26_选股缓存二期_L1池缓存_L2按日信号缓存_计划书.md`,接缝在 selector 内部,tools 自动受益)。L1: resolve_universe 输出按日缓存(json,LRU 10),省拉池+ST过滤 ~17s。L2: 信号按(公式+池内容哈希+1d+复权)×交易日 parquet 存储;全命中(子区间/历史并集覆盖)零公式调用,任一缺失→整段重算按天入库(e2e 实测推翻"按缺失区段补算":TDX 51 批固定地板 ~16s 与扫描量几乎无关,区段补算不省钱)。安全线:当日永不缓存;最近2交易日条目仅当日命中(mtime 判);>60 天重算(除权漂移);批次失败区段不落盘(`FormulaRunner.last_batch_errors` 区分真空/失败空)。实测:子区间 0.03s,同区间重跑 0.15s,与直跑 parity 一致。**2026-07-27 投毒事件**:test_sector_selection mock 3 股池经接缝写入真实 data/universe_cache(key 与用户 QUANTQQ 配置相同)→ 用户回测 5003 只变 3 只仅 21 笔。修复:conftest autouse 隔离四个缓存模块 default_cache_root 到 per-test tmp(全量测试后生产缓存目录必须为空)+ selector L1 命中 <10 只告警 |
| 细粒度进度 | `core/progress.py` | 2026-07-26:全局模块状态报告器(report/snapshot/reset,无人读时 ~1µs no-op)。深层循环埋点: ST过滤(stock_filter)/公式批次(formula_runner)/取数(kline_cache+data_fetcher 窗口批)/核心loop(每100bar)/engine 边界。锚点 ANCHORS 映射全局百分比(选股 10-45,实测占 92% 耗时),done/total 速率法 ETA。server `/api/status` 融合(粗 _cb 与细粒度取 max,单调不回退 guard `_last_served_pct`,additive 字段 detail/eta_s,STAGE_NAMES 替换粗 step 名);前端缓动逼近+文字"阶段 · 批次 x/y · 预计剩余"。不改 progress_callback (pct,step) 契约 |
| 止损管理 | `backtest/stop_config.py` | 止损/止盈/移动止盈/阶梯止盈。`stop_config.py` 兜底含 priority + capabilities 字段(2026-07-13 修复)。stop_manager.py 已于候选 D C2 删除。**卖出冷却(2026-07-23)**: engine 配置 `sell_cooldown_days`(交易日,默认0=关,零行为变化),全清仓后 N 个交易日内禁止同票重新买入,持仓中换股(reason=1)不受限;loop 层参数 `sell_cooldown_bars`(=days×bpday),跳过计数在 `sell_cooldown` 日志。信号层 30 日首信号过滤在 `selection/signal_rules.py`(工具函数,非引擎默认行为) |
| 复权口径 | `core/dividend_type.py` | **统一 int/str 映射(候选 D,0b47db5)**:DataFetcher/FormulaRunner 内部用 `to_tdx_str`/`to_formula_int` 归一化,允许混传。`assert_consistent` 由 pipeline.py:101 调用 |
| 公式系统 | TDX 公式翻译 + `core/formula_runner.py` | 通达信公式执行封装,统一入口。批量脚本 `batch_*.py` 大部分已删(2026-07-13 清 35 个废弃脚本) |
| Web 后端 | `server.py` | API + 进度反馈。**现状(2026-07-14 已完成)**:`/api/run` 走 `Pipeline.run` + `ResultWriter`（统一完整流程接缝，2026-07-14 372f59b）；进度回调由 `ResultWriter.on_progress` 驱动 `pipeline_status` 单例（不再手工赋值）；`PipelineResult` frozen dataclass 统一返回结构。C5 真实盘口验证通过（路径 A/B 数字字节级一致）。 |
| Web 前端 | `web/index.html` + `vera-ui.js` | 管理后台 UI |
| 测试 | `tests/` | pytest 套件,改核心函数后必跑。守卫式 + 字节级 parity + 能力透传 + 默认值锁 + 复权口径边界 + 进度回调签名 |
| 报告推送 | `tools/send_report_feishu.py` | 2026-08-23:任意 MD 报告一条命令推飞书卡片(webhook 读 `.env` 的 FEISHU_WEBHOOK_URL,半密钥不打印)。表格转「｜」文字行、代码围栏转缩进、长文按段落自动拆卡(≤8KB/片,标题带 n/N)、逐卡校验返回 code==0。**飞书自定义 bot 卡片不支持 HTML/本地图片**(图片需 image_key,自定义 bot 无上传接口),图表只能注明本机路径。实证:2026-08-23 智能化深化研报 4+1 卡推送成功;P1a 研究包的投递组件直接复用它 |
| 报告投递(邮箱) | `tools/send_report_workbuddy.py` | 2026-08-24:任意 HTML 报告一条命令投递到本机 WorkBuddy 的 agent 信箱(`python tools/send_report_workbuddy.py 报告.html [--subject] [--inline]`)。走 WorkBuddy 本机 connector-proxy(127.0.0.1:64079,MCP over HTTP),token 从运行中的 WorkBuddy 进程命令行现取不落盘不打印,HTML 作正文+附件发到 `agent-mail_GetMe` 拿到的本人邮箱。仅 stdlib;WorkBuddy 需在运行,且 agent 邮箱须已在「更多→我的邮箱」开通(注销时报 MailboxDeactivatedError 带指引)。与飞书互补:飞书给卡片、邮箱给可下载打开的完整 HTML |
| 报告投递(任意邮箱) | `tools/send_email.py` | 2026-08-24 用户要求的**独立能力**:标准 SMTP 发任意文件到任意邮箱(`python tools/send_email.py 文件 [--to 收件人] [--subject] [--inline]`),默认收件人 jayziheng@agent.qq.com。发件账号/授权码读 `.env` 的 `SMTP_USER`/`SMTP_PASSWORD`(半密钥不打印),host 默认 smtp.qq.com、587 STARTTLS 失败回退 465 SSL。**QQ 邮箱对外 SMTP 用「授权码」不是登录密码**(认证失败会明确提示)。与 workbuddy/飞书三路互补:workbuddy 只发自己、飞书给卡片、本工具发任意人 |
| 名称缓存 | `core/data_fetcher.py::get_name_map` | {代码:简称} 全量映射(~6300条):进程级缓存 7d TTL,**TDX(主) → 腾讯 qt.gtimg.cn(备,2026-08-27 新增,代码全集取自 kline_cache 清单,批量报价拼名称)**;失败/空表不缓存下次重试(2026-08-27 页面简称全丢事件修复,此前空表被永久缓存)。**教训:服务可在 TDX 未开时启动,名称会随 TDX 就绪自动恢复** |
| 影子校尺 | `trade/shadow.py` + `tools/shadow_compare.py` | 2026-08-23 P0(源自当日 MA20vs动量研判):旧 MA20 三态(已迁 `trade/legacy_three_state.py::compute_signal`,2026-09-05 治理III W2 自 rotation 迁出)作影子策略,`RotationFeature._shadow_tick` 每日落盘 `data/shadow_rotation.jsonl`(**只记录不交易**,fail-soft 绝不影响交易链);对比工具 TDX→新浪降级取数,同尺重放两套规则算季度滚动 90 日收益,**影子连续 2 季赢动量超 10pp → 提示人工复审换规则**。**2026-08-24 口径修正**:重放口径由「T 日信号当天生效」改为「T+1 生效」(原口径对高频策略系统性乐观,交易越勤偏差越大);修正后首次裁决反转——动量 +187.9% vs 影子 +146.0%(2.7 年窗),**影子未连续跑赢,无需复审**;此前「影子连 3 季跑赢」系口径偏差假象(用户「T日还是T+1成交」一问发现,详见 research/2026-08-24 组合寻优报告第六节) |
| TQ 数据通道 | `core/tdx_tq.py` | 通达信 TQ-Python 只读数据薄封装 (2026-08-28, 源自 TDX SkillHub 研究):**懒加载**(模块导入零 TDX 依赖,测试/server 起动不需要通达信) + fail-soft(异常一律 None/[],研究数据绝不抛) + 纯归一化函数(`norm_etf_list/norm_stock_ext/norm_snapshot` 可独立单测) + 模块锁串行(tqcenter 类级连接非线程安全)。7 公开接口 ≤ 铁律 8:`available()/etf_of_index(指数→跟踪ETF,代码用 .SH/.SZ;部分 .CSI 宽基如 000300/905/852 服务端返回空,特殊 .CSI 如 950162 反而通)/stock_ext(get_more_info 88 字段:市值/涨停跌停价/ZAFPre2D~60D 多日涨幅/换手)/snapshot(实时+基金净值 Jjjz)`。**数据面实测口径**(诊断脚本 `research/tdx_skillhub/tq_probe_诊断脚本.py`,2026-08-28):FN 专业财务/SC 市场统计/BK 板块统计**需客户端先下载数据包**(未下时报空或 NoneType 崩);BK 板块代码须带 `.SH` 后缀;kzz 按**转债代码**查(传正股报错);**交易接口一律不封装**(QMT 唯一通道铁律)。SKILL 说明书落仓 `research/tdx_skillhub/skills/`;tdx-tq-local(HTTP 17709)本机 v7.73 不通弃用。消费方:ETF 行业轮动扩容的指数→ETF 映射、14:50 研究的估值/动量快照 |
| 实盘交易 | `trade/` + `trade_main.py` | 2026-07-26 P1 MVP(计划书 `docs/2026-07-26_实盘交易系统计划书.md`):QMT 实盘交易,单进程单写者 EventEngine。9 模块:gateway(xtquant 唯一收口+FakeGateway)/events(静态接线)/store(SQLite WAL+JSONL 原始回报)/book(账本+状态机)/executor(预埋单+撤单流水线+两级价格阶梯)/monitor(订阅+心跳+QMT 轮询降级)/reconciler(三方对账只告警不回写)/risk(5 道闸+三重态急停)/api(薄层,独立 8081,交易页为 web 第三页签)。税费未计(P2 回溯补算)。止盈止损复刻回测 stop_loss 结构(parity 测试锁死);风控做减法(集中度闸已砍,sizing 校验接替);设置面板热生效(账号/路径类需重启)。**2026-07-27 ETF 误卖事件后**:时段感知(自动规则仅连续竞价,人工命令任何时段放行;心跳分时段+页面人话原因)、ETF 不纳入自动管理(照常对账)、手工成交对账认领(不拉闸)。**尾盘自动买入 MVP(2026-07-27)**:14:52 TDX 公式选股自动买入(trade/signals.py 桥+工作线程,不堵唯一写者),T+1 次日自动接入预埋/监控,设置面板可配可关。**尾盘价格市场感知(同日实测五连废单驱动)**:深市 14:57 后收盘竞价只收限价单——买挂涨停价/卖挂跌停价(单一价格撮合,成交价=收盘价),沪市维持对手最优。**冷启动 -1 事件(2026-09-02)**:机器重启后 XtMiniQmt 刚起 62 秒就启 trade_main → `connect()` 返回 -1 崩溃;QMT 登录初始化需 30s~2min,**先等 QMT 就绪再启 trade_main**(同路径独立连接测试 rc=0 证实,非代码/配置问题) |
| AI 设置 | `llm/ai_config.py` + `ai_api.py` + 对话大脑三档接线 | 2026-09-06 web 独立页签「AI 设置」(回测/公式体检/公式农场/交易/分析/交易记录/研究/数据准备之后的第 9 页签):配置对话大脑三档 API Key/接入地址/模型,存 `config/ai.json`(不入 git,Key 只打码回传),**保存即热生效无需重启**。三档各按各的协议:**快速档**(OpenAI 兼容,`llm/providers.py` 每次 chat 现读 fast 段 → 影响快速对话+政策提取 extractor+交易复盘 llm_review)/**标准档**(Anthropic 兼容,`brain/claude_cli.py` spawn 时按 standard 段注入 `ANTHROPIC_BASE_URL/AUTH_TOKEN/MODEL` env,不碰 ~/.claude 原文件)/**深度档**(DSH 适配器,`brain/dsh_channel.py` spawn 前按 deep 段改写 dsh-runtime settings 的 agent-default-model)。合并语义唯一实现 = `ai_config.merge_patch`(纯函数:字段非空覆盖、空串保留旧值、`__clear__:true` 整档清空回落默认;三档共用 `_FIELDS` 防手写漂移——曾因 deep 档 `is-not-None` 写错静默清空,已修复有测试锁)。无配置时三档完全回落原默认(零行为变化,有测试锁)。审计:docs/audit/2026-09-06_AI设置页签_审计报告.md |

**历史背景**:`_simulate_core_v3`(39 参数私有函数)曾是事实公共入口,被 4 脚本 + 4 测试直调。候选 A 阶段 1 + 阶段 1.5 收编 5 脚本 + `optimize_strategies` 收编 + 清理 `optimize_full` 死 import,**生产直调完全清零**(锁私有完整达成,2026-07-13 e62e0ab)。候选 D C4 清理 34 个孤儿脚本(2026-07-14 aa54d19);2026-08-01 批次 5 C3 删孤立的 preprocessor.py,根目录仅余 main.py / server.py 两入口。候选 D C2 删 `stop_manager.py`(死代码,无调用方)。**批量注意**:`Pipeline.run()` 每次执行 `initialize+close`,不适合 in-process 高频复用;批量场景用 subprocess 并行调度 `tools/gs_run_one.py`。

## 战略方向(2026-07-26 轴间实验已判定)

**两层架构(先选塘再下竿) = 第一层择塘 → 第二层池内选股**。轴间对照实验结论(`tools/pond_axis_experiment.py`, 数据 `output/pond_axis/report.json`, 方法: 20日动量/周调仓/单票10万/2024-01~2026-07, GP1014+超赢王牛股双公式交叉验证):

1. **行业轴(128个881板块)对头部10公式 0 胜出**: 扩样验证 (2026-07-26, `output/pond_axis/report.json`) — 10 个头部公式无一以行业轴为最优轴, 9/10 动量选行业不赢随机行业 (z: -6.2~+1.4, 仅两涨一缩 z=+2.1 边缘但仍被板轴压制)。**结论: 行业轴不作为默认轴, 但保留在定轴流程里逐公式检验** (题材型未测公式可能例外)
2. **板轴(主板/创业/科创/北交)是默认最优轴**: 10 公式中 6 个以板轴为最优, 全部 10 个板轴 > 随机选板。指数轴同向但恒弱于板轴 (0 最优)
3. **第一层必须按公式配轴(可插拔), 不能一刀切**: 全A最优 4/10 (超赢王牛股/财富金生！/绝底/GUPIAO_018 — 厚边际高换手), 板轴最优 6/10 (薄边际)。**每个公式上线前必跑 `tools/pond_axis_experiment.py` 定轴 (全A/板/指数/行业), 数据说话, 不凭公式类型猜**
4. **动态择板跑赢随机板, 但不保证跑赢事后最优静态板** (超赢王: 动态 +1240% < 静态主板 +2283%) — 择塘赚的是"不站错队"的钱, 不是"永远站最优队"的钱

**多重检验校正(Deflated Sharpe 等) 用户拍板不做, 勿再提。**

**板块缓存 TTL(已实施 2026-08-01 D6, 本条目过时留档)**: `core/data_cache.py` 板块列表/成份股 24h、简称映射 7d 惰性过期(`DataCache`, has_* 读侧判过期→回源重拉 TDX), 测试 `tests/test_data_cache.py::TestDataCacheTTL` 锁定。跨自然日语义以 24h 绝对 TTL 近似(成份调整日级频率, 偏差可接受)。

## 协作风格(用户四禁,违反即止损)

0. **大白话 + 打比方(2026-07-26 用户明确要求)**: 跟用户交流一律先说人话——复杂概念必须先打比方、用生活例子讲明白, 再补专业术语; 禁止甩术语堆砌。这条在"四禁"之前
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

### 开工前深模块检查单(2026-09-06 用户拍板铁律)

**写任何新模块/重构/新功能前,先过这五问(约 30 秒),答不上来别动手:**

1. **放哪层?** 逻辑该进深模块(领域实现)还是薄层(路由/门面转发)?项目惯例 = 路由薄 + 实现厚(data_cache_api 薄 / kline_cache_maintenance 厚;ai_api 薄 / ai_config 厚)。计算类逻辑一律下沉纯函数模块。
2. **接口多大?** 能不能更小?≤8 个公开方法/函数(铁律 8)。新公开符号是否真的要被外部用?纯测试需要的就加 `_` 前缀当内部接缝。
3. **有没有同类第三份?** grep 一下同语义逻辑是否已有实现——**同一条规则手写 N 份必然漂移**(教训:AI 设置三档合并手写三份,deep 档判断符写错静默清空;trade 买卖口径、行情陈旧判定都犯过)。发现第二份以上 → 收口单一真相源。
4. **删除测试**: 删掉这个模块,复杂度会散回调用方(该留)还是原地消失(是透传,该并)?
5. **改完登记没?** 新子系统/新页签 → CLAUDE.md 架构骨架一行 + CHANGELOG 条目 + 及时 commit(三件套,做完=登记完)。

设计词汇(深/浅/接口/接缝/删除测试/locality)以 codebase-design 为准,模糊时先调该 skill。

### 沉淀经验(2026-09-06,源自 AI 设置功能审计)

1. **同一条规则手写 N 份,必然漂移**: 三档合并语义手写三遍,deep 档一处 `is-not-None` 写错 → 留空保存静默清空配置,全靠测试才抓到。规则多副本是 VERA 反复踩的坑类(买卖口径×2、行情陈旧判定×3、下单逻辑×5),每次发现新副本都应收口,不单修。
2. **做完 ≠ 登记完**: Definition of done 含"登记 CLAUDE.md/CHANGELOG/commit"三件套,但执行常漏(本次 AI 设置就漏了,靠登记体检才补上)。新功能收尾时把三件套当验收项逐条打勾。
3. **环境红 vs 真红**: TDX 关着(Sunday)、TQ 连接关闭提示、DeprecationWarning 都是"可证明无关"的环境噪音;判据 = 退出码 + 触及路径是否绿,别被噪音带偏也别拿噪音当借口。机制(退出码/机器校验)比自觉可靠。
4. **Key/Token 半密钥惯例**: 界面可配的密钥只落 gitignore 文件(config/ai.json 因 `*.json` 天然不入库),读取只回打码(前4+****+后4),绝不明文回传;保存留空=保留旧值(不清空),显式清空走 `__clear__` 标记。

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:
- 写新模块/重构/设计模块接口 → invoke codebase-design（深模块词汇与原则, 开工前检查单见工作流约定）
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
