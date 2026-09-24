# -*- coding: utf-8 -*-
"""v0_check: 公式农场 v0 离线回归总控(只读, 不碰 TDX)。

用法: python -X utf8 tools/formula_farm/v0_check.py
输出: scratch/formula_farm_v0/ 下 v0 报告 txt + 397 条 GBK .tni 打包样本。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tools.formula_farm import common, intake, dedupe, static_vetting, pack_tni  # noqa: E402

GONGSHI = common.GONGSHI_DIR
L = []


def log(s=""):
    print(s)
    L.append(s)


def main():
    os.makedirs(common.OUT_DIR, exist_ok=True)
    log("=" * 66)
    log("公式农场 v0 离线回归报告")
    log("=" * 66)

    # 1) intake
    records = intake.iter_md(GONGSHI)
    log("\n[1] intake: 解析 md %d 篇" % len(records))
    no_code = [r["file"] for r in records if not r["code"]]
    no_url = [r["file"] for r in records if not r["url"]]
    log("    无源码块: %d %s" % (len(no_code), no_code[:5]))
    log("    无来源URL: %d %s" % (len(no_url), no_url[:5]))

    # 2) dedupe
    log("\n[2] dedupe: 已做集合(URL + 源码哈希)")
    done = dedupe.build_done_set(GONGSHI, common.GS_TXT_DIR)
    log("    已做URL %d | 已做哈希 %d" % (len(done["urls"]), len(done["hashes"])))
    fresh, dup = [], {"url": 0, "hash": 0}
    for r in records:
        hits = dedupe.is_dup(r, done)
        if hits:
            for h in hits:
                dup[h] += 1
        else:
            fresh.append(r["file"])
    log("    语料中判'已做': %d/468 (url命中%d, hash命中%d) | 判'新': %d"
        % (len(records) - len(fresh), dup["url"], dup["hash"], len(fresh)))
    log("    注意: v0 语料=已做集合本身, 期望全判已做(重复/换标题经 hash 兜住)")

    # 3) vetting 基线
    res = static_vetting.build_clean(GONGSHI)
    log("\n[3] vetting: md %d → select %d → clean %d / excluded %d"
        % (res["total_md"], res["total_select"], res["clean_count"], res["excluded_count"]))
    bl = os.path.join(GONGSHI, "_clean_list.txt")
    base = open(bl, encoding="utf-8").read().splitlines()
    mine_list = [c["gs"] for c in res["clean"]]
    log("    clean 编号 == gongshi _clean_list.txt(397): %s"
        % ("PASS" if mine_list == base else "FAIL"))
    bj = json.load(open(os.path.join(GONGSHI, "_clean_formulas.json"), encoding="utf-8"))
    theirs = {(e["gs"], e["file"]): set(e["tokens"]) for e in bj["excluded"]}
    minex = {(e["gs"], e["file"]): set(e["tokens"]) for e in res["excluded"]}
    log("    excluded(gs+file+tokens) == 基线 51 条: %s"
        % ("PASS" if minex == theirs else "FAIL"))

    # 4) L2 未来函数信息扫描(干净集, 非硬闸)
    l2 = static_vetting.l2_future_scan(res["clean"])
    log("\n[4] L2 更严黑名单信息扫描(干净集 %d 条):" % len(res["clean"]))
    if l2:
        tokc = {}
        for e in l2:
            for t in e["tokens"]:
                tokc[t] = tokc.get(t, 0) + 1
        log("    会再被剔除 %d 条: %s" % (len(l2), dict(tokc)))
        for e in l2[:8]:
            log("      %s %s %s" % (e["gs"], e["file"], e["tokens"]))
    else:
        log("    0 条(干净集无更严黑名单命中)")

    # 5) pack round-trip (397 条 GBK 明文 .tni)
    rows = [{"gs": c["gs"], "url": c["url"], "code": c["code"]} for c in res["clean"]]
    blob = pack_tni.build_pack(rows)
    secs = pack_tni.parse_pack(blob)
    ok = len(secs) == len(rows)
    if ok:
        for a, b in zip(secs, rows):
            if a["name"] != b["gs"] or a["code"].rstrip() != b["code"].rstrip():
                ok = False
                break
    out = os.path.join(common.OUT_DIR, "farm_v0_%d条.tni" % len(rows))
    with open(out, "wb") as f:
        f.write(blob)
    log("\n[5] pack round-trip: %d 段, 源码无损(含中文): %s" % (len(secs), "PASS" if ok else "FAIL"))
    log("    产物: %s (%d bytes)" % (out, len(blob)))
    if ok and rows and "中文变量" not in secs[0]["code"]:
        cn = sum(1 for s in secs if any("\u4e00" <= ch <= "\u9fff" for ch in s["code"]))
        log("    含中文源码的段数: %d" % cn)

    # 6) verdict
    allpass = (mine_list == base) and (minex == theirs) and ok
    log("\n" + "=" * 66)
    log("V0 判定: %s" % ("PASS — 复现 gongshi 基线, 可进入 v1 真爬" if allpass else "FAIL"))
    log("=" * 66)
    return 0 if allpass else 1


if __name__ == "__main__":
    sys.exit(main())
