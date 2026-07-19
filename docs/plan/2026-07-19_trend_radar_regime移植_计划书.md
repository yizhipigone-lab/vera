# trend_radar/regime 完美移植 + A 股适配 计划书

> **状态**:待审(2026-07-19)
> **来源**:[HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) `agent/src/skills/correlation-regime/SKILL.md`(MIT,4 模式完整代码 + 8 Notes)
> **目标**:**完美 1:1 移植** correlation-regime 4 模式到 VERA `trend_radar/`,适配 A 股,接动态避雷
> **遵循**:plan-audit-iteration(写→审→迭代→交付);不破坏现有(独立新模块,不动 engine/5m 回测)

---

## 0. 一句话目标

把 Vibe-Trading 的 4 模式 regime 检测(edge density + hysteresis + 归因 + 漂移榜)**算法原样移植**到 `trend_radar/regime.py`,数据换 A 股板块指数、参数按 A 股校准,输出 regime 状态 → **动态调避雷阈值**(FUSED 严格/正常宽松),解决我们"1年 total≤-2、3年 total≤0"的痛点。

---

## 1. 来源 4 模式算法(1:1 移植,代码已扒全)

### Mode 1:Regime 检测(edge density + hysteresis)

```python
def compute_edge_density(returns, corr_window=60, edge_threshold=0.5) -> pd.Series
    # |ρ| ≥ 0.5 的资产对占比 = "市场融合度" [0,1]

def detect_regimes(density, smooth_window=5, enter_threshold=0.65, exit_threshold=0.45) -> pd.DataFrame
    # hysteresis(Schmitt trigger):≥enter 进入 FUSED,≤exit 退出
    # trailing 平滑(causal,防前视);死区防 chatter
```

### Mode 2:Regime 风险语境(de-grossing)

```python
def regime_exposure_context(regimes, base_gross=1.0, fused_gross=0.5) -> pd.Series
    # FUSED → gross 0.5(减仓不清仓);de-gross don't liquidate
```

### Mode 3:危机首发归因(Honesty Protocol)

```python
def first_mover_attribution(returns, baseline_window=120, move_window=3,
    alarm_z=8.0, watch_z=3.0, lead_gap=2, macro_span=1, macro_fraction=0.6) -> dict
    # move-intensity(rolling |returns|)→ robust z(median/MAD,shift 防自污染)
    # 4 verdict:NAME / MACRO / AMBIGUOUS / ABSTAIN(宁不说话,不报错名)
```

**★ 审计 H2 baseline 决策**:用 **Mode 1 的 DEFUSED(fused==0)子集**算 median/MAD,**不用 blind trailing**(源 Note:post-crisis 的"伪 calm"是验证中真实踩过的坑)。

### Mode 4:相关性重连排行榜(缓慢漂移)

```python
def rewiring_leaderboard(returns, calm_mask, event_mask, min_bars=40) -> pd.DataFrame
    # score = |Δρ| 矩阵行均值(event 相关矩阵 vs calm 基线)
    # 高分 = 关系变化最大(缓慢阴跌,Mode 3 盲区)
```

**★ 审计 L2 mask 定义**:calm_mask = Mode 1 `fused==0`;event_mask = 怀疑段(regime onset 前 run-up,或未 FUSED 的异常段),min_bars=40。

**Mode 3 + Mode 4 互补**:暴力崩盘(M3)+ 缓慢漂移(M4),一起报。

---

## 2. A 股适配(兼顾 A 股特点)

| 项 | 原版(加密) | A 股适配 | 依据 |
|---|---|---|---|
| **数据** | 17 币 1 分钟 | **申万一级 28 行业指数 日级 returns** | 板块指数稳定,数据全;概念指数后续 |
| 数据源 | 加密交易所 | **akshare 板块指数**(免费)或 tushare `sw_index_daily`(审计 M1:`index_daily` 不含申万,`sw_index_daily` 需 2000+ 积分) | 步骤 1 先验通数据源 |
| corr_window | 60 bar | 60 交易日(~3 月) | A 股日级 OK |
| edge_threshold | 0.5 | 0.5 | raw-return 相关适用(Note 3) |
| **enter/exit** | 0.65/0.45 | **按 A 股 calm 期 density 分布校准**(90-95 分位 enter,中位 exit) | Note:锚定 calm 分布 |
| smooth_window | 5 | 5 | 杀单 bar 尖峰即可 |
| **alarm_z(M3)** | 8.0 | **walk-forward 校准**(审计 M2:板块指数无涨跌停,z 已吸收波动,原"涨跌停"论据不对等) | A 股历史危机回归定 |
| baseline_window(M3) | 120 | 120 交易日(半年) | OK |
| move_window(M3) | 3 | 3 | OK |
| **停牌** | 无 | **停牌板块剔除**(审计 M3:板块指数无涨跌停;停牌 0 收益污染相关) | a_share_adapter 处理 |
| **停牌** | 无 | **停牌板块剔除**(否则相关失真) | 0 收益污染相关 |
| **退市** | 易有 | 板块指数不退市,个股级才需(M3/M4 个股版才需含退市) | Note 5 |
| **因果** | trailing-only | **trailing-only**(VERA 铁律一致) | Note 1 |

---

## 3. 文件结构(独立新模块,不动 engine)

```
   trend_radar/                          ← 新建顶层目录
   ├── regime.py              compute_edge_density + detect_regimes(M1)
   ├── exposure.py            regime_exposure_context(M2)
   ├── attribution.py         first_mover_attribution(M3)
   ├── rewiring.py            rewiring_leaderboard(M4)
   ├── a_share_adapter.py     A股适配:板块指数拉取/停牌剔除/参数校准
   ├── dynamic_threshold.py   regime → 避雷阈值映射(VERA 独有接缝)
   └── __init__.py
```

---

## 4. 接缝(VERA 独有,自研)

### 4.1 动态避雷阈值(regime → 阈值)

```
   regime 正常(DEFUSED)→ 避雷 total≤-2(宽松,吃行情)
   regime FUSED        → 避雷 total≤0(严格,保命)
```

接 `tools/combo_filter_test.py`:把固定 `--threshold` 换成 regime 驱动的动态阈值。

### 4.2 减仓语境(FUSED → 自动减仓,★审计 H1 已拍板:B)

**★ 审计 H1 用户拍板(2026-07-19):选 B**。regime 是**市场结构(相关性融合),非宏观/地缘事件**,不属铁律 1 范畴。**FUSED 时自动减仓 gross→0.5**(de-gross don't liquidate,源 skill 验证结论)。`PipelineResult` 加 `regime_gross`(执行字段)。声明:regime 只做市场结构层面的仓位调整,**不碰宏观/地缘**(那类仍只报告,铁律 1 不变)。

### 4.3 regime → 选股碰撞

趋势雷达输出 regime 状态,FUSED 时趋势排名加权收紧(严格避雷 + 减仓)。

---

## 5. 8 条 Notes(诚实风险,全部移植时遵守)

1. **因果是全部**:无 centered smoothing、无同 bar baseline、阈值不在被评分事件上调。每个历史结论要经"该 bar 时能否算出"检验。
2. **非交易信号**:regime 退出 vs 价格止损,regime 输。M1-4 作风险语境/归因,不作买卖。
3. **阈值不跨相关性估计器**:raw-return 的 edge_threshold 不能用于 partial/residual。
4. **事件窗口左删失**:市场重融快,优先连续 tape,别拼接事件窗。
5. **幸存者偏差**:退市资产消失,个股级 M3/M4 要含退市原始数据。
6. **归因范围**:alarm 只抓快速暴力;缓慢漂移在 watch/M4;宏观在 MACRO。报 calm 期每日误报率作诚实指标。
7. **标签人类 + walk-forward**:calibration 事件标签来自公开记录(事后复盘/公告),非系统输出,防循环。
8. **MAD 可能为 0**(死市场):守分母,0→NaN,防 z-score 爆炸。

---

## 6. 移植步骤

| 步骤 | 做什么 | 验证 |
|---|---|---|
| 1 | A 股参数校准:申万指数跑 2020-2026 density 分布,定 enter/exit | density 分布图 + calm 期分位 |
| 2 | `regime.py`(M1 完整移植 + trailing) | 单元测试:已知事件(2024 微盘股崩盘)能进 FUSED |
| 3 | `exposure.py`(M2)+ `attribution.py`(M3) | M3 walk-forward 校准 alarm_z(A 股历史危机) |
| 4 | `rewiring.py`(M4) | M4 事件窗口选择 + 和 M3 互补验证 |
| 5 | `dynamic_threshold.py` + `a_share_adapter.py` | regime → 阈值映射 + 板块数据适配 |
| 6 | 接 `combo_filter_test`:动态阈值 vs 固定阈值跨牛熊 delta | 动态避雷 1年+3年 delta 稳定性 |

---

## 7. 验证(A 股已知历史事件回归,审计 M4)

用 A 股已知危机做 walk-forward 回归(审计建议 ≥5 事件才统计显著,源用 8 个):
- 2024-01~02 微盘股崩盘
- 2024-04 新国九条(政策冲击)
- 2024-09 逼空大涨
- 2025-04 关税战(地缘)
- 2025 某调整(待选)

**calm 期定义**(审计 M4):calm = Mode 1 检测的 DEFUSED(fused==0)子集,**非 blind trailing**(源 Note:post-crisis "伪 calm" 是真实踩过的坑)。

验证:regime 在危机时点进 FUSED + calm 期每日误报率(目标 <1%/天,原版 0.008/天)。

---

## 8. 未决(实施时确认)

| # | 决策点 | 默认/待确认 |
|---|---|---|
| 1 | 板块选择 | 申万一级(28)起步;概念指数(同花顺)后续 |
| 2 | enter/exit 阈值 | 按 A 股 2020-2026 density 分布 90-95 分位/中位(步骤 1 定) |
| 3 | alarm_z(M3) | A 股历史危机 walk-forward 校准(10-12 起) |
| 4 | M4 事件窗口 | run-up 窗口(regime onset 前)vs 怀疑段 |
| 5 | 校准事件标签 | 公开记录(复盘/公告),walk-forward |

---

## 9. 不破坏现有(规则 3)

- `trend_radar/` **独立新模块**,不动 `backtest/engine.py` / `selection/` / `core/`
- 5m 回测 + 优化止损(年化 42% 那条)**绝对不动**
- 接缝在 `server.py` `Pipeline.run` 之后(`PipelineResult` 加字段)
- 复用 `core/dividend_type.py` 复权口径(板块指数也要前复权)
- **5m 铁律边界**(审计 M5):regime 状态日级,5m 回测是 T 收盘买入;声明 regime **不改买卖口径**(只调避雷阈值),5m 回测绝对不动

---

## 10. 回测选项化 + 历史数据可得性(★2026-07-19 用户追问补)

### 10.1 回测选项化(趋势雷达可开关)

趋势雷达作为**可选开关**接入回测,支持 A/B 对照:

| 模式 | 接入 | 用途 |
|---|---|---|
| 基线 | 不接趋势雷达 | 对照基准 |
| 基线 + regime | 只接 regime 动态避雷 | 验证 regime 增量 |
| 基线 + 全部 | regime + 舆情 + 新闻 + LLM | 全趋势雷达 |

各因子(regime/舆情/新闻/LLM)独立开关,在 `combo_filter_test` / `factor_sweep` 里参数化。

### 10.2 历史数据可得性(关键:决定哪些能回测)

**实测(2026-07-19)**:申万一级 31 指数,tushare `index_daily(ts_code='801010.SI')` 拉 6431 条(~26 年),2020-2026 完全覆盖。

| 因子类型 | 历史数据 | 能历史回测 | 验证方式 |
|---|---|---|---|
| **板块指数**(regime) | ✅ 有(实测 6431 条) | ✅ 能 | 历史回测(6 年) |
| 资金流/北向/估值/财务 | ✅ 有(tushare) | ✅ 能 | 历史回测 |
| **舆情/新闻/LLM 文本** | ❌ **没有**(快照只能前向) | ❌ **不能** | **实盘增量**(部署后累积 3-6 月) |

**关键结论**:趋势雷达分两类因子,验证方式不同:
- **结构化(板块/资金)**:历史回测 — regime 能完整验证(6 年数据)
- **文本(舆情/新闻/LLM)**:**不能历史回测**,只能实盘积累 + 前向验证

**对计划书的影响**:
- regime(本计划书主体)→ 能完整历史回测(申万指数 6 年)
- 后续舆情/新闻/LLM 因子 → 从部署开始累积,不能回测,只能前向验证

---

## 11. 风险控制

| 风险 | 控制 |
|---|---|
| 参数 A 股不适配 | 步骤 1 先校准,用历史事件回归验证 |
| 前视偏差 | trailing-only,Note 1 |
| regime 被误用为交易信号 | 明确:只调避雷阈值 + 减仓语境,不买卖(Note 2) |
| M3 误报 | alarm bar 设高,4 verdict + ABSTAIN(Note 6) |
| 数据缺失(停牌板块) | a_share_adapter 剔除停牌 |

---

## 附录:来源代码完整性

5 个函数(compute_edge_density / detect_regimes / regime_exposure_context / first_mover_attribution / rewiring_leaderboard)已从 SKILL.md 完整扒取(~150 行纯 pandas+numpy)。**机器校验(审计)**:函数名 + 11 参数 + 8 Notes 全与源 SKILL.md 一致,无失真。移植 = 1:1 复制算法 + A 股数据层 + 参数校准 + VERA 接缝。不重新发明算法。

---

**文档结束。待 code-reviewer 审计(机器校验来源引用 + A 股适配可行性 + 8 Notes 完整性)。**
