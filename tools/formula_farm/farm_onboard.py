# -*- coding: utf-8 -*-
"""farm_onboard: 闸门② — TDX 全自动准备 + 批量入库(零人工摆窗)。

准备链(治"要人提前开通达信/关弹窗/开公式管理器"):
  [1/4] 通达信没开 → 启动 TdxW.exe, 轮询主窗口(登录可能 30s~2min)
  [2/4] 关已知拦路弹窗(通达信信息/TQ策略; 刷新行情只告警不动手, 防掐掉在跑的数据刷新)
  [3/4] 公式管理器不在 → 主窗口 Ctrl+F 唤起
  [4/4] 逐条 add_formula(截图留痕, 连续 2 败熔断), 完成发飞书 + 写日报
"""
import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
PY = sys.executable

import psutil  # noqa: E402
import pyautogui  # noqa: E402
from pywinauto import Application, Desktop  # noqa: E402

from tools.formula_farm import gui_onboard  # noqa: E402

TDX_EXE = r"E:\NEW_TDX\TdxW.exe"
MAIN_TITLE_KEY = "通达信金融终端"
STRAY_CLOSE = {"通达信信息", "TQ策略"}          # 可安全关闭的弹窗
STRAY_WARN = {"刷新行情"}                        # 只告警不动手
RUNS = os.path.join(ROOT, "data", "formula_farm", "runs")
REPORTS = os.path.join(ROOT, "data", "formula_farm", "reports")


def log(s):
    print(s, flush=True)


def _win(title_key, exact=False):
    for w in Desktop(backend="win32").windows():
        try:
            t = w.window_text() or ""
            if (t == title_key) if exact else (title_key in t):
                return w
        except Exception:
            pass
    return None


def _tdx_running():
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info["name"] or "").lower() == "tdxw.exe":
                return True
        except Exception:
            pass
    return False


def ensure_tdx():
    log("[1/4] 检查通达信进程...")
    if not _tdx_running():
        if not os.path.exists(TDX_EXE):
            raise RuntimeError("找不到通达信主程序: %s" % TDX_EXE)
        log("   通达信未运行, 启动 %s ..." % TDX_EXE)
        subprocess.Popen([TDX_EXE], cwd=os.path.dirname(TDX_EXE))
    t0 = time.time()
    while time.time() - t0 < 240:
        if _win(MAIN_TITLE_KEY):
            log("   主窗口就绪 (%.0fs)" % (time.time() - t0))
            time.sleep(3)  # 再等登录/初始化收尾
            return True
        time.sleep(3)
    raise RuntimeError("等通达信主窗口超时 240s(可能卡在登录弹窗, 请人工看一眼)")


def close_stray():
    log("[2/4] 清理拦路弹窗...")
    closed = []
    for _ in range(3):
        found = False
        for w in Desktop(backend="win32").windows():
            try:
                t = w.window_text() or ""
                if t in STRAY_CLOSE:
                    w.close()
                    closed.append(t)
                    found = True
                elif t in STRAY_WARN:
                    log("   ⚠ 存在弹窗『%s』(不动手, 防掐掉数据刷新)" % t)
            except Exception:
                pass
        if not found:
            break
        time.sleep(1)
    log("   已关弹窗: %s" % (closed or "无"))


def open_manager():
    log("[3/4] 打开公式管理器...")
    if _win("公式管理器"):
        log("   公式管理器已在, 直接复用")
        return True
    main = _win(MAIN_TITLE_KEY)
    if not main:
        raise RuntimeError("主窗口不见了")
    gui_onboard.force_foreground(main.handle)
    time.sleep(0.5)
    pyautogui.hotkey("ctrl", "f")  # 通达信唤起公式管理器热键
    t0 = time.time()
    while time.time() - t0 < 12:
        if _win("公式管理器"):
            log("   公式管理器已打开 (Ctrl+F)")
            return True
        time.sleep(0.5)
    raise RuntimeError("Ctrl+F 没唤起公式管理器(热键失效? 请人工开一次后重试)")


def _latest_check(date_str=None):
    d = date_str or time.strftime("%Y-%m-%d")
    fp = os.path.join(RUNS, d, "check.json")
    if not os.path.exists(fp):
        # 找最近的 check.json
        cands = []
        for day in os.listdir(RUNS) if os.path.isdir(RUNS) else []:
            p = os.path.join(RUNS, day, "check.json")
            if os.path.exists(p):
                cands.append(p)
        if not cands:
            return None, None
        fp = max(cands, key=os.path.getmtime)
    return json.load(open(fp, encoding="utf-8")), fp


def push_feishu(md_path, title):
    try:
        r = subprocess.run([PY, "-X", "utf8", os.path.join(ROOT, "tools", "send_report_feishu.py"),
                            md_path, title], cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        log("   飞书推送: %s" % ("成功" if r.returncode == 0 else "跳过/失败(%s)" % (r.stderr or r.stdout)[-120:]))
    except Exception as e:
        log("   飞书推送失败(不影响入库): %r" % e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-add", type=int, default=20)
    ap.add_argument("--run-date", default=None)
    ap.add_argument("--fail-streak", type=int, default=2,
                    help="连续失败几次熔断(大批量续跑可调大, 失败条目本身会每轮重试并留痕)")
    args = ap.parse_args()

    chk, fp = _latest_check(args.run_date)
    if not chk:
        log("没有待入库清单 — 先点『检查增量』")
        raise SystemExit(1)
    vetted = chk.get("vetted", [])
    # 断点续跑: onboard.json 里有记录的文件不再入库——成功的跳过,
    # 编译失败的同样终态跳过(TDX 确定性拒绝, 重试永远失败, 2026-09-06 熔断空转教训)
    import glob as _g
    done_files = set()
    for p in _g.glob(os.path.join(RUNS, "*", "onboard.json")):
        try:
            for it in json.load(open(p, encoding="utf-8")).get("items", []):
                if it.get("ok") or "编译失败" in (it.get("msg") or ""):
                    done_files.add(it.get("file"))
        except Exception:
            pass
    before = len(vetted)
    vetted = [v for v in vetted if v["file"] not in done_files]
    if before != len(vetted):
        log("   断点续跑: 已入库 %d 条跳过, 剩余 %d" % (before - len(vetted), len(vetted)))
    log("待入库清单 %s: %d 条" % (os.path.basename(fp), len(vetted)))
    if not vetted:
        log("没有可入库的新公式, 收工")
        raise SystemExit(0)

    ensure_tdx()
    close_stray()
    open_manager()

    from tools.formula_farm.daily_run import _next_gs_name
    n = _next_gs_name()
    log("[4/4] 逐条入库 (编号起点 GS%04d, 本轮上限 %d)..." % (n, args.max_add))
    results, fail_streak = [], 0
    for rec in vetted[: args.max_add]:
        name = "GS%04d" % n
        n += 1
        t0 = time.time()
        try:
            ok, msg = gui_onboard.add_formula(name, rec["code"], desc=rec.get("url", ""))
            if ok:
                node = gui_onboard.find_node(name)
                if not node:
                    ok, msg = False, "编辑器关了但树里没找到(复核失败)"
        except Exception as e:
            ok, msg = False, "EXC %r" % e
        log("   %s %s <- %s | %s (%.0fs)" % ("✓" if ok else "✗", name,
                                            rec["file"][:36], msg, time.time() - t0))
        results.append({"gs": name, "file": rec["file"], "url": rec.get("url", ""),
                        "ok": ok, "msg": msg})
        fail_streak = 0 if ok else fail_streak + 1
        if fail_streak >= args.fail_streak:
            log("   ✗ 连续 %d 次失败, 终止本批(防带病批量)" % args.fail_streak)
            break

    date_str = chk.get("date", time.strftime("%Y-%m-%d"))
    ok_n = sum(1 for x in results if x["ok"])
    os.makedirs(os.path.join(RUNS, date_str), exist_ok=True)
    ob_path = os.path.join(RUNS, date_str, "onboard.json")
    # 合并写(2026-09-06 血泪教训): 覆盖写会把历史 ok 记录冲掉 → 断点失效重复入库
    merged = {}
    if os.path.exists(ob_path):
        try:
            for it in json.load(open(ob_path, encoding="utf-8")).get("items", []):
                merged[it.get("file")] = it
        except Exception:
            pass
    for it in results:
        merged[it["file"]] = it   # 同文件取最新一轮的结果
    with open(ob_path, "w", encoding="utf-8") as f:
        json.dump({"date": date_str, "finished_at": time.strftime("%H:%M:%S"),
                   "items": list(merged.values())}, f, ensure_ascii=False, indent=1)
    os.makedirs(REPORTS, exist_ok=True)
    md = ["# 公式农场入库结果 — %s\n" % date_str,
          "入库 **成功 %d / 失败 %d**(本批 %d 条, 待入库 %d 条)\n" % (
              ok_n, len(results) - ok_n, len(results), len(vetted))]
    md += ["- %s **%s** %s — %s ([来源](%s))" % ("✅" if x["ok"] else "❌", x["gs"],
                                                 x["file"], x["msg"], x["url"])
           for x in results]
    rpt = os.path.join(REPORTS, "%s_入库结果_公式农场.md" % date_str)
    with open(rpt, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    log("完成: 成功 %d / 失败 %d -> %s" % (ok_n, len(results) - ok_n, rpt))
    push_feishu(rpt, "公式农场入库完成 %s" % date_str)


if __name__ == "__main__":
    main()
