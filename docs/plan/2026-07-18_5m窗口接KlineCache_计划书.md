# 5m 窗口路径接 KlineCache — 三级漏斗 实施计划书

> 版本 v2 · 2026-07-18 · 作者 VERA
> v2 修订: 经 code-reviewer 审计 (2 HIGH + 5 MEDIUM + 2 LOW), 技术方案不变, 修订测试骨架/风险归因/G3 caveat/parity 对称化/调用方盘点/并行竞态。
> 前置共识（本次讨论确定）：5m 回测的"按需拉数据"应是 **缓存 → 拉 TDX → 才降级** 三级漏斗。现状降级（`degrade_5m`）已经是兜底，但 5m 窗口拉取（`get_kline_windowed`）完全不读本地缓存，每次回测重拉 TDX —— 既慢又给 TDX 添负担。本计划把 `KlineCache` 接进窗口路径，`degrade_5m` 自然降级为"缓存 + TDX 都拉不到"的最后兜底。

---

## 1. 背景与目标

### 1.1 背景
- `KlineCache`（`core/kline_cache.py`）早已 **5m-capable**：
  - `cache.get(period="5m")` 可用（`kline_cache.py:128`）；
  - `_ensure` 对 5m 做 gap 检测（`kline_cache.py:217` `if period in ("1d","5m")`）；
  - 5m 子目录在 `__init__` 创建（`kline_cache.py:52`）；
  - 它本身就是"读 parquet → miss-fetch TDX → gap 检测标 intact=false 告警"的漏斗。
- **1d 路径已接缓存**：`engine.py:798` / `:1329` / `:1281` 三处都传 `use_cache=self.use_kline_cache`（默认 True，见 `engine.py:721`）。
- **唯一缺口**：5m 窗口路径 `get_kline_windowed`（`data_fetcher.py:256-367`）在 `:309` 调 `cls.get_kline(..., period=period)` **不带 `use_cache`**，每次回测对同样窗口重拉 TDX。
- 用户直觉对：`degrade_5m` 现在就是"拉不到才降级"的兜底；缺的只是缓存这一层。把缓存接上，漏斗就齐了。

### 1.2 目标
- **G1 三级漏斗显式化**：5m 窗口路径 = 本地 KlineCache（命中即返）→ miss 则拉 TDX（落盘缓存）→ 仍缺才 `degrade_5m` 用 1d 填。
- **G2 重复回测提速**：同一选股集合二次回测命中本地 parquet，零 TDX 往返，少给 TDX 添负担。
- **G3 返回值首轮冷缓存字节级一致**：`use_cache` True/False 的 `Close` 矩阵首轮一致（Task 4 parity 验证）；`degrade_5m` 的输入/输出不变。
  - ⚠️ **caveat（审计 M1）**："字节级不变"仅限返回 DataFrame 的值。缓存路径会**新增副作用**：写 parquet、触发 F4/F5/F6 告警、改 manifest intact 标志 —— 这些在 TDX 直拉路径不存在。所以严格讲是"返回值不变 + 新增缓存副作用"，不是"correctness 完全不动"。
  - ⚠️ **fill_data 耦合**：`use_cache=True` 时 `fill_data` 参数被 `_get_kline_via_cache` 的 `_tdx_fetcher` 静默改写为 `False`（`data_fetcher.py:155` 硬编码）。engine 实际用法永远 `fill_data=False`（`engine.py:795`），不踩此坑；但接口契约层面是 hidden coupling，§3.2 明示。
- **G4 向后兼容**：`get_kline_windowed` 新增 `use_cache` 参数默认 False；现有调用方与测试 fake 不破。

### 1.3 非目标
- 不改 `KlineCache` 本身（它已支持 5m，本计划只做接缝）。
- 不改 `degrade_5m` 逻辑（它已是最后兜底，位置不变）。
- 不改 `BacktestLoop` / 信号构造。
- 不自动化"缓存预热策略"（`warmup_kline_cache.py` / `backfill_kline_cache.py` 已存在，给运维指引即可）。

---

## 2. 现状审计（file:line 实证，已 Read 校验）

| 位置 | 现状 | 改造点 |
|---|---|---|
| `core/data_fetcher.py:256-367` | `get_kline_windowed` 按"窗口起月份"分桶，每桶 `:309` 调 `get_kline` **不带 use_cache** | 加 `use_cache` 形参透传 |
| `core/data_fetcher.py:84-92` | `get_kline(use_cache=True)` → `_get_kline_via_cache` → `cache.get`，已 5m 可用 | 复用，不动 |
| `core/data_fetcher.py:136-166` | `_get_kline_via_cache` 的 `_tdx_fetcher` 传 `fill_data=False`（停牌 NaN 口径保留）| 对照确认，不动 |
| `core/kline_cache.py:128 / :217` | `cache.get` 支持 `period="5m"`；`_ensure` 对 5m 做 gap 检测 | 不动 |
| `backtest/engine.py:793-796` | 5m 走 `get_kline_windowed`，**不传 use_cache** | 传 `use_cache=self.use_kline_cache` |
| `tools/calibrate_degrade_5m.py:73-75` | 校准工具调 windowed，**不传 use_cache**（审计 M3 补列）| 默认 False 兜底，行为不变，不动 |
| `backtest/engine.py:798 / :1329 / :1281` | 1d 各处已传 `use_cache` | 对照，不动 |
| `backtest/engine.py:1327-1329` | `degrade_5m` 内部 1d 拉取已 `use_cache` | 不动（已是缓存路径）|
| `tests/test_engine_5m_window.py:36` | `fake_windowed` 签名无 `use_cache` | 加默认形参 |
| `tests/test_degrade_5m.py:340` | patch `get_kline_windowed` | 核对 fake 签名，按需补 |

**关键正确性论证（为何字节级不变）**：
- `cache.get(codes, b_start, b_end, "5m")` 对每只股 `_ensure [b_start, b_end]`，返回 union-wide DataFrame，结构与 `get_kline` TDX 直拉一致（键 `Open/High/Low/Close/Volume/Amount` × `DatetimeIndex`）。
- windowed 的桶合并逻辑（`:344-361`）在两种来源输出上行为相同（都是同结构 wide DataFrame）。
- `fill_data=False` 在缓存 miss-fetch 路径同样保留（`_get_kline_via_cache` 的 `_tdx_fetcher` 传 `fill_data=False`），**停牌 NaN 口径不变**。
- 复权口径：`cache.get` 只收 `front`（`kline_cache.py:132` fail-fast），windowed 传 `dividend_type="front"`（`engine.py:795`），一致。

---

## 3. 技术方案

### 3.1 三级漏斗（改后）
```
engine.run 5m 分支 (engine.py:793, 在 run() 内非 _fetch_data)
  └─ get_kline_windowed(use_cache=self.use_kline_cache)        ← 改: 透传
       └─ 每桶 get_kline(use_cache=True)                        ← 改: 透传
            ├─ cache.get(codes, b_start, b_end, "5m")           ← 命中 parquet 走 _ensure 校验后返
            │    └─ _ensure: miss-fetch TDX → 落盘 → gap 检测 → 标 intact
            └─ 返回（仍缺的股-天 = TDX 源头真没有, 如 6.23 缺口周）
  └─ degrade_5m（engine.py:815, opt-in）: 对"仍缺"股-天用 1d 填   ← 最后兜底, 不动
```

### 3.2 改动面（最小，3 处源码 + 3 处测试 fake）
1. `get_kline_windowed` 加 keyword-only 形参 `use_cache: bool = False`，`:309` 透传。
2. `engine.py:793` 调用加 `use_cache=self.use_kline_cache`。
3. 三处测试 fake 签名补 `use_cache=False`（或 `**kwargs`）：`test_engine_5m_window.py:36`、`test_degrade_5m.py:341`、以及 Task 1 新增 fake。
4. 新增两条测试：`use_cache=True` 时 windowed 透传给 `get_kline`（Task 1）；engine `use_kline_cache` 透传到 windowed（Task 2）。

> **fill_data 耦合明示（审计 M1）**：`use_cache=True` 时 `fill_data` 参数无效，始终按 `False` 拉（`_get_kline_via_cache` 的 `_tdx_fetcher` 硬编码，`data_fetcher.py:155`）。engine 用法不踩坑（永远 `fill_data=False`），其他调用方若传 `fill_data=True, use_cache=True` 需知此耦合。

### 3.3 F4 前段截断的运维指引（非代码）
`KlineCache._ensure`（`kline_cache.py:184`）对"请求起点早于缓存起点"**只告警不回拉**（F4 设计）。若选股窗口起点早于 5m 缓存首日，前段会缺。对策：用 `tools/backfill_kline_cache.py --period 5m --start <最早信号日>` 把缓存灌到覆盖最早信号。本计划不自动化预热，只写入运维清单。

---

## 4. 任务分解（TDD，bite-sized）

### Task 1：`get_kline_windowed` 加 `use_cache` 形参（RED → GREEN）

**文件：**
- 改：`core/data_fetcher.py:256-367`（签名 + `:309` 透传）
- 测试：`tests/test_kline_cache.py`（新增用例）

- [ ] **Step 1: 写失败测试**

```python
def test_get_kline_windowed_passes_use_cache(monkeypatch):
    """use_cache=True 时, get_kline_windowed 应把 use_cache 透传给 get_kline。"""
    import pandas as pd
    import core.data_fetcher as df_mod

    seen = {}

    def fake_get_kline(stock_list, start_time="", end_time="", period="1d",
                       dividend_type="front", count=-1, fill_data=True,
                       field_list=None, *, use_cache=False, force_refresh=False):
        seen["use_cache"] = use_cache
        idx = pd.DatetimeIndex(["2026-06-30 09:35", "2026-06-30 09:40"])
        close = pd.DataFrame({"000001": [10.0, 10.1]}, index=idx)
        return {f: close.copy() for f in ["Open", "High", "Low", "Close", "Volume", "Amount"]}

    monkeypatch.setattr(df_mod.DataFetcher, "get_kline", fake_get_kline)
    sel = pd.DataFrame({"stock_code": ["000001"], "select_date": ["2026-06-30"]})
    df_mod.DataFetcher.get_kline_windowed(
        sel, period="5m", dividend_type="front", fill_data=False, use_cache=True)
    assert seen.get("use_cache") is True
```

- [ ] **Step 2: 跑测试确认 FAIL**

`pytest tests/test_kline_cache.py::test_get_kline_windowed_passes_use_cache -v`
预期：FAIL（`TypeError: get_kline_windowed() got an unexpected keyword argument 'use_cache'`）

- [ ] **Step 3: 最小实现**

`data_fetcher.py:256` 签名改为（`use_cache` keyword-only，放 `fill_data` 之后）：
```python
    @classmethod
    def get_kline_windowed(
        cls,
        selections: pd.DataFrame,
        period: str,
        window_trading_days: int = 45,
        dividend_type: str = "front",
        fill_data: bool = False,
        *,
        use_cache: bool = False,
    ) -> tuple:
```

`data_fetcher.py:309` 的桶内调用加 `use_cache=use_cache`：
```python
            data = cls.get_kline(
                codes,
                start_time=b_start.strftime("%Y%m%d"),
                end_time=b_end.strftime("%Y%m%d"),
                period=period,
                dividend_type=dividend_type,
                fill_data=fill_data,
                use_cache=use_cache,
            )
```

- [ ] **Step 4: 跑测试确认 PASS**

`pytest tests/test_kline_cache.py::test_get_kline_windowed_passes_use_cache -v`

- [ ] **Step 5: 提交**

```bash
git add core/data_fetcher.py tests/test_kline_cache.py
git commit -m "feat(data): get_kline_windowed 加 use_cache 形参 (三级漏斗接缝)"
```

---

### Task 2：engine 5m 路径透传 `use_cache`

**文件：**
- 改：`backtest/engine.py:793-796`
- 测试：`tests/test_engine_5m_window.py`

- [ ] **Step 1: 写失败测试（审计 H1 修订：复用本文件 `_run_5m` 模式，不引入不存在的符号）**

`tests/test_engine_5m_window.py` 现有公共驱动 `_run_5m`（`:29`）已封装 `BacktestEngine({'period':'5m'})` + mock `get_kline_windowed`（`:36`）+ `eng.run(...)`（`:54`）触发 5m 分支。改两处：

(a) `:36` 的 `fake_windowed` 签名补 `use_cache=False` 并捕获：
```python
    def fake_windowed(selections, period, window_trading_days, dividend_type,
                      fill_data, *, use_cache=False):
        called['window_trading_days'] = window_trading_days
        called['use_cache'] = use_cache          # 新增捕获
        return kline, mask
```

(b) 文件末尾新增用例：
```python
def test_engine_5m_passes_use_kline_cache(monkeypatch):
    """engine.use_kline_cache (默认 True) 应透传到 get_kline_windowed 的 use_cache。"""
    close, codes, idx = _make_5m()
    mask = pd.DataFrame(True, index=idx, columns=codes)
    called = _run_5m(monkeypatch, close, mask, capture={})
    assert called.get('use_cache') is True
```
（engine.py:721 `use_kline_cache` 默认 True，故 `{'period':'5m'}` 即为 True，无需显式传。）

- [ ] **Step 2: 跑确认 FAIL**

`pytest tests/test_engine_5m_window.py::test_engine_5m_passes_use_kline_cache -v`

- [ ] **Step 3: 实现**

`engine.py:793-796` 加一行 `use_cache=self.use_kline_cache,`：
```python
            kline, window_mask = DataFetcher.get_kline_windowed(
                selections, period=self.period,
                window_trading_days=win_td, dividend_type="front", fill_data=False,
                use_cache=self.use_kline_cache,
            )
```

- [ ] **Step 4: PASS**

- [ ] **Step 5: 提交**

```bash
git add backtest/engine.py tests/test_engine_5m_window.py
git commit -m "feat(engine): 5m 窗口路径透传 use_kline_cache 接入三级漏斗"
```

---

### Task 3：测试 fake 签名兼容（防回归）

**文件：**
- `tests/test_engine_5m_window.py:36` — `fake_windowed` 加 `use_cache=False` 形参（或 `**kwargs`）
- `tests/test_degrade_5m.py:341` — lambda 缺 `use_cache` 形参（**已 Read 确认**：`(selections, period, window_trading_days, dividend_type, fill_data)`，无 `**kwargs`），**必须**补 `use_cache=False` 或改 `**kwargs`，否则 engine 改后必然 `TypeError`（审计 M4：非"按需"是"必须"）

- [ ] **Step: 跑相关全量测试**

`pytest tests/test_engine_5m_window.py tests/test_degrade_5m.py tests/test_kline_cache.py tests/test_engine_run_path.py -v`
预期：全绿。任一 `TypeError: unexpected keyword argument 'use_cache'` 即漏改 fake。

---

### Task 4：冷缓存字节级 parity 验证（一次性脚本，非回归）

**文件：**
- 建：`tools/verify_5m_cache_parity.py`（独立新文件，不进回归套件）

对同一选股集合，分别 `use_cache=False` / `use_cache=True` 跑 `get_kline_windowed`，比对 `Close` 矩阵 `np.allclose(a, b, equal_nan=True, atol=1e-9)`。证明首轮缓存路径与 TDX 直拉同数据。手动跑一次，把结果贴到本计划书 §7。

```python
# tools/verify_5m_cache_parity.py 骨架
import numpy as np
from core.data_fetcher import DataFetcher

def main(selections, period="5m"):
    k_tdx, _ = DataFetcher.get_kline_windowed(selections, period=period,
                                              dividend_type="front", fill_data=False,
                                              use_cache=False)
    k_cache, _ = DataFetcher.get_kline_windowed(selections, period=period,
                                                dividend_type="front", fill_data=False,
                                                use_cache=True)
    # 审计 M2: 先断言 index/columns 完全相等再比值, reindex_like 会静默丢差异
    ct, cc = k_tdx["Close"], k_cache["Close"]
    assert ct.index.equals(cc.index), f"index 不一致: {ct.index.symmetric_difference(cc.index)[:5]}"
    assert ct.columns.equals(cc.columns), f"columns 不一致: {set(ct.columns) ^ set(cc.columns)}"
    ok = np.allclose(ct.values, cc.values, equal_nan=True, atol=1e-9)
    print(f"[parity] {'PASS' if ok else 'FAIL'}")
    return ok
```

---

## 5. 测试清单
- [ ] `test_get_kline_windowed_passes_use_cache`（新，Task 1）
- [ ] `test_engine_5m_passes_use_kline_cache`（新，Task 2）
- [ ] `test_engine_5m_window.py` 全套（fake 签名兼容后仍绿）
- [ ] `test_degrade_5m.py` 全套（fake 签名兼容后仍绿）
- [ ] `test_kline_cache.py` 全套（无改动，回归）
- [ ] `test_engine_run_path.py`（回归）
- [ ] 冷缓存 parity 脚本（手动，贴结果到 §7）

---

## 6. 风险与回滚

| 风险 | 缓解 |
|---|---|
| F4 前段截断（窗口起点早于缓存首日）| 缓存层 `kline_cache.py:184` `_ensure` 已对"请求起点早于缓存起点"告警（`kline_truncated`，审计 H2：是 cache 层不是 engine 层）；运维用 `backfill_kline_cache.py --period 5m --start <最早信号日>` 预热覆盖 |
| 缓存 5m 复权 shift（分红）| `KlineCache` F6 重叠 bar 检测已覆盖（`kline_cache.py:206-216`）|
| 桶范围比单股窗口宽 → 缓存多存 | 可接受（本地 parquet，首次后受益）|
| 测试 fake 漏改 `use_cache` → TypeError | Task 3 集中改（3 处，含 test_degrade_5m.py:341 必须）+ 全量跑 |
| 并行会话 git 撞车 | 改动小（3 源码文件），尽快分 Task 提交 |
| 并行 subprocess parquet 写竞态（审计 M5）| `_write_merge` 是 read-modify-write + `os.replace`，锁是 per-instance threading.Lock **不跨进程**（`kline_cache.py:56/273-287`）；1d 路径已有同样限制，5m 继承。缓解：批量并行前先 `warmup/backfill` 预热，避免运行时 miss-fetch 并发写；5m 窗口回测通常单进程串行，影响有限 |

**回滚**：`use_kline_cache: false` 一开关即回退 TDX 直拉（engine `:721` 已有该配置项与 fallback 路径）。

---

## 7. 验收标准（交付前逐条核）

1. §5 测试清单全绿。✅ (72 项四文件 + 全套 684 passed; code-reviewer APPROVE, LOW-1 反向测试已补)
2. 冷缓存 parity：`use_cache` True/False 的 `Close` 矩阵 `np.allclose(equal_nan=True)`。
   - 跑出结果: **PASS** (2026-07-18, 000001.SZ/600000.SH/002008.SZ × 信号日 2026-06-23, OHLCV+Amount 六字段全 PASS, shape (912,3))
   - ⚠️ 过程记录: 首跑 FAIL — 600000.SH 差 624 值, 比值 0.9546 = 浦发除权因子。TDX 客户端登录后因子表剧烈同步 (5m 缓存 9.26 / 1d 缓存 8.86 / TDX 直拉在 9.53→9.28→8.86 间漂移), 删 parquet+manifest 按当前 TDX 重建后, 1d/5m 同基准 (6-23 均 8.86), parity 转 PASS。**接缝代码无问题, 是数据层因子漂移** — 分红季温缓存可能短期与 TDX 直拉不一致 (F5 冷却 24h + F6 只在增量扩展时检测, 见校准实验报告 §数据层遗留)。
3. 二次回测（温缓存）：5m 桶拉取零 TDX 往返（日志无"获取 N 只股票 5m K线数据"行）。✅ (2026-07-18 实测: 温缓存 windowed 拉取 TDX 拉取日志行数 = 0, Close (912,3))
4. `degrade_5m` 开启时，对 6.23 缺口周仍正常降级（兜底位置不变）。✅ (degrade 代码零改动, test_degrade_5m.py 22 项全绿; 6.23 真实缺口已被 TDX 补齐, 降级正确空转; 真实缺口填充路径已由校准实验覆盖)

---

## 8. 执行选择（实施时）

落地时两种执行方式：
1. **子 agent 分任务**（推荐）：每 Task 派一个 fresh subagent，任务间 review。
2. **当前会话内联**：按 Task 顺序执行，checkpoint review。
