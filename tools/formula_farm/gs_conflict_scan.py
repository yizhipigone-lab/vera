# -*- coding: utf-8 -*-
"""gs_conflict_scan: 查 GS 编号撞号, 输出「入库了但从未被评估」的公式清单。

背景 (2026-09-17 撞号事件):
  GS 号是入库时按全局计数发的, 起点取自 gs_txt 文件名 + TDX 公式树里的最大编号。
  gs_txt 导出是另一步骤, 两次入库之间该最大值可能还没刷新 → 号被重复发放
  (同一天就撞: 2026-09-06 批 GS0649 指向两个不同公式)。
  粗扫目标集按 **GS 号** 算 (_pending → _sweep), 号被最早那条占用后, 后批次那条
  永远不进目标集 —— 实测累计入库 857 条公式, 真正被评估的只有 822 条, 35 条静默漏掉。

  发号起点已修 (core.farm_ledger.max_gs_number 并入取值来源, daily_run._next_gs_name),
  本工具用来**出丢失清单**并**复查以后不再新增撞号**。

用法:
    python tools/formula_farm/gs_conflict_scan.py           # 只打印
    python tools/formula_farm/gs_conflict_scan.py --write   # 另存清单 MD
"""
import argparse
import json
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

RUNS = os.path.join(ROOT, "data", "formula_farm", "runs")
DATA = os.path.join(ROOT, "data", "formula_farm")


def log(s):
    print(s, flush=True)


def onboard_items():
    """[(batch, gs, file)] —— 只收 ok=True 的。"""
    out = []
    import glob
    for fp in sorted(glob.glob(os.path.join(RUNS, "*", "onboard.json"))):
        try:
            with open(fp, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        batch = d.get("date") or os.path.basename(os.path.dirname(fp))
        for it in d.get("items", []):
            if it.get("ok") and it.get("gs") and it.get("file"):
                out.append((batch, it["gs"], it["file"]))
    return out


def conflicts():
    """{gs: [(batch, file), ...]} —— 只留同一个号对应多个不同文件的。

    返回项按批次日期升序: 第一条是「占了号、有成绩」的, 其余是「被漏掉」的。
    """
    by_gs = defaultdict(set)
    for batch, gs, f in onboard_items():
        by_gs[gs].add((batch, f))
    out = {}
    for gs, pairs in by_gs.items():
        if len({f for _, f in pairs}) > 1:
            out[gs] = sorted(pairs)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="另存清单 MD")
    args = ap.parse_args()

    items = onboard_items()
    conf = conflicts()
    files = {f for _, _, f in items}
    lost = [f for gs, pairs in conf.items() for _, f in pairs[1:]]
    log("入库成功条目 %d 条 / 不同公式文件 %d 个" % (len(items), len(files)))
    log("撞号 GS 号 %d 个, 被漏掉(从未被粗扫评估)的公式 %d 条"
        % (len(conf), len(lost)))

    if not conf:
        log("没有撞号 —— 发号起点修复后应长期保持这个结果")
        return 0

    arch_path = os.path.join(DATA, "archive.json")
    try:
        with open(arch_path, encoding="utf-8") as f:
            arch = json.load(f)
    except Exception:
        arch = {}

    lines = ["# GS 编号撞号 —— 入库了但从未被评估的公式清单", "",
             "- 生成日期: %s" % __import__("time").strftime("%Y-%m-%d"),
             "- 累计入库公式 %d 条, 其中被评估 %d 条, 撞号漏掉 %d 条"
             % (len(files), len(arch), len(lost)),
             "- 漏掉原因: GS 号按全局计数发放, 但起点取值漏了「账本里已发过的号」, "
             "两次入库之间号被复用; 粗扫目标集按 GS 号算, 号被最早那条占用后 "
             "后批次那条不再进目标集。",
             "- 现状: 发号起点已修 (见 core/farm_ledger.next_gs_number); "
             "本清单为存量留档, 用户 2026-09-17 决定暂不补扫 (补扫需重走通达信界面入库)。",
             "",
             "| GS 号 | 已评估(保住号的) | 从未评估(漏掉的) | 漏掉那条的入库批次 |",
             "|---|---|---|---|"]
    for gs in sorted(conf):
        pairs = conf[gs]
        keep = pairs[0]
        for batch, f in pairs[1:]:
            lines.append("| %s | %s | %s | %s |"
                         % (gs, keep[1], f, batch))
    lines += ["", "## 补扫说明", "",
              "若要补这 %d 条: 需在通达信开着的情况下重新入库 (会拿到新 GS 号), "
              "再用 `farm_backtest.py --rebuild-archive` 重建档案。" % len(lost), ""]
    body = "\n".join(lines)

    for gs in sorted(conf)[:10]:
        pairs = conf[gs]
        log("   %s: 保住 %s | 漏掉 %s (%s)"
            % (gs, pairs[0][1][:28], pairs[1][1][:28], pairs[1][0]))

    if args.write:
        import time
        out = os.path.join(DATA, "%s_GS撞号未评估公式清单.md"
                           % time.strftime("%Y-%m-%d"))
        with open(out, "w", encoding="utf-8") as f:
            f.write(body)
        log("已写出 %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
