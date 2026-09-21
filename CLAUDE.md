# VERA 项目上下文

> 跨会话持久上下文。新会话先读这里,避免重复踩坑。具体代码坑以 tests/ 和 git 历史为准,这里只放不会过时的铁律。

## 项目定位

- **VERA** = 个人实盘管理后台 + AI 驱动策略平台
- **已实盘,处于脆弱期**:v1/v2 跑通,部分策略实盘中,稳定性与工程化是当前瓶颈
- **本地部署优先**(不云端);策略与资金量级 = 高度机密;董秘/公司身份 = 公开
- **记忆/知识本地持久化(铁律, 2026-08-18 用户拍板)**:项目知识与会话记忆一律本地持久化,**禁用云端记忆**。Hindsight 已切**本地 daemon 模式**(`~/.hindsight/coding-agent.json` 的 `serverMode: daemon`, 本地服务 127.0.0.1:9077, 数据在 `~/.pg0/instances/`, 插件在 `~/.dsh/cordis.patch.yml`);本地文档(`research/` `docs/` `notes/`)仍是兜底。事实抽取 LLM 走 **DeepSeek `deepseek-v4-flash`**(`.env` 的 `DEEPSEEK_API_KEY`);视觉读图走 **modlens + GLM `glm-4.5v`**(deepseek-v4-pro 是纯文本, 看不了图, 必须靠 modlens 转文字)。**坑**: 厂商 detectLlm 用 Unix `which`(Windows 误判无 LLM → daemon 不启动, 须显式设 `HINDSIGHT_API_LLM_PROVIDER`)、GBK 编码(须 `PYTHONUTF8=1`)、huggingface 被墙(须 `HF_ENDPOINT=https://hf-mirror.com`)、daemon 5 分钟空闲退出(须 `daemonIdleTimeout:86400`)。完整照着做手册: `docs/2026-08-18_Hindsight记忆与modlens视觉本地化配置_全过程记录.md`。
- **本机 Python 不在系统 PATH(2026-09-16 修)**:敲 `python` 曾只命中微软商店的 0 字节占位符(报 "Python was not found; ... Microsoft Store"),真实解释器 = `D:\Program Files\Python313\python.exe`(3.13.15, pandas 2.3.3);已把该目录 + `Scripts` 写进**用户 PATH**(HKCU\Environment)。**两个坑**: ①已在运行的进程与 DSH 工具 shell 继承旧环境变量, 不走 PATH 时用全路径 `"D:\Program Files\Python313\python.exe"`(项目内 `data/formula_farm/runs/*.bat` 一直是全路径写法);②`.bat` 必须 **CRLF 且无 BOM**, 纯 LF 会让 cmd 把 `set` / `if (` 解析成乱码(实测 `'ogram' is not recognized`);③含中文的 `.bat` 必须存 **GBK 编码**, 不能 UTF-8+`chcp 65001`——cmd 在 65001 代码页解析批处理有多字节错位 bug, 会把中文注释拦腰截断当命令执行(2026-09-16 实测: 双击 `start_vera.bat` 报「Windows 找不到文件 '窗口继承同一份'」+「The system cannot find the path specified.」, 碎片正来自第 14 行注释; 当日 `start_vera.bat`/`p0_tick_watch.bat`/`stop_vera.bat` 已全部转 GBK, 子窗口在 start 命令内各自 `chcp 65001` 保 Python 中文日志)。`start_vera.bat` / `p0_tick_watch.bat` 顶部已内置 `PYDIR` 指路 + 找不到就早退。
- **claude CLI 不在任何 PATH(2026-09-16 修)**: 研究大脑标准档 `brain/claude_cli.py` 用 `shutil.which("claude")` 找命令, 而 claude 装在 `D:\Program Files\nodejs` (npm 全局, `claude.cmd` 内含真实 exe 路径, 不需要 node 本体在 PATH), 用户/机器 PATH 都没有 → 报「claude CLI 未安装, 大脑不可用」并降级快速档(大盘/盘面快路径)。已写入用户 PATH + `start_vera.bat` 内置 `NODEDIR` 指路(同 PYDIR 模式)。**两个坑**: ①已运行进程继承旧环境变量, 改完注册表必须**用带新 PATH 的环境重启** server/scheduler 才生效; ②PowerShell 直接敲 `claude` 命中 `claude.ps1` 被执行策略拦, 走 cmd / Python `shutil.which` 无此问题(`.CMD` 在 PATHEXT 内)。
- **akshare 已装(2026-09-16, 1.18.94, 清华源)**: 此前未装 → 研究大脑大盘快照五个板块(A股指数/港股/美股/南向资金/涨停池)全缺、`policy_pipeline` 舆情搜索退化、ETF 轮动腾讯指数兜底(`trade/rotation.py` 惰性 import)不可用。装后四路实测全通(sina 指数 562 行/港股 38 行/东财涨停池 89 行/南向 2716 行)。**坑**: ①`brain/ak_sections.py` 的 `HAS_AK` 与 `news_search._HAS_AKSHARE` 是**模块级**判定, 装完必须重启 server/scheduler 才生效(trade 是函数内惰性 import 不用重启);②东财个别端点(如 `stock_zh_index_spot_em`)会被对端断连, 但大脑盘面用的不是它, 别拿它当"akshare 坏了"的判据。
- **手机访问 DSH 对话界面(3080, 2026-09-16 配置)**:3080 只绑回环(官方明确不支持 `dsh web --host 0.0.0.0`); 远程要过两道门 —— ①`/api` 的 **Host/Origin 信任栅栏**(非回环主机必须出现在 `trustedHosts`, 否则 403);②启动时控制台打印的**一次性 `?token=`**(带它访问一次才种 cookie)。已落配置: `tailscale serve --bg --tcp=3080 tcp://127.0.0.1:3080` (**原始 TCP 转发, 不认主机名**, 域名和纯 IP 都能进; 早先的 `--http=3080` 按主机名路由会把纯 IP 404 掉, 已弃用) + `~/.dsh/profiles/web/cordis.patch.yml`(覆盖 `connection` 行: `trustedHosts` 列 tailnet 完整域名/短名/IP 三种写法、`cookieMaxAgeDays: 3650` 十年免重贴)。**三个坑**: ①用户 patch 层**不支持 `!!js`**(只能写字面量);②patch 是**整块替换**目标行 config、不深合并, 漏写字段回落默认值;③改前先用 `node apps/cli/lib/bin.js --profile web --dump-config` **不启动**验一遍(会报未匹配目标, 并标出哪层改了哪行)。另: VERA 自己的手机版页面 `http://<tailnet-ip>:8080/m` 因 8080 绑 0.0.0.0, 本来就通。

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
8. **如无必要勿增实体**:不加进程、不加中间件、不加转发层、不写第二份规则;trade/ 模块公开接口以"能一口气读完"为准,**gateway/store 两个基础设施模块例外**(契约面即接口面:gateway 的抽象方法就是柜台契约本身,store 的方法是持久化契约,拆成多个类只会加转发层),其余模块 ≤8 个方法(2026-07-26 审计 M7 裁决:修文档口径,不拆模块;2026-08-16 M7 重论证:允许 store 同文件内按表域内聚出子 store——共享连接、父类管 DDL,不加转发层,见 eb4cf4b)。**2026-09-19 架构审查批次 5.3 扩围(仍按 M7 先例:修口径不双轨)**: 实测 6+ 模块超 8 —— 除 gateway/store 外 **`config`(配置模型:字段即契约)与 `book`(订单状态机+常量)同属"契约面即接口面",一并豁免**; **`*_api.py` 薄路由层按 HTTP 端点计、不按方法数计**(薄层职责就是转发,拆它只会加一层); 其余超标的(实测 executor 21 / config 17 / monitor 12 / channel_manager 12 / analysis 10 / risk 9 / book 19)**计入待拆清单但不为拆而拆** —— 下次动该模块时顺手分内聚块, 新代码不得再增公开方法。判据从"数方法个数"改成"**能不能一口气读完 + 公开面是不是契约本身**"

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
| Web 前端 | `web/index.html` + `vera-ui.js` | 管理后台 UI (PC 写死 min-width:1280px, 不适配手机) |
| 手机版页面 | `web/mobile.html` + `/m` | 2026-08-18 手机端入口 (8080 绑 0.0.0.0, 局域网/tailscale 直连): 交易监控(5 秒轮询)+盈亏分析+研究大脑三页签, 急停带二次确认。数据与 PC 同源 (8081 trade api + 8080 研究对话), 颜色字体与 PC 同源 (tokens.css), **功能独立维护不自动跟随 PC** (接口加字段要手工同步, 教训=2026-09-04 `_month` 幽灵行)。2026-09-16 六条优化: 页面切后台暂停轮询+回前台即刷 (visibilitychange); 状态行补 channel/通道异常/ths 未武装 (09-07 T4 新字段); 分析页签改首次自动加载+手动刷新 (原只拉一次看旧数); 交易服务地址支持 `?trade=<base>` 覆盖并记 localStorage (`?trade=reset` 清除); 交易页新增 ETF 轮动状态区 (逐份锚定日/目标腿/动量/决策, 读 `/api/trade/rotation/last`); PC 页签栏右侧加「手机版 /m」入口 |
| 公式农场 | `tools/formula_farm/*` + `core/farm_runner.py`/`farm_api.py` + `core/farm_rules.py` | 2026-09-06「公式农场」(计划书 `docs/plan/2026-09-06_股旁网公式农场自动化_计划书.md`): 抓新公式 → 去重 → 静态体检 → 入库 TDX → 粗扫回测 → 日报推飞书, 人只看日报拍板。**⚠ 本行已压缩: 逐轮勘误与实测数字见 CHANGELOG 与 `docs/audit/2026-09-16_公式农场*.md`、`docs/plan/2026-09-16/17/20_公式农场*.md`, 改动前先读对应那份**(2026-09-20 因 CLAUDE.md 超加载预算压缩, 只留判断与纪律)。页面**四段闸门**(check→onboard→verify→backtest), `FarmRunner` 串行 + `last_status.json` 恢复。**三条唯一真相源(不写第二份)**: ①达标口径 `core/farm_rules.py`(用户拍板: 年化≥10% 且 |回撤|≤15% 且 笔数≥20 —— 年化线 2026-09-22 由 15% 下调, 同日按新口径重算了 archive.json 全部历史判定(只重算判定不回跑回测); <20 记「样本不足」不参与最优评选); ②未来函数黑名单 `tools/future_tokens.py`; ③作废名单 `data/formula_farm/voided.json`(**独立于 archive.json** —— `--rebuild-archive` 会冲掉档案内标记)。**作废名单治本**: `voided_scan` 一次性回扫查出 203 条踩线(原"达标 7 条"里 6 条事后踩线) + 粗扫入口加**扫前复检**(`voided_scan.guard`), 治「门禁只在入库那一刻跑」。**唯一真相源**: 入库索引/断点集/账本 = `core/farm_ledger.py`; 看板汇总 = `core/farm_summary.py`(mtime 缓存, `/api/farm/status` fail-soft)。**达标榜回填回测页**: 前端**快照 27 字段后逐字段直写**(不走 applyConfigDict, 它会把缺失字段重置默认值), **绝不代点开始回测**(人工过目是最后一道闸); 回填必须复位会改结果的池子/语义开关(审计 HIGH-2: 板块选择非空时股票池下拉框被静默忽略; **通达信没开时回填会不可逆删掉用户板块选择**)。**口径提醒(报告里照抄)**: 300万本金/单票2万=轻仓, 账户年化 ≈ 暴露×单笔边际×笔数; 轻仓下夏普结构性为负、卡玛在 |回撤|<0.01% 被死区归零, 别单指标下结论。**三条血泪教训**: ①**「看得见 ≠ 生效」三变体** = 后端进程比代码旧 / js 直接读磁盘所以按钮新 / DOM 元素被自己抹掉 —— 改 `core/` 下 Python **必须重启 server.py**; ②**进度可见性**(2026-09-20 用户报「说运行中却没进度」): `_sweep` 用 `capture_output=True` 把子进程进度行全吞了 + `FarmRunner` 只把最后一行原文当 `stage`(注释写着"供解析进度"却从未实现) + 日志贴在闸门①卡片而用户在④卡片看 → 改为 `_run_stream` 实时中继(**中继线程内打屏失败必须吞掉**, 否则线程死=管道没人读=子进程写满缓冲区永久卡死)+解析 `[i/N]`+实时输出贴**正在跑的那张卡**; ③**粗扫增量落盘与中断自愈**(2026-09-20): 报告/成绩单/档案原来只在**整轮终点**一次性写, 中途重启=已扫部分全被吞 → 改**扫一条落一条**(原子写+fail-soft)+启动**孤儿对账** `_sync_archive_orphans`。**重启约束**: farm_runner 属 `core/`, 需**闸门空闲后**重启 server.py(runner 状态只在内存, 抢着重启会把在跑的子进程变孤儿)。**改编选股栏目墙误判修复(2026-09-20, CHANGELOG 同日条)**: 栏目被误杀一个半月(intake 仅 5 篇 vs 通达信栏目 1232 篇)——文章页墙标记只挡**原著**, 改编选股源码在墙下完整可见; 三处同根误判一次修: ①墙判定改「有墙标记**且**源码无 `XG:` 输出行才算墙」 ②「主图标题直接跳过」对改编选股栏目是错的(每篇都是选股公式, 主图指原著) → 标题过滤唯一实现 `_keep_article` ③删「抽样 2 篇定整栏目」的鲁莽闸(实测该栏目**墙分布不均**(近期可见/老文章真被墙)且高负载触发**临时墙** → 逐篇判定+重跑自愈); 用户拍板只补抓**近一年 ~700 篇**(`--all --max-pages 7`; 全栏目 111 页 9300+ 篇/约15年)。**坑**: 网站给中文变量名加内链, 提取源码有 `crOSS` 大小写瑕疵(TDX 函数名大小写不敏感, 不碍编译)。 |
| 停机日资产补算 | `trade/asset_gapfill.py` + `tools/gapfill_daily_asset.py` | 2026-09-10(9/9 日历缺格事件):VERA 没开机/没归档的交易日 `daily_asset` 缺行 → 分析页那格显示"无成交",下一格还把缺失日涨跌吞成单日。补算 =「最后一个实测锚点 + 缺口内成交回放 + 每日**不复权**收盘价」逐日推,写入带 `source='derived'`(实测行 `'eod'` 永不被覆盖,存储层还有一道保险)。**两端夹逼**:用缺口后的实测行当尺子,残差 0 最好、≤容差(默认 0.5%)则写入并把残差按日均摊(否则误差全砸右端那格)、超容差拒写并报警。缺价/无右端尺子/无锚点 → fail-closed 不写。**三条入口共用同一模块**:开机自动(`_startup_catchup` 接在 15:05 补偿归档后)/分析页「补算停机日」按钮(入队→消费者线程,连续竞价时段拒绝)/离线 CLI。实证:9/9 推成 +240.40 且与 9/10 实测分毫不差;8/31-9/1 两天停机因停机期间手工买入未入账而差 ¥1,847.20(0.17%,已标注)。计划书 `docs/plan/2026-09-10_停机日资产补算_计划书.md` |
| 研究库检索增强 (RAG) | `brain/search_engine.py` + `tools/mail_to_corpus.py` + `config/brain_index.json` | 2026-09-17(M5, 计划书 `docs/plan/2026-09-17_研究库检索增强RAG_计划书.md`, 计划书 §13 = 实施记录)。**⚠ 本行已压缩: 全部实测数字/验收细节见该计划书 §13 与 CHANGELOG 同日条**(2026-09-20 因 CLAUDE.md 超加载预算压缩, 只留纪律)。检索库 = 公司档案 + `research/` + `docs/` + 邮箱外部研报(`docs/research_inbox/`)。**顺序铁律**: 先只做 `source` 口径收口并量基线, 再加语料 —— 否则分不清是口径变了还是新语料稀释了 top-k。**`source` 唯一出口 `_to_source`**(`relative_to` 全模块只剩这一处; 由「vault 相对」改「**项目根相对**」) + **三重防线**(唯一出口/项目根外抛错不退化/构建后自检 source 数 == 文件数) + **`SOURCE_FORMAT=2` 版本闸门**(版本不匹配**拒绝写入**)。**失败语义**: 读不到/嵌入失败记 `skipped` **不算失败**, 真漏/旧文件残留才**拒绝落盘**, 嵌入失败重试一次。**语料范围配置驱动** `config/brain_index.json`(读不到=老行为), 排掉每天自动落盘的 `docs/brief`/`docs/report`; **`docs/audit` 2026-09-17 M7 移出排除名单**(提示词要求审计/根因类先搜库, 排掉=要求搜空库)。**扩语料必报两个口径**: 反事实实测(只切换 `docs/audit` 进不进索引)命中率**都是 95.5%**, 但 **MRR 0.9015→0.8447** —— "没挤占 top-k"**只在命中率口径下成立**。**新鲜度** `index_freshness()` + CLI `fresh`(从 meta.jsonl 现算, 不新建 stamp 文件)。**邮箱语料** `tools/mail_to_corpus.py`: qqmail 收信 + **自己解 zip 取 .md** → 哈希去重 → 落 `docs/research_inbox/`(gitignore), 注入来源 + 「**外部产物未经本系统复核**」警告; **技能缺失/解不出 md 一律非零退出**。**实测**: 4799 文件 / 9842 块; 评测 44 例 **42/44 = 95.5%, MRR 0.9015**。**坑**: 索引曾被 pytest 毒化(639 条 source 是临时路径), 见 §0 与 conftest 隔离。 |
| 盘后复盘报告 | `notes_gen/daily.py` + `scheduler/__main__.py`(15:55 job) + `data/daily_review/<日期>.md` | 2026-09-17(M6, 计划书 `docs/plan/2026-09-17_盘后复盘报告_计划书.md`)。**⚠ 本行已压缩: 逐段文案与验收见该计划书与 CHANGELOG 同日条**(2026-09-20 因 CLAUDE.md 超加载预算压缩, 只留纪律)。收盘后一条消息看完「大盘体温表 + 我的账户 + 需要你留意的清单 + 今天做对的 + 我的交易行为 + 累计统计」。**依赖单向朝下**: 复盘编排器在**更外层**同时 import 两边 —— 盘面段**复用** `market_position_runner.thermometer_md()`(不写第二份文案), 账户段**复用** `daily_report` 表的 `payload_json`(**不重算**, 重算必然与飞书日报漂移), 月度口径**复用** `trade.analysis.build_daily_pnl_view`, 止损阈值**复用** `trade.config.CostStopConfig`(只报硬止损那条 —— 移动止盈要峰值/阶梯要分档记录, 本报告不复原, 硬凑就是编)。**铁律 1 的守护是双向的**: 大盘位置模块仍绝不 import trade(正向 AST), 另有**反向 AST 断言** `trade_main.py` 与 `trade/` 不许 import `notes_gen`(否则「报告→交易」反馈环就通了)。**落盘 `data/daily_review/`**(`data/` 已被 RAG 排除, **天然不会自我污染检索库**; 绝不落 `docs/`)。**调度**: 15:55 的 job 由「推体温表」改为「**先补采 → 再推复盘**」—— 体温表是复盘的盘面段, 两个 job 各推一次会让用户同时收到两条重叠卡(有测试锁死 `push_thermometer` 不许再出现)。**停机纪律**: 当日无 EOD 归档时账户段明写「今日没有交易归档」, **绝不拿昨天数字冒充今天**。**实测抓到并修一个单位错**: payload 里的 `turnover` 是**成交额(元)**不是百分比(写方 `trade/daily_report.py:68` 存 Σ|amount|), 按百分比打印会把 12 万元印成 12% —— 对写方核实后改正, 并有测试锁。**大白话**: 每个数字后跟一句解释, 留意清单**不许出现评价性措辞**(有反向断言)。**隔夜简报(09:05, M7 补)**: `notes_gen/morning.py`; **内容只放「过了一夜新发生的」**(美股/港股/南向), 体温表只作一句背景 —— 15:50 与次日 09:05 拿到的是**同一天同一份**数据, 早上重推=把昨天作业今早再交一遍。**不许直接转 `market_snapshot()`**(那是给大脑的原始数据包: NaN/错位列/没单位的数字), 走 `market_panel.overnight_facts()` + `_overnight_plain()`(人话模板, 由数字生成)。硬规则: 没有隔夜新信息**不发空卡**; 昨天 15:55 没推成则**附带补发**。**未接**: 隔夜消息/政策那一段; 该 job 尚未真跑过。 |
| 测试 | `tests/` | pytest 套件,改核心函数后必跑。守卫式 + 字节级 parity + 能力透传 + 默认值锁 + 复权口径边界 + 进度回调签名 |
| 报告推送 | `tools/send_report_feishu.py` | 2026-08-23:任意 MD 报告一条命令推飞书卡片(webhook 读 `.env` 的 FEISHU_WEBHOOK_URL,半密钥不打印)。表格转「｜」文字行、代码围栏转缩进、长文按段落自动拆卡(≤8KB/片,标题带 n/N)、逐卡校验返回 code==0。**飞书自定义 bot 卡片不支持 HTML/本地图片**(图片需 image_key,自定义 bot 无上传接口),图表只能注明本机路径。实证:2026-08-23 智能化深化研报 4+1 卡推送成功;P1a 研究包的投递组件直接复用它 |
| 报告投递(邮箱) | `tools/send_report_workbuddy.py` | 2026-08-24:任意 HTML 报告一条命令投递到本机 WorkBuddy 的 agent 信箱(`python tools/send_report_workbuddy.py 报告.html [--subject] [--inline]`)。走 WorkBuddy 本机 connector-proxy(127.0.0.1:64079,MCP over HTTP),token 从运行中的 WorkBuddy 进程命令行现取不落盘不打印,HTML 作正文+附件发到 `agent-mail_GetMe` 拿到的本人邮箱。仅 stdlib;WorkBuddy 需在运行,且 agent 邮箱须已在「更多→我的邮箱」开通(注销时报 MailboxDeactivatedError 带指引)。与飞书互补:飞书给卡片、邮箱给可下载打开的完整 HTML |
| 报告投递(任意邮箱) | `tools/send_email.py` | 2026-08-24 用户要求的**独立能力**:标准 SMTP 发任意文件到任意邮箱(`python tools/send_email.py 文件 [--to 收件人] [--subject] [--inline]`),默认收件人 jayziheng@agent.qq.com。发件账号/授权码读 `.env` 的 `SMTP_USER`/`SMTP_PASSWORD`(半密钥不打印),host 默认 smtp.qq.com、587 STARTTLS 失败回退 465 SSL。**QQ 邮箱对外 SMTP 用「授权码」不是登录密码**(认证失败会明确提示)。与 workbuddy/飞书三路互补:workbuddy 只发自己、飞书给卡片、本工具发任意人 |
| 名称缓存 | `core/data_fetcher.py::get_name_map` | {代码:简称} 全量映射(~6300条):进程级缓存 7d TTL,**TDX(主) → 腾讯 qt.gtimg.cn(备,2026-08-27 新增,代码全集取自 kline_cache 清单,批量报价拼名称)**;失败/空表不缓存下次重试(2026-08-27 页面简称全丢事件修复,此前空表被永久缓存)。**教训:服务可在 TDX 未开时启动,名称会随 TDX 就绪自动恢复** |
| 影子校尺 | `trade/shadow.py` + `tools/shadow_compare.py` | 2026-08-23 P0(源自当日 MA20vs动量研判):旧 MA20 三态(已迁 `trade/legacy_three_state.py::compute_signal`,2026-09-05 治理III W2 自 rotation 迁出)作影子策略,`RotationFeature._shadow_tick` 每日落盘 `data/shadow_rotation.jsonl`(**只记录不交易**,fail-soft 绝不影响交易链);对比工具 TDX→新浪降级取数,同尺重放两套规则算季度滚动 90 日收益,**影子连续 2 季赢动量超 10pp → 提示人工复审换规则**。**2026-08-24 口径修正**:重放口径由「T 日信号当天生效」改为「T+1 生效」(原口径对高频策略系统性乐观,交易越勤偏差越大);修正后首次裁决反转——动量 +187.9% vs 影子 +146.0%(2.7 年窗),**影子未连续跑赢,无需复审**;此前「影子连 3 季跑赢」系口径偏差假象(用户「T日还是T+1成交」一问发现,详见 research/2026-08-24 组合寻优报告第六节) |
| TQ 数据通道 | `core/tdx_tq.py` | 通达信 TQ-Python 只读数据薄封装 (2026-08-28, 源自 TDX SkillHub 研究):**懒加载**(模块导入零 TDX 依赖,测试/server 起动不需要通达信) + fail-soft(异常一律 None/[],研究数据绝不抛) + 纯归一化函数(`norm_etf_list/norm_stock_ext/norm_snapshot` 可独立单测) + 模块锁串行(tqcenter 类级连接非线程安全)。7 公开接口 ≤ 铁律 8:`available()/etf_of_index(指数→跟踪ETF,代码用 .SH/.SZ;部分 .CSI 宽基如 000300/905/852 服务端返回空,特殊 .CSI 如 950162 反而通)/stock_ext(get_more_info 88 字段:市值/涨停跌停价/ZAFPre2D~60D 多日涨幅/换手)/snapshot(实时+基金净值 Jjjz)`。**数据面实测口径**(诊断脚本 `research/tdx_skillhub/tq_probe_诊断脚本.py`,2026-08-28):FN 专业财务/SC 市场统计/BK 板块统计**需客户端先下载数据包**(未下时报空或 NoneType 崩);BK 板块代码须带 `.SH` 后缀;kzz 按**转债代码**查(传正股报错);**交易接口一律不封装**(QMT 唯一通道铁律)。SKILL 说明书落仓 `research/tdx_skillhub/skills/`;tdx-tq-local(HTTP 17709)本机 v7.73 不通弃用。消费方:ETF 行业轮动扩容的指数→ETF 映射、14:50 研究的估值/动量快照 |
| ETF 轮动 (错峰) | `trade/rotation.py` + `trade/store.py` | **2026-09-16 资金三份错峰**(计划书/实施总结 `docs/plan/2026-09-16_ETF轮动资金三份错峰改造_计划书.md` / `_实施总结.md`)。**⚠ 本行已压缩: 全部实测数字与三轮研究见 `research/2026-09-16_ETF轮动*.md` 与 CHANGELOG**(2026-09-20 因 CLAUDE.md 超加载预算压缩, 只留结构与纪律)。`rotation.signal_day` 单值=N=1 / 列表=资金等分 N 份, 各份独立按各自锚定日跑同一套 20 日动量择腿 + 日频 15% 移动止损(**单一代码路径**, N=1 退化)。**结构**: 份内簿记 `rotation_lots`(QMT 同码合并持仓的份归属; **持仓/止损判定以 lots 为准**, QMT 只剩对账+can_use; 漂移只告警不改账); 统一执行 pass 一次算 N 遍、**分单不并单**(回报按 order_id 归份)、`clear_external_sells` 每轮仅一次; 在途台账 `rotation_open_sells`(2026-09-17 加 `rotation_open_buys`) + **隔夜核销**(只核销自己挂的单, 人工单不动簿记); **失败方向单向**: 先写台账成功再改内存, 任何崩溃窗口只让"账本 ≥ 真实"(绝不"少记→补买→超仓"), 查询异常一律**不动台账**; `rotation_lots_understated` fail-closed 闸(份簿记<QMT 持仓 → 该码本轮不补买 + 连续 3 轮升级告警 + 人工修账入口)。**上线硬约束**: 重启 `trade_main` **必须 ≥15:05**(14:54–15:00 重启会触发启动补偿补跑一轮调仓); **改份数/锚定需重启**; **改腿代码(`cyb_etf`/`risk_etf2`)面板热生效、旧腿由轮动在下一信号日自动卖旧买新**(2026-09-21 修复: working 代码集并入簿记持仓; 此前旧代码掉出配置名册 → 旧腿卖不掉也不受移动止损, 台架实测"目标算对、零成交"); 迁移按手轮转分份+碎股归尾+entry_high 继承 + 「已初始化」标志闸(防误删重迁); `rotation_tranche_state` 逐份信号, 任何路径落库统一注入 pending_target/entry_high/has_target(根治 2026-09-10 盘后手动触发冲掉好状态的失忆事故)。**研究结论(三轮共 244 组)**: 基线 10 年年化 34.03%/回撤 -22.68%/卡玛 1.50 已属**局部最优**, 无一同口径全胜的改动; 20 日窗口是**网格峰值**(25/60 日都更差; 13 年窗复核仍是峰值); **阈值增强不稳定**(年度内大幅落后基线) → 参数不改, 换规则须人工拍板。 |
| 实盘交易 | `trade/` + `trade_main.py` | 2026-07-26 P1 MVP(计划书 `docs/2026-07-26_实盘交易系统计划书.md`): QMT 实盘, **单进程单写者 EventEngine**。模块: gateway(xtquant 唯一收口+FakeGateway)/events(静态接线)/store(SQLite WAL+JSONL 原始回报)/book(账本+状态机)/executor(预埋单+撤单流水线+两级价格阶梯)/monitor(订阅+心跳+QMT 轮询降级)/reconciler(三方对账只告警不回写)/risk(5 道闸+三重态急停)/api(薄层, 8081)。税费未计(P2 回溯补算); 止盈止损复刻回测结构(有 parity 测试); 设置面板热生效(账号/路径类需重启)。**⚠ 本行已压缩: 逐次事件与审计报告见 CHANGELOG 与 `docs/audit/2026-09-16_QMT日线新鲜度*.md`**(2026-09-20 因 CLAUDE.md 超加载预算压缩)。**几条硬纪律**: ①**QMT 是持仓/资产/成交唯一真相源**, 对账只告警+熔断, 永不自动改账/重发; ②时段感知(自动规则仅连续竞价, 人工命令任何时段放行), **ETF 不纳入自动管理**(照常对账); ③深市 14:57 后收盘竞价只收限价单 → 买挂涨停价/卖挂跌停价, 沪市维持对手最优; ④**先等 QMT 就绪再启 trade_main**(登录初始化需 30s~2min, 抢启会 `connect()` 返回 -1 崩); ⑤**日线新鲜度**: 补下载判据 = "取空 **或** 末根 < 应有一根", 补下载失败**按 60 秒重试**(成功 600 秒), 仍陈旧则打 `history_stale` 并在轮动侧来源名标注 —— 治"QMT 本地日线滞后静默污染动量参照点"; ⑥`auto_buy` 实时价与当日日线**双计已修**(网关记"**实际返回序列**的末根日期", 记原始 df 末根会让盘中路径判错)。**坑**: `exchange-calendars` 的 XSHG 历**只到 2026-12-31** → 装库不等于覆盖 2027, 公告前只能"知道自己不可信"(待办: 2026 年 12 月升库或补 `_HOLIDAYS_2027`)。 |
| 当日决策台账 | `trade/decision_codes.py` + `trade/decision_api.py` + `trade/decision_backfill.py` + `tools/backfill_daily_decision.py` + `web/js/decision_util.mjs`/`decision.js` | **2026-09-18**(用户需求原话: 交易记录「当天没有记录要说明原因, 有记录也要说明为什么, 且方便看历史原因」; 计划书+落地记录 `docs/plan/2026-09-18_交易记录当日决策台账_实施计划书.md`)。**⚠ 本行已压缩: 实施细节/回填口径见该计划书**(2026-09-20 因 CLAUDE.md 超加载预算压缩, 只留纪律)。**一张表** `daily_decision`: 一天×一条策略×一个对象=一行, 固定回答六件事(谁/哪天/做了什么/为什么/凭什么/关联哪几笔单); 主键 `(trade_date,strategy,subject)` **天然幂等**。**三态分离**: 有动作 / 无动作但有原因 / **根本没运行(`NO_RUN`, 只读侧合成+回填按"没有痕迹"推断, 永不落成 `live`)**。**来源可信度分级** `live`(3)>`backfill_exact`(2)>`backfill_text`(1)>`inferred`(0), **低可信度不许覆盖高可信度**(写入口一处把关; 判据不能用"是 live 就不许覆盖", 那会把实时重跑也拦掉)。**唯一写入口 `DailyDecisionStore.log(rows)`** 一个人管四件事: ①非交易日拒写 ②动作强弱合并 `SELL>BUY>FAIL>INFO>HOLD` 且**「更弱的那次不许丢」双向成立** ③`_merge_events` 保证「留下几条」与写入顺序无关(同强度重复时不接旧 events 会把轨迹整份清空 —— 实测 08-04 那天一只票 12 种文案只剩 1 条; 同条只存一份+封顶 50 保最早那批) ④出错吞掉+重试一次+写审计。**原因码唯一真相源** `trade/decision_codes.py`, 三个分类纯函数**实时与回填共用**; 老明细三种历史形态靠**「键缺失」而不是「值为 None」**判定(`momentum:null` 是"今天没重算"的明确信号)。**时间戳口径**: `updated_ts`=主结论那条动作发生的时刻; 实时缺省即写库那刻, **只有历史回填要传 `event_ts`**(否则 7 月 30 日那 14 次重试会被标成回填日期)。**两个只读端点**(当天+按月日历); 日历读库失败时**不把交易日标成"没运行"**, 返回空格子+`note`。**前端两坑**: ①取数**必须自己拼 8081**(`tradeApiBase(location.hostname)`) —— `trade.js` 的 `get` 被 IIFE 包住从没挂到 `window`, 退化成本源相对路径会打 8080 → 404 却被报成"交易服务不可达", **错因与报出来的话完全不是一回事**; ②报错统一走 `describeTradeError()`: **「连不上」(看进程)≠「服务端报错」(看日志)**。**上线硬约束**: 重启 `trade_main`(8081) 且**必须 ≥15:05**; `server.py`(8080) 不用重启(每次请求读盘 index.html)。 |
| AI 设置 | `llm/ai_config.py` + `ai_api.py` + 对话大脑三档接线 | 2026-09-06 web 独立页签「AI 设置」(回测/公式体检/公式农场/交易/分析/交易记录/研究/数据准备之后的第 9 页签):配置对话大脑三档 API Key/接入地址/模型,存 `config/ai.json`(不入 git,Key 只打码回传),**保存即热生效无需重启**。三档各按各的协议:**快速档**(OpenAI 兼容,`llm/providers.py` 每次 chat 现读 fast 段 → 影响快速对话+政策提取 extractor+交易复盘 llm_review)/**标准档**(Anthropic 兼容,`brain/claude_cli.py` spawn 时按 standard 段注入 `ANTHROPIC_BASE_URL/AUTH_TOKEN/MODEL` env,不碰 ~/.claude 原文件)/**深度档**(DSH 适配器,`brain/dsh_channel.py` spawn 前按 deep 段改写 dsh-runtime settings 的 agent-default-model)。合并语义唯一实现 = `ai_config.merge_patch`(纯函数:字段非空覆盖、空串保留旧值、`__clear__:true` 整档清空回落默认;三档共用 `_FIELDS` 防手写漂移——曾因 deep 档 `is-not-None` 写错静默清空,已修复有测试锁)。无配置时三档完全回落原默认(零行为变化,有测试锁)。审计:docs/audit/2026-09-06_AI设置页签_审计报告.md |
| 大盘位置 | `core/market_position.py`(纯数学) + `core/market_position_runner.py`(IO) + `market_position_api.py` + `tools/market_position_collect.py` + `web/js/market_position.js` | 2026-09-17(用户拍板"提升空间最大的方向")。**⚠ 本行已压缩: 细节(全部实测数字/逐轮勘误)在计划书 `docs/plan/2026-09-17_大盘位置与趋势研判_计划书.md` 与 CHANGELOG 同日条, 改动前先读那份**(2026-09-20 因 CLAUDE.md 超加载预算而压缩, 只保留判断与纪律)。把大盘从"每天一张照片"改成"连续录像 + 历史照镜子 + 每日体温表"。**分层**: 纯数学层(零 IO/零网络/零 trade, 8 公开函数=铁律 8 上限; `tests/test_market_position.py` AST 断言无 `import trade`) → IO 层(7 公开接口; 直读 kline_cache parquet 是性能取舍, 目录契约有测试锁) → API/前端。**踩过并已修的坑**: ①盘前抓数会造"有日期无成交"空壳 bar → 有效交易日判据=当日成交量>0 占比≥50%, 数据滞后照落盘但打 `stale` 且抬头写明"数据截至 X 日"; ②"全部 upsert"会把窗口头部算不出的字段**覆盖成 null**(实测 118 条 `amount_pct_1y` 只坏不好、且记录看着完整) → `WARMUP_BARS=250+50` 只输出窗口内最后 bars 天, 回归测试逐条断言"跑前有值跑后不许变 null"; ③折叠区容器尺寸 0 → echarts 画空白, 画图函数必须加展开守卫。**实测结论(读法有纪律)**: ERP 是唯一又强又稳的正向维度(12 月 rho +0.55); 十年百分位强**负**相关(−0.57, 方向反直觉); 宽度类四个持有期全看不出 → 标「仅描述现状, 不作预测依据」; 影子回放三条规则净口径 95% 区间全跨 0 → 只写「**无显著优势或劣势**」, **不写"无效"/"跑输买入持有"**; 牛熊口径与买入持有年化持平但回撤浅 7.3pp(唯一值得跟踪, 但与既有大势过滤同源、单一 13.7 年样本 → 换规则须人工拍板)。指标体检遵守《公式因子体检方法论》(双窗口/数族不数因子/必带警告), **不做** DSR/PBO, 如实披露"共检验 28 个组合"。**两条口径必须并列不许挑一个**: `regime`(年线斜率) 与 `regime_20`(20% 法则) 同时给 —— 依据是外部两份报告同天相隔 47 分钟结论相反、根因就是口径不同; 年线口径日频翻状态太勤, 输出里**明写"这个口径的『走了多久』不可用"**而不是偷偷加去抖(=发明第三种口径)。**照镜子**结论基于距离最近的一档(`similar_days(quantile=0.05)`)而**不是** top-5 中位数, 且必须同时报**有效独立样本 N_eff**(116 天挨得近, 真正独立的信息只有个位数)。**调度**: 每交易日 15:50 采集(排在 15:45 缓存补尾段**之后**) + 当天 15:55 推飞书体温表并与复盘合成一条(**绝不次日早上重推同一张表** —— 那是同一份收盘数据)。**衔接裁决**: 复盘模块单向 import 大盘位置复用体温表; 大盘位置模块**绝不** import trade。 |
| 大盘环境仪表盘·事件跟踪 | `core/market_events.py`(台账+衰减) + `core/market_event_scan.py`(Agent 扫描辅助+RUBRIC) + `core/market_event_sources.py`(候选源取数层) + `core/event_cli.py`(录入 CLI) + `core/market_dashboard_runner.py`(快照编排) + `web/js/market_dashboard.js` | 2026-09-18 四页签改造(总览/指标明细/事件跟踪/历史走势)的事件子板块。**⚠ 本行已压缩: 取数源细节见 `docs/plan/2026-09-19_事件跟踪数据源扩充_计划书.md`, 逐次勘误见 CHANGELOG 2026-09-19 条**(2026-09-20 因 CLAUDE.md 超加载预算压缩, 只留纪律)。**数据流铁律(2026-09-19 用户排障确立)**: 事件先落**台账** `data/market_position/events.jsonl`(唯一真相源), 页面读的是**快照** `data/market_position/dashboard.jsonl` —— 事件只在仪表盘刷新(`refresh_close`/`refresh_fill` → `_build_snapshot` → `daily_tick`)时才被"烤进"快照, **台账有货 ≠ 页面显示**; 查"事件跟踪为空"先对两个文件的写入时间。两条约定(2026-09-19 用户拍板): **①扫描收尾必刷新** —— Agent 扫描录完事件必须顺手触发一次刷新(POST `/api/market_position/dashboard/refresh` 或本地 `mdr.refresh_close(write=True)`), 否则要等下个定时点; **②周末兜底 job** `market_dashboard_weekend` 每周六 18:00 跑一次 `refresh_close`(weekly 不看交易日; 三个 daily job 16:30/08:30/09:30 有交易日门槛, 周末不跑), 周末落库的事件最迟周六晚进快照。衰减口径: 自然日线性(首日满额到期归零), 史诗级 30 天 ±1.0 / 普通重大 10 天 ±0.5 / 短期情绪 3 天 ±0.2, 合计封顶 ±1; 常驻跟踪(tracker, 如美联储利率预期=美债 2 年代理)不衰减不过期, 读数变动 ≥0.02 才改分。**坑(2026-09-19 修)**: 周末刷新用最近交易日做衰减基准, 晚于基准日录入的事件剩余天数曾超过满额把分数放大到初始分之上 → `_days_left` 已 clamp 到满额(有测试锁)。**取数层**: 公开接口仅 3 个, `_SOURCES` 注册表加新源=加一行; 五源候选池 = akshare 三源 + **美联储 RSS**(一手, category=Monetary Policy 高优先) + **华尔街见闻快讯**(转述 secondary, 普通重大及以上须复核一手原文); 候选带 `fact_level`/`hint`; RUBRIC 有「溯源终点清单」(政策→央行/统计局/证监会/政府网, 美联储→federalreserve.gov 原文)。**fed_rate 一手源升级**: 美财政部 CSV 主源 + akshare 兜底 + 双源**同日**偏差 >5bp 告警(日期不齐不比, 防滞后误报); 并挂进调度器 08:30 fill job 开头(fail-soft), **scheduler 需重启生效**。**新源两坑(fixture 锁死)**: 美联储 RSS 带 UTF-8 BOM 必须 utf-8-sig; pubDate 是 GMT 必须折北京日(FOMC 18:00 GMT = 北京次日凌晨)。被墙/失效源(FRED/GDELT/BLS/金十/RSSHub 公共实例)一律不接。 |
| 舆情台账页 | `sentiment_api.py` + `brain/sentiment_pipeline.py`/`news_dedup.py`/`alert_rules.py` + `web/js/sentiment.js` | 2026-09-20(用户三轮讨论拍板; 细节见 CHANGELOG 同日"舆情页看不见三连修"条): 盘中每 10 分钟一轮 = **两条腿** —— 腿A 新闻(去重 → LLM 打分 → 规则1 个股情绪突变 / 规则2 板块新闻密集) + 腿B 行情快照(规则3 大盘指数异动 / 规则4 量能异常默认关) → 推送抑制(同 code 30 分钟、单轮最多 3 条) → 飞书实时卡 + 落 `alert_log` → 页面台账 + 15:05 日报。**口径纪律(页面自己的话)**: 这是**台账**("当时发生了什么")不是**指标**("接下来会怎样") —— 只给计数与原始记录, **不给评分/权重/总分**(宽度类已被 `core/market_validity` 证伪, 舆情同族); 「净」= Σ极性(正负相抵后的余额), 指数类 `polarity = 涨跌幅÷10`, **榜单排序只按次数不按净**。**四张表(唯一入口 `news_dedup`; store 只读写投影, 统计一律在上层算)**: `news_seen`(去重) / `alert_log`(异动) / `tick_log`(每轮心跳, L1) / `news_log`(每条新闻五态判定 `pass`/`weak`/`error`/`missing`/`truncated`, L2); `SCHEMA_VERSION=3`。**关键回归锁**: `missing`(打分器没返回)**绝不标记已去重** —— 否则那条新闻永久丢失(同 "polarity=NULL 静默失效 5 周" 那类坑)。**关注范围(2026-09-20 C1)**: `watch_keywords` = 手写概念 + `policy_kb.policy_tagger.sector_names_by_priority` 的 **P1+P2 行业名**(共 47 词), **AVOID 档按拍板不盯**; 旧代码 `concepts[:3]` 截断已删(那份 128 行业表此前压根没接上)。**取数(C2)**: 腿A = **大水管 + 本地筛**(复用 `core.market_event_sources.fetch_all_candidates` 的五源快讯, **命中关注词才进流水线** —— 筛在前、打分在后, 把 LLM 预算留给该看的东西); 腿B = P1 **轮转**按词补搜(东财 `stock_news_em`); 实测 110 条 → 命中 29 条。**铁律 1**: 本链零 `import trade`(AST 断言), 永不联仓位。**改这几个 .py 必须重启 `server.py` 与 `scheduler`**; 改 `sentiment.js` 记得 bump `?v=` 防浏览器缓存。 |

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
5. **禁止简写/省略, 一律完整表达(2026-09-16 用户明确要求, 与规则 0 同源)**: 标的写「中文全名(代码)」不写裸代码; 日期写「9月16日」不写「9/16」; 不写自造短句(「仍停 9/15」必须写成「它的日线数据最新只到9月15日」); 黑话首次出现必须配一句白话解释; 数量带单位(「24万400股」「约53万元」)。**完整条款见 `AGENTS.md` 沟通风格第 6 条**(单一真相源, 不在两处各写一份); 要求原文与我犯过的反例见 `docs/2026-09-16_用户沟通规则_禁止简写一律写全_要求记录.md`。
6. **清晰 / 专业 / 浅显易懂 —— 覆盖「所有给用户的内容」, 不只是对话(2026-09-17 用户明确要求)**: 用户自述是**小白**, 明确要求大盘研判、复盘报告、**所有给他的数据与表述**都用大白话 + 打比方, 不要黑话, 且要"清晰、专业、但浅显易懂"。**适用范围不限于聊天回复, 还包括系统自动产出的一切**: 大盘体温表、趋势研判结论、盘后复盘报告、飞书卡片、邮箱正文、Web 页签文案、研究报告的结论段。**"专业"与"好懂"不冲突**: 该给的数字/口径/样本量/警告一个都不能少, 只是顺序要改成「先用白话说什么意思 → 再给数字」。**落地靠模板不靠自觉**: 报告类功能必须把这条写进提示词/文案常量, 验收标准要有"大白话"这一条。**完整条款见 `AGENTS.md` 沟通风格第 7 条**; 要求原文与我的反例见 `docs/2026-09-17_用户沟通规则_大白话与浅显易懂_要求记录.md`。**已落地(2026-09-17, M4)** 大盘位置子系统: 体温表开头「先说人话再给数字」+ 5 个人话模板函数(`_width_plain`/`_hl_plain`/`_amount_plain`/`_zdt_plain`/`_position_plain`, **解释由数字生成、不写死**) + `CALIBER_FOOTER` 整段重写(删掉「生存者偏差」等黑话) + 页签估值卡片 + `trade/llm_review.py::_PROMPT` 第 7 条; **反向锁**: 报告里出现黑话即判红(`test_markdown_carries_all_three_caveats_in_plain_chinese`)。**未覆盖**: `brain/` 对话、政策/舆情报告、公式农场日报文案 —— 后续项, 不假装全系统改完。

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
