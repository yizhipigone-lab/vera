# trend_radar/regime 移植 + A 股适配 计划书

> **状态**:v2 **挂起**(2026-07-19 用户拍板):先执行 `2026-07-19_避雷阈值信号锦标赛_计划书.md`(便宜信号源先行),锦标赛结果按该计划书 §5 决策树决定本计划书 归档/复活评审
> **来源**:[HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) `agent/src/skills/correlation-regime/SKILL.md`(MIT,4 模式完整代码 + 8 Notes)
> **目标**:移植 correlation-regime 到 VERA `trend_radar/`,适配 A 股,接动态避雷
> **遵循**:plan-audit-iteration(写→审→迭代→交付);不破坏现有(独立新模块,不动 engine/5m 回测)
>
> **v2 修订记录**(2026-07-19 审计后修订):
> 1. B1:`regime_gross` 从"执行字段"降级为**建议字段 + 离线 A/B 验证**,通过前不执行化、不碰 engine
> 2. B2:enter/exit 校准改 **walk-forward 预注册设计**(校准窗 2020-2022 / 验证窗 2023-2026),消除 in-sample 循环
> 3. B3:补**方向性盲区**处理——FUSED 带方向标签(up/down),减仓与严格阈值默认仅对 down-FUSED 生效
> 4. 数据源矛盾消解:以 §10.2 实测为准(tushare `index_daily` 可拉申万,推翻审计 M1 旧结论),akshare 仅作备用
> 5. 修正:申万一级 31 个(非 28);删重复的停牌行(指数层面无停牌概念,保留 MAD=0 守护);删"板块指数前复权"(指数无复权概念)
> 6. 措辞诚实化:放弃"完美 1:1"表述——M3 基线按源文 Calibration Discipline 改良(DEFUSED 子集),非 1:1;源 skill 关键验证数字自带"不可独立验证"标注
> 7. 补:成功标准预注册、验证事件扩至 7 个、数据落地机制、接缝 join 机制、测试清单、MIT attribution
> 8. M3/M4 降为二期(P1 先打通 M1→动态阈值链);砍 §4.3"趋势排名加权收紧"(无规格夹带,单独立项)

---

## 0. 一句话目标

把 Vibe-Trading 的 regime 检测(edge density + hysteresis)**算法忠实移植**到 `trend_radar/`,数据换 A 股申万一级行业指数(31 个)、参数按 A 股 walk-forward 校准,输出 regime 状态 → **动态调避雷阈值**(down-FUSED 严格/正常宽松),解决我们"1年 total≤-2、3年 total≤0"的痛点。M3/M4(归因/漂移榜)二期再做。

---

## 1. 来源 4 模式算法

### Mode 1:Regime 检测(edge density + hysteresis)— 1:1 移植

```python
def compute_edge_density(returns, corr_window=60, edge_threshold=0.5) -> pd.Series
    # |ρ| ≥ 0.5 的资产对占比 = "市场融合度" [0,1]

def detect_regimes(density, smooth_window=5, enter_threshold=0.65, exit_threshold=0.45) -> pd.DataFrame
    # hysteresis(Schmitt trigger):≥enter 进入 FUSED,≤exit 退出
    # trailing 平滑(causal,防前视);死区防 chatter
```

### Mode 2:Regime 风险语境(de-grossing)— 1:1 移植(描述性语境)

```python
def regime_exposure_context(regimes, base_gross=1.0, fused_gross=0.5) -> pd.Series
    # 源文定位:"Descriptive gross-exposure context per bar (NOT a trade signal)"
    # FUSED → 参考 gross 0.5;de-gross don't liquidate
```

### Mode 3:危机首发归因(Honesty Protocol)— **改良移植(非 1:1)**,二期

```python
def first_mover_attribution(returns, baseline_window=120, move_window=3,
    alarm_z=8.0, watch_z=3.0, lead_gap=2, macro_span=1, macro_fraction=0.6) -> dict
    # move-intensity(rolling |returns|)→ robust z(median/MAD,shift 防自污染)
    # 4 verdict:NAME / MACRO / AMBIGUOUS / ABSTAIN(宁不说话,不报错名)
```

**★ 审计 H2 baseline 决策(保留)**:median/MAD 基线用 **Mode 1 的 DEFUSED(fused==0)子集**,不用源码默认的 blind trailing rolling——这是源文 Calibration Discipline 的明文要求("Baseline from detected calm, not from a blind trailing window"),post-crisis "伪 calm" 是源方验证中真实踩过的坑。**注意:这意味 M3 是改良移植**,实现上需掩码式滚动基线(比源码 `rolling(120).median()` 复杂),且回测起点前 warmup 期 regime 未定义 → M3 在该区间不可用,需显式处理。

### Mode 4:相关性重连排行榜(缓慢漂移)— 1:1 移植,二期

```python
def rewiring_leaderboard(returns, calm_mask, event_mask, min_bars=40) -> pd.DataFrame
    # score = |Δρ| 矩阵行均值(event 相关矩阵 vs calm 基线)
    # 高分 = 关系变化最大(缓慢阴跌,Mode 3 盲区)
```

calm_mask = Mode 1 `fused==0`;event_mask = regime onset 前 run-up 或未 FUSED 的异常段;min_bars=40。

**Mode 3 + Mode 4 互补**:暴力崩盘(M3)+ 缓慢漂移(M4),一起报。

**★ 证据强度诚实标注(审计要求)**:源 skill 自述其关键验证数字(regime cycle 数、0.008/天误报率、零错名、减仓改善风险调整收益)全部是"author's **unpublished internal replays** … **not independently verifiable**";公开回归只钉死 density/hysteresis 数学。本计划书所有引用源方"验证结论"处均应理解为此前提,且加密 1 分钟 tape 的数字不向 A 股日级迁移——我们只移植算法,一切数字以 A 股自校准为准。

---

## 2. A 股适配

| 项 | 原版(加密) | A 股适配 | 依据 |
|---|---|---|---|
| **数据** | 17 币 1 分钟 | **申万一级 31 个行业指数 日级 returns** | §10.2 实测:31 指数、tushare `index_daily('801010.SI')` 拉 6431 条(~26 年),2020-2026 全覆盖 |
| 数据源 | 加密交易所 | **tushare `index_daily`**(实测可用,推翻审计 M1 旧结论);akshare 板块指数仅作 tushare 积分失效时的备用 | §10.2 实测(2026-07-19) |
| 复权 | 无 | **无**——指数无分红除权,不存在复权问题;`core/dividend_type.py` 在此无适用对象(修订:删除 v1 的错误复用) | 指数性质 |
| corr_window | 60 bar | 60 交易日(~3 月) | A 股日级 OK |
| edge_threshold | 0.5 | 0.5 | raw-return 相关适用(Note 3) |
| **enter/exit** | 0.65/0.45 | **walk-forward 校准**(见 §6 步骤 1:校准窗分布 + calm 自举,验证窗只许样本外使用) | Note:锚定 calm 分布 + Note 1/7 |
| smooth_window | 5 | 5 | 杀单 bar 尖峰即可 |
| **alarm_z(M3,二期)** | 8.0 | walk-forward 校准(板块指数无涨跌停,z 已吸收波动) | A 股历史危机回归定 |
| baseline_window(M3) | 120 | 120 交易日(半年) | OK |
| move_window(M3) | 3 | 3 | OK |
| 死市场守护 | 无 | MAD=0 → NaN(Note 8),防 z-score 爆炸;指数层面无停牌概念(成分股停牌由指数编制方处理,修订:删 v1 重复的停牌行) | Note 8 |
| **退市** | 易有 | 板块指数不退市;二期若做个股级 M3/M4 才需含退市原始数据 | Note 5 |
| **因果** | trailing-only | **trailing-only**(VERA 铁律一致) | Note 1 |
| **方向性** | 未区分 | **新增:FUSED 带方向标签(up/down)**,见 §2.1 | 审计 B3:A 股存在向上融合(逼空) |

### 2.1 方向性处理(★审计 B3 新增,v1 盲区)

edge density 不分涨跌:普涨逼空时相关性同样融合,regime 会进 FUSED。v1 未定义此情形行为,若按 v1 的"FUSED→减仓+严格阈值",系统会在 2024-09 式逼空中自动砍仓——而源方 de-gross 证据全部来自崩盘 tape,对向上融合零背书。

**设计**:detect_regimes 输出除 `fused` 外增加 `direction` 列:FUSED onset 窗口内等权指数(或申万全 A 代理)累计收益 ≥0 → `up`,否则 `down`。

**默认动作门控(待 §7 验证数据复核)**:

| regime 状态 | 避雷阈值 | regime_gross 建议值 |
|---|---|---|
| DEFUSED | total≤-2(宽松) | 1.0(不干预) |
| FUSED + down | total≤0(严格) | 0.5(建议减仓) |
| FUSED + up | total≤-2(不变) | 1.0(不干预) |

**2024-09 逼空的期望行为(预注册)**:检测器允许进 FUSED(检测正确),但 up-FUSED **不得**触发严格阈值与减仓建议(动作门控正确)。该事件同时是方向门控的验证用例。

---

## 3. 文件结构(独立新模块,不动 engine)

```
   trend_radar/                          ← 新建顶层目录
   ├── regime.py              compute_edge_density + detect_regimes(M1,1:1)+ direction 标签(§2.1)
   ├── exposure.py            regime_exposure_context(M2,1:1,描述性)
   ├── a_share_adapter.py     申万指数拉取/增量更新/本地缓存/warmup 预拉校验
   ├── dynamic_threshold.py   regime(+direction)→ 避雷阈值映射(VERA 独有接缝)
   ├── __init__.py
   │
   ├── attribution.py         【二期】first_mover_attribution(M3,改良:DEFUSED 基线)
   └── rewiring.py            【二期】rewiring_leaderboard(M4,1:1)
```

**数据落地(★v2 新增)**:申万指数日线存 `data/index_daily/`(parquet,一股一文件或单文件长表,由 a_share_adapter 管理增量更新);不塞 `core/kline_cache`(个股口径,含复权/manifest 逻辑,不适用)。每日更新由 pipeline 运行前调用 adapter 的 `ensure_updated()` 完成。**warmup 前置依赖**:任何回测/验证起点前,必须已预拉 ≥ `corr_window + smooth_window + 10` 交易日(≈80 交易日)的指数历史,adapter 启动时校验,不足则报错而非静默产出 NaN regime。

---

## 4. 接缝(VERA 独有,自研)

### 4.1 动态避雷阈值(regime → 阈值)

按 §2.1 门控表映射。接 `tools/combo_filter_test.py`:

- 现状:`--threshold` 为全年单一整数,过滤逻辑在 `tools/combo_filter_test.py:93`(`total_score > threshold`)。
- 改造:新增 `--dynamic-threshold` 开关与 `--regime-series <parquet>` 参数(parquet 列:`date, fused, direction`,由 trend_radar 离线生成);开启后按 `trade_date` join regime 序列,逐日应用映射阈值。
- **因果约束**:t 日使用的 regime 状态必须只由 ≤t 的指数数据算出(density warmup 天然满足 trailing);join 前校验 regime 序列最后一根数据日期 ≤ t,防前视。该约束进 `tests/test_dynamic_threshold.py`。

### 4.2 减仓语境(★审计 B1 修订:v1 的"执行字段"降级)

**v1 自相矛盾处**:`PipelineResult` 加"执行字段"与"不动 engine"不可兼得——仓位缩放发生在引擎内部,事后加字段减不了仓;且 v1 全程无一步验证减仓 0.5 的损益效果。

**v2 拍板(分两阶段)**:

- **本计划书范围内**:`PipelineResult` 加 `regime_gross`(**建议/报告性质字段**,不执行、不碰 engine),与 §2.1 门控表一致(down-FUSED → 0.5,其余 → 1.0)。定位与源文一致:"Descriptive gross-exposure context, NOT a trade signal"。
- **离线 A/B 验证(§6 步骤 6b)**:事后模拟——回测权益曲线在 down-FUSED 区间按 gross 0.5 缩放(该区间日收益 ×0.5,其余不变),与基准对比。通过 §7.2 预注册标准,才允许**单独立项**谈执行化(那时才设计 engine 接缝,不在本计划书范围)。

**铁律 1 边界声明(不变)**:regime 是市场结构(相关性融合),非宏观/地缘事件;本计划书只做市场结构层面的阈值调整与减仓建议,不碰宏观/地缘(那类仍只报告)。

### 4.3 ~~regime → 选股碰撞~~(**v2 删除**)

v1 的"趋势排名加权收紧"无规格、无验证步骤,属夹带,砍出本计划书。如有需要,在动态阈值链验证穿之后单独立项。

---

## 5. 8 条 Notes(诚实风险,全部移植时遵守)

1. **因果是全部**:无 centered smoothing、无同 bar baseline、阈值不在被评分事件上调。每个历史结论要经"该 bar 时能否算出"检验。
2. **非交易信号**:regime 退出 vs 价格止损,regime 输。M1-4 作风险语境/归因,不作买卖。`regime_gross` 在本计划书内仅为建议字段(§4.2)。
3. **阈值不跨相关性估计器**:raw-return 的 edge_threshold 不能用于 partial/residual。
4. **事件窗口左删失**:市场重融快,优先连续 tape,别拼接事件窗。
5. **幸存者偏差**:退市资产消失,二期个股级 M3/M4 要含退市原始数据。
6. **归因范围**:alarm 只抓快速暴力;缓慢漂移在 watch/M4;宏观在 MACRO。报 calm 期每日误报率作诚实指标。
7. **标签人类 + walk-forward**:calibration 事件标签来自公开记录(事后复盘/公告),非系统输出,防循环;阈值再校准只允许用完全审定在过去的事件。
8. **MAD 可能为 0**(死市场):守分母,0→NaN,防 z-score 爆炸。

---

## 6. 移植步骤

| 步骤 | 做什么 | 验证 |
|---|---|---|
| 1 | **walk-forward 参数校准(★B2 修订)**:`a_share_adapter` 拉数 → 校准窗 **2020-01~2022-12** 跑 density 分布。calm 自举(一轮封顶,防反复拟合):先以源默认 0.65/0.45 为先验跑 M1 → 取 DEFUSED 子集的 density 分布 → enter=该分布 90-95 分位、exit=中位 → 定稿冻结 | density 分布图 + 校准窗 calm 分位记录(存档,供审计复核) |
| 2 | `regime.py`(M1 1:1 + direction 标签 + trailing) | 单测(§8)+ 验证窗 2023-01~2026-07 样本外:§7.1 事件表逐一核 regime 行为 |
| 3 | `exposure.py`(M2)+ `a_share_adapter.py` + `dynamic_threshold.py` | 单测 + warmup 校验 + join 因果性测试 |
| 4 | `PipelineResult.regime_gross`(建议字段)+ server 透出 | 字段透传测试;不碰 engine |
| 5 | 接 `combo_filter_test`:动态阈值 A/B | §7.2 预注册标准判定 |
| 6b | `regime_gross` 离线 A/B(down-FUSED 段权益 ×0.5 事后模拟) | §7.2 预注册标准判定;**通过才允许单独立项谈执行化** |
| 7 | 【二期】`attribution.py`(M3 改良)+ `rewiring.py`(M4) | 二期开工前先定义消费方(落 json 报告?web 展示?),再做 walk-forward alarm_z 校准与 M3/M4 互补验证 |

---

## 7. 验证(A 股已知历史事件,walk-forward)

### 7.1 事件表(★v2 扩至 7 个,标注校准/验证窗归属)

| # | 事件 | 窗口归属 | 用途 |
|---|---|---|---|
| 1 | 2020-02 新冠崩盘 | 校准窗 | sanity check 用,**不作验证证据**(阈值在此窗内定) |
| 2 | 2021-02 核心资产崩盘 | 校准窗 | 同上 |
| 3 | 2022-04 + 2022-10 两轮大跌 | 校准窗 | 同上 |
| 4 | 2024-01~02 微盘股崩盘 | 验证窗 | 样本外:期望进 down-FUSED |
| 5 | 2024-04 新国九条(政策冲击) | 验证窗 | 样本外:期望进 down-FUSED |
| 6 | 2024-09 逼空大涨 | 验证窗 | 样本外:**方向门控用例**——允许进 FUSED,但必须标 up,不得触发严格阈值/减仓建议 |
| 7 | 2025-04 关税战(地缘) | 验证窗 | 样本外:期望进 down-FUSED |

验证窗 4 个危机型事件中要求 ≥3 个正确进入 down-FUSED(预注册通过线,防事后挑数);calm 期每日误报率目标 <1%/天(Note 6;源方 0.008/天为加密不可验证数字,仅作参考锚)。

**calm 期定义**(审计 M4,保留):calm = Mode 1 检测的 DEFUSED(fused==0)子集,非 blind trailing。

### 7.2 A/B 成功标准(★v2 预注册,评审时可调,实施后不可改)

**动态阈值 vs 固定阈值**(combo_filter_test,1年+3年两窗):
- 通过线:剔除后组合的 Calmar 不差于固定阈值方案 0.1 以上,且最大回撤改善 ≥2pp,年化收益下降 ≤1pp;
- 同时报告换手/剔除数量变化,剔除量暴增(>2×)视为失败(说明阈值切换抖动)。

**regime_gross 离线 A/B**:
- 通过线:模拟权益的 Calmar 优于基准,且 2024-09 逼空段收益损失 ≤ 基准该段的 10%(验证方向门控确实避免了踏空);
- 不通过 → regime_gross 保持纯报告字段,本计划书到此为止,不执行化。

---

## 8. 测试清单(★v2 新增,项目惯例 tests/ pytest)

| 文件 | 覆盖 |
|---|---|
| `tests/test_regime_density.py` | edge density 边界:常数列(零方差)、NaN warmup 区间、资产数=2 最小矩阵 |
| `tests/test_regime_hysteresis.py` | 状态机:enter/exit 穿越、死区内不翻转、单 bar 尖峰被 smooth 杀掉、exit≥enter 抛错 |
| `tests/test_regime_direction.py` | up/down 标签正确性;2024-09 式合成数据(普涨融合)标 up |
| `tests/test_regime_causality.py` | 因果性:截断 t 日后数据,t 日及之前的 density/regime 输出逐值不变 |
| `tests/test_dynamic_threshold.py` | date→regime join 因果约束(§4.1);门控表映射;regime 序列数据日期超界报错 |
| `tests/test_ashare_adapter.py` | warmup 不足报错(非静默 NaN);增量更新幂等 |
| 【二期】`tests/test_attribution.py` | MAD=0 守护(Note 8)、shift(1) 防自污染、4 verdict 分支、DEFUSED 掩码基线 |
| 【二期】`tests/test_rewiring.py` | min_bars 不足抛错、calm/event 掩码 reindex |

---

## 9. 未决(实施时确认)

| # | 决策点 | 默认/待确认 |
|---|---|---|
| 1 | 板块选择 | 申万一级(31)起步;概念指数(同花顺)后续 |
| 2 | enter/exit 阈值 | 校准窗 2020-2022 density 分布 + calm 自举一轮(步骤 1 定,定稿冻结) |
| 3 | alarm_z(M3,二期) | A 股历史危机 walk-forward 校准(10-12 起) |
| 4 | M4(二期)事件窗口 | run-up 窗口(regime onset 前)vs 怀疑段 |
| 5 | 校准事件标签 | 公开记录(复盘/公告),walk-forward(Note 7) |
| 6 | 方向判别的指数代理 | 默认:31 行业等权指数 onset 窗口累计收益;备选:万得全 A/沪深 300(实施时定) |
| 7 | §7.2 通过线数值 | 评审时可调;实施后冻结 |

---

## 10. 不破坏现有(规则 3)

- `trend_radar/` **独立新模块**,不动 `backtest/engine.py` / `selection/` / `core/`
- 5m 回测 + 优化止损(年化 42% 那条)**绝对不动**
- 接缝在 `server.py` `Pipeline.run` 之后(`PipelineResult` 加 `regime_gross` 建议字段)
- 指数数据独立存 `data/index_daily/`,不复用 `core/dividend_type.py`(指数无复权概念,v2 修正)
- **5m 铁律边界**(审计 M5):regime 状态日级,5m 回测是 T 收盘买入;声明 regime **不改买卖口径**(只调避雷阈值 + 建议字段),5m 回测绝对不动

---

## 11. 回测选项化 + 历史数据可得性

### 11.1 回测选项化(趋势雷达可开关)

| 模式 | 接入 | 用途 |
|---|---|---|
| 基线 | 不接趋势雷达 | 对照基准 |
| 基线 + regime | 只接 regime 动态避雷 | 验证 regime 增量 |
| 基线 + 全部 | regime + 舆情 + 新闻 + LLM | 全趋势雷达 |

各因子(regime/舆情/新闻/LLM)独立开关,在 `combo_filter_test` / `factor_sweep` 里参数化。

### 11.2 历史数据可得性(关键:决定哪些能回测)

**实测(2026-07-19)**:申万一级 31 指数,tushare `index_daily(ts_code='801010.SI')` 拉 6431 条(~26 年),2020-2026 完全覆盖。**该实测推翻审计 M1 旧结论("index_daily 不含申万"),本计划书以此为准;akshare 仅作备用。**

| 因子类型 | 历史数据 | 能历史回测 | 验证方式 |
|---|---|---|---|
| **板块指数**(regime) | ✅ 有(实测 6431 条) | ✅ 能 | 历史回测(校准窗 + 验证窗) |
| 资金流/北向/估值/财务 | ✅ 有(tushare) | ✅ 能 | 历史回测 |
| **舆情/新闻/LLM 文本** | ❌ **没有**(快照只能前向) | ❌ **不能** | **实盘增量**(部署后累积 3-6 月) |

**关键结论**:趋势雷达分两类因子,验证方式不同:
- **结构化(板块/资金)**:历史回测 — regime 能完整验证
- **文本(舆情/新闻/LLM)**:**不能历史回测**,只能实盘积累 + 前向验证

---

## 12. 风险控制

| 风险 | 控制 |
|---|---|
| 参数 A 股不适配 | walk-forward 校准 + 验证窗样本外回归(§6/§7),校准/验证分窗冻结 |
| 校准 in-sample 循环 | 校准窗 2020-2022 / 验证窗 2023-2026 预注册分割;calm 自举一轮封顶(步骤 1) |
| 向上融合误减仓(逼空踏空) | 方向门控(§2.1),2024-09 为预注册验证用例;up-FUSED 不触发严格阈值/减仓 |
| 前视偏差 | trailing-only(Note 1)+ 因果性测试(§8) |
| regime 被误用为交易信号 | 本计划书内 regime_gross 仅为建议字段(§4.2);执行化需单独立项 + A/B 通过 |
| 减仓证据强度 | 源方减仓数字不可独立验证(§1 标注);以本地 A/B(步骤 6b)为唯一依据 |
| M3 误报(二期) | alarm bar 设高,4 verdict + ABSTAIN(Note 6) |
| 死市场 | MAD=0 → NaN 守护(Note 8) |
| warmup 数据不足 | adapter 启动校验,不足报错而非静默 NaN(§3) |

---

## 附录:来源代码完整性

5 个函数(compute_edge_density / detect_regimes / regime_exposure_context / first_mover_attribution / rewiring_leaderboard)已从 SKILL.md 完整扒取(~150 行纯 pandas+numpy),函数名 + 参数 + 8 Notes 与源一致。**移植口径(v2 诚实化)**:M1/M2/M4 按源码 1:1;M3 基线按源文 Calibration Discipline 改良(DEFUSED 子集),direction 标签为 VERA 自研增量——不重新发明算法,但也不掩饰改良点。

**License**:源仓库标注 MIT(v1 记录,实施前需核对仓库 LICENSE 文件);移植文件的模块头须保留 MIT copyright notice 与来源链接(attribution 义务)。

---

**文档结束。v2 修订版,待审。**
