# -*- coding: utf-8 -*-
"""farm_ledger — 公式农场入库账本 (纯数据逻辑, 零 GUI/零 TDX 依赖)。

2026-09-16 看板审计收口: save_onboard / load_done_files / load_onboard_index
原在 tools/formula_farm/common.py, 看板 (core/farm_summary) 要用 → 出现
core→tools 分层倒挂。迁来 core 后引用方向归正 (tools→core, 与 farm_rules
同一先例); tools/formula_farm/common.py 做兼容再导出, 既有调用方零改动。

- save_onboard: 每入一条立即合并记账 (中途停止不丢账, 2026-09-16);
- load_done_files: 断点跳过集 (ok + 编译失败终态);
- load_onboard_index: 全库 ok 条目索引 (取最早入库批次);
- next_gs_number: 下一个可用 GS 编号 (发号防撞号, 2026-09-17)。
"""
import glob
import json
import os
import re
import time


def save_onboard(ob_path, date_str, items):
    """合并写 onboard.json (读旧 → 按文件合并 → 原子替换落盘)。

    2026-09-06 血泪教训: 覆盖写会把历史 ok 记录冲掉 → 断点失效重复入库,
    所以一律合并写 (同文件取最新一轮的结果)。
    供 farm_onboard「每入一条立即记账」逐条调用 (中途停止不丢账)。
    """
    merged = {}
    if os.path.exists(ob_path):
        try:
            with open(ob_path, encoding="utf-8") as f:
                for it in json.load(f).get("items", []):
                    merged[it.get("file")] = it
        except Exception:
            pass
    for it in items:
        merged[it["file"]] = it
    tmp = ob_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"date": date_str, "finished_at": time.strftime("%H:%M:%S"),
                   "items": list(merged.values())}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ob_path)


def load_done_files(runs_dir):
    """断点跳过集: 所有 onboard.json 里 ok 或「编译失败」的 file 集合。

    编译失败同样终态跳过 (TDX 确定性拒绝, 重试永远失败, 2026-09-06 熔断空转教训)。
    """
    done = set()
    for p in glob.glob(os.path.join(runs_dir, "*", "onboard.json")):
        try:
            with open(p, encoding="utf-8") as f:
                for it in json.load(f).get("items", []):
                    if it.get("ok") or "编译失败" in (it.get("msg") or ""):
                        done.add(it.get("file"))
        except Exception:
            pass
    return done


def load_onboard_index(runs_dir, logger=None):
    """所有 onboard.json 的 ok 条目 → {gs: {file, url, date}} (取最早入库批次)。"""
    _log = logger or (lambda s: None)
    idx = {}
    for fp in glob.glob(os.path.join(runs_dir, "*", "onboard.json")):
        try:
            with open(fp, encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:                                   # noqa: BLE001
            _log("   ! 读 %s 失败: %r" % (os.path.basename(fp), e))
            continue
        date = d.get("date") or os.path.basename(os.path.dirname(fp))
        for it in d.get("items", []):
            gs = it.get("gs")
            if not gs or not it.get("ok"):
                continue
            cur = idx.get(gs)
            if cur is None or date < cur["date"]:
                idx[gs] = {"file": it.get("file", ""), "url": it.get("url", ""),
                           "date": date}
    return idx


def next_gs_number(gs_txt_dir, runs_dir, tree_max=0):
    """下一个可用的 GS 编号 = max(gs_txt 文件名里的号, 账本里发过的号, TDX 树里的号) + 1。

    2026-09-17 撞号事件: 发号起点原先只看 gs_txt 文件名 + TDX 公式树里的最大编号,
    而 gs_txt 导出是另一个步骤 —— 两次入库之间这个最大值可能还没刷新, 于是号被
    重复发放 (同一天就撞: 2026-09-06 批 GS0649 指向两个不同公式)。后果是后批次
    那条公式因 GS 号已被占用而永远不进粗扫目标集 (累计入库 857 条, 实际只评估 822)。

    账本是「发过哪些号」的唯一权威记录 (每入一条立即写, 中途停止不丢账), 故并入取值。
    纯函数 (只读目录), GUI 取 TDX 树那步留在调用方, 便于单测。
    """
    best = 0
    for p in glob.glob(os.path.join(gs_txt_dir, "gs_*_GS*.txt")):
        m = re.search(r"GS(\d+)", os.path.basename(p))
        if m:
            best = max(best, int(m.group(1)))
    for p in glob.glob(os.path.join(runs_dir, "*", "onboard.json")):
        try:
            with open(p, encoding="utf-8") as f:
                items = json.load(f).get("items", [])
        except Exception:
            continue
        for it in items:
            digits = (it.get("gs") or "")[2:]
            if digits.isdigit():
                best = max(best, int(digits))
    return max(best, tree_max) + 1
