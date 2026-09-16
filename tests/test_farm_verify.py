"""公式农场第四闸门「定量复核」 (farm_verify) 测试。

2026-09-11 补: 计划书 §4 步骤 6 的"上架后定量复核(repaint_check +
future_func_check)"此前只在文档里, 生产三闸门没有它。本闸门把它接上, 且:
- 复核**不通过**与复核**跑失败**必须分开写 (前者是结论, 后者是故障);
- 解析不出来的公式标「未解析」, 绝不默认通过。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.formula_farm import farm_verify as fv  # noqa: E402

REPAINT_OUT = """[INFO] 股票池 5003 只, 截断日 20260811
[INFO] GS1285 截断选股 (end=20260811)...
[INFO] GS1285 全量选股 (end=20260911)...
  GS1285: 窗口信号 截断=12 全量=12 消失=0 新增=0 不一致率=0.00%
  → 完全一致, 因果公式
[INFO] GS1292 截断选股 (end=20260811)...
  GS1292: 窗口信号 截断=3 全量=5 消失=0 新增=2 不一致率=40.00%
    新增: 600000.SH 20260811
  → 重画实锤 (不一致率>2%)
"""

FUTURE_OUT = """[INFO] GS1285 选股中...
[INFO] GS1285: 信号 300 → 120 (30日规则后)

=== 判定 (T+1 保留率 = T+1收益 / T+0收益) ===
GS1285: T+0=+18.5% T+1=+15.0% 保留率=81% → 衰减正常
GS1292: T+0=+30.0% T+1=+3.0% 保留率=10% → 疑似未来函数
"""


def test_parse_repaint_extracts_rate_and_verdict():
    got = fv.parse_repaint(REPAINT_OUT)
    assert got["GS1285"]["ok"] is True
    assert got["GS1285"]["rate"] == 0.0
    assert got["GS1292"]["ok"] is False
    assert got["GS1292"]["rate"] == 0.4
    assert "重画" in got["GS1292"]["verdict"]


def test_parse_future_extracts_keep_ratio():
    got = fv.parse_future(FUTURE_OUT)
    assert got["GS1285"]["ok"] is True
    assert got["GS1285"]["keep"] == 0.81
    assert got["GS1292"]["ok"] is False
    assert "未来函数" in got["GS1292"]["verdict"]


def test_unparsed_formula_is_not_a_pass():
    got = fv.parse_repaint("[INFO] 什么都没输出\n")
    assert got == {}
    # 目标里有、解析结果里没有 → 未解析 (非通过)
    merged = fv.merge_verdicts(["GS1285", "GS9999"], got, {})
    assert merged["GS9999"]["repaint"]["ok"] is None
    assert "未解析" in merged["GS9999"]["repaint"]["verdict"]


def test_tool_failure_is_reported_as_failure_not_pass():
    merged = fv.merge_verdicts(["GS1285"], {}, {}, repaint_err="rc=2")
    assert merged["GS1285"]["repaint"]["ok"] is None
    assert "复核失败" in merged["GS1285"]["repaint"]["verdict"]
    assert "rc=2" in merged["GS1285"]["repaint"]["verdict"]


def test_build_report_has_verdicts_and_keeps_failures_visible():
    merged = fv.merge_verdicts(["GS1285", "GS1292"], fv.parse_repaint(REPAINT_OUT),
                               fv.parse_future(FUTURE_OUT))
    md = fv.build_verify_report(merged, {"date": "2026-09-11", "cmd_repaint": "python tools/repaint_check.py",
                                         "cmd_future": "python tools/future_func_check.py",
                                         "cutoff": "20260811"})
    assert md.startswith("# ")
    assert "GS1285" in md and "GS1292" in md
    assert "未通过" in md or "不通过" in md
    assert "重画" in md and "未来函数" in md
    assert "<div" not in md


def test_overall_status_counts():
    merged = fv.merge_verdicts(["GS1285", "GS1292"], fv.parse_repaint(REPAINT_OUT),
                               fv.parse_future(FUTURE_OUT))
    stat = fv.summarize(merged)
    assert stat["total"] == 2
    assert stat["pass"] == 1
    assert stat["fail"] == 1


# ── 2026-09-11 实测: 两个工具连跑时, 第二个可能撞上 TDX 连接刚被关掉的窗口 ──

def test_run_retry_recovers_after_transient_failure(monkeypatch):
    """第一次失败(典型: TDX 连接路径为空) → 重试一次成功。"""
    calls = {"n": 0}

    def fake_run(cmd, timeout):
        calls["n"] += 1
        return ("", "rc=1") if calls["n"] == 1 else ("判定了", "")

    monkeypatch.setattr(fv, "_run", fake_run)
    monkeypatch.setattr(fv, "RETRY_PAUSE", 0)
    out, err = fv._run_retry(["x"], 10, 1, "未来函数甄别")
    assert err == "" and out == "判定了" and calls["n"] == 2


def test_run_retry_gives_up_and_reports(monkeypatch):
    """一直失败 → 如实返回失败 (调用方写成「复核失败」, 绝不当通过)。"""
    monkeypatch.setattr(fv, "_run", lambda cmd, timeout: ("", "rc=1"))
    monkeypatch.setattr(fv, "RETRY_PAUSE", 0)
    out, err = fv._run_retry(["x"], 10, 1, "重画检测")
    assert err == "rc=1"
