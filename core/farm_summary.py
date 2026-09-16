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
(路径, mtime) 进程内缓存 (容量 32 条 LRU 淘汰, 防长跑内存膨胀, 审计 M1);
onboard 索引/断点集按 (文件数, 最大 mtime) 签名缓存 (审计 M5: 旧 mtime
恢复/删非最新文件也会变签名)。
"""
import glob
import json
import os
import re

from core import farm_rules
from core.farm_ledger import load_done_files, load_onboard_index

_CACHE: dict = {}
_CACHE_CAP = 32


# ── 小工具 ────────────────────────────────────────────────

def _read_json(path):
    """读 json, 按 (path, mtime) 缓存; 不存在/坏文件 → None。LRU 容量淘汰。"""
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
    while len(_CACHE) >= _CACHE_CAP:          # dict 保序: 逐最旧 (审计 M1)
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[path] = (m, v)
    return v


def _mtime(p):
    """getmtime 的安全版 (审计 L1: glob 完到 stat 之间文件被删的 TOCTOU)。"""
    try:
        return os.path.getmtime(p)
    except OSError:
        return -1.0


def _latest(pattern):
    cands = [c for c in glob.glob(pattern) if _mtime(c) >= 0]
    return max(cands, key=_mtime) if cands else None


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
    """缓存签名 = (文件数, 最大 mtime) —— 审计 M5: 只取 max mtime 会漏
    「保留时间戳恢复旧目录」「删除非最新文件」两类变化。"""
    return (len(files), max((_mtime(f) for f in files), default=0.0))


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


def _voided(root):
    """作废名单 {gs: {reason, date, ...}} (2026-09-17 GS1318/PLOYLINE 漏网事件)。

    独立于 archive.json 的单一真相源: --rebuild-archive 全量重建档案会冲掉
    写在档案里的任何标记, 放独立文件才不会被重建抹掉。
    文件不存在 → {}(零行为变化, 老数据/测试 fixture 不受影响)。
    """
    d = _read_json(os.path.join(root, "voided.json")) or {}
    items = d.get("items")
    return items if isinstance(items, dict) else {}


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
        # 2026-09-16 审计 F-A: _done_files 必须提出循环 —— 在推导式里调用
        # 会对每个 vetted 条目各做一次 glob+stat (1055 条 → 每次轮询白烧 ~290ms)
        done = _done_files(root)
        remain = len([v for v in (chk.get("vetted") or [])
                      if v.get("file") not in done])
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
    has_check = bool(_latest_check(data_root))   # 审计 L2: 坏 check.json
    # (_read_json 失败回落 {}) 不算"已检查", 不能解锁②
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


# ── 达标榜 → 回测页回填 (2026-09-16 计划书) ─────────────────────────────

def _pct_txt(v):
    """0.08 → 8% / 0.005 → 0.5% (文案用; 去尾零)。"""
    s = ("%g" % (v * 100))
    return s + "%"


def _money_txt(v) -> str:
    """金额带单位: 2000 → 「2000元」, 20000 → 「2万」, 3000000 → 「300万」。"""
    return "%g万" % (v / 10000) if v >= 10000 else "%g元" % v


def _caliber_text(c) -> str:
    """回填横幅的口径文案 — 后端唯一生成 (审计 LOW-8: 前端不得再手写第二份)。

    2026-09-16 审计第二轮 HIGH-A: 必须披露"同口径复跑"的**全部已知差异** ——
    引擎入口不同 (粗扫 run_cached 不支持 degrade_5m → 缺 5m 的股-天丢信号;
    回测页 run() 恒 degrade_5m=true → 用日线 OHLC 补满 48 根 bar 照样开仓) +
    数据窗口不同 (粗扫 60 交易日稀疏窗口 fill_data=False vs 本页连续区间)。
    只披露 trailing confirm 不足以让用户判断"数字能不能并排比"。
    """
    period = {"5m": "5分线", "1m": "1分线", "1d": "日线"}.get(c["period"], c["period"])
    entry = {"close_t": "T日收盘买入", "open_t1": "次日开盘买入"}.get(
        c["entry_price_mode"], c["entry_price_mode"])
    confirm = {"intraday": "移动止盈盘中触线(回测页标注为偏乐观的旧语义)",
               "real": "移动止盈条件单语义"}.get(c["trailing_confirm"],
                                                c["trailing_confirm"])
    return ("%s · %s · %s本金 · 单票上限%s · 最低买入%s · %s · %s · %s"
            " · ⚠ 粗扫走稀疏60交易日窗口且缺5m的股-天丢信号, 本页为连续区间且"
            "5m降级补线(缺数据用日线补满), 笔数可能偏多, 数字别与粗扫并排比"
            % (c["universe"].split(" ")[0], period, _money_txt(c["capital"]),
               _money_txt(c["max_buy"]), _money_txt(c["min_buy"]),
               entry, confirm, c["priority"]))


def backtest_prefill(data_root: str, gs: str) -> dict:
    """按 GS 编号从档案构建回测页回填包 (公开接口 3/3)。

    口径一律取 farm_rules.SWEEP_CALIBER (单一真相源, 与报告抬头同对象)。
    gs 不在档案 → KeyError; 已作废(voided.json 登记) → ValueError;
    best 为空或六项数值参数有缺 → ValueError。
    """
    arch = _read_json(os.path.join(data_root, "archive.json")) or {}
    if gs not in arch:
        raise KeyError(gs)
    # 2026-09-17: 作废公式禁止回填复跑 (踩未来函数黑名单, 复跑只会再烧一次机时)
    v = _voided(data_root).get(gs)
    if v:
        raise ValueError("该公式已作废, 不再回填复跑 — %s" % (v.get("reason") or "踩黑名单"))
    e = arch[gs]
    best = e.get("best") or {}
    p = best.get("params") or {}
    # 审计 LOW-6/第二轮 LOW-F: **六项**数值参数全验 (原只验 cost, 后改四键;
    # cond_days=None 会静默变「无条件时间止盈」, cond_profit=None 会经 `or 0`
    # 变 0% 即「持仓 N 天必卖」—— 语义被悄悄改掉)
    need = [k for k in ("cost", "act", "dd", "time_days", "cond_days", "cond_profit")
            if p.get(k) is None]
    # 审计 LOW-H: window 缺失时原会静默沿用用户旧区间跑, 横幅看不出跑的不是
    # 粗扫窗口 —— 直接拒, 逼重建档案 (实测当前 783 条 best 全有 window)
    if not e.get("window"):
        need.append("window")
    if not best or need:
        raise ValueError("该公式没有带参数的最优组合%s, 请先跑④重建档案 "
                         "(python tools/formula_farm/farm_backtest.py "
                         "--max-formulas 0 --rebuild-archive --no-push)"
                         % ("(缺 %s)" % "/".join(need) if need else ""))
    parts = ["硬止损%s" % _pct_txt(abs(p["cost"])),
             "移动止盈激活%s/回撤%s" % (_pct_txt(p["act"]), _pct_txt(p["dd"])),
             "时间止损%d天" % (p.get("time_days") or 0)]
    if (p.get("cond_days") or 0) > 0:
        parts.append("条件时间止盈%d天/盈利%s"
                     % (p["cond_days"], _pct_txt(p.get("cond_profit") or 0)))
    ladder_note = ""
    if (p.get("ladder") or "off") != "off":
        ladder_note = ("原组合含阶梯止盈(%s, 档位未存入粗扫明细), "
                       "已按关闭回填, 请人工核对" % p["ladder"])
    c = farm_rules.SWEEP_CALIBER
    return {"gs": gs, "file": e.get("file", ""), "url": e.get("url", ""),
            "window": e.get("window"), "params": p,
            "caliber": {"universe_type": c["universe_type"],
                        "universe": c["universe"], "period": c["period"],
                        "entry_price_mode": c["entry_price_mode"],
                        "trailing_confirm": c["trailing_confirm"],
                        "capital": c["capital"], "max_buy": c["max_buy"],
                        "min_buy": c["min_buy"],
                        "priority_value": c["priority_value"],
                        "priority": c["priority"]},
            "combo_text": " + ".join(parts), "ladder_note": ladder_note,
            "caliber_text": _caliber_text(c)}


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


#: 榜单各组下发上限 (审计 F-B: 全量 822 条 = 343 KB/次, 2 秒轮询扛不动;
#: 计数在 board_totals 全给, 截尾组前端标「仅列前 N 条」)
_BOARD_CAP = {"pass": 200, "insufficient": 50, "fail": 30, "void": 50}


def overview(data_root: str) -> dict:
    """漏斗 + 达标榜 + 淘汰原因 Top3。数量全部唯一键口径, 不虚报。

    2026-09-16 审计 F-B: 榜单分组**截尾下发** (计数全给, 行只给前 N 条) ——
    实测全量 822 条归档 = 343 KB/次, status 2 秒轮询扛不动; 折叠组用户
    本来就少展开, 前 30 条足够代表。
    """
    # 漏斗: 进货 → 上架 → 抽检 → 试穿 → 达标
    intake_n = len(glob.glob(os.path.join(data_root, "intake", "**", "*.md"),
                             recursive=True))
    idx = _onboard_index(data_root)
    verified, verify_pass = {}, 0
    for fp in sorted(glob.glob(os.path.join(_runs(data_root), "*", "verify.json")),
                     key=_mtime):
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
    # 作废名单: 从达标/样本不足/未达标三组里摘出来单独成组 (2026-09-17)
    void_map = _voided(data_root)
    void_rows, live_rows = [], []
    for r in rows:
        v = void_map.get(r["gs"])
        if v:
            r["void_reason"] = v.get("reason", "")
            void_rows.append(r)
        else:
            live_rows.append(r)
    swept = [r for r in rows if r["verdict_code"] != "invalid"]
    by = {"pass": [], "insufficient": [], "fail": [], "void": void_rows}
    for r in live_rows:
        key = (r["verdict_code"] if r["verdict_code"] in ("pass", "insufficient")
               else "fail")
        by[key].append(r)
    for lst in by.values():
        lst.sort(key=lambda r: (r["annret"] is None, -(r["annret"] or 0)))
    totals = {k: len(v) for k, v in by.items()}
    capped = {k: v[:_BOARD_CAP[k]] for k, v in by.items()}
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
            {"key": "void", "label": "作废(踩黑名单)", "count": len(void_rows)},
            {"key": "pass", "label": "达标",
             "count": totals["pass"]}],
        "top_reasons": [[c, n] for c, n in top],
        "board": capped,
        "board_totals": totals,
        "board_total": len(rows),
    }
