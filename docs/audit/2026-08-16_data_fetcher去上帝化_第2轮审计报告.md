# data_fetcher 去上帝化计划书 · 第 2 轮审计报告

> 成文日期 2026-08-16 · 文档类型 审计报告 · 审计对象 `docs/plan/2026-08-16_data_fetcher去上帝化_计划书.md`（v2）

## 总评

第 1 轮 3 条修订全部真改对了（calendar_fetcher 回调成立、合并日历彻底撤回、调用方计数全对）。但 v2 定稿前还有 2 处必改 + 5 处建议。

## 必须改（2 处，均已改正）

1. **`calendar_fetcher` 契约漏「已排序」声明**：`_window_end` 用 `bisect.bisect_left`（`data_fetcher.py:260`）依赖有序，契约只写 `(start_str,end_str)->List[Timestamp]`。→ v3 补「必须返回已升序去重 Timestamp」。
2. **依赖清单写错**：多了 numpy（两函数都不用）、漏了 typing（需 `List/Optional`）。→ v3 已改。

## 建议改（5 处，均已吸收）

3. re-export 缺模块级 import 会 NameError → 阶段 1 补「先 `from .window import merge_window_masks`」。
4. docstring「失败/空」过宽（代码只覆盖空/None，不覆盖回调抛异常）→ 改「空/None」。
5. monkeypatch 清单漏 4 个测试文件 → 已补。
6. 跨文档「合并日历」残留（深模块计划书:208）→ 已同步。
7. `data_fetcher.py:397` 注释「见 _merge_window_masks docstring」搬移后 stale → 实施时一并改。

## 结论

修完 2 处必改 + 5 处建议后，计划书进入最终定稿（v3）。27 处 `[文件:行号]` 引用机器校验 100% 通过。
