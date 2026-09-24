# -*- coding: utf-8 -*-
"""voided_scan: 已入库公式按现行黑名单全量回扫 → 并进作废名单。

为什么需要它 (2026-09-17 PLOYLINE 事件):
  采集闸门只在「入库那一刻」跑一次体检 (farm_check → static_vetting.vet_runtime),
  入库之后黑名单再加严, 历史公式不会自动重判 —— GS0607/GS1318 等就是这么漏进达标榜的。
  本工具补上「存量回扫」这一步, 并把结果写进 `data/formula_farm/voided.json`。

纪律 (与用户 2026-09-17 决定一致):
  - **只增不删**: 已在名单里的条目不覆盖、不删除 (人工填的作废原因优先);
  - 默认只报告不落盘, 加 `--apply` 才写;
  - 判定一律复用 static_vetting.vet_runtime (与入库闸门同一份规则, 不写第二份黑名单)。

同时对外提供**扫前复检**接口供粗扫调用 (farm_backtest):
  - `intake_records()` 一次构建 {file: rec}; `guard(gs, file, records)` 命中即登记并返回原因。
  这样以后黑名单再加 token, 旧公式只要被重新扫到就会自动拦下, 不依赖人记得回扫
  —— 本次事故的根本原因就是「门禁只在入库那一刻跑一次」。

用法:
    python tools/formula_farm/voided_scan.py            # 只报告
    python tools/formula_farm/voided_scan.py --apply    # 写入名单
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from tools.formula_farm import intake, static_vetting  # noqa: E402

RUNS = os.path.join(ROOT, "data", "formula_farm", "runs")
VOIDED = os.path.join(ROOT, "data", "formula_farm", "voided.json")
ARCHIVE = os.path.join(ROOT, "data", "formula_farm", "archive.json")


def log(s):
    print(s, flush=True)


def onboarded_items():
    """所有批次 onboard.json 里 ok=True 的条目 → [(gs, file, batch)]。"""
    import glob
    out = []
    for fp in sorted(glob.glob(os.path.join(RUNS, "*", "onboard.json"))):
        try:
            with open(fp, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        batch = os.path.basename(os.path.dirname(fp))
        for it in d.get("items", []):
            if it.get("ok") and it.get("gs") and it.get("file"):
                out.append((it["gs"], it["file"], batch))
    return out


def scan_onboarded(records_by_file):
    """对每个已入库文件跑体检 → {file: [reasons]} (只含命中的)。"""
    hits = {}
    for fname, rec in records_by_file.items():
        ok, reasons = static_vetting.vet_runtime(rec)
        if not ok:
            hits[fname] = reasons
    return hits


def load_voided():
    try:
        with open(VOIDED, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"updated": "", "note": "", "items": {}}


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def intake_records():
    """一次构建 {file: rec} (粗扫扫前复检复用, 避免每条目标都重扫 intake)。"""
    return {r["file"]: r for r in intake.iter_intake() if r.get("file")}


def guard(gs, file, records):
    """扫前复检: 返回作废原因(并登记) 或 None(放行)。

    ① 已在名单 → 直接返回原原因(不重复登记);
    ② 否则现场跑 vet_runtime, 命中就登记并返回原因。
    """
    known = (load_voided().get("items") or {}).get(gs)
    if known:
        return known.get("reason") or "已在作废名单"
    rec = records.get(file)
    if not rec:
        return None                      # 源码找不到就不拦(交给原有流程报错)
    ok, reasons = static_vetting.vet_runtime(rec)
    if ok:
        return None
    reason = "; ".join(reasons)
    register_void(gs, reason, file)
    return reason


def _entry(gs, reason, source_file, prev="(由 voided_scan 回扫登记)"):
    """作废条目 (带作废前的成绩留证, 便于事后追溯)。"""
    arch = _read_json(ARCHIVE).get(gs) or {}
    best = arch.get("best") or {}
    return {"date": time.strftime("%Y-%m-%d"), "reason": reason,
            "prev_verdict": (arch.get("verdict") or {}).get("code", prev),
            "prev_annret": best.get("annret"),
            "source_file": source_file}


def register_void(gs, reason, source_file="", prev="(由 voided_scan 回扫登记)"):
    """把一条并进作废名单 (只增不删 + 原子写)。返回 True=新登记, False=已存在。"""
    doc = load_voided()
    items = doc.get("items") or {}
    if gs in items:
        return False
    items[gs] = _entry(gs, reason, source_file, prev)
    doc["items"] = items
    _write(doc)
    return True


def _write(doc):
    """原子写 voided.json (tmp + replace, 防半截文件)。"""
    doc["updated"] = time.strftime("%Y-%m-%d")
    doc.setdefault("note", "公式作废名单 — 唯一真相源 (voided_scan 回扫 + 人工登记)")
    tmp = VOIDED + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, VOIDED)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写入 voided.json (默认只报告)")
    args = ap.parse_args()

    items = onboarded_items()
    files = {f for _, f, _ in items}
    log("已入库成功条目 %d 条, 不同公式文件 %d 个" % (len(items), len(files)))

    records_by_file = {r["file"]: r for r in intake.iter_intake()
                       if r.get("file") in files}
    log("能解析到源码的 %d 个 (缺失 %d 个)"
        % (len(records_by_file), len(files) - len(records_by_file)))

    hits = scan_onboarded(records_by_file)
    # 同一文件可能被多条 GS 引用 (撞号), 取 GS 列表
    gs_of = {}
    for gs, f, _ in items:
        gs_of.setdefault(f, []).append(gs)

    doc = load_voided()
    known = doc.get("items") or {}
    new = {}
    for fname, reasons in sorted(hits.items()):
        for gs in sorted(set(gs_of.get(fname, []))):
            if gs in known:
                continue
            new[gs] = _entry(gs, "; ".join(reasons), fname)

    log("回扫命中 %d 个文件; 已在名单 %d 条; 本次新增 %d 条"
        % (len(hits), len(known), len(new)))
    if new:
        log("新增明细 (前 20 条):")
        for gs, v in sorted(new.items())[:20]:
            log("   %s  %s  <- %s" % (gs, v["reason"][:46], v["source_file"][:38]))
        if len(new) > 20:
            log("   ... 另有 %d 条" % (len(new) - 20))

    if not args.apply:
        log("\n[只报告模式] 未写入。要落盘请加 --apply")
        return 0

    merged = dict(known)
    merged.update(new)
    doc["items"] = merged
    _write(doc)
    log("\n已写入 %s: 名单共 %d 条" % (VOIDED, len(merged)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
