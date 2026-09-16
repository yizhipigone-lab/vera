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
- **手机访问 DSH 对话界面(3080, 2026-09-16 配置)**:3080 只绑回环, 官方明确不支持 `dsh web --host 0.0.0.0`; 远程要过两道门 —— ①`/api` 的 **Host/Origin 信任栅栏**(非回环主机必须出现在 `trustedHosts`, 否则 403);②启动时控制台打印的**一次性 `?token=`**(带它访问一次才种 cookie; cookie 按 host:port 绑定、持久密钥签名、默认寿命 30 天)。已落配置: `tailscale serve --bg --tcp=3080 tcp://127.0.0.1:3080` (**原始 TCP 转发, 不认主机名** —— 域名和纯 IP 都能进; 早先用的 `--http=3080` HTTP 反代按主机名路由, 实测纯 IP 被它 404 掉, 已弃用) + `~/.dsh/profiles/web/cordis.patch.yml`(覆盖 `connection` 行: `trustedHosts` 列 tailnet 完整域名/短名/IP 三种写法、`cookieMaxAgeDays: 3650` 十年免重贴)。实测(改动后、重启前): 纯 IP `http://100.94.120.22:3080/` 由 404 变 **401**(说明已打到 DSH), `http://100.94.120.22:3080/api/status` 仍是 **403**(白名单要重启才生效)。**三个坑**: ①用户 patch 层**不支持 `!!js`**(bundle 层支持; 文件头注释写"允许"是错的, 实测报 `unknown tag !<tag:yaml.org,2002:js>`), 只能写字面量;②patch 是**整块替换**目标行 config、不深合并, 漏写字段回落默认值;③改前先用 `node apps/cli/lib/bin.js --profile web --dump-config` **不启动**验一遍(会报未匹配目标, 并标出哪层改了哪行)。另: VERA 自己的手机版页面 `http://<tailnet-ip>:8080/m` 因 8080 绑 0.0.0.0, 本来就通。

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
| Web 前端 | `web/index.html` + `vera-ui.js` | 管理后台 UI (PC 写死 min-width:1280px, 不适配手机) |
| 手机版页面 | `web/mobile.html` + `/m` | 2026-08-18 手机端入口 (8080 绑 0.0.0.0, 局域网/tailscale 直连): 交易监控(5 秒轮询)+盈亏分析+研究大脑三页签, 急停带二次确认。数据与 PC 同源 (8081 trade api + 8080 研究对话), 颜色字体与 PC 同源 (tokens.css), **功能独立维护不自动跟随 PC** (接口加字段要手工同步, 教训=2026-09-04 `_month` 幽灵行)。2026-09-16 六条优化: 页面切后台暂停轮询+回前台即刷 (visibilitychange); 状态行补 channel/通道异常/ths 未武装 (09-07 T4 新字段); 分析页签改首次自动加载+手动刷新 (原只拉一次看旧数); 交易服务地址支持 `?trade=<base>` 覆盖并记 localStorage (`?trade=reset` 清除); 交易页新增 ETF 轮动状态区 (逐份锚定日/目标腿/动量/决策, 读 `/api/trade/rotation/last`); PC 页签栏右侧加「手机版 /m」入口 |
| 公式农场 | `tools/formula_farm/*` + `core/farm_runner.py`/`farm_api.py` + `core/farm_rules.py` | 2026-09-06「公式农场」(计划书 `docs/plan/2026-09-06_股旁网公式农场自动化_计划书.md`):股旁网抓新公式 → 去重(URL+归一化源码哈希双键) → 静态体检(未来函数/筹码/主图/跨周期黑名单) → GUI 入库 TDX → 粗扫回测 → 日报推飞书,人只看日报拍板。页面**四段闸门**(check 检查增量 / onboard 一键入库 / **verify 定量复核** / backtest 粗扫;verify 是 2026-09-11 补上的计划书 §4 步骤 6:重画检测+未来函数甄别,需 TDX 在跑),`FarmRunner` 串行执行 + `last_status.json` 恢复。**达标口径唯一真相源 = `core/farm_rules.py`**(2026-09-11 用户拍板:年化≥15% 且 回撤≤15% 且 笔数≥20;<20 记「样本不足」且不参与最优评选)—— 收口原因:此前 gs 系三份报告各硬编码一份 `0.30/0.15/1000`,与 09-09 批实际在用的 15% 冲突(同一条规则三份实现必然漂移)。**未来函数黑名单唯一真相源 = `tools/future_tokens.py`**(2026-09-16 全系统审计 F4 收口:六份手写副本全部改引, 27 token 并集; 同日按用户未来函数清单补录 DYNAINFO/FINANCE/CAPITAL → 30 token; 后 GS1318 用 PLOYLINE(POLYLINE 别名) 漏网补录 → 31 token; 消费方 = static_vetting / farm common / pipeline common / gs_formula_filter / scan_gs_formulas(本地追加 FILTERX) / gs_make_report)。**公式作废名单唯一真相源 = `data/formula_farm/voided.json`**(2026-09-17 PLOYLINE 漏网事件: 门禁只在采集时跑一次、入库后不重判 → 第一批达标 7 条里 6 条事后查出踩线, 其中 5 条已进 winners 且 GS0607 原标「可用池」; 名单独立于 `archive.json`, 因为 `--rebuild-archive` 全量重建会冲掉写在档案里的标记; 消费方 = farm_summary `_voided` 把作废条目从达标/样本不足/未达标三组摘出单独成组 + 漏斗加「作废(踩黑名单)」级 + `backtest_prefill` 对作废公式抛 ValueError 禁止回填复跑; 原始成绩保留在档案里可追溯; 审计报告 `docs/audit/2026-09-17_公式农场黑名单PLOYLINE漏网_审计报告.md`)。**存量回扫与扫前复检**(2026-09-17 用户拍板, 治本项): `tools/formula_farm/voided_scan.py`(默认只报告, `--apply` 落盘, **只增不删**)一次性回扫全部已入库公式 → 实测 **203 条踩线**(6 条原有 + 194 条新登记: DYNAINFO 122 / FINANCE 69 / CAPITAL 69 / PLOYLINE 5, 有重叠), 榜单变为 达标 1 / 候选池 118→**81** / 未达标 697→**540** / 作废 6→**200**; 粗扫入口 `farm_backtest` 增加**扫前复检**(`voided_scan.guard`: 已登记的直接剔除, 其余开扫前再跑一次体检, 命中即登记作废并跳过) —— 治本「门禁只在入库那一刻跑一次」, 以后黑名单再加 token 旧公式只要被扫到就自动拦下。**GS 撞号(2026-09-17 已修)**: GS 号按全局计数发, 起点原只看 gs_txt 文件名 + TDX 树的最大编号, 而 gs_txt 导出是另一个步骤 → 两次入库之间号被复用(同一天就撞: 2026-09-06 批 GS0649 指向两个不同公式); 实测 **35 个号撞号、35 条公式入库后从未进粗扫目标集**(累计入库 857 条, 实际只评估 822 条)。**修法**: 取值规则收口纯函数 `core.farm_ledger.next_gs_number`(并入账本已发过的号, 取 TDX 树那步留在 daily_run), 清单工具 `tools/formula_farm/gs_conflict_scan.py --write`; 存量 35 条按用户拍板**暂不补扫**(补扫需重走 TDX 界面入库), 清单落 `data/formula_farm/2026-09-17_GS撞号未评估公式清单.md`。粗扫目标集**跨批次补扫**(所有 onboard ok 且无有效结果者,旧批次优先防饿死;`--max-formulas 0`=全部),报告必须写全口径+达标线+**声明区间 vs 实测窗口**+本批/本轮/余量,并逐行给判定与原因;prep 报 `no_signals` 立即停牌(`NO_SIGNALS.txt`)不再白烧。**口径提醒**(报告里照抄):300万本金/单票2万 = 轻仓,账户年化 ≈ 暴露 × 单笔边际 × 笔数,主要在排"出票多不多";轻仓下夏普结构性为负(每期扣 1.5%/年无风险利率)、卡玛在 回撤<0.01% 被死区归零(`backtest/metrics.py`),别单指标下结论。**2026-09-16 看板改造**(计划书 `docs/plan/2026-09-16_公式农场流水线卡片与总览看板_计划书.md`,含逐行复核勘误):页面 = 总览漏斗(采集→入库→复核→粗扫→达标)+达标榜(达标/样本不足/未达标折叠,终审榜仍人工维护 `data/formula_farm/winners/index.md` 不重复建设)+流水线卡片(状态灯五态/成绩单/上步无产物下步置灰,方案B宽口径)。数据源:③跑完写 `runs/<日期>/verify.json`、④跑完写 `runs/<日期>/backtest_summary.json` 并增量更新累计档案 `data/formula_farm/archive.json`(`--rebuild-archive` 全量重建;旧轮次 md 正则兜底,复核 md 须容忍 `**未通过**` 星号);汇总 = `core/farm_summary.py` 纯函数(mtime 缓存)随 `/api/farm/status` 下发(fail-soft)。**入库索引/断点集/账本唯一真相源 = `core/farm_ledger.py`**(2026-09-16 看板审计 M2 自 common.py 迁正分层, tools 引 core 与 farm_rules 同向: `load_onboard_index`/`load_done_files`/`save_onboard` 三函数, farm_onboard/farm_backtest/farm_summary 三方共引防第三份, `tools/formula_farm/common.py` 兼容再导出旧调用方零改动; 每入一条立即写, 中途停止不丢账)。入库 `--max-add` 默认 0=全部;人工停止记 `stopped` 与真失败分开(停止标记在 runner 返回时快照,防竞态误记);闸门输出全量落 `runs/<日期>/<闸门>.log`,失败自动抓最后 Traceback 块异常行上卡片,页面有「查看完整日志」。**2026-09-16 达标榜回填回测页**(计划书 `docs/plan/2026-09-16_公式农场达标榜回填回测页_计划书.md`,审计报告 `docs/audit/2026-09-16_公式农场达标榜回填回测页_审计报告.md` 两轮):达标组每行「→ 回测页」→ `GET /api/farm/prefill?gs=`(`^GS\d{4}$` 白名单, KeyError→404/ValueError→409)→ 前端**快照 27 字段后逐字段直写**(不走 applyConfigDict, 它会把缺失字段重置默认值冲掉佣金/滑点)+ `switchTab('backtest')` + 悬浮横幅(来源/组合文案/口径文案由后端 `_caliber_text` 唯一生成; 「恢复原配置」= 首次回填前快照, 二次点击不覆盖)。**绝不代点开始回测**(两口径铁律: 人工过目是最后一道闸)。**粗扫口径执行面唯一真相源 = `core/farm_rules.SWEEP_CALIBER`**(2026-09-16 自 farm_backtest.CALIBER 收口, 报告抬头与回填同对象: 沪深300 type23/5m/前复权/300万/单票2万/close_t/**trailing_confirm=intraday**(审计 HIGH-1: 粗扫不写 confirm → 引擎默认盘中触线, 而回测页默认 real 条件单语义, 不回填即静默换语义)/priority 显示值+机器值双字段); **回填必须复位会改结果的池子/语义开关**(审计 HIGH-2: 板块选择非空时股票池下拉框被 selector 静默忽略 → 清 sectors + exclude_st/include_etf/etf_only/first_day/公式卖出/因子过滤; 因子过滤关闭经 `pipeline._apply_factor_filter` 的 `enabled` 闸确认生效)；档案 `best.params`(cost/act/dd/ladder/time_days/cond_days/cond_profit, `_slim_best` 写入, ladder 空记 None 防 str(None) 假告警)为回填数据源, 旧档缺 params → 409 提示 `--rebuild-archive`。**2026-09-16 两轮审计后加固**(第二轮报告 `docs/audit/2026-09-16_公式农场达标榜回填回测页_第二轮审计报告.md`): ①`_caliber_text` 必须披露**引擎入口差异**——粗扫走 `engine.run_cached`(不支持 degrade_5m, 缺 5m 的股-天丢信号)+60 交易日稀疏窗口, 回测页走 `run()` 且 `config_mapper` 恒 `degrade_5m=true`(缺数据日线补满 → 笔数偏多), 「数字别与粗扫并排比」写进横幅; ②`min_buy`(2000 元)纳入 SWEEP_CALIBER 并回填 cfgMinBuy——它是 `backtest/loop/entry.py` 硬闸门, 设错会让复跑一笔不开; ③六项数值键 + window 缺失一律 409(不静默兜底: cond_profit 经 `or 0` 会变「持仓 N 天必卖」); ④前端板块状态收口 `setSectors`(唯一写入口, 含 toggleUniverseDropdown), 快照读**磁盘** localStorage 且板块 UI 未加载时只清内存不落盘(**通达信没开时点回填会不可逆删掉用户板块选择**——第二轮 MEDIUM-B, 本功能唯一数据丢失级缺陷); ⑤程序化写 cfgFormula 必须调 `config.notifyFormulaChanged()` 刷因子规则面板(回填与 applyConfigDict 共引, 消除两副本) |
| 停机日资产补算 | `trade/asset_gapfill.py` + `tools/gapfill_daily_asset.py` | 2026-09-10(9/9 日历缺格事件):VERA 没开机/没归档的交易日 `daily_asset` 缺行 → 分析页那格显示"无成交",下一格还把缺失日涨跌吞成单日。补算 =「最后一个实测锚点 + 缺口内成交回放 + 每日**不复权**收盘价」逐日推,写入带 `source='derived'`(实测行 `'eod'` 永不被覆盖,存储层还有一道保险)。**两端夹逼**:用缺口后的实测行当尺子,残差 0 最好、≤容差(默认 0.5%)则写入并把残差按日均摊(否则误差全砸右端那格)、超容差拒写并报警。缺价/无右端尺子/无锚点 → fail-closed 不写。**三条入口共用同一模块**:开机自动(`_startup_catchup` 接在 15:05 补偿归档后)/分析页「补算停机日」按钮(入队→消费者线程,连续竞价时段拒绝)/离线 CLI。实证:9/9 推成 +240.40 且与 9/10 实测分毫不差;8/31-9/1 两天停机因停机期间手工买入未入账而差 ¥1,847.20(0.17%,已标注)。计划书 `docs/plan/2026-09-10_停机日资产补算_计划书.md` |
| 测试 | `tests/` | pytest 套件,改核心函数后必跑。守卫式 + 字节级 parity + 能力透传 + 默认值锁 + 复权口径边界 + 进度回调签名 |
| 报告推送 | `tools/send_report_feishu.py` | 2026-08-23:任意 MD 报告一条命令推飞书卡片(webhook 读 `.env` 的 FEISHU_WEBHOOK_URL,半密钥不打印)。表格转「｜」文字行、代码围栏转缩进、长文按段落自动拆卡(≤8KB/片,标题带 n/N)、逐卡校验返回 code==0。**飞书自定义 bot 卡片不支持 HTML/本地图片**(图片需 image_key,自定义 bot 无上传接口),图表只能注明本机路径。实证:2026-08-23 智能化深化研报 4+1 卡推送成功;P1a 研究包的投递组件直接复用它 |
| 报告投递(邮箱) | `tools/send_report_workbuddy.py` | 2026-08-24:任意 HTML 报告一条命令投递到本机 WorkBuddy 的 agent 信箱(`python tools/send_report_workbuddy.py 报告.html [--subject] [--inline]`)。走 WorkBuddy 本机 connector-proxy(127.0.0.1:64079,MCP over HTTP),token 从运行中的 WorkBuddy 进程命令行现取不落盘不打印,HTML 作正文+附件发到 `agent-mail_GetMe` 拿到的本人邮箱。仅 stdlib;WorkBuddy 需在运行,且 agent 邮箱须已在「更多→我的邮箱」开通(注销时报 MailboxDeactivatedError 带指引)。与飞书互补:飞书给卡片、邮箱给可下载打开的完整 HTML |
| 报告投递(任意邮箱) | `tools/send_email.py` | 2026-08-24 用户要求的**独立能力**:标准 SMTP 发任意文件到任意邮箱(`python tools/send_email.py 文件 [--to 收件人] [--subject] [--inline]`),默认收件人 jayziheng@agent.qq.com。发件账号/授权码读 `.env` 的 `SMTP_USER`/`SMTP_PASSWORD`(半密钥不打印),host 默认 smtp.qq.com、587 STARTTLS 失败回退 465 SSL。**QQ 邮箱对外 SMTP 用「授权码」不是登录密码**(认证失败会明确提示)。与 workbuddy/飞书三路互补:workbuddy 只发自己、飞书给卡片、本工具发任意人 |
| 名称缓存 | `core/data_fetcher.py::get_name_map` | {代码:简称} 全量映射(~6300条):进程级缓存 7d TTL,**TDX(主) → 腾讯 qt.gtimg.cn(备,2026-08-27 新增,代码全集取自 kline_cache 清单,批量报价拼名称)**;失败/空表不缓存下次重试(2026-08-27 页面简称全丢事件修复,此前空表被永久缓存)。**教训:服务可在 TDX 未开时启动,名称会随 TDX 就绪自动恢复** |
| 影子校尺 | `trade/shadow.py` + `tools/shadow_compare.py` | 2026-08-23 P0(源自当日 MA20vs动量研判):旧 MA20 三态(已迁 `trade/legacy_three_state.py::compute_signal`,2026-09-05 治理III W2 自 rotation 迁出)作影子策略,`RotationFeature._shadow_tick` 每日落盘 `data/shadow_rotation.jsonl`(**只记录不交易**,fail-soft 绝不影响交易链);对比工具 TDX→新浪降级取数,同尺重放两套规则算季度滚动 90 日收益,**影子连续 2 季赢动量超 10pp → 提示人工复审换规则**。**2026-08-24 口径修正**:重放口径由「T 日信号当天生效」改为「T+1 生效」(原口径对高频策略系统性乐观,交易越勤偏差越大);修正后首次裁决反转——动量 +187.9% vs 影子 +146.0%(2.7 年窗),**影子未连续跑赢,无需复审**;此前「影子连 3 季跑赢」系口径偏差假象(用户「T日还是T+1成交」一问发现,详见 research/2026-08-24 组合寻优报告第六节) |
| TQ 数据通道 | `core/tdx_tq.py` | 通达信 TQ-Python 只读数据薄封装 (2026-08-28, 源自 TDX SkillHub 研究):**懒加载**(模块导入零 TDX 依赖,测试/server 起动不需要通达信) + fail-soft(异常一律 None/[],研究数据绝不抛) + 纯归一化函数(`norm_etf_list/norm_stock_ext/norm_snapshot` 可独立单测) + 模块锁串行(tqcenter 类级连接非线程安全)。7 公开接口 ≤ 铁律 8:`available()/etf_of_index(指数→跟踪ETF,代码用 .SH/.SZ;部分 .CSI 宽基如 000300/905/852 服务端返回空,特殊 .CSI 如 950162 反而通)/stock_ext(get_more_info 88 字段:市值/涨停跌停价/ZAFPre2D~60D 多日涨幅/换手)/snapshot(实时+基金净值 Jjjz)`。**数据面实测口径**(诊断脚本 `research/tdx_skillhub/tq_probe_诊断脚本.py`,2026-08-28):FN 专业财务/SC 市场统计/BK 板块统计**需客户端先下载数据包**(未下时报空或 NoneType 崩);BK 板块代码须带 `.SH` 后缀;kzz 按**转债代码**查(传正股报错);**交易接口一律不封装**(QMT 唯一通道铁律)。SKILL 说明书落仓 `research/tdx_skillhub/skills/`;tdx-tq-local(HTTP 17709)本机 v7.73 不通弃用。消费方:ETF 行业轮动扩容的指数→ETF 映射、14:50 研究的估值/动量快照 |
| ETF 轮动 (错峰) | `trade/rotation.py` + `trade/store.py` | **2026-09-16 资金三份错峰** (计划书+实施总结 `docs/plan/2026-09-16_ETF轮动资金三份错峰改造_计划书.md` / `_实施总结.md`, 10 年回测 `research/2026-09-16_ETF轮动周频vs每日双负保险丝_10年回测研究报告.md`): `rotation.signal_day` 单值=N=1 / 列表=资金等分 N 份, 各份独立按各自锚定日跑同一套 20 日动量择腿 + 日频 15% 移动止损 (单一代码路径, N=1 退化)。关键结构: 份内簿记 `rotation_lots` 表 (QMT 同码合并持仓的份归属; 持仓/止损判定以 lots 为准, QMT 只剩对账+can_use; 漂移只告警不改账); 统一执行 pass 一次运行算 N 遍、分单不并单 (成交回报按 order_id 归份)、`clear_external_sells` 每次运行仅一次; 在途卖单台账 `rotation_open_sells` 隔夜核销 (只核销自己挂的单, 人工单不动簿记); 迁移按手轮转分份+碎股归尾+entry_high 继承→20日高→实时价+「已初始化」标志闸 (防误删重迁); `rotation_tranche_state` 逐份信号, 任何路径落库统一注入 pending_target/entry_high/has_target (D7, 根治 2026-09-10 盘后手动触发冲掉好状态的失忆事故); 改份数/锚定需重启。前身 (2026-08-14 双池分配 / 08-20 动量改造 T 日执行) 见该计划书链。**2026-09-16 改良迭代研究** (报告 `research/2026-09-16_ETF轮动基线诊断与改良迭代_研究报告.md`, 引擎 `tools/etf_rotation_iter_research.py`, 口径=T 收盘信号/T+1 开盘成交+前复权, 与上轮 T 收盘口径不混): 基线 10 年年化 34.03%/回撤 -22.68%/夏普 1.27/卡玛 1.50/换手 431 已属局部最优 (18 组改动无一同口径全胜); 阈值 1.5% 年化 +1.1pp 但参数非单调存过拟合 (2026 年内 +4.2% 远落后基线 +16.0%); 大势过滤=熊市年化 7.5%→14.5% 换牛市少赚 ~12pp (保守备选); 多腿/窗口 10/15/60 日/仓位梯度/止损 10%/废三份/避险择强全部负收益留证; 发现并修正旧脚本「双腿≤0 误作空仓(现金)」低估 ~9pp 的口径错误 (生产规则=满仓黄金); 止损 10 年仅触发 9 次 → 真正刹车是「双腿双负→黄金」信号规则; **第二轮补测 67 组**(引擎 `tools/etf_rotation_iter_research2.py`, 覆盖用户允许改动清单全维度, 数据 `output/etf_rotation_iter2/report.json`): 资金分配比例单调无甜点 (卡玛随占比升); weekday 强弱两轮口径高度重合 (周三恒强/周一恒弱) 但**两份(周三+周五)卡玛 1.57 唯一超基线**仍属事后选择 (生产维持三份); 20 日窗口为网格峰值 (25 日 28.7%/60 日 9.6%); 阈值 3% 年化最高 35.78% 但卡玛 1.34 不过基线; 避险非黄金全败 (纯债 25.6~25.7%); top2 各半全场最浅回撤 -18.76%; 行业腿无替代 (半导体/酒账面亮眼回撤 -27.7%/-42.4%, 豆粕 +7.3pp 样本 5.75 年留档); **第三轮全网格** (`tools/etf_rotation_iter_research3.py`, 三轮总账 244 组, 数据 `output/etf_rotation_iter3/report.json`): 窗口×阈值 64 格中 **20 日整行是高原且卡玛榜首=基线本身**; 网格冠军 (止损 12%+两份 卡玛 1.64 / 半导体+阈 3% 1.51) 属多重比较最大值不采信; **13 年窗 (2013-08→2026-09) 复核: 20 日仍是峰值 (26.1%), 阈值 1.5% 反拖累** → 窗口可信/阈值增强不稳定 |
| 实盘交易 | `trade/` + `trade_main.py` | 2026-07-26 P1 MVP(计划书 `docs/2026-07-26_实盘交易系统计划书.md`):QMT 实盘交易,单进程单写者 EventEngine。9 模块:gateway(xtquant 唯一收口+FakeGateway)/events(静态接线)/store(SQLite WAL+JSONL 原始回报)/book(账本+状态机)/executor(预埋单+撤单流水线+两级价格阶梯)/monitor(订阅+心跳+QMT 轮询降级)/reconciler(三方对账只告警不回写)/risk(5 道闸+三重态急停)/api(薄层,独立 8081,交易页为 web 第三页签)。税费未计(P2 回溯补算)。止盈止损复刻回测 stop_loss 结构(parity 测试锁死);风控做减法(集中度闸已砍,sizing 校验接替);设置面板热生效(账号/路径类需重启)。**2026-07-27 ETF 误卖事件后**:时段感知(自动规则仅连续竞价,人工命令任何时段放行;心跳分时段+页面人话原因)、ETF 不纳入自动管理(照常对账)、手工成交对账认领(不拉闸)。**尾盘自动买入 MVP(2026-07-27)**:14:52 TDX 公式选股自动买入(trade/signals.py 桥+工作线程,不堵唯一写者),T+1 次日自动接入预埋/监控,设置面板可配可关。**尾盘价格市场感知(同日实测五连废单驱动)**:深市 14:57 后收盘竞价只收限价单——买挂涨停价/卖挂跌停价(单一价格撮合,成交价=收盘价),沪市维持对手最优。**冷启动 -1 事件(2026-09-02)**:机器重启后 XtMiniQmt 刚起 62 秒就启 trade_main → `connect()` 返回 -1 崩溃;QMT 登录初始化需 30s~2min,**先等 QMT 就绪再启 trade_main**(同路径独立连接测试 rc=0 证实,非代码/配置问题)。**日线新鲜度(2026-09-16)**:`RealGateway.query_daily_closes/_range` 的补下载判据由「取空」扩为「空 或 末根 < 应有一根」(`_expected_last_bar_day`: ≥15:05 且当日为交易日→当日, 否则上一交易日; 日历判不出不猜; 同码防抖 600s; 历史区间按右端封顶)—— 修 QMT 本地日线滞后(实测 513100 末根停 9/14)静默污染轮动动量参照点 + 停机日补算; 实证: 启动补算 9/11、9/14 由 fail-closed 拒写转为写入。**2026-09-16 审计+修复**(审计报告 `docs/audit/2026-09-16_QMT日线新鲜度判定_审计报告.md` 36 引用 / 修复实施报告 `docs/audit/2026-09-16_QMT日线新鲜度判定修复_实施报告.md` 23 引用, 均 100% 机器校验): 审计两条**已修** —— ①补下载后复检新鲜度(仍陈旧 → WARN + `history_stale` 标记, 轮动侧来源名带「QMT(陈旧)」);②补下载失败改按 60 秒重试(成功仍 600 秒, 原"失败不盖戳"方案会每次调用都真阻塞被否)。日历三条**已落地**: 装 `exchange-calendars 4.13.2`、`calendar_covers()` 按**实际覆盖区间**判可信、不可信时盘后只要求上一交易日+每天一条 WARNING。**实测大坑**: 该库 XSHG 历**只到 2026-12-31**(库自己报 "holidays are only recorded to the year 2026") → **装库并不覆盖 2027**, 2027 年放假安排公告前只能"知道自己不可信"; 待办(2026 年 12 月)= 公告后升级库或补 `_HOLIDAYS_2027` 并顺延覆盖区间常量。另: `auto_buy` 实时价与当日日线双计**已修**(网关新增只读观测「实际返回序列的末根日期」`last_daily_bar_day`, 闸门判"当日已在序列里"就不再追加实时价; **坑**: 必须记"实际返回序列"的末根, 记原始 df 末根会让 14:52 盘中路径判错) |
| AI 设置 | `llm/ai_config.py` + `ai_api.py` + 对话大脑三档接线 | 2026-09-06 web 独立页签「AI 设置」(回测/公式体检/公式农场/交易/分析/交易记录/研究/数据准备之后的第 9 页签):配置对话大脑三档 API Key/接入地址/模型,存 `config/ai.json`(不入 git,Key 只打码回传),**保存即热生效无需重启**。三档各按各的协议:**快速档**(OpenAI 兼容,`llm/providers.py` 每次 chat 现读 fast 段 → 影响快速对话+政策提取 extractor+交易复盘 llm_review)/**标准档**(Anthropic 兼容,`brain/claude_cli.py` spawn 时按 standard 段注入 `ANTHROPIC_BASE_URL/AUTH_TOKEN/MODEL` env,不碰 ~/.claude 原文件)/**深度档**(DSH 适配器,`brain/dsh_channel.py` spawn 前按 deep 段改写 dsh-runtime settings 的 agent-default-model)。合并语义唯一实现 = `ai_config.merge_patch`(纯函数:字段非空覆盖、空串保留旧值、`__clear__:true` 整档清空回落默认;三档共用 `_FIELDS` 防手写漂移——曾因 deep 档 `is-not-None` 写错静默清空,已修复有测试锁)。无配置时三档完全回落原默认(零行为变化,有测试锁)。审计:docs/audit/2026-09-06_AI设置页签_审计报告.md |
| 大盘位置 | `core/market_position.py`(纯数学) + `core/market_position_runner.py`(IO) + `market_position_api.py` + `tools/market_position_collect.py` + `web/js/market_position.js` | 2026-09-17(用户拍板"提升空间最大的方向"; 计划书 `docs/plan/2026-09-17_大盘位置与趋势研判_计划书.md`):把大盘从"每天一张照片"改成"连续录像 + 历史照镜子 + 每日体温表"。**①纯数学层**(零 IO/零网络/零 trade, 8 个公开函数 = 铁律 8 上限): 十年百分位(`rolling(2430).rank(pct=True)`, min_periods=750 = 满 3 年才给数)、市场宽度(站上 MA20/MA60 占比、创 60 日新高/新低家数、成交占比)、全市场成交额、涨跌停家数(复用 `core/limit_ratio.py`)、历史相似日(6 维特征 z-score 欧氏距离 + 两条纪律: 排除最近 20 日、选中日前后 20 日去重)、前向收益。**为免 MA250 写第二份, `core/index_regime.py` 新增只读出口 `ma_and_slope`**(纯加性, 无行为变化)。**②IO 层**(7 个公开接口): 读 `data/kline_cache/1d/*.parquet` → upsert `data/market_position/daily.jsonl`(一天一行, tmp+replace 原子写, `_COLLECT_LOCK` 串行锁防页面手动采集与调度器并发写坏)。**直读 parquet 是性能取舍**: `KlineCache.get()` 要为 5211 只票各跑一次 `_ensure` 且拼 6 个字段, 我们只要 4 列 —— 实测直读 17~28 秒; 目录契约由 `tests/test_market_position_runner.py` 断言与 `KlineCache._parquet_path` 生成**同一路径**, 防缓存布局改动后静默读空。**③实测踩坑(重要)**: 日线缓存盘前抓数会造出"有日期、无成交"的**空壳 bar**(2026-09-16 09:16 写入的 000001.SZ 那根 open=high=low=close 且 volume=0) → 有效交易日判据 = 当日成交量>0 的股票占比 ≥50%, `last_valid_date` 实测正确回退到 9 月 15 日; 数据滞后照常落盘但打 `stale` 标记, 体温表抬头写明"数据截至 X 日"。**④自愈**: 日常采集算近 300 个交易日并**全部** upsert, "停机三天后开机"自动补齐, 无需单独补数逻辑。**⑤照镜子首跑实测**(数据日 2026-09-15): 最像的 5 天 = 2022-01-24 / 2016-12-26 / 2017-12-08 / 2017-01-25 / 2026-04-07, 之后 20 日沪深300 中位数 +2.4%、上涨占比 80%; 报告强制附 `MIRROR_WARNING` 常量"样本只有 5 个, 历史相似不是预测"。**同日位置**: 上证十年百分位 90.9%、沪深300 73.0%、创业板指 88.4%, 而站上 20 日均线仅 21.2%、新高 51 家 vs 新低 275 家、全A成交额 16037 亿元处于一年 **1.6% 分位** —— 指数高位 + 宽度与量能冰点, 实证"只看指数会得出相反结论"。**⑥择时影子回放** `shadow_replay`(三条候选规则只记录不交易, **T+1 口径**(2026-08-24 教训), 年化按真实日历跨度折算): 2013-01~2026-09 结果 —— 沪深300>MA20 年化 +2.6%/回撤 -36.0%/夏普 0.15、宽度≥50% +0.6%/-50.2%/0.01、**牛熊=牛 +4.3%/-38.8%/0.27**、买入持有(对照) +4.3%/-46.7%/0.24。**读法**: 均线与大势两条明显输给买入持有; **牛熊口径与买入持有年化持平 (+4.3%) 但回撤浅 7.9pp、夏普更高** —— 是唯一值得继续跟踪的候选, 但它本身就是项目既有牛熊口径(与 ETF 轮动大势过滤同源, 存在"同一逻辑被同一份数据反复证明"的嫌疑), 且仅 13.7 年单一样本, 不足以支撑改规则; 换规则仍须人工拍板。**首跑抓到两个真 bug 并修**: ①`regime` 规则原落 `'bull'/'range'` 而回放按 `=='on'` 判定 → 持仓占比恒 0%; ②年化分母原用录像条数(5501)而非真实交易日, 2174 天无指数数据被算进分母 → 买入持有被压成 +2.6%(真实 +4.3%)。**⑦三条已知偏差写进 `CALIBER_FOOTER` 常量**(ST 股 ±5% 涨停漏计=低估 / 退市股不在缓存=生存者偏差 / 2016~2019 百分位窗口不足十年), 报告与页面原样带出。**⑧铁律守护**: `tests/test_market_position.py` 用 AST 断言三模块无 `import trade`(业务铁律 1: 只报告不联仓位)。**⑨调度**: `scheduler/__main__.py` 每交易日 15:50 采集(排在 15:45 缓存补尾段**之后**, 否则指标是旧的) + **当天 15:55 推飞书「大盘体温表」并与复盘报告合成一条**(2026-09-17 用户纠错: 原设计是"次日 09:05 推", 实测跑 `expected_last_trading_day` 发现 **15:50 与次日 09:05 得到的数据日期是同一天、记录逐字段相同 → 早上那张卡一个新数字都没有, 等于把昨天作业今早再交一遍**; 体温表是收盘价算出来的, 天生属于"收盘后", 挪到早上不会变。**改动零风险**: 那两个 job 从未跑过, 用户尚未重启 scheduler)。**回归锁**: 断言推送时刻 **≥15:45**(必须用最新一天的数据), 防将来又挪回早上。若用户要早上的触点, 走**隔夜简报**(美股三大指数隔夜收盘/港股/隔夜消息, 复用 `brain/market_panel.market_snapshot()` 现成三块), **体温表只作一句背景**, 绝不在两个时刻推同一张表。**⑩页签**: 第 10 个页签「大盘位置」(位置表/宽度趋势图/照镜子/影子规则/完整体温表), `vera-ui.js` hash 白名单加 `market`。**后续衔接裁决**(§12): 盘后复盘报告"页面合、模块分" —— 复盘模块单向 import 大盘位置复用体温表, 大盘位置模块绝不 import trade |

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
6. **清晰 / 专业 / 浅显易懂 —— 覆盖「所有给用户的内容」, 不只是对话(2026-09-17 用户明确要求)**: 用户自述是**小白**, 明确要求大盘研判、复盘报告、**所有给他的数据与表述**都用大白话 + 打比方, 不要黑话, 且要"清晰、专业、但浅显易懂"。**适用范围不限于聊天回复, 还包括系统自动产出的一切**: 大盘体温表、趋势研判结论、盘后复盘报告、飞书卡片、邮箱正文、Web 页签文案、研究报告的结论段。**"专业"与"好懂"不冲突**: 该给的数字/口径/样本量/警告一个都不能少, 只是顺序要改成「先用白话说什么意思 → 再给数字」。**落地靠模板不靠自觉**: 报告类功能必须把这条写进提示词/文案常量, 验收标准要有"大白话"这一条。**完整条款见 `AGENTS.md` 沟通风格第 7 条**; 要求原文与我的反例见 `docs/2026-09-17_用户沟通规则_大白话与浅显易懂_要求记录.md`。

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
