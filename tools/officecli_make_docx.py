# -*- coding: utf-8 -*-
"""把候选A阶段2审计报告 md 转成 Word(.docx) —— 用 officecli batch 一次性生成。"""
import json, subprocess, sys, pathlib

BIN = pathlib.Path(r"C:/Users/liuziheng/AppData/Local/OfficeCLI/officecli.exe")
OUT = pathlib.Path(r"e:/1target/VERA/docs/audit/2026-07-15_候选A阶段2_项目审计报告.docx")

cmds = []
def add(parent, typ, props=None):
    c = {"command": "add", "parent": parent, "type": typ}
    if props:
        c["props"] = props
    cmds.append(c)

def para(text, style="Normal", bold=False, italic=False, font=None, size=None, color=None):
    p = {"text": text, "style": style}
    if bold: p["bold"] = "true"
    if italic: p["italic"] = "true"
    if font: p["font"] = font
    if size: p["size"] = str(size)
    if color: p["color"] = color
    add("/body", "paragraph", p)

def heading(text, level=1):
    para(text, style=f"Heading{level}")

def bullet(text):
    para(text, style="ListBullet")

def code(text):
    add("/body", "paragraph", {"text": text, "font": "Consolas", "size": 9, "color": "444444"})

# 表格：第一行 set tc[1] + add cell 扩列；后续行 add row(预生成列) 后 set 各列
_table_counter = {"i": 0}
def table(rows):
    """rows: list of list of str, 第一行视为表头。"""
    _table_counter["i"] += 1
    ti = _table_counter["i"]
    add("/body", "table")
    tbl = f"/body/tbl[{ti}]"
    ncols = len(rows[0])
    # 第一行：set tc[1] + add cell 扩到 ncols
    for c in range(ncols):
        val = rows[0][c]
        if c == 0:
            cmds.append({"command": "set", "path": f"{tbl}/tr[1]/tc[1]",
                         "props": {"text": val, "bold": "true", "shading": "E7E6E6"}})
        else:
            add(f"{tbl}/tr[1]", "cell", {"text": val, "bold": "true", "shading": "E7E6E6"})
    # 后续行
    for r, row in enumerate(rows[1:], start=2):
        add(tbl, "row")
        for c in range(ncols):
            cmds.append({"command": "set", "path": f"{tbl}/tr[{r}]/tc[{c+1}]",
                         "props": {"text": row[c]}})

# ============ 内容 ============
para("候选 A 阶段 2 — 项目审计报告书", style="Title")

meta = [
    ("审计对象", "核心回测循环拆解(_simulate_core_v3 527 行 → backtest/loop/ 子包)"),
    ("审计日期", "2026-07-15"),
    ("分支", "feat/候选A阶段2-核心循环拆解"),
    ("基线 commit", "5c461b6(159 passed + 5 skipped)"),
    ("完成 commit", "45bcfb5(261 passed + 5 skipped)"),
    ("审计方法", "三角色并行独立审计(架构/代码质量/parity+危险发现)+ 直接读真实代码 + 跑测试复测"),
]
for k, v in meta:
    add("/body", "paragraph", {"text": f"{k}: {v}", "italic": "true", "color": "555555"})

heading("1. 一句话结论", 1)
para("核心循环拆解完成, 行为与原版字节级一致, 可合并。527 行单体函数拆成 16 个文件 / 1331 行的 Strategy 模式子包, 50 组 parity 字节级验证全绿, 性能退化 1.81x(< 2x 阈值), 现有 159 测试零改动全绿, 9 个最危险共识发现 8 个 VERIFIED + 1 个行号漂移(已修)。")

heading("2. 交付物清单", 1)
heading("2.1 新代码(backtest/loop/ 1331 行)", 2)
table([
    ["文件", "行", "职责"],
    ["state.py", "326", "BacktestParams/Context/Position/PositionBook/TradeBuffer/Bar/TradeColumns + dtype 断言"],
    ["loop.py", "275", "BacktestLoop.run() 主循环(卖出/买入/权益/期末)"],
    ["exit_engine.py", "120", "Priority 枚举 + ExitDispatcher(多结果模型)"],
    ["builder.py", "94", "39 参数 → BacktestLoop 对象图(兼容壳 + parity 共用)"],
    ["entry.py", "73", "EntryEngine(买入 + 换股 reason=1)"],
    ["equity.py", "46", "EquityTracker(权益曲线, 期末不平仓)"],
    ["absolute.py", "48", "FormulaSellStrategy(reason=12 绝对优先级)"],
    ["strategies/base.py", "75", "ExitStrategy/AbsoluteStrategy Protocol + TriggerResult"],
    ["strategies/{6个}.py", "227", "6 个策略 adapter(cost_stop/ladder_tp/trailing/time_stop/cond_time/first_day)"],
])

heading("2.2 engine.py 改动", 2)
bullet("_simulate_core_v3:527 行 → ~45 行兼容壳, 转调 BacktestLoop.run(), 39 参数签名零改动")
bullet("_simulate_core_v3_legacy:旧 527 行原样保留作 parity 甲骨文")
bullet("ENGINE_VERSION:v3.3-limit-up-filter-20260605 → v3.4-loop-refactor-20260714")
bullet("顶部 docstring 指向新结构;run_cached docstring 参数数 10 → 9(H2)")

heading("2.3 测试(985 行)", 2)
table([
    ["文件", "用例数", "职责"],
    ["test_loop_strategies.py", "32", "6 策略单测 + dtype 断言 + TradeBuffer + PositionBook swap-and-pop"],
    ["test_loop_dispatcher.py", "17", "3 优先级 + trailing_first 双触发 6 场景 + formula_sell + capability gating"],
    ["test_loop_parity.py", "50", "legacy vs 兼容壳字节级 parity(3 优先级×8 seed / 4 capability / 双触发 / 退市 / 停牌 / 连续 ladder / T+1 / bpday / cond_time / first_day / 无 high_low / 滑点印花 / max_position_pct / 空信号 / 换股)"],
    ["test_loop_perf.py", "3", "性能基准(CR1, < 2x 本地 / < 3x CI)"],
])
para("全量:261 passed, 5 skipped, 0 failed(1.81s)", bold=True)

heading("3. 三角色独立审计结论", 1)
heading("3.1 架构审计(architect agent)— PASS", 2)
para("6 项审计点全部通过, 逐行对照 legacy:")
table([
    ["审计点", "结论", "证据"],
    ["CA1 trailing_first 双触发", "PASS", "_eval_trailing_first 精确复刻 legacy:240-267 + 398-461, 双触发返回 [ladder_partial, trailing/cost]"],
    ["CA2/CA3 类型定义", "PASS", "BacktestParams 10 字段 / Context 17 字段完整覆盖所有策略读取"],
    ["CA4 execution_price 折叠", "PASS", "各策略 check 内算好填入 TriggerResult, 对齐 legacy:327-367"],
    ["执行逻辑", "PASS", "换股(1-commission)/退市/formula_partial/ladder 凑整全部对齐"],
    ["capability gating", "PASS", "builder 按 enabled 过滤, 禁用策略不进 dict"],
    ["优先级顺序", "PASS", "三分支 + 公共尾部 + formula_sell 绝对优先 + 退市先于一切"],
])
para("发现 4 个 LOW(死代码/注释/冗余, 无行为影响), 已在审计修复批次处理 3 个。")

heading("3.2 代码质量审计(python-reviewer agent)— PASS, 7.5/10", 2)
para("无 CRITICAL, 无 HIGH 级 bug。HIGH 全是类型标注缺失(H1-H5)。NaN 守卫完整保留, frozen dataclass 用得到位, 协议一致性无瑕疵, 文件均 < 800 行。")
para("已修复:H1-H5(类型标注 + R9 只读强制)、M2-M7(除零守卫 / assert→raise / dict 鲁棒 / dtype 归一 / 双触发不变量)、L1-L2(死 import)。修复后预估 8.5+/10。")

heading("3.3 parity + 危险发现审计(general-purpose agent)— 8 VERIFIED + 1 PARTIAL", 2)
para("9 个最危险共识发现:")
table([
    ["#", "危险发现", "状态", "证据"],
    ["1", "Dispatcher 单结果→多结果", "VERIFIED", "evaluate() -> List[TriggerResult], 双触发返回 2 元素"],
    ["2", "测试数量口径", "VERIFIED", "pytest tests/ 实跑 261 passed + 5 skipped"],
    ["3", "性能基准 < 2x", "VERIFIED", "独立复测 1.769x(与声称 1.81x 吻合)"],
    ["4", "parity 矩阵维度", "VERIFIED", "50 组全绿, 6 边界全覆盖"],
    ["5", "BacktestParams/Context 定义", "VERIFIED", "state.py:39-52 / 71-97 完整"],
    ["6", "execution_price 折叠", "VERIFIED", "TriggerResult 含字段, 无方法"],
    ["7", "Cash 归 loop", "VERIFIED", "loop.py:59 持有, 参数出入组件"],
    ["8", "阶段 4 排期完成", "VERIFIED", "兼容壳 ~45 行, ENGINE_VERSION bump, legacy 保留"],
    ["9", "formula_sell 规则在 engine.py", "PARTIAL→已修", "规则在 engine.py VERIFIED;行号 :24→:30 漂移, 已改按内容引用"],
])
para("6 个最危险边界场景全部被 parity 覆盖:trailing_first 双触发 / 多持仓 swap-and-pop / 连续 bar ladder bitmask 累计 / formula_sell+delisting 同 bar / 空信号 / T+1 bpday>1。")

heading("4. 修复批次(commit 45bcfb5)", 1)
para("根据三角色审计, 修复 12 项:")
table([
    ["级别", "项", "修复"],
    ["HIGH", "H1-H4", "补全 builder/loop.run/entry/absolute 类型标注"],
    ["HIGH", "H5/R9", "LadderTpStrategy 对 ladder_profits/ratios .copy() 强制只读"],
    ["MEDIUM", "M2", "BacktestParams.__post_init__ 校验 bpday/lot_size>=1"],
    ["MEDIUM", "M3", "TrailingStrategy 防御 entry_px==0"],
    ["MEDIUM", "M4", "assert_state_dtype 改 raise TypeError(防 python -O)"],
    ["MEDIUM", "M5", "_PRIORITY_ORDER 用 .get() 防 KeyError"],
    ["MEDIUM", "M6", "_execute_dual 双触发不变量注释"],
    ["MEDIUM", "M7", "builder np.asarray(ladder, dtype=float64) 归一化"],
    ["LOW", "L1-L2", "删死 import"],
    ["LOW", "LOW-3", "修 exit_engine trailing 注释"],
    ["—", "#9", "计划书 §10.5 formula_sell 引用改按内容(非易漂移行号)"],
    ["—", "CR1", "新增 test_loop_perf.py 固化性能基准"],
])
para("修复后全量 261 passed + 5 skipped, 零回归。")

heading("5. 残留风险与遗留项", 1)
heading("5.1 已知残留(非阻塞)", 2)
table([
    ["#", "项", "严重度", "说明"],
    ["1", "性能余量不大", "LOW", "1.81x 接近 2x 阈值。当前数据规模(500×100)OK, 真实大批量(2500×500)需复核。若成瓶颈, 给 Context/Position 加 __slots__ 或复用 buffer"],
    ["2", "_sell_bar 115 行", "LOW", "超 50 行准则, 但是主循环协调器, 拆分损可读性, 合理例外"],
    ["3", "退市逻辑内联在 loop", "LOW", "计划书原拟 PositionBook.evict_delisted(), 实际内联在 _sell_bar(与 sell 循环强耦合)。行为正确(parity 守护), 架构小偏离"],
    ["4", "pp 一名两用", "LOW", "loop.py 里 pp 是持仓指针, Context.pp 是盈亏比例。未改名(改名涉及 6 文件), 靠注释区分"],
    ["5", "Context 冗余字段", "LOW", "pos_high_px/pos_high_hi/hp_profit 无策略读取, 留作扩展"],
    ["6", "legacy 甲骨文保留", "INFO", "_simulate_core_v3_legacy 527 行保留供 parity。未来可删, 建议保留一个版本周期"],
])

heading("6. 验收门槛核对", 1)
heading("6.1 必须全绿", 2)
bullet("pytest tests/ — 261 passed + 5 skipped 零失败")
bullet("test_run_cached_parity.py — 7 个字节级 parity 零失败")
bullet("test_loop_parity.py — 50 组对照零失败")
bullet("test_loop_perf.py — 性能 < 2x(本地)/ < 3x(CI)")
heading("6.2 必须保留", 2)
bullet("_simulate_core_v3 函数签名(22 必需 + 17 可选, 全 positional)零改动")
bullet("run_cached 9 位置参数零改动")
bullet("trade 9 列含义(TradeColumns 锁定)")
bullet("equity_curve / trades DataFrame 形状")
bullet("现有日志格式(兼容壳用同一 logger, source name 不变)")
heading("6.3 必须有", 2)
bullet("每个 ExitStrategy adapter ≥1 独立单测(32 个策略单测)")
bullet("docs/architecture/loop.md(含 ATR 范例 + 60 分钟 checklist)")
bullet("AbsoluteStrategy 独立测试(formula_sell 3 种 priority 都先触发)")
bullet("dtype assertion(区分 int32/float64, raise TypeError)")
bullet("性能基准报告(test_loop_perf.py 固化)")
bullet("BacktestParams/Context/TriggerResult/Position/TradeColumns 完整定义")

heading("7. 业务铁律核对(CLAUDE.md)", 1)
table([
    ["铁律", "状态"],
    ["回测买入价 = 信号日 T 收盘价", "✅ EntryEngine 保留(entry.py 注释 + bp=price_np[i])"],
    ["两套买卖口径不混", "✅ 兼容壳/BacktestLoop 只走回测 T 收盘路径, 未碰 sim_trader"],
    ["策略评价给夏普/Calmar/回撤/胜率", "不在本次范围(metrics 未动)"],
    ["不破坏现有功能", "✅ 261 passed, 6 个直调 _simulate_core_v3 的测试文件零改动全绿"],
])

heading("8. 总体判定", 1)
table([
    ["维度", "判定"],
    ["架构正确性", "✅ PASS(6/6 审计点, 逐行对照 legacy)"],
    ["代码质量", "✅ PASS 7.5→8.5+/10(修复后)"],
    ["parity 字节级", "✅ 50 组全绿, 6 边界全覆盖"],
    ["危险发现修复", "✅ 9/9(8 VERIFIED + 1 已修)"],
    ["性能", "✅ 1.81x < 2x"],
    ["向后兼容", "✅ 签名零改动, 261 passed 零回归"],
    ["业务铁律", "✅ 不破坏"],
])
para("最终判定:可合并。系统从\"527 行单体函数\"演进为\"可单独测试/维护/扩展的 Strategy 模式子包\", 行为字节级不变, 6 个最危险共识发现全部解决。残留 6 项均为 LOW, 不阻塞。", bold=True)

heading("9. 回滚方法", 1)
code("git reset --hard 5c461b6   # 回到拆解前基线")
code("git checkout master        # 或只回滚本分支")

heading("10. 提交历史(本分支)", 1)
for line in [
    "45bcfb5 fix(候选A阶段2-审计修复): 类型标注+除零守卫+R9只读+鲁棒性+性能基准",
    "118b96e feat(候选A阶段2-stage5): 文档+ATR范例+修docstring/CLAUDE.md参数数",
    "5d2006a feat(候选A阶段2-stage4): 兼容壳+ENGINE_VERSION bump+50组parity",
    "d27edd8 feat(候选A阶段2-stage3): BacktestLoop主循环+字节级parity验证",
    "cb598b7 feat(候选A阶段2-stage2): ExitDispatcher多结果模型+FormulaSellStrategy",
    "e45d9f2 feat(候选A阶段2-stage1): 数据结构+6策略adapter+dtype断言",
    "57eca19 docs(候选A阶段2): 计划书v3修正版 + 基线快照",
    "5c461b6 (基线) feat(C5): 审计修复收尾",
]:
    code(line)

para("")
para("审计执行:2026-07-15, 三角色并行(architect + python-reviewer + general-purpose)+ 直接代码验证 + 测试复测", italic=True, color="555555")
para("审计结论:可合并。系统已就绪。", italic=True, color="555555", bold=True)

# ============ 执行 ============
if OUT.exists():
    OUT.unlink()
subprocess.run([str(BIN), "create", str(OUT)], check=True, capture_output=True)
import tempfile
tmpf = pathlib.Path(tempfile.gettempdir()) / "officecli_batch.json"
tmpf.write_text(json.dumps(cmds, ensure_ascii=False), encoding="utf-8")
r = subprocess.run([str(BIN), "batch", str(OUT), "--input", str(tmpf), "--stop-on-error"],
                   capture_output=True, text=True, encoding="utf-8")
try:
    d = json.loads(r.stdout)
    results = d.get("data", {}).get("results", [])
    ok = sum(1 for x in results if x.get("success"))
    fail = [x for x in results if not x.get("success")]
    print(f"commands total={len(cmds)} | item ok={ok} fail={len(fail)} | overall={d.get('success')}")
    for f in fail[:10]:
        print("  FAIL:", f.get("error") or f.get("message") or f)
except Exception as e:
    print("parse error:", e)
    print("stdout:", r.stdout[:2000])
    print("stderr:", r.stderr[:2000])
subprocess.run([str(BIN), "close", str(OUT)], capture_output=True)
print("written:", OUT, f"({OUT.stat().st_size} bytes)")
