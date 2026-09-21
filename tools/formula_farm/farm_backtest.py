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
- **口径唯一真相源** = `core/farm_rules` (2026-09-22 起 年化≥10% 且 |回撤|≤15% 且 笔数≥20);
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
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
PY = sys.executable

from core import farm_rules  # noqa: E402
from tools.formula_farm.common import load_onboard_index, push_feishu  # noqa: E402  # F6/收口
from tools.formula_farm import voided_scan  # noqa: E402  # 2026-09-17 扫前复检闸门

RUNS = os.path.join(ROOT, "data", "formula_farm", "runs")
REPORTS = os.path.join(ROOT, "data", "formula_farm", "reports")
SWEEP = os.path.join(ROOT, "tools", "gs_5m_sweep.py")
COMBOS36 = os.path.join(ROOT, "output", "gs_filter", "coarse_subset36.json")
SWEEP_OUT = os.path.join(ROOT, "output", "gs_5m_sweep")
#: 累计档案 (达标榜数据源, 2026-09-16 看板计划书): 每轮粗扫后增量更新
ARCHIVE = os.path.join(ROOT, "data", "formula_farm", "archive.json")

#: 报告抬头里的口径 — 2026-09-16 收口: 唯一真相源 = core.farm_rules.SWEEP_CALIBER
#: (原此处手写一份, 与回填接口/实际取数三处必然漂; 别名为兼容旧读取处)
CALIBER = farm_rules.SWEEP_CALIBER

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
    """所有 onboard.json 的 ok 条目 → {gs: {file, url, date}} (取最早入库批次)。

    2026-09-16 防漂移收口: 实现迁至 common.load_onboard_index (看板也要用同一份)。
    """
    return load_onboard_index(RUNS, logger=log)


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

#: 单条公式三个阶段的墙钟上限 (prep+run+report 各自计时)
SWEEP_TIMEOUT = 5400


def _run_stream(cmd, timeout):
    """跑子进程并**边跑边把输出转出来**, 返回 (rc, 合并输出文本)。

    2026-09-20 用户报「说运行中但看不到进度」: 旧版 capture_output=True 把
    gs_5m_sweep 每 25 组就 flush 一次的 `[GS1369 shard 0] 25/36 (0.05/s, ETA 7min)`
    全攒在内存里, 只在**阶段结束时**回放最后 2 行 —— 一个公式要跑十几分钟, 页面
    这十几分钟一个字都不动, 停在上一阶段的诊断行上 (看着像报错)。
    改成实时中继后, 这些行会立刻流进 FarmRunner 的 log_tail (页面实时输出) 和
    runs/<日期>/<闸门>.log。stderr 合流 (no_signals 判定与报错定位都靠这份文本)。
    超时语义与 subprocess.run 保持一致: 杀掉子进程后抛 TimeoutExpired。
    """
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    buf = []

    def _pump():
        for ln in proc.stdout:
            ln = ln.rstrip("\n")
            buf.append(ln)
            if not ln.strip():
                continue
            try:
                log("     %s" % ln.strip()[:200])
            except Exception:                                 # noqa: BLE001
                # 中继线程死掉 = 管道没人读 → 子进程写满缓冲区后**永久卡死**。
                # 打屏失败 (编码等) 绝不许拖垮中继本身。
                pass

    reader = threading.Thread(target=_pump, daemon=True)
    reader.start()
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    finally:
        reader.join(timeout=5)
        try:
            proc.stdout.close()
        except Exception:                                     # noqa: BLE001
            pass
    return rc, "\n".join(buf)


def _sweep(gs: str, start: str, end: str) -> str:
    """跑 prep/run/report 三段; 返回错误说明 (空串 = 成功)。

    prep 报 `no_signals` (公式不在 TDX 或区间内零信号) → **短路**: 直接停牌返回,
    不再往下跑 run/report (旧代码会拿缺失的 cache 去跑 run, 抛
    `FileNotFoundError: meta.json` → 被记成"闸门失败", 其实公式本身就没信号)。
    三段的 stdout/stderr 实时中继 (见 _run_stream), 不再只回放尾巴 2 行。
    """
    for cmd, extra in (("prep", []),
                       ("run", ["--stage", "refine", "--combos-file", COMBOS36]),
                       ("report", [])):
        rc, out = _run_stream([PY, "-X", "utf8", SWEEP, cmd, gs,
                               "--start", start, "--end", end] + extra,
                              SWEEP_TIMEOUT)
        if '"status": "no_signals"' in out or '"status":"no_signals"' in out:
            os.makedirs(os.path.dirname(_park_marker(gs)), exist_ok=True)
            with open(_park_marker(gs), "w", encoding="utf-8") as f:
                f.write("prep 报 no_signals (%s): TDX 无此公式 或 区间 %s~%s 内零信号\n"
                        % (time.strftime("%Y-%m-%d %H:%M"), start, end))
            return "NO_SIGNALS"
        if rc != 0:
            return "%s rc=%d %s" % (cmd, rc, out[-160:].replace("\n", " "))
    return ""


# ────────────────────────── 判定装配 (2026-09-16 审计 M4 收口) ──────────────────────────

def _verdict_of_rows(rows):
    """组合行 → (best, verdict)。装配唯一实现 —— build_report / archive_entry /
    _stats_of 三处共用 (原三份手写, best=None 的 invalid 字典逐字重复, 必然漂移)。
    阈值本身仍归 farm_rules (单一真相源), 本函数只做装配。"""
    best = farm_rules.pick_best(rows or [])
    if best is None:
        return None, {"code": farm_rules.INVALID, "label": "无有效组合",
                      "reason": "36 组全部失败 或 区间内零信号"}
    return best, farm_rules.verdict(best.get("annret"), best.get("maxdd"),
                                    best.get("trades"))


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
        best, v = _verdict_of_rows(item.get("rows"))
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


# ────────────────────────── 看板数据源 (2026-09-16 计划书阶段 1) ──────────────────────────

def _slim_best(best):
    if best is None:
        return None
    # 2026-09-16 回填回测页: 组合参数一并入档 (CSV 列齐全, 无需解析 key)
    params = {}
    for col, cast in (("cost", float), ("act", float), ("dd", float),
                      ("time_days", int), ("cond_days", int),
                      ("cond_profit", float)):
        try:
            params[col] = cast(best.get(col))
        except (TypeError, ValueError):
            params[col] = None
    # 审计 LOW-7: ladder 是字符串, 不能用 str(None) 强转 (会变成 "None" 触发
    # 假的「含阶梯止盈」告警); 空/缺一律记 None = 关闭
    lad = best.get("ladder")
    params["ladder"] = str(lad) if lad not in (None, "") else None
    return {"key": best.get("key", ""), "annret": _f(best.get("annret")),
            "maxdd": _f(best.get("maxdd")), "calmar": _f(best.get("calmar")),
            "winrate": _f(best.get("winrate")), "trades": int(_f(best.get("trades"))),
            "params": params}

def archive_entry(gs, info, rows, window):
    """单公式归档条目 (纯函数)。判定/最优组合走 _verdict_of_rows (装配唯一实现)。"""
    best, v = _verdict_of_rows(rows)
    return {"file": info.get("file", ""), "url": info.get("url", ""),
            "onboard_date": info.get("date", ""),
            "best": _slim_best(best), "verdict": v,
            "window": list(window) if window else None,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}


def update_archive(path, gs_list, idx, rebuild=False):
    """累计档案增量更新 (原子写)。rebuild=True 丢弃旧档全量重建。"""
    arch = {}
    if not rebuild and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                arch = json.load(f)
        except Exception as e:                                   # noqa: BLE001
            log("   ! 旧档案读取失败, 本轮按全量重写处理: %r" % e)
            arch = {}
    for gs in gs_list:
        arch[gs] = archive_entry(gs, idx.get(gs) or {},
                                 _rows_of(gs), _window_of(gs))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(arch, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return arch


def _stats_of(results):
    """本轮判定计数 (报告与 backtest_summary.json 共用同一装配 _verdict_of_rows)。"""
    stats = {"pass": 0, "fail": 0, "insufficient": 0, "invalid": 0}
    for item in results:
        _, v = _verdict_of_rows(item.get("rows"))
        stats[v["code"] if v["code"] in stats else "invalid"] += 1
    return stats


def _atomic_write(path, text):
    """tmp+replace 原子写 —— farm_summary 2 秒轮询在读, 不许露出半截文件。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def write_backtest_summary(path, ctx, stats):
    """卡片④成绩单数据源落盘 (md 给人看, json 给程序读)。原子写 (2026-09-20)。"""
    out = {"date": ctx.get("date", ""), "batch_date": ctx.get("batch_date", ""),
           "stats": stats, "remaining": len(ctx.get("remaining") or []),
           "total": ctx.get("total"), "done": ctx.get("done")}
    _atomic_write(path, json.dumps(out, ensure_ascii=False, indent=1))
    return out


# ────────────────────────── 主流程 ──────────────────────────

def _assemble_batch(batch_items):
    """最新批次 → (done, parked, remaining, results)。装配唯一实现 —
    循环内增量落盘与终点收口共用, 防两套口径漂移 (2026-09-20)。"""
    done = [g for g in sorted(batch_items) if _rows_of(g)]
    parked = [g for g in sorted(batch_items) if not _rows_of(g) and _parked(g)]
    remaining = [g for g in sorted(batch_items)
                 if not _rows_of(g) and not _parked(g)]
    results = ([{"gs": g, "file": batch_items[g]["file"], "rows": _rows_of(g)}
                for g in done]
               + [{"gs": g, "file": batch_items[g]["file"], "rows": []}
                  for g in parked])
    return done, parked, remaining, results


def _checkpoint(batch_items, batch_date, run_date, declared, errors, no_signals):
    """把当前进度落盘: 报告 md + 成绩单 json (每条公式扫完即调, 中断不吞已扫部分)。

    2026-09-20 用户报障: 三样产物原只在终点一次性写, 中途重启 = 已扫部分在
    报告/成绩单/榜单上全不可见。写失败 fail-soft —— sweep CSV 才是真 payload,
    落盘故障不许杀掉整轮。
    """
    done, parked, remaining, results = _assemble_batch(batch_items)
    windows = [w for w in (_window_of(g) for g in done) if w]
    actual = (min(w[0] for w in windows), max(w[1] for w in windows)) if windows else None
    ctx = {"date": run_date, "batch_date": batch_date, "declared": declared,
           "actual": actual, "total": len(batch_items), "done": len(done),
           "parked_n": len(parked), "remaining": remaining,
           "sweep_errors": list(errors) + ["%s(零信号停牌)" % g for g in no_signals],
           "universe": CALIBER["universe"], "period": CALIBER["period"],
           "dividend": CALIBER["dividend"], "capital": CALIBER["capital"],
           "max_buy": CALIBER["max_buy"], "priority": CALIBER["priority"]}
    try:
        _atomic_write(os.path.join(REPORTS, "%s_粗扫报告_公式农场.md" % run_date),
                      build_report(results, ctx))
        write_backtest_summary(
            os.path.join(RUNS, run_date, "backtest_summary.json"),
            ctx, _stats_of(results))
    except Exception as e:                                       # noqa: BLE001
        log("   ! 进度落盘失败 (本轮 sweep 继续, 报告/成绩单暂不更新): %r" % e)
    return ctx


def _archive_one(gs, idx):
    """单公式入档 (fail-soft: 写失败不杀整轮, 下轮孤儿对账兜底)。"""
    try:
        update_archive(ARCHIVE, [gs], idx)
    except Exception as e:                                       # noqa: BLE001
        log("   ! %s 入档失败 (本轮继续, 下轮启动时孤儿对账兜底): %r" % (gs, e))


def _sync_archive_orphans(idx):
    """孤儿对账: 有 sweep 结果/停牌标记但不在档案的公式补登 (中断自愈)。

    中断轮扫完的公式已有 CSV → 下轮不在待扫清单 → 旧逻辑 (终点只入档本轮
    targets) 永远进不了档案: 2026-09-20 实测 ~60 条被吞。每轮启动对一次账,
    无需人工 --rebuild-archive。
    """
    try:
        with open(ARCHIVE, encoding="utf-8") as f:
            arch = json.load(f)
    except Exception:                                            # noqa: BLE001
        arch = {}
    orphans = [g for g in idx if g not in arch and (_rows_of(g) or _parked(g))]
    if orphans:
        log("孤儿对账: %d 条有结果但不在档案 (中断残留), 补登: %s" % (
            len(orphans), ",".join(sorted(orphans)[:8])
            + ("..." if len(orphans) > 8 else "")))
        try:
            # 整批一次读写 (增量合并), 逐条写是 O(n²) 磁盘抖动
            update_archive(ARCHIVE, sorted(orphans), idx)
        except Exception as e:                                   # noqa: BLE001
            log("   ! 孤儿补登失败 (本轮继续): %r" % e)
    return len(orphans)


def main():
    ap = argparse.ArgumentParser(description="公式农场粗扫 (闸门④)")
    ap.add_argument("--start", default="20240801")
    ap.add_argument("--end", default=time.strftime("%Y%m%d"))
    ap.add_argument("--max-formulas", type=int, default=0,
                    help="本轮最多扫几条; 0(默认)=全部待扫")
    ap.add_argument("--only", default="", help="只扫这些 GS (逗号分隔), 调试用")
    ap.add_argument("--include-done", action="store_true", help="已有结果的也重扫")
    ap.add_argument("--no-push", action="store_true", help="不推飞书 (本地对照用)")
    ap.add_argument("--rebuild-archive", action="store_true",
                    help="全量重建累计档案 archive.json (首次/怀疑档案漂移时, 耗时数十秒)")
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

    # 2026-09-17 扫前复检闸门 (PLOYLINE 漏网事件的治本项):
    # 已登记作废的直接剔除; 其余每条开扫前再跑一次体检, 命中即登记作废并跳过。
    # 这样以后黑名单再加 token, 旧公式只要被重新扫到就自动拦下, 不依赖人记得回扫。
    voided_now = voided_scan.load_voided().get("items") or {}
    skipped = [g for g in targets if g in voided_now]
    targets = [g for g in targets if g not in voided_now]
    if skipped:
        log("   ⛔ %d 条已在作废名单, 跳过: %s" % (
            len(skipped), ",".join(sorted(skipped)[:8]) + ("..." if len(skipped) > 8 else "")))
    records = voided_scan.intake_records()

    log("入库总表 %d 条; 最新批次 %s 共 %d 条" % (len(idx), batch_date, len(batch_items)))
    log("本轮待扫 %d 条%s (含跨批次补扫: 旧的优先, 不会被新批次饿死)" % (
        len(targets), "" if targets else " (无)"))

    # 2026-09-20 增量落盘: 日期在开头取一次 (跨午夜长跑不许中途换报告文件名);
    # 孤儿对账补登中断残留; 循环前先来一次 checkpoint, 页面立即看到「余 N 条待补」。
    run_date = time.strftime("%Y-%m-%d")
    declared = (args.start, args.end)
    _sync_archive_orphans(idx)
    errors, no_signals = [], []
    _checkpoint(batch_items, batch_date, run_date, declared, errors, no_signals)
    for i, gs in enumerate(targets, 1):
        info = idx.get(gs) or batch_items.get(gs) or {}
        log("[%d/%d] %s <- %s (入库 %s)" % (i, len(targets), gs,
                                            str(info.get("file", ""))[:34],
                                            info.get("date", "?")))
        hit = voided_scan.guard(gs, info.get("file", ""), records)
        if hit:
            log("   ⛔ 扫前复检命中黑名单 → 登记作废并跳过: %s" % hit)
        else:
            err = _sweep(gs, args.start, args.end)
            _ROWS_CACHE.pop(gs, None)          # 重扫后清缓存, 报告读新结果
            rows = _rows_of(gs)
            if err == "NO_SIGNALS":
                no_signals.append(gs)
                log("   · TDX 无此公式 / 区间零信号 → 停牌 (不再重复 prep), 报告记「无有效组合」")
            elif err:
                errors.append("%s: %s" % (gs, err))
                log("   ✗ %s" % err)
            else:
                best = farm_rules.pick_best(rows)
                if best is None:
                    log("   ! 无有效组合 (36 组全失败)")
                else:
                    v = farm_rules.verdict(best.get("annret"), best.get("maxdd"),
                                           best.get("trades"))
                    log("   %s 年化%.2f%% 回撤%.2f%% %s笔 | %s | %s" % (
                        v["label"], _f(best["annret"]) * 100,
                        _f(best["maxdd"]) * 100, int(_f(best["trades"])),
                        best["key"], v["reason"]))
        # 扫一条落一条: 四种结局 (达标/未达标/停牌/作废) 同等待遇 ——
        # 与旧终点「targets 全入档」语义对齐, 只是时机提前到每条扫完
        _archive_one(gs, idx)
        _checkpoint(batch_items, batch_date, run_date, declared, errors, no_signals)

    # 终点收口: 最后一次 checkpoint (与循环内同一实现, 幂等) + 报告推送
    ctx = _checkpoint(batch_items, batch_date, run_date, declared, errors, no_signals)
    rpt = os.path.join(REPORTS, "%s_粗扫报告_公式农场.md" % run_date)
    log("粗扫报告: %s" % rpt)
    log("粗扫成绩: %s" % os.path.join(RUNS, run_date, "backtest_summary.json"))
    log("本批 %d 条 / 有结果 %d 条 / 零信号停牌 %d 条 / 待扫 %d 条" % (
        len(batch_items), ctx["done"], ctx["parked_n"], len(ctx["remaining"])))
    if args.rebuild_archive:
        arch = update_archive(ARCHIVE, sorted(idx), idx, rebuild=True)
        log("累计档案: %s (%d 条, 全量重建)" % (ARCHIVE, len(arch)))
    else:
        log("累计档案: %s (本轮 %d 条已逐条入档)" % (ARCHIVE, len(targets)))
    if not args.no_push:
        push_feishu(rpt, "公式农场粗扫 %s" % run_date)


if __name__ == "__main__":
    main()
