# CHANGELOG — VERA 质量演进基线

> 记录每次系统性迭代的基线变化、关键改动、测试增量、剩余风险。
> 用户/审计员可凭此追溯"7.5 → 8.3 → 9.0"演进路径。

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