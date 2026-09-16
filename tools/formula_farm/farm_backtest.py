# -*- coding: utf-8 -*-
"""farm_backtest: 闸门④ — 对入库公式跑 5m 粗扫(36 代表组合) + 出报告。

2026-09-11 审计驱动的修复 (详见 docs/plan/2026-09-11_公式农场粗扫报告修复_计划书.md):

- **F1 不再静默漏扫**: 目标集 = 所有 `runs/*/onboard.json` 里 ok 且**尚无有效 sweep 结果**
  的公式 (跨批次补扫, 旧的优先 → 不会被新批次饿死); `--max-formulas 0`(默认) = 全部扫完。
  日志与报告如实写「本批 N 条 / 本轮 M 条 / 余 K 条」。
  (旧行为: 固定 `items[:10]`, 日志还打 [i/20] 分母, 剩下 10 条既没报也没补扫机制。)
- **F2 报告抬头**: 全口径 (池/周期/复权/本金/单票上限/买入价/优先级) + 达标线 +
  **声明区间 vs 实测窗口** (实测窗口取 prep 缓存 meta 的 5m bar 首末 —— 旧报告写
  "20240801~20260911", 实际数据只有 2024-09-11 起)。
- **F3 判定**: 逐行给 达标/未达标/样本不足/无有效组合 + 人话原因; 「最优组合」
  只在笔数 ≥ 20 的行里选 (旧行为: 按年化取最大 → 3 笔 100% 胜率、卡玛 8.23 的
  GS1292 被当成最优)。
- **口径唯一真相源** = `core/farm_rules` (年化≥15% 且 |回撤|≤15% 且 笔数≥20);
  本文件不再自己写任何阈值。
- 飞书推送结果如实记录 (旧代码无论成败都打印"飞书已推送")。

用法:
    python tools/formula_farm/farm_backtest.py                 # 全部待扫 + 出报告
    python tools/formula_farm/farm_backtest.py --max-formulas 5
    python tools/formula_farm/farm_backtest.py --only GS1295,GS1296 --no-push
"""
import argparse
import csv
import glob
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
PY = sys.executable

from core import farm_rules  # noqa: E402

RUNS = os.path.join(ROOT, "data", "formula_farm", "runs")
REPORTS = os.path.join(ROOT, "data", "formula_farm", "reports")
SWEEP = os.path.join(ROOT, "tools", "gs_5m_sweep.py")
COMBOS36 = os.path.join(ROOT, "output", "gs_filter", "coarse_subset36.json")
SWEEP_OUT = os.path.join(ROOT, "output", "gs_5m_sweep")

#: 报告抬头里的口径 (与 gs_5m_sweep 的默认值一致; 改口径要同时改这两处口径常量)
CALIBER = {"universe": "沪深300 (TDX type 23)", "period": "5m", "dividend": "前复权",
           "capital": 3_000_000.0, "max_buy": 20_000.0,
           "entry": "信号日 T 最后一根 5m bar (15:00) 收盘买入",
           "priority": "移动止盈优先", "combos": 36}

_ROWS_CACHE: dict[str, list] = {}


def log(s):
    print(s, flush=True)


# ────────────────────────── 目标集 (F1) ──────────────────────────

def _latest_onboard_items() -> tuple:
    """最新一次入库批次 (按文件 mtime) → (date, {gs: item})。

    2026-09-11 实测踩到: 「本批」必须按**最新 onboard.json** 定, 不能按"每个公式
    最早入库日" —— 同一公式会出现在多个批次文件里 (09-06 那批 819 条与 09-11 那批
    20 条有重叠), 按最早日归类会把"本批 20 条"缩成 2 条。
    """
    cands = glob.glob(os.path.join(RUNS, "*", "onboard.json"))
    if not cands:
        return "", {}
    fp = max(cands, key=os.path.getmtime)
    try:
        d = json.load(open(fp, encoding="utf-8"))
    except Exception as e:                                       # noqa: BLE001
        log("   ! 读 %s 失败: %r" % (fp, e))
        return "", {}
    batch = {it["gs"]: it for it in d.get("items", [])
             if it.get("ok") and it.get("gs")}
    return (d.get("date") or os.path.basename(os.path.dirname(fp))), batch


def _onboard_index() -> dict:
    """所有 onboard.json 的 ok 条目 → {gs: {file, url, date}} (取最早入库批次)。"""
    idx: dict[str, dict] = {}
    for fp in glob.glob(os.path.join(RUNS, "*", "onboard.json")):
        try:
            d = json.load(open(fp, encoding="utf-8"))
        except Exception as e:                                   # noqa: BLE001
            log("   ! 读 %s 失败: %r" % (os.path.basename(fp), e))
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


def _rows_of(gs: str) -> list:
    """该公式全部 sweep CSV 的有效行 (error 行剔除); 结果进程内缓存。"""
    if gs in _ROWS_CACHE:
        return _ROWS_CACHE[gs]
    rows = []
    for fp in glob.glob(os.path.join(SWEEP_OUT, gs, "sweep_*.csv")):
        try:
            with open(fp, encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    if r.get("error"):
                        continue
                    try:
                        r["_ann"] = float(r["annret"])
                    except (TypeError, ValueError):
                        continue
                    rows.append(r)
        except Exception as e:                                   # noqa: BLE001
            log("   ! 读 %s 失败: %r" % (fp, e))
    _ROWS_CACHE[gs] = rows
    return rows


def _window_of(gs: str):
    """该公式实测数据窗口 (首末 5m bar 日期) —— 取 prep 缓存 meta 的 index。"""
    for fp in glob.glob(os.path.join(SWEEP_OUT, gs, "**", "meta.json"),
                        recursive=True):
        try:
            idx = json.load(open(fp, encoding="utf-8")).get("index") or []
        except Exception:                                        # noqa: BLE001
            continue
        if idx:
            return (str(idx[0])[:10], str(idx[-1])[:10])
    return None


def _pending(idx: dict, include_done: bool) -> list:
    """待扫公式 (无有效结果、且没被"零信号"停牌), 按入库批次升序 → 旧的不被新批次饿死。"""
    out = [g for g in idx
           if (include_done or (not _rows_of(g) and not _parked(g)))]
    return sorted(out, key=lambda g: (idx[g]["date"], g))


def _park_marker(gs: str) -> str:
    """「TDX 无此公式 / 区间零信号」的停牌标记路径。

    这类公式每次 prep 都白跑一遍 (还要多跑一次注定失败的 run) —— 2026-09-11 实测
    40 条待扫里 38 条属于此类, 白烧半个多小时。停牌后不再进待扫清单, 报告里照常
    以「无有效组合」列出来, 人工确认后用 --include-done 或删标记即可重扫。
    """
    return os.path.join(SWEEP_OUT, gs, "NO_SIGNALS.txt")


def _parked(gs: str) -> bool:
    return os.path.exists(_park_marker(gs))


# ────────────────────────── 粗扫 ──────────────────────────

def _sweep(gs: str, start: str, end: str) -> str:
    """跑 prep/run/report 三段; 返回错误说明 (空串 = 成功)。

    prep 报 `no_signals` (公式不在 TDX 或区间内零信号) → **短路**: 直接停牌返回,
    不再往下跑 run/report (旧代码会拿缺失的 cache 去跑 run, 抛
    `FileNotFoundError: meta.json` → 被记成"闸门失败", 其实公式本身就没信号)。
    """
    for cmd, extra in (("prep", []),
                       ("run", ["--stage", "refine", "--combos-file", COMBOS36]),
                       ("report", [])):
        r = subprocess.run([PY, "-X", "utf8", SWEEP, cmd, gs,
                            "--start", start, "--end", end] + extra,
                           cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=5400)
        out = (r.stdout or "") + (r.stderr or "")
        if '"status": "no_signals"' in out or '"status":"no_signals"' in out:
            os.makedirs(os.path.dirname(_park_marker(gs)), exist_ok=True)
            with open(_park_marker(gs), "w", encoding="utf-8") as f:
                f.write("prep 报 no_signals (%s): TDX 无此公式 或 区间 %s~%s 内零信号\n"
                        % (time.strftime("%Y-%m-%d %H:%M"), start, end))
            return "NO_SIGNALS"
        if r.returncode != 0:
            return "%s rc=%d %s" % (cmd, r.returncode, out[-160:].replace("\n", " "))
        # 2026-09-11: 正常 stdout 里有关键诊断 (选股完成 N 信号 / 零信号提示),
        # 旧代码全丢弃 → 出问题查不到线索。这里留摘要。
        tail = out.strip().splitlines()
        for ln in tail[-2:]:
            if ln.strip():
                log("     %s" % ln.strip()[:150])
    return ""


# ────────────────────────── 报告 (纯函数, 可单测) ──────────────────────────

def build_report(results: list, ctx: dict) -> str:
    """把「每公式的多组合结果」渲染成报告 md。

    results: [{"gs", "file", "rows": [组合行 dict, ...]}, ...]
    ctx: date / declared / actual / total / done / remaining / capital / max_buy /
         period / dividend / universe / priority
    判定与最优评选全部走 core.farm_rules (单一真相源)。
    """
    lines = ["# 公式农场粗扫报告 — %s\n" % ctx.get("date", "")]
    d0, d1 = (ctx.get("declared") or ("", ""))
    a = ctx.get("actual")
    lines.append("口径: %s 池 / %s / %s / 本金%s / 单票上限%s / %s / %s\n" % (
        ctx.get("universe", ""), ctx.get("period", ""), ctx.get("dividend", ""),
        _money(ctx.get("capital")), _money(ctx.get("max_buy")),
        CALIBER["entry"], ctx.get("priority", "")))
    lines.append("%s\n" % farm_rules.describe())
    if a:
        lines.append("区间: 声明 %s~%s; **实测窗口 %s~%s** (按 prep 缓存的 5m bar 首末, "
                     "与声明不一致时以实测为准)\n" % (d0, d1, a[0], a[1]))
    else:
        lines.append("区间: 声明 %s~%s; 实测窗口未知 (缺 prep 缓存 meta, 不猜)\n" % (d0, d1))

    stats = {"pass": 0, "fail": 0, "insufficient": 0, "invalid": 0}
    table_rows, pending = [], []
    for item in results:
        best = farm_rules.pick_best(item.get("rows") or [])
        if best is None:
            v = {"code": farm_rules.INVALID, "label": "无有效组合",
                 "reason": "36 组全部失败 或 区间内零信号"}
        else:
            v = farm_rules.verdict(best.get("annret"), best.get("maxdd"),
                                   best.get("trades"))
        stats[v["code" if v["code"] in stats else "invalid"]] += 1
        if best is None:
            table_rows.append("| %s | %s | — | — | — | — | — | — | %s |" % (
                item["gs"], _short(item.get("file", "")), v["label"]))
        else:
            table_rows.append("| %s | %s | %s | %.1f%% | %.2f | %.1f%% | %.1f%% | %s | %s: %s |" % (
                item["gs"], _short(item.get("file", "")), best.get("key", ""),
                _f(best.get("annret")) * 100, _f(best.get("calmar")),
                _f(best.get("maxdd")) * 100, _f(best.get("winrate")) * 100,
                int(_f(best.get("trades"))), v["label"], v["reason"]))
    for gs in (ctx.get("remaining") or []):
        pending.append(gs)

    lines.append("批次: 本批入库 %s 条 · 本轮粗扫 %s 条 · 余 %d 条待扫%s\n" % (
        ctx.get("total"), ctx.get("done"), len(pending),
        " (下轮自动补扫)" if pending else " (本批已扫完)"))
    lines.append("本轮判定: 达标 %d · 未达标 %d · 样本不足 %d · 无有效组合 %d\n" % (
        stats["pass"], stats["fail"], stats["insufficient"], stats["invalid"]))
    if ctx.get("sweep_errors"):
        errs = [str(e) for e in ctx["sweep_errors"]]
        shown = "; ".join(errs[:4])
        more = " 等 %d 条" % len(errs) if len(errs) > 4 else ""
        lines.append("本轮异常/停牌: %s%s\n" % (shown, more))
    lines.append("| 公式 | 来源 | 最优组合 | 年化 | 卡玛 | 最大回撤 | 胜率 | 笔数 | 判定 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    lines += table_rows
    if pending:
        lines.append("\n未扫清单 (%d 条): %s — 下轮自动补扫, 不会被新批次饿死" % (
            len(pending), "、".join(pending)))
    lines.append("\n> 提醒: 粗扫是 300 万本金 + 单票上限 2 万的**轻仓**口径, 账户年化 ≈ 仓位暴露 × "
                 "单笔边际 × 笔数 (主要在排「这公式出票多不多」, 不是「边际厚不厚」); 轻仓 + 少样本时"
                 "夏普会结构性为负 (每期扣 1.5%/年无风险利率)、卡玛在 |回撤|<0.01% 时被死区归零, "
                 "别拿单个指标下结论。")
    lines.append("\n> 下一步: 达标公式进精调(2592 组)+两段式样本外终审, 需人工放行。")
    return "\n".join(lines) + "\n"


def _money(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "?"
    return "%.0f万" % (v / 10000) if v >= 10000 else "%.0f" % v


def _short(s, n=18):
    return (s or "")[:n]


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ────────────────────────── 主流程 ──────────────────────────

def push_feishu(md_path, title):
    try:
        r = subprocess.run([PY, "-X", "utf8", os.path.join(ROOT, "tools", "send_report_feishu.py"),
                            md_path, title], cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        if r.returncode == 0:
            log("飞书推送: 成功")
        else:
            log("飞书推送: 失败 rc=%d %s" % (r.returncode,
                                            (r.stderr or r.stdout or "")[-120:]))
    except Exception as e:                                       # noqa: BLE001
        log("飞书推送: 异常 %r" % e)


def main():
    ap = argparse.ArgumentParser(description="公式农场粗扫 (闸门④)")
    ap.add_argument("--start", default="20240801")
    ap.add_argument("--end", default=time.strftime("%Y%m%d"))
    ap.add_argument("--max-formulas", type=int, default=0,
                    help="本轮最多扫几条; 0(默认)=全部待扫")
    ap.add_argument("--only", default="", help="只扫这些 GS (逗号分隔), 调试用")
    ap.add_argument("--include-done", action="store_true", help="已有结果的也重扫")
    ap.add_argument("--no-push", action="store_true", help="不推飞书 (本地对照用)")
    args = ap.parse_args()

    idx = _onboard_index()
    if not idx:
        log("没有入库记录 — 先点『一键入库』")
        raise SystemExit(1)
    batch_date, batch_items = _latest_onboard_items()
    if args.only:
        want = [s.strip() for s in args.only.split(",") if s.strip()]
        targets = [g for g in want if g in idx or g in batch_items]
        missing = [g for g in want if g not in idx and g not in batch_items]
        if missing:
            log("   ! 这些 GS 不在入库清单里, 跳过: %s" % ",".join(missing))
    else:
        targets = _pending(idx, args.include_done)
    if args.max_formulas and len(targets) > args.max_formulas:
        targets = targets[:args.max_formulas]

    log("入库总表 %d 条; 最新批次 %s 共 %d 条" % (len(idx), batch_date, len(batch_items)))
    log("本轮待扫 %d 条%s (含跨批次补扫: 旧的优先, 不会被新批次饿死)" % (
        len(targets), "" if targets else " (无)"))

    errors, no_signals = [], []
    for i, gs in enumerate(targets, 1):
        info = idx.get(gs) or batch_items.get(gs) or {}
        log("[%d/%d] %s <- %s (入库 %s)" % (i, len(targets), gs,
                                            str(info.get("file", ""))[:34],
                                            info.get("date", "?")))
        err = _sweep(gs, args.start, args.end)
        _ROWS_CACHE.pop(gs, None)          # 重扫后清缓存, 报告读新结果
        rows = _rows_of(gs)
        if err == "NO_SIGNALS":
            no_signals.append(gs)
            log("   · TDX 无此公式 / 区间零信号 → 停牌 (不再重复 prep), 报告记「无有效组合」")
            continue
        if err:
            errors.append("%s: %s" % (gs, err))
            log("   ✗ %s" % err)
            continue
        best = farm_rules.pick_best(rows)
        if best is None:
            log("   ! 无有效组合 (36 组全失败)")
            continue
        v = farm_rules.verdict(best.get("annret"), best.get("maxdd"), best.get("trades"))
        log("   %s 年化%.2f%% 回撤%.2f%% %s笔 | %s | %s" % (
            v["label"], _f(best["annret"]) * 100, _f(best["maxdd"]) * 100,
            int(_f(best["trades"])), best["key"], v["reason"]))

    # 报告范围 = 最新批次 20 条: 有结果的照实判, 零信号停牌的记「无有效组合」,
    # 其余显式列进未扫清单 (F1: 不许静默漏)
    done = [g for g in sorted(batch_items) if _rows_of(g)]
    parked = [g for g in sorted(batch_items) if not _rows_of(g) and _parked(g)]
    remaining = [g for g in sorted(batch_items)
                 if not _rows_of(g) and not _parked(g)]
    results = ([{"gs": g, "file": batch_items[g]["file"], "rows": _rows_of(g)}
                for g in done]
               + [{"gs": g, "file": batch_items[g]["file"], "rows": []}
                  for g in parked])
    windows = [w for w in (_window_of(g) for g in done) if w]
    actual = (min(w[0] for w in windows), max(w[1] for w in windows)) if windows else None
    ctx = {"date": time.strftime("%Y-%m-%d"), "declared": (args.start, args.end),
           "actual": actual, "total": len(batch_items), "done": len(done),
           "remaining": remaining, "sweep_errors": errors + ["%s(零信号停牌)" % g for g in no_signals],
           "universe": CALIBER["universe"], "period": CALIBER["period"],
           "dividend": CALIBER["dividend"], "capital": CALIBER["capital"],
           "max_buy": CALIBER["max_buy"], "priority": CALIBER["priority"]}
    md = build_report(results, ctx)

    os.makedirs(REPORTS, exist_ok=True)
    rpt = os.path.join(REPORTS, "%s_粗扫报告_公式农场.md" % ctx["date"])
    with open(rpt, "w", encoding="utf-8") as f:
        f.write(md)
    log("粗扫报告: %s" % rpt)
    log("本批 %d 条 / 有结果 %d 条 / 零信号停牌 %d 条 / 待扫 %d 条" % (
        len(batch_items), len(done), len(parked), len(remaining)))
    if not args.no_push:
        push_feishu(rpt, "公式农场粗扫 %s" % ctx["date"])


if __name__ == "__main__":
    main()
