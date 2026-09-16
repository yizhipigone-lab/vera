# -*- coding: utf-8 -*-
"""farm_summary — 公式农场看板汇总 (纯函数, 2026-09-16 计划书阶段 2)。

两个构建器 (公开接口就这两个, 其余全内部):
- gate_summaries(data_root): 四张流水线卡片的成绩单 + 按钮就绪标志/提示;
- overview(data_root): 总览漏斗 + 达标榜 + 淘汰原因 Top3。

规则来源全部收口他处, 本模块只做"读产物 + 汇总", 不自己定义任何判定:
- 判定口径 = core.farm_rules (达标榜分组直接读 archive 里 farm_rules 的结论);
- 入库索引/断点集 = tools.formula_farm.common (load_onboard_index/load_done_files,
  与 farm_onboard/farm_backtest 同一份实现, 防第三份手写)。
core 引 tools 的说明: formula_farm/common 只依赖 stdlib + tools.future_tokens,
引用链无环; 收口收益大于分层洁癖。

性能: status 轮询 2 秒一次, check.json 含千条公式源码(MB 级), 一律按
(路径, mtime) 进程内缓存; onboard 索引/断点集按 glob 最大 mtime 缓存。
"""
import glob
import json
import os
import re

from tools.formula_farm.common import load_done_files, load_onboard_index

_CACHE: dict = {}


# ── 小工具 ────────────────────────────────────────────────

def _read_json(path):
    """读 json, 按 (path, mtime) 缓存; 不存在/坏文件 → None。"""
    try:
        m = os.path.getmtime(path)
    except OSError:
        return None
    hit = _CACHE.get(path)
    if hit and hit[0] == m:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f)
    except Exception:
        return None
    _CACHE[path] = (m, v)
    return v


def _latest(pattern):
    cands = glob.glob(pattern)
    return max(cands, key=os.path.getmtime) if cands else None


def _md(date_str):
    """2026-09-16 → 9月16日 (显示用; 解析不出原样返回)。"""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", date_str or "")
    return "%d月%d日" % (int(m.group(2)), int(m.group(3))) if m else (date_str or "?")


def _runs(root):
    return os.path.join(root, "runs")


def _reports(root):
    return os.path.join(root, "reports")


def _onboard_files(root):
    return glob.glob(os.path.join(_runs(root), "*", "onboard.json"))


def _glob_sig(files):
    return max((os.path.getmtime(f) for f in files), default=0.0)


def _onboard_index(root):
    files = _onboard_files(root)
    sig = _glob_sig(files)
    hit = _CACHE.get("##onboard_idx")
    if hit and hit[0] == sig:
        return hit[1]
    idx = load_onboard_index(_runs(root))
    _CACHE["##onboard_idx"] = (sig, idx)
    return idx


def _done_files(root):
    files = _onboard_files(root)
    sig = _glob_sig(files)
    hit = _CACHE.get("##done_files")
    if hit and hit[0] == sig:
        return hit[1]
    done = load_done_files(_runs(root))
    _CACHE["##done_files"] = (sig, done)
    return done


def _latest_check(root):
    fp = _latest(os.path.join(_runs(root), "*", "check.json"))
    return (_read_json(fp) or {}) if fp else None


def _latest_onboard(root):
    fp = _latest(os.path.join(_runs(root), "*", "onboard.json"))
    return (_read_json(fp) or {}) if fp else None


# ── ①②③④ 卡片成绩单 ─────────────────────────────────────

# 真实历史报告原文 (2026-09-11): 「结论: 通过 2 · **未通过 0** · 无法判定(...) 0」
# —— 星号与括号尾巴都要容忍 (计划书勘误 2)
_RE_VERIFY_MD = re.compile(
    r"结论:\s*通过\s*(\d+)\s*·\s*\**\s*未通过\s*(\d+)\s*\**\s*·\s*无法判定[^0-9]*(\d+)")
_RE_BT_MD = re.compile(
    r"本轮判定:\s*达标\s*(\d+)\s*·\s*未达标\s*(\d+)\s*·\s*样本不足\s*(\d+)\s*·\s*无有效组合\s*(\d+)")
_RE_BT_REMAIN = re.compile(r"余\s*(\d+)\s*条待扫")


def _check_card(root):
    d = _latest_check(root)
    if not d:
        return {"text": "", "ready": True, "hint": ""}
    return {"text": "%s · 新增可入库 %d 条 / 淘汰 %d 条 / 重复 %d 条" % (
                _md(d.get("date")), len(d.get("vetted") or []),
                len(d.get("excluded") or []), len(d.get("dup") or [])),
            "ready": True, "hint": ""}


def _onboard_card(root):
    latest = _latest_onboard(root)
    idx = _onboard_index(root)
    if not latest:
        return {"text": "", "ready": False, "hint": "先跑② 一键入库"}
    items = latest.get("items") or []
    ok_n = sum(1 for it in items if it.get("ok"))
    fail_n = len(items) - ok_n
    text = "%s 本批: 成功 %d 条 / 失败 %d 条 · 全库已入库 %d 条" % (
        _md(latest.get("date")), ok_n, fail_n, len(idx))
    chk = _latest_check(root)
    if chk:
        remain = len([v for v in (chk.get("vetted") or [])
                      if v.get("file") not in _done_files(root)])
        text += " · 还剩 %d 条待入" % remain
    return {"text": text, "ready": True, "hint": ""}


def _verify_card(root):
    fp = _latest(os.path.join(_runs(root), "*", "verify.json"))
    if fp:
        d = _read_json(fp) or {}
        s = d.get("stats") or {}
        text = "%s · 通过 %d / 未通过 %d / 无法判定 %d" % (
            _md(d.get("date")), s.get("pass", 0), s.get("fail", 0),
            s.get("unknown", 0))
        return {"text": text}
    # 旧轮次兜底: 最新复核 md 的结论行 (正则容忍星号/括号)
    fp = _latest(os.path.join(_reports(root), "*_定量复核_公式农场.md"))
    if fp:
        try:
            with open(fp, encoding="utf-8") as f:
                m = _RE_VERIFY_MD.search(f.read())
        except OSError:
            m = None
        if m:
            day = os.path.basename(fp)[:10]
            return {"text": "%s · 通过 %s / 未通过 %s / 无法判定 %s" % (
                _md(day), m.group(1), m.group(2), m.group(3))}
    return {"text": "暂无成绩单 (跑一轮即生成)"}


def _backtest_card(root):
    fp = _latest(os.path.join(_runs(root), "*", "backtest_summary.json"))
    if fp:
        d = _read_json(fp) or {}
        s = d.get("stats") or {}
        return {"text": "%s · 达标 %d / 未达标 %d / 样本不足 %d / 余 %d 条待补" % (
            _md(d.get("date")), s.get("pass", 0), s.get("fail", 0),
            s.get("insufficient", 0), d.get("remaining", 0))}
    fp = _latest(os.path.join(_reports(root), "*_粗扫报告_公式农场.md"))
    if fp:
        try:
            with open(fp, encoding="utf-8") as f:
                body = f.read()
        except OSError:
            body = ""
        m, r = _RE_BT_MD.search(body), _RE_BT_REMAIN.search(body)
        if m:
            return {"text": "%s · 达标 %s / 未达标 %s / 样本不足 %s / 余 %s 条待补" % (
                _md(os.path.basename(fp)[:10]), m.group(1), m.group(2),
                m.group(3), r.group(1) if r else "?")}
    return {"text": "暂无成绩单 (跑一轮即生成)"}


def gate_summaries(data_root: str) -> dict:
    """四张卡片: 成绩单文本 + 按钮就绪标志/人话提示。

    就绪判据 (计划书方案 B + 勘误 1): ②要有任意 check.json; ③要**最新批次**
    onboard.json 有 ok 条目 (farm_verify 只认最新一批); ④要任意 ok 入库
    (跨批次补扫), 未过复核时只给 note 提醒不锁死。
    """
    cards = {"check": _check_card(data_root),
             "onboard": _onboard_card(data_root),
             "verify": _verify_card(data_root),
             "backtest": _backtest_card(data_root)}
    has_check = _latest_check(data_root) is not None
    latest_ob = _latest_onboard(data_root)
    latest_has_ok = bool(latest_ob) and any(
        it.get("ok") for it in latest_ob.get("items", []))
    cards["onboard"]["ready"] = has_check
    cards["onboard"]["hint"] = "" if has_check else "先跑① 检查增量"
    cards["verify"]["ready"] = latest_has_ok
    cards["verify"]["hint"] = "" if latest_has_ok else "先跑② 一键入库"
    any_ok = bool(_onboard_index(data_root))
    cards["backtest"]["ready"] = any_ok
    cards["backtest"]["hint"] = "" if any_ok else "先跑② 一键入库"
    has_verify = bool(
        glob.glob(os.path.join(_runs(data_root), "*", "verify.json"))
        or glob.glob(os.path.join(_reports(data_root), "*_定量复核_公式农场.md")))
    cards["backtest"]["note"] = "" if has_verify else "本轮还没过定量复核"
    return cards


# ── 总览看板 ─────────────────────────────────────────────

def _reason_category(reason: str) -> str:
    """淘汰原因归类: 「筹码/专有函数:WINNER(」「跨周期引用(#MONTH 等)」→ 类名。"""
    return (reason or "").split(":")[0].split("(")[0] or "其它"


def _board_row(gs, e):
    best = e.get("best") or {}
    v = e.get("verdict") or {}
    return {"gs": gs, "file": e.get("file", ""), "url": e.get("url", ""),
            "onboard_date": e.get("onboard_date", ""),
            "key": best.get("key", ""), "annret": best.get("annret"),
            "maxdd": best.get("maxdd"), "calmar": best.get("calmar"),
            "winrate": best.get("winrate"), "trades": best.get("trades"),
            "verdict_code": v.get("code", ""), "verdict_label": v.get("label", ""),
            "reason": v.get("reason", "")}


def overview(data_root: str) -> dict:
    """漏斗 + 达标榜 + 淘汰原因 Top3。数量全部唯一键口径, 不虚报。"""
    # 漏斗: 进货 → 上架 → 抽检 → 试穿 → 达标
    intake_n = len(glob.glob(os.path.join(data_root, "intake", "**", "*.md"),
                             recursive=True))
    idx = _onboard_index(data_root)
    verified, verify_pass = {}, 0
    for fp in sorted(glob.glob(os.path.join(_runs(data_root), "*", "verify.json")),
                     key=os.path.getmtime):
        d = _read_json(fp) or {}
        for gs, f in (d.get("formulas") or {}).items():
            verified[gs] = f          # 同日/跨日重跑: 新的覆盖旧的
    verified_n = len(verified)
    verify_pass = sum(1 for f in verified.values() if f.get("concl") == "通过")
    if not verified:
        # 旧轮次兜底 (2026-09-16 前没有 verify.json): 最新复核 md 的聚合数
        fp = _latest(os.path.join(_reports(data_root), "*_定量复核_公式农场.md"))
        if fp:
            try:
                with open(fp, encoding="utf-8") as f:
                    m = _RE_VERIFY_MD.search(f.read())
            except OSError:
                m = None
            if m:
                verified_n = int(m.group(1)) + int(m.group(2)) + int(m.group(3))
                verify_pass = int(m.group(1))
    arch = _read_json(os.path.join(data_root, "archive.json")) or {}
    rows = [_board_row(gs, e) for gs, e in arch.items()]
    swept = [r for r in rows if r["verdict_code"] != "invalid"]
    by = {"pass": [], "insufficient": [], "fail": []}
    for r in rows:
        key = (r["verdict_code"] if r["verdict_code"] in ("pass", "insufficient")
               else "fail")
        by[key].append(r)
    for lst in by.values():
        lst.sort(key=lambda r: (r["annret"] is None, -(r["annret"] or 0)))
    # 淘汰原因 Top3 (最新一轮检查)
    top = []
    chk = _latest_check(data_root)
    if chk:
        agg: dict = {}
        for e in (chk.get("excluded") or []):
            for rs in (e.get("reasons") or []):
                c = _reason_category(rs)
                agg[c] = agg.get(c, 0) + 1
        top = sorted(agg.items(), key=lambda kv: -kv[1])[:3]
    return {
        "empty": intake_n == 0 and not idx,
        "funnel": [
            {"key": "intake", "label": "进货(已采集)", "count": intake_n},
            {"key": "onboard", "label": "上架(已入库)", "count": len(idx)},
            {"key": "verified", "label": "抽检(已复核)",
             "count": verified_n, "pass": verify_pass},
            {"key": "swept", "label": "试穿有结果", "count": len(swept)},
            {"key": "pass", "label": "达标",
             "count": len(by["pass"])}],
        "top_reasons": [[c, n] for c, n in top],
        "board": by,
        "board_total": len(rows),
    }
