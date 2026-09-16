# -*- coding: utf-8 -*-
"""common.save_onboard 合并记账测试 (2026-09-16, 不起 TDX/GUI)。

2026-09-16 复审 M4: 函数自 farm_onboard 迁入 formula_farm/common.py ——
原实现让本测试 import farm_onboard, 连带 psutil/pyautogui/pywinauto 硬 import,
新环境 pytest 收集即炸; 账本落盘是纯数据逻辑, 不该拖 GUI 依赖。

锁定两条铁律:
1. 合并写 —— 历史 ok/编译失败 记录不被新一轮覆盖冲掉 (2026-09-06 血泪教训,
   冲掉 → 断点续跑失效 → 同一公式重复入库);
2. 同文件取最新一轮结果 (重试成功的要盖掉旧的失败记录)。
"""
import json

from tools.formula_farm.common import save_onboard
from core.farm_ledger import next_gs_number


def _read(ob_path):
    return {it["file"]: it
            for it in json.loads(ob_path.read_text(encoding="utf-8"))["items"]}


def test_merge_preserves_history(tmp_path):
    ob = tmp_path / "onboard.json"
    save_onboard(str(ob), "2026-09-16",
                 [{"file": "a.md", "ok": True, "msg": "", "gs": "GS0001"},
                  {"file": "b.md", "ok": False, "msg": "编译失败", "gs": "GS0002"}])
    # 新一轮只入一条 —— 旧的两条必须还在 (逐条记账场景)
    save_onboard(str(ob), "2026-09-16",
                 [{"file": "c.md", "ok": True, "msg": "", "gs": "GS0003"}])
    items = _read(ob)
    assert set(items) == {"a.md", "b.md", "c.md"}
    assert items["a.md"]["ok"] is True
    assert items["b.md"]["msg"] == "编译失败"


def test_same_file_latest_wins(tmp_path):
    ob = tmp_path / "onboard.json"
    save_onboard(str(ob), "2026-09-16",
                 [{"file": "a.md", "ok": False, "msg": "EXC 超时", "gs": "GS0001"}])
    # 同一文件重试成功 → 盖掉旧失败记录 (EXC 类失败允许重试, 与编译失败终态不同)
    save_onboard(str(ob), "2026-09-16",
                 [{"file": "a.md", "ok": True, "msg": "", "gs": "GS0050"}])
    items = _read(ob)
    assert len(items) == 1
    assert items["a.md"]["ok"] is True
    assert items["a.md"]["gs"] == "GS0050"


# ---- 2026-09-17 GS 撞号事件: 发号起点必须把账本算进去 ----

def test_next_gs_number_uses_ledger_when_gs_txt_stale(tmp_path):
    """撞号根因防回归: gs_txt 还是旧的(最大 GS1285)、账本已发到 GS1318 时,
    必须发 GS1319 —— 否则号被复用, 后批次那条公式永远不被粗扫评估。

    真实事故: 累计入库 857 条公式, 因撞号只评估了 822 条, 35 条静默漏掉。
    """
    runs = tmp_path / "runs"
    (runs / "2026-09-06").mkdir(parents=True)
    save_onboard(str(runs / "2026-09-06" / "onboard.json"), "2026-09-06",
                 [{"file": "a.md", "ok": True, "msg": "", "gs": "GS1285"},
                  {"file": "b.md", "ok": True, "msg": "", "gs": "GS1318"}])
    gs_txt = tmp_path / "gs_txt"
    gs_txt.mkdir()
    (gs_txt / "gs_1_GS1285_旧导出.txt").write_text("x", encoding="utf-8")
    # gs_txt 只到 1285, 树也没读到 → 账本的 1318 必须胜出
    assert next_gs_number(str(gs_txt), str(runs), tree_max=0) == 1319


def test_next_gs_number_tree_max_wins_when_larger(tmp_path):
    """TDX 树里已存在更大的号 (手工加过公式) 时, 以最大者为准。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    assert next_gs_number(str(tmp_path / "no_such_dir"), str(runs),
                          tree_max=2000) == 2001


def test_next_gs_number_from_empty(tmp_path):
    """空目录从 GS0001 起 (不发生异常)。"""
    assert next_gs_number(str(tmp_path / "nope"), str(tmp_path), tree_max=0) == 1


def test_next_gs_number_counts_failed_entries(tmp_path):
    """失败条目也占过号 (TDX 树里可能已建了节点) → 也要算进去, 否则重发。"""
    runs = tmp_path / "runs"
    (runs / "2026-09-11").mkdir(parents=True)
    save_onboard(str(runs / "2026-09-11" / "onboard.json"), "2026-09-11",
                 [{"file": "c.md", "ok": False, "msg": "编译失败", "gs": "GS1500"}])
    assert next_gs_number(str(tmp_path / "nope"), str(runs), tree_max=0) == 1501

