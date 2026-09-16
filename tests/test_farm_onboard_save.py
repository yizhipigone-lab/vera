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
