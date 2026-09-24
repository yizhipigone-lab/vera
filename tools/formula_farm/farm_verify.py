# -*- coding: utf-8 -*-
"""farm_verify: 闸门⑤ — 定量复核 (信号重画 + 未来函数甄别)。

计划书 §4 步骤 6 早就写了"上架后定量复核 (repaint_check + future_func_check)",
但生产闸门只有 检查/入库/粗扫 三个, 这道**体检双闸的第二闸**一直缺 (2026-09-11 审计发现)。
本闸门补上它, 位置在「入库之后、粗扫之前」—— 重画/未来函数的公式不该浪费粗扫算力。

两条纪律 (与审计约定一致):

1. **「复核不通过」与「复核跑失败」分开写**: 前者是结论 (公式有问题), 后者是故障
   (工具/TDX 挂了)。两者都写进报告, 绝不混为一谈, 更不会因为工具挂了就显示"通过"。
2. **解析不到就标「未解析」**, 默认不通过 —— 宁可让人多看一眼, 不放过假阴性。

用法:
    python tools/formula_farm/farm_verify.py                  # 复核最新一批入库公式
    python tools/formula_farm/farm_verify.py --limit 3        # 只复核前 3 条
    python tools/formula_farm/farm_verify.py --formulas GS1285,GS1292
    python tools/formula_farm/farm_verify.py --dry-run        # 只打印要跑的命令
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
PY = sys.executable

# 判定阈值以被测工具为唯一真相源 (2026-09-16 F5 收口, 原此处硬编码 0.02/0.6)
from tools.formula_farm.common import push_feishu  # noqa: E402  # F6 收口
from tools.future_func_check import FUTURE_MIN_KEEP  # noqa: E402 T+1 保留率下限 (<0.6 = 显著衰减)
from tools.repaint_check import REPAINT_MAX_RATE  # noqa: E402 重画不一致率上限 (>2% = 重画实锤)

RUNS = os.path.join(ROOT, "data", "formula_farm", "runs")
REPORTS = os.path.join(ROOT, "data", "formula_farm", "reports")

# repaint_check: "  GS1285: 窗口信号 截断=12 全量=12 消失=0 新增=0 不一致率=0.00%"
_RE_REPAINT = re.compile(r"^\s*([\w\u4e00-\u9fff\.\-！]+):\s*窗口信号.*不一致率=([\d.]+)%")
# repaint_check 的判定行紧随公式行: "  → 完全一致, 因果公式"
_RE_ARROW = re.compile(r"^\s*→\s*(.+?)\s*$")
# future_func_check: "[INFO] GS1285: 信号 300 → 120 (30日规则后)"
_RE_SIGNALS = re.compile(r"\[INFO\]\s*([\w\u4e00-\u9fff\.\-！]+):\s*信号\s*(\d+)\s*→\s*(\d+)")
# future_func_check: "GS1285: T+0=+18.5% T+1=+15.0% 保留率=81% → 衰减正常"
_RE_FUTURE = re.compile(
    r"^([\w\u4e00-\u9fff\.\-！]+):\s*T\+0=([+-]?[\d.]+)%\s*T\+1=([+-]?[\d.]+)%\s*"
    r"保留率=([\d.]+)%\s*→\s*(.+?)\s*$")


def log(s):
    print(s, flush=True)


#: 工具失败后的重试间隔(秒) —— 2026-09-11 实测: 两个工具连跑时, 第二个偶尔撞上
#: TDX 连接刚被前一个关掉的窗口 ("连接路径为空, 请先调用 tq.initialize(path)")。
#: 单独重跑就正常 → 一次重试即可救回, 省得用户白等一轮 3 分钟的重画检测。
RETRY_PAUSE = 20


# ────────────────────────── 解析 (纯函数, 有测试) ──────────────────────────

def parse_repaint(out: str) -> dict:
    """解析 repaint_check 输出 → {gs: {rate, verdict, ok}}。

    ok 由**不一致率**判定 (rate ≤ 2%), 不依赖工具那行判定的措辞 (措辞会改, 数字不会)。
    """
    got, cur = {}, None
    for line in (out or "").splitlines():
        m = _RE_REPAINT.match(line)
        if m:
            gs, rate = m.group(1), float(m.group(2)) / 100.0
            cur = gs
            got[gs] = {"rate": rate, "verdict": "", "ok": rate <= REPAINT_MAX_RATE}
            continue
        a = _RE_ARROW.match(line)
        if a and cur:
            got[cur]["verdict"] = a.group(1)
            cur = None
    for gs, v in got.items():
        if not v["verdict"]:
            v["verdict"] = ("不一致率 %.2f%%%s" % (
                v["rate"] * 100,
                " (重画实锤)" if not v["ok"] else " (因果)"))
    return got


def parse_future(out: str) -> dict:
    """解析 future_func_check 输出 → {gs: {keep, verdict, ok, signals}}。

    ok 由**T+1 保留率**判定 (≥60%), 同样不依赖措辞。
    """
    got, sig = {}, {}
    for line in (out or "").splitlines():
        m = _RE_SIGNALS.search(line)
        if m:
            sig[m.group(1)] = (int(m.group(2)), int(m.group(3)))
            continue
        f = _RE_FUTURE.match(line.strip())
        if f:
            gs, keep = f.group(1), float(f.group(4)) / 100.0
            got[gs] = {"keep": keep, "verdict": f.group(5).strip(),
                       "ok": keep >= FUTURE_MIN_KEEP,
                       "signals": sig.get(gs)}
    for gs, (raw, kept) in sig.items():
        if gs not in got and raw == 0:
            got[gs] = {"keep": None, "verdict": "零信号 (无可复核)", "ok": None,
                       "signals": (raw, kept)}
    return got


def merge_verdicts(formulas, repaint: dict, future: dict,
                   repaint_err: str = "", future_err: str = "") -> dict:
    """把两份解析结果按公式对齐 → {gs: {"repaint": {...}, "future": {...}}}。

    缺结果的公式一律 **ok=None + 「未解析」/「复核失败」** —— 绝不当通过。
    """
    def _pick(mp, gs, err, label):
        if gs in mp:
            return mp[gs]
        if err:
            return {"ok": None, "rate": None, "keep": None,
                    "verdict": "%s复核失败 (%s)" % (label, err)}
        return {"ok": None, "rate": None, "keep": None,
                "verdict": "%s未解析 (未匹配到复核输出)" % label}

    return {gs: {"repaint": _pick(repaint, gs, repaint_err, "重画检测"),
                 "future": _pick(future, gs, future_err, "未来函数甄别")}
            for gs in formulas}


def concl_of(v: dict) -> str:
    """单公式复核总结论 (md 报告/verify.json/summarize 共用, 防同规则第三份手写)。"""
    oks = [v["repaint"].get("ok"), v["future"].get("ok")]
    return ("未通过" if False in oks
            else ("通过" if all(o is True for o in oks) else "无法判定"))


def summarize(merged: dict) -> dict:
    """总览: 两项都通过 = pass; 任一项明确不通过 = fail; 其余 = unknown。

    2026-09-16 看板审计 M4: 三态判断统一走 concl_of (原此处又手写一份)。
    """
    stat = {"total": len(merged), "pass": 0, "fail": 0, "unknown": 0}
    _KEY = {"通过": "pass", "未通过": "fail", "无法判定": "unknown"}
    for v in merged.values():
        stat[_KEY[concl_of(v)]] += 1
    return stat


def write_verify_json(path, merged: dict, ctx: dict) -> dict:
    """结构化复核结论落盘 (2026-09-16 看板数据源: md 给人看, json 给程序读)。"""
    out = {"date": ctx.get("date", ""), "cutoff": ctx.get("cutoff", ""),
           "stats": summarize(merged),
           "formulas": {gs: {"repaint_ok": v["repaint"].get("ok"),
                             "future_ok": v["future"].get("ok"),
                             "concl": concl_of(v),
                             "repaint_verdict": v["repaint"].get("verdict", ""),
                             "future_verdict": v["future"].get("verdict", "")}
                        for gs, v in merged.items()}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return out


def build_verify_report(merged: dict, ctx: dict) -> str:
    """渲染复核报告 md (纯函数)。"""
    stat = summarize(merged)
    lines = ["# 公式农场定量复核报告 — %s\n" % ctx.get("date", ""),
             "复核对象: 最新一批入库公式 %d 条 (截断日 %s)\n" % (
                 stat["total"], ctx.get("cutoff", "")),
             "命令: `%s` / `%s`\n" % (ctx.get("cmd_repaint", ""), ctx.get("cmd_future", "")),
             "判据: 重画 = 截断前后信号不一致率 ≤ %.0f%%; 未来函数 = T+1 保留率 ≥ %.0f%%\n" % (
                 REPAINT_MAX_RATE * 100, FUTURE_MIN_KEEP * 100),
             "结论: 通过 %d · **未通过 %d** · 无法判定(复核失败/未解析/零信号) %d\n" % (
                 stat["pass"], stat["fail"], stat["unknown"]),
             "| 公式 | 重画检测 | 未来函数甄别 | 结论 |", "|---|---|---|---|"]
    for gs, v in merged.items():
        r, f = v["repaint"], v["future"]
        lines.append("| %s | %s | %s | %s |" % (
            gs, _cell(r), _cell(f), concl_of(v)))
    lines.append("\n> 说明: 「未通过」= 复核判定公式有问题 (重画/未来函数), 应由人工决定是否"
                 "从候选里剔除; 「无法判定」= 工具跑失败或输出没匹配上, **不等于通过**, "
                 "重跑一次或人工看一眼。重画/未来函数的公式不进精调。")
    return "\n".join(lines) + "\n"


def _cell(d: dict) -> str:
    if d.get("ok") is True:
        return "✅ %s" % (d.get("verdict") or "")
    if d.get("ok") is False:
        return "❌ %s" % (d.get("verdict") or "")
    return "⚠ %s" % (d.get("verdict") or "无法判定")


# ────────────────────────── 主流程 ──────────────────────────

def _latest_batch_formulas(limit: int = 0) -> list:
    """最新 onboard.json 的 ok 公式 (入库批次顺序)。"""
    cands = glob.glob(os.path.join(RUNS, "*", "onboard.json"))
    if not cands:
        return []
    fp = max(cands, key=os.path.getmtime)
    d = json.load(open(fp, encoding="utf-8"))
    gs = [it["gs"] for it in d.get("items", []) if it.get("ok") and it.get("gs")]
    return gs[:limit] if limit else gs


def _run(cmd: list, timeout: int) -> tuple:
    """跑一个复核工具 → (stdout, err 串空=成功)。"""
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except Exception as e:                                       # noqa: BLE001
        return "", "%s: %r" % (os.path.basename(cmd[1]), e)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        return out, "%s rc=%d" % (os.path.basename(cmd[1]), r.returncode)
    return out, ""


def _run_retry(cmd: list, timeout: int, retries: int, label: str) -> tuple:
    """跑复核工具, 失败自动重试 (TDX 连接被上一个工具带崩时的救火)。

    重试仍然失败 → 如实返回错误串, 由调用方写成「复核失败」; 绝不改成"通过"。
    """
    out, err = "", ""
    for attempt in range(max(0, int(retries)) + 1):
        out, err = _run(cmd, timeout)
        if not err:
            return out, ""
        log("   ! %s 第 %d 次失败: %s" % (label, attempt + 1, err))
        if attempt < int(retries):
            log("   · %d 秒后重试 (常见原因: TDX 连接被上一个工具关掉)" % RETRY_PAUSE)
            time.sleep(RETRY_PAUSE)
    return out, err


def main():
    ap = argparse.ArgumentParser(description="公式农场定量复核 (闸门⑤)")
    ap.add_argument("--formulas", default="", help="逗号分隔; 默认=最新一批入库公式")
    ap.add_argument("--limit", type=int, default=0, help="最多复核几条 (0=全部)")
    ap.add_argument("--cutoff", default="", help="重画检测截断日 YYYYMMDD (默认 45 天前)")
    ap.add_argument("--start", default="20260101")
    ap.add_argument("--end", default=time.strftime("%Y%m%d"))
    ap.add_argument("--timeout", type=int, default=7200, help="单个工具超时(秒)")
    ap.add_argument("--retry", type=int, default=1,
                    help="工具失败后重试次数 (默认 1; 连跑时的 TDX 连接抖动靠它救)")
    ap.add_argument("--dry-run", action="store_true", help="只打印命令不执行")
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()

    if args.formulas:
        formulas = [s.strip() for s in args.formulas.split(",") if s.strip()]
    else:
        formulas = _latest_batch_formulas(args.limit)
    if not formulas:
        log("没有可选复核的公式 — 先点『一键入库』或传 --formulas")
        raise SystemExit(1)
    cutoff = args.cutoff or time.strftime(
        "%Y%m%d", time.localtime(time.time() - 45 * 86400))

    cmd_rep = [PY, "-X", "utf8", os.path.join(ROOT, "tools", "repaint_check.py"),
               "--formulas", ",".join(formulas), "--cutoff", cutoff,
               "--start", args.start, "--end", args.end]
    cmd_fut = [PY, "-X", "utf8", os.path.join(ROOT, "tools", "future_func_check.py"),
               "--formulas", ",".join(formulas), "--start", args.start,
               "--end", args.end]
    log("复核 %d 条 (截断日 %s): %s" % (len(formulas), cutoff, ",".join(formulas)))
    if args.dry_run:
        log("  重画检测: %s" % " ".join(cmd_rep))
        log("  未来函数: %s" % " ".join(cmd_fut))
        return

    os.makedirs(RUNS, exist_ok=True)
    log("① 重画检测 (截断 %s vs 全量)..." % cutoff)
    out_r, err_r = _run_retry(cmd_rep, args.timeout, args.retry, "重画检测")
    log("   rc=%s" % ("0" if not err_r else err_r))
    log("② 未来函数甄别 (T+0 vs T+1 移位)..." )
    out_f, err_f = _run_retry(cmd_fut, args.timeout, args.retry, "未来函数甄别")
    log("   rc=%s" % ("0" if not err_f else err_f))

    merged = merge_verdicts(formulas, parse_repaint(out_r), parse_future(out_f),
                            repaint_err=err_r, future_err=err_f)
    stat = summarize(merged)
    date_str = time.strftime("%Y-%m-%d")
    # 2026-09-16 看板数据源: 结构化结论落 runs/<日期>/verify.json
    os.makedirs(os.path.join(RUNS, date_str), exist_ok=True)
    vjson = os.path.join(RUNS, date_str, "verify.json")
    write_verify_json(vjson, merged, {"date": date_str, "cutoff": cutoff})
    log("复核成绩: %s" % vjson)
    with open(os.path.join(RUNS, "verify_%s.log" % date_str), "w", encoding="utf-8") as f:
        f.write("=== repaint_check ===\n%s\n\n=== future_func_check ===\n%s\n" % (out_r, out_f))
    os.makedirs(REPORTS, exist_ok=True)
    md = build_verify_report(merged, {"date": date_str, "cutoff": cutoff,
                                      "cmd_repaint": " ".join(cmd_rep)[:200],
                                      "cmd_future": " ".join(cmd_fut)[:200]})
    rpt = os.path.join(REPORTS, "%s_定量复核_公式农场.md" % date_str)
    with open(rpt, "w", encoding="utf-8") as f:
        f.write(md)
    log("复核报告: %s" % rpt)
    log("结论: 通过 %d · 未通过 %d · 无法判定 %d" % (
        stat["pass"], stat["fail"], stat["unknown"]))
    if not args.no_push:
        push_feishu(rpt, "公式农场定量复核 %s" % date_str)


if __name__ == "__main__":
    main()
