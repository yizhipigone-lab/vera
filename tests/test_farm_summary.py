# -*- coding: utf-8 -*-
"""core/farm_summary 看板汇总测试 (2026-09-16 计划书阶段 2, 全 tmp 目录造假产物)。"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import farm_summary as fs  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_cache():
    """模块级 _CACHE 跨测试隔离 (复审指出: 此前靠 tmp 路径不同碰巧安全)。"""
    fs._CACHE.clear()
    yield
    fs._CACHE.clear()


@pytest.fixture()
def root(tmp_path):
    r = tmp_path / "ff"
    (r / "runs" / "2026-09-16").mkdir(parents=True)
    (r / "reports").mkdir(parents=True)
    (r / "intake" / "xuangu").mkdir(parents=True)
    return r


def _wj(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def test_empty_root_all_gates_blocked(root):
    s = fs.gate_summaries(str(root))
    assert s["onboard"]["ready"] is False
    assert "先跑①" in s["onboard"]["hint"]
    assert s["verify"]["ready"] is False
    assert "先跑②" in s["verify"]["hint"]
    assert s["backtest"]["ready"] is False
    assert s["backtest"]["note"] == "本轮还没过定量复核"
    ov = fs.overview(str(root))
    assert ov["empty"] is True
    assert all(f["count"] == 0 for f in ov["funnel"])


def test_check_card_and_unlock_onboard(root):
    _wj(root / "runs" / "2026-09-16" / "check.json", {
        "date": "2026-09-16",
        "vetted": [{"file": "a.md"}, {"file": "b.md"}],
        "excluded": [{"file": "c.md", "reasons": ["筹码/专有函数:WINNER("]},
                     {"file": "d.md", "reasons": ["跨周期引用(#MONTH 等)",
                                                  "筹码/专有函数:COST("]}],
        "dup": [{"file": "e.md"}]})
    s = fs.gate_summaries(str(root))
    assert "新增可入库 2 条" in s["check"]["text"]
    assert "淘汰 2 条" in s["check"]["text"]
    assert s["onboard"]["ready"] is True
    ov = fs.overview(str(root))
    assert ov["top_reasons"][0] == ["筹码/专有函数", 2]
    assert ["跨周期引用", 1] in ov["top_reasons"]


def test_onboard_card_unlocks_verify_and_backtest(root):
    _wj(root / "runs" / "2026-09-16" / "check.json",
        {"date": "2026-09-16", "vetted": [{"file": "a.md"}, {"file": "b.md"},
                                          {"file": "c.md"}],
         "excluded": [], "dup": []})
    _wj(root / "runs" / "2026-09-16" / "onboard.json", {
        "date": "2026-09-16",
        "items": [{"gs": "GS0001", "file": "a.md", "url": "http://x", "ok": True, "msg": ""},
                  {"gs": "GS0002", "file": "b.md", "url": "", "ok": False, "msg": "编译失败"},
                  {"gs": "GS0003", "file": "c.md", "url": "", "ok": False, "msg": "EXC 超时"}]})
    s = fs.gate_summaries(str(root))
    assert "成功 1 条 / 失败 2 条" in s["onboard"]["text"]
    assert "全库已入库 1 条" in s["onboard"]["text"]
    # 还剩待入 = 3 个 vetted − (ok 1 + 编译失败 1) = 1 (EXC 可重试不算终态)
    assert "还剩 1 条待入" in s["onboard"]["text"]
    assert s["verify"]["ready"] is True
    assert s["backtest"]["ready"] is True
    ov = fs.overview(str(root))
    funnel = {f["key"]: f for f in ov["funnel"]}
    assert funnel["onboard"]["count"] == 1


def test_verify_json_card_and_overview(root):
    _wj(root / "runs" / "2026-09-16" / "verify.json", {
        "date": "2026-09-16", "cutoff": "20260801",
        "stats": {"total": 3, "pass": 2, "fail": 1, "unknown": 0},
        "formulas": {"GS0001": {"concl": "通过"}, "GS0002": {"concl": "通过"},
                     "GS0003": {"concl": "未通过"}}})
    s = fs.gate_summaries(str(root))
    assert "通过 2 / 未通过 1 / 无法判定 0" in s["verify"]["text"]
    assert s["backtest"]["note"] == ""   # 有复核产物 → 不再提醒
    ov = fs.overview(str(root))
    funnel = {f["key"]: f for f in ov["funnel"]}
    assert funnel["verified"]["count"] == 3
    assert funnel["verified"]["pass"] == 2


def test_verify_md_fallback_tolerates_stars_and_parens(root):
    (root / "reports" / "2026-09-11_定量复核_公式农场.md").write_text(
        "# x\n结论: 通过 2 · **未通过 0** · 无法判定(复核失败/未解析/零信号) 0\n",
        encoding="utf-8")
    s = fs.gate_summaries(str(root))
    assert "通过 2 / 未通过 0 / 无法判定 0" in s["verify"]["text"]
    # 总览漏斗同步兜底: 无 verify.json 时用 md 聚合数 (通过 2 / 共 2)
    ov = fs.overview(str(root))
    funnel = {f["key"]: f for f in ov["funnel"]}
    assert funnel["verified"]["count"] == 2
    assert funnel["verified"]["pass"] == 2


def test_backtest_json_card(root):
    _wj(root / "runs" / "2026-09-16" / "backtest_summary.json", {
        "date": "2026-09-16", "batch_date": "2026-09-16",
        "stats": {"pass": 1, "fail": 14, "insufficient": 3, "invalid": 0},
        "remaining": 2, "total": 20, "done": 18})
    s = fs.gate_summaries(str(root))
    assert "达标 1 / 未达标 14 / 样本不足 3 / 余 2 条待补" in s["backtest"]["text"]


def test_backtest_md_fallback(root):
    (root / "reports" / "2026-09-11_粗扫报告_公式农场.md").write_text(
        "# x\n批次: 本批入库 20 条 · 本轮粗扫 20 条 · 余 0 条待扫 (本批已扫完)\n"
        "本轮判定: 达标 0 · 未达标 14 · 样本不足 6 · 无有效组合 0\n",
        encoding="utf-8")
    s = fs.gate_summaries(str(root))
    assert "达标 0 / 未达标 14 / 样本不足 6 / 余 0 条待补" in s["backtest"]["text"]


def test_overview_board_groups_sorted(root):
    _wj(root / "archive.json", {
        "GS0001": {"file": "a.md", "url": "http://a", "onboard_date": "2026-09-06",
                   "best": {"key": "A", "annret": 0.20, "maxdd": -0.1,
                            "calmar": 2.0, "winrate": 0.6, "trades": 30},
                   "verdict": {"code": "pass", "label": "达标", "reason": "ok"}},
        "GS0002": {"file": "b.md", "url": "", "onboard_date": "2026-09-06",
                   "best": {"key": "A", "annret": 0.30, "maxdd": -0.01,
                            "calmar": 8.0, "winrate": 1.0, "trades": 3},
                   "verdict": {"code": "insufficient", "label": "样本不足",
                               "reason": "3 笔"}},
        "GS0003": {"file": "c.md", "url": "", "onboard_date": "2026-09-06",
                   "best": None,
                   "verdict": {"code": "invalid", "label": "无有效组合",
                               "reason": "零信号"}}})
    ov = fs.overview(str(root))
    assert [r["gs"] for r in ov["board"]["pass"]] == ["GS0001"]
    assert [r["gs"] for r in ov["board"]["insufficient"]] == ["GS0002"]
    assert [r["gs"] for r in ov["board"]["fail"]] == ["GS0003"]  # invalid 归折叠组
    funnel = {f["key"]: f for f in ov["funnel"]}
    assert funnel["swept"]["count"] == 2      # 有结果 = 非 invalid
    assert funnel["pass"]["count"] == 1
    assert ov["board_total"] == 3
    # 行字段齐全 (前端表格直接用)
    row = ov["board"]["pass"][0]
    assert row["annret"] == 0.20 and row["url"] == "http://a"
    assert row["onboard_date"] == "2026-09-06"


def test_overview_board_capped_but_totals_full(root):
    """审计 F-B: 行截尾下发 (payload 防爆), 计数全给。"""
    arch = {}
    for i in range(60):
        arch["GS1%03d" % i] = {
            "file": "f.md", "url": "", "onboard_date": "2026-09-06",
            "best": {"key": "A", "annret": 0.30, "maxdd": -0.01,
                     "calmar": 8.0, "winrate": 1.0, "trades": 3},
            "verdict": {"code": "insufficient", "label": "样本不足", "reason": "3 笔"}}
    _wj(root / "archive.json", arch)
    ov = fs.overview(str(root))
    assert ov["board_totals"]["insufficient"] == 60      # 计数全
    assert len(ov["board"]["insufficient"]) == 50        # 行截尾 (cap=50)
    funnel = {f["key"]: f for f in ov["funnel"]}
    assert funnel["swept"]["count"] == 60


# ── 2026-09-16 看板审计: 复审缺口补锁 ──

def test_read_json_cache_invalidates_on_mtime_change(root):
    """缓存失效主场景: 文件改写 (mtime 变) 后必须读到新内容。"""
    p = root / "runs" / "2026-09-16" / "check.json"
    _wj(p, {"date": "2026-09-16", "vetted": [{"file": "a.md"}], "excluded": [], "dup": []})
    os.utime(p, (1_000_000_000, 1_000_000_000))
    assert "新增可入库 1 条" in fs.gate_summaries(str(root))["check"]["text"]
    _wj(p, {"date": "2026-09-16",
            "vetted": [{"file": "a.md"}, {"file": "b.md"}, {"file": "c.md"}],
            "excluded": [], "dup": []})
    os.utime(p, (1_000_000_100, 1_000_000_100))
    assert "新增可入库 3 条" in fs.gate_summaries(str(root))["check"]["text"]


def test_onboard_sig_covers_file_count_not_just_max_mtime(root):
    """审计 M5: 保留时间戳拷入旧目录 (文件数变, max mtime 不变) 也必须刷新。"""
    _wj(root / "runs" / "2026-09-15" / "onboard.json", {
        "date": "2026-09-15",
        "items": [{"gs": "GS0001", "file": "a.md", "url": "", "ok": True, "msg": ""}]})
    os.utime(root / "runs" / "2026-09-15" / "onboard.json", (1_000_000_000,) * 2)
    assert "全库已入库 1 条" in fs.gate_summaries(str(root))["onboard"]["text"]
    # 第二个日期目录以更旧的 mtime 出现 —— 只看 max mtime 的签名会漏
    _wj(root / "runs" / "2026-09-14" / "onboard.json", {
        "date": "2026-09-14",
        "items": [{"gs": "GS0002", "file": "b.md", "url": "", "ok": True, "msg": ""}]})
    os.utime(root / "runs" / "2026-09-14" / "onboard.json", (999_000_000,) * 2)
    assert "全库已入库 2 条" in fs.gate_summaries(str(root))["onboard"]["text"]


def test_corrupt_check_json_does_not_unlock_onboard(root):
    """审计 L2: 坏 check.json (解析失败) 不算"已检查", ② 保持置灰。"""
    (root / "runs" / "2026-09-16" / "check.json").write_text("{损坏", encoding="utf-8")
    s = fs.gate_summaries(str(root))
    assert s["onboard"]["ready"] is False
    assert "先跑①" in s["onboard"]["hint"]


def test_verify_md_generator_matches_fallback_regex(root):
    """防漂移锁: build_verify_report 的当前输出必须能被兜底正则解析 —
    报告措辞哪天改了, 兜底会静默退化, 本测试先红。"""
    from tools.formula_farm import farm_verify as fv
    merged = {"GS0001": {"repaint": {"ok": True, "verdict": "x"},
                         "future": {"ok": True, "verdict": "y"}}}
    md = fv.build_verify_report(merged, {"date": "2026-09-16", "cutoff": "20260801"})
    m = fs._RE_VERIFY_MD.search(md)
    assert m and m.groups() == ("1", "0", "0")


def test_backtest_md_generator_matches_fallback_regex(root):
    """防漂移锁: build_report 的当前输出必须能被兜底正则解析。"""
    from tools.formula_farm import farm_backtest as fb
    results = [{"gs": "GS0001", "file": "a.md",
                "rows": [{"key": "A", "annret": "0.20", "maxdd": "-0.10",
                          "calmar": "2.0", "winrate": "0.60", "trades": "30"}]}]
    ctx = {"date": "2026-09-16", "declared": ("20240801", "20260916"),
           "actual": None, "total": 2, "done": 1, "remaining": ["GS0002"],
           "sweep_errors": [], "universe": "沪深300", "period": "5m",
           "dividend": "前复权", "capital": 3_000_000, "max_buy": 20_000,
           "priority": "移动止盈优先"}
    md = fb.build_report(results, ctx)
    m = fs._RE_BT_MD.search(md)
    assert m and m.groups() == ("1", "0", "0", "0")
    r = fs._RE_BT_REMAIN.search(md)
    assert r and r.group(1) == "1"


# ── 2026-09-16 达标榜回填回测页: backtest_prefill ──

def _archive_with_params(root, cond_days=0, ladder="off"):
    _wj(root / "archive.json", {
        "GS0607": {"file": "70667_捕捉起涨点.md", "url": "http://gupang/x",
                   "onboard_date": "2026-09-06",
                   "best": {"key": "c-0.2_a0.08", "annret": 0.244, "maxdd": -0.034,
                            "calmar": 7.16, "winrate": 0.61, "trades": 58,
                            "params": {"cost": -0.2, "act": 0.08, "dd": 0.005,
                                       "ladder": ladder, "time_days": 20,
                                       "cond_days": cond_days,
                                       "cond_profit": 0.03 if cond_days else 0.0}},
                   "verdict": {"code": "pass", "label": "达标", "reason": "ok"},
                   "window": ["2024-09-11", "2026-09-16"]}})


def test_backtest_prefill_builds_package(root):
    _archive_with_params(root)
    d = fs.backtest_prefill(str(root), "GS0607")
    assert d["gs"] == "GS0607" and d["window"] == ["2024-09-11", "2026-09-16"]
    assert d["params"]["cost"] == -0.2 and d["params"]["time_days"] == 20
    assert d["combo_text"] == "硬止损20% + 移动止盈激活8%/回撤0.5% + 时间止损20天"
    assert d["ladder_note"] == ""
    cal = d["caliber"]
    assert cal["universe_type"] == "23" and cal["period"] == "5m"
    assert cal["entry_price_mode"] == "close_t"
    assert cal["capital"] == 3_000_000 and cal["max_buy"] == 20_000
    assert cal["priority_value"] == "trailing_first"
    # 口径与 farm_rules 单一真相源同源
    from core import farm_rules
    assert cal["universe"] == farm_rules.SWEEP_CALIBER["universe"]


def test_backtest_prefill_cond_days_appends_text(root):
    _archive_with_params(root, cond_days=7)
    d = fs.backtest_prefill(str(root), "GS0607")
    assert "条件时间止盈7天/盈利3%" in d["combo_text"]


def test_backtest_prefill_ladder_note_when_not_off(root):
    _archive_with_params(root, ladder="l2")
    d = fs.backtest_prefill(str(root), "GS0607")
    assert "阶梯止盈" in d["ladder_note"] and "人工核对" in d["ladder_note"]


def test_backtest_prefill_missing_gs_raises_keyerror(root):
    _archive_with_params(root)
    import pytest as _pt
    with _pt.raises(KeyError):
        fs.backtest_prefill(str(root), "GS9999")


def test_backtest_prefill_missing_params_raises_valueerror(root):
    _wj(root / "archive.json", {
        "GS0001": {"file": "a.md", "best": None,
                   "verdict": {"code": "invalid"}},
        "GS0002": {"file": "b.md", "best": {"key": "k", "annret": 0.2},
                   "verdict": {"code": "pass"}}})   # 旧档案: 有 best 无 params
    import pytest as _pt
    for gs in ("GS0001", "GS0002"):
        with _pt.raises(ValueError):
            fs.backtest_prefill(str(root), gs)


def test_backtest_prefill_validates_all_numeric_keys(root):
    """审计 LOW-6 + 第二轮 LOW-F: **六项**数值键缺失都要拒 (原只验 cost/四键)。

    cond_days=None 会静默变「无条件时间止盈」; cond_profit=None 经 `or 0` 变 0%
    即「持仓 N 天必卖」—— 语义被悄悄改掉, 所以必须拒而不是兜底。
    """
    base = {"cost": -0.2, "act": 0.08, "dd": 0.005, "ladder": "off",
            "time_days": 20, "cond_days": 0, "cond_profit": 0.0}
    import pytest as _pt
    for miss in ("cost", "act", "dd", "time_days", "cond_days", "cond_profit"):
        p = dict(base, **{miss: None})
        _wj(root / "archive.json", {"GS0001": {
            "file": "a.md", "best": {"key": "k", "params": p},
            "verdict": {"code": "pass"}, "window": ["2024-09-02", "2026-09-03"]}})
        fs._CACHE.clear()
        with _pt.raises(ValueError) as ei:
            fs.backtest_prefill(str(root), "GS0001")
        assert miss in str(ei.value)


def test_backtest_prefill_requires_window(root):
    """审计第二轮 LOW-H: window 缺失必须拒 —— 否则静默沿用用户旧区间跑。"""
    base = {"cost": -0.2, "act": 0.08, "dd": 0.005, "ladder": "off",
            "time_days": 20, "cond_days": 0, "cond_profit": 0.0}
    _wj(root / "archive.json", {"GS0001": {
        "file": "a.md", "best": {"key": "k", "params": base},
        "verdict": {"code": "pass"}}})          # 无 window
    import pytest as _pt
    fs._CACHE.clear()
    with _pt.raises(ValueError) as ei:
        fs.backtest_prefill(str(root), "GS0001")
    assert "window" in str(ei.value)


def test_backtest_prefill_caliber_text_discloses_engine_gap(root):
    """审计第一轮 HIGH-1 + 第二轮 HIGH-A/LOW-G: 口径文案必须披露
    ①移动止盈确认语义 ②引擎入口差异(缺 5m 丢信号 vs 降级补线) ③最低买入。"""
    _archive_with_params(root)
    d = fs.backtest_prefill(str(root), "GS0607")
    assert d["caliber"]["trailing_confirm"] == "intraday"
    assert d["caliber"]["min_buy"] == 2000.0
    t = d["caliber_text"]
    assert "盘中触线" in t
    assert "沪深300" in t and "5分线" in t and "T日收盘买入" in t
    assert "最低买入2000元" in t                   # 带单位 (LOW-G: 硬闸口径)
    assert "降级补线" in t and "别与粗扫并排比" in t   # HIGH-A 披露
    from core import farm_rules
    assert farm_rules.SWEEP_CALIBER["trailing_confirm"] == "intraday"
    assert farm_rules.SWEEP_CALIBER["min_buy"] == 2000.0
