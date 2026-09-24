# data_fetcher 去上帝化计划书 · 第 1 轮审计报告

> 成文日期 2026-08-16 · 文档类型 审计报告 · 审计对象 `docs/plan/2026-08-16_data_fetcher去上帝化_计划书.md`（v1）

## 总评

方向对、事实核查通过率高（594 行/13 方法、16 个行号引用、调用方清单基本准确、「拆多类=加转发层」判断正确）。但 **2 处实质设计漏洞**，v1 不能直接照做。

## 必须改（2 处，均已改正）

1. **薄壳拆分结构空洞**：拉日历块（`data_fetcher.py:245-255`）依赖 groupby 产物 `first_sig.min()/last_sig.max()`，而按「trading_days 必传」设计这些量在纯函数内部，薄壳拿不到 `global_end`。→ v2 改用 `calendar_fetcher` 回调：拉日历留在纯函数体内、把「怎么拉」参数化，实现「逻辑一字不动搬 + 无 TDX 依赖」两全。
2. **合并日历会吞异常、破坏 server.py 兜底**：`get_trading_dates` 异常上抛、`server.py:455-467` 依赖它降级本地 JSON；委托 `get_trading_days` 后异常被吞为 `[]`。→ v2 撤回「合并日历」，改为「补 docstring 区分 raw/robust」。

## 建议改（均已吸收）

- 调用方计数修正：`get_trading_days` engine.py 实 1 处（898）、signal_day_cache 实 2 处（160/186），「sweep 工具」是 monkeypatch 点非调用方。
- 文案矛盾（§3.3 说统一参数顺序、阶段 3 又保留旧签名）→ 已统一为「保留旧签名」。
- 补 window.py 依赖清单（normalize_list / bisect / logger）。
- logger 名变更 + 测试 3 处调用点名 + 2 处 caplog scope。
- count=-1 差异、行数偏乐观（594→~517 非 490）、「有序」措辞收窄。

## 结论

v1 经审计修订为 v2，2 处实质漏洞 + 全部建议改正，27 处引用机器校验 100% 通过。v2 可进入第 2 轮审计。
