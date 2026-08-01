"""brain/eval.py — golden set 回答质量评估（计划书 v2 落地修正：闸门可数化）。

对计划书 Phase 2 闸门"正确回答+引用依据"（人判）的修正：
固定 10 题（golden_set.json）逐题问大脑，must_contain 正则全中算过，
通过率 ≥ 8/10 才算 Phase 2 闸门达标 —— 与 P1"人审准确率 ≥80%"同口径。

v2 模式（--v2）：加载 golden_set_v2.json（30 题 5 类），增加 --judge 进行 LLM 打分。

用法：python -m brain.eval [--v2] [--judge] [--limit 3] [--timeout 120]
需要真实 claude CLI；CLI 缺失时如实报告不可评估，不伪造结果。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path

from brain.claude_cli import _find_cli, ask_brain_sync

_GOLDEN = Path(__file__).resolve().parent / "golden_set.json"
_GOLDEN_V2 = Path(__file__).resolve().parent / "golden_set_v2.json"
_REPORT_DIR = Path(__file__).resolve().parent.parent / "output" / "brain_eval"
PASS_THRESHOLD = 0.8


def load_golden(path: Path | None = None) -> list[dict]:
    return json.loads((path or _GOLDEN).read_text(encoding="utf-8"))


def check_answer(case: dict, answer: str) -> bool:
    """must_contain 全部正则命中算过。"""
    return all(re.search(p, answer or "") for p in case.get("must_contain", []))


def run_eval(limit: int | None = None, timeout: int = 300,
             max_turns: int = 20, channel: str = "eval") -> dict:
    """逐题问大脑，返 {total, passed, pass_rate, gate_pass, details}。

    每题独立 channel（eval_g01/eval_g02…）：上下文不跨题累积——共享 session
    会让后题背着前 9 题的历史，又慢又蠢（实测 g01-g03 因此 max turns 翻车）。
    """
    cases = load_golden()[:limit] if limit else load_golden()
    report = {"total": len(cases), "passed": 0, "pass_rate": 0.0,
              "gate_pass": False, "details": []}
    if not _find_cli():
        report["error"] = "claude CLI 未安装，无法评估（非失败，是环境不具备）"
        return report
    for c in cases:
        r = ask_brain_sync(c["q"], timeout=timeout, max_turns=max_turns,
                           channel=f"{channel}_{c['id']}", archive=False)
        ok = r["success"] and check_answer(c, r["answer"])
        report["passed"] += int(ok)
        report["details"].append({
            "id": c["id"], "pass": ok, "success": r["success"],
            "low_confidence": r.get("low_confidence", True),
            "answer_preview": r.get("answer", "")[:200],  # 失败诊断用
        })
    report["pass_rate"] = report["passed"] / report["total"] if report["total"] else 0.0
    report["gate_pass"] = report["pass_rate"] >= PASS_THRESHOLD
    return report


def run_eval_v2(limit: int | None = None, timeout: int = 300,
                max_turns: int = 20, channel: str = "eval",
                with_judge: bool = False) -> dict:
    """v2 模式：30 题 5 类，正则 + 可选 LLM judge。"""
    cases = load_golden(_GOLDEN_V2)[:limit] if limit else load_golden(_GOLDEN_V2)
    report = {
        "mode": "v2+judge" if with_judge else "v2",
        "total": len(cases), "regex_pass": 0, "regex_pass_rate": 0.0,
        "dim_avg": {}, "overall_avg": 0.0, "weak_cases": [], "details": [],
        "judge_caveat": "grader 与被测模型同 provider，分数仅用于版本间纵向对比"
        if with_judge else "",
    }
    if not _find_cli():
        report["error"] = "claude CLI 未安装，无法评估（非失败，是环境不具备）"
        return report

    if with_judge:
        from brain.eval_judge import judge_answer
    dim_sums = {"accuracy": 0.0, "completeness": 0.0, "citation": 0.0,
                "counter": 0.0, "clarity": 0.0}
    judged_count = 0

    for c in cases:
        r = ask_brain_sync(c["q"], timeout=timeout, max_turns=max_turns,
                           channel=f"{channel}_{c['id']}", archive=False)
        regex_ok = r["success"] and check_answer(c, r["answer"])
        report["regex_pass"] += int(regex_ok)
        detail = {
            "id": c["id"], "category": c.get("category", ""),
            "regex_pass": regex_ok, "success": r["success"],
            "answer_preview": r.get("answer", "")[:200],
        }
        if with_judge:
            j = judge_answer(c["q"], r.get("answer", ""),
                             c.get("judge_focus", ""))
            if "error" not in j:
                detail["judge"] = j
                for d in dim_sums:
                    dim_sums[d] += j["scores"].get(d, 0)
                judged_count += 1
            else:
                detail["judge_error"] = j["error"]
        report["details"].append(detail)

    report["regex_pass_rate"] = report["regex_pass"] / report["total"] if report["total"] else 0.0

    if with_judge and judged_count > 0:
        report["dim_avg"] = {d: round(s / judged_count, 2) for d, s in dim_sums.items()}
        report["overall_avg"] = round(sum(report["dim_avg"].values()) / 5, 2)
        report["weak_cases"] = [
            {"id": d["id"], "overall": d.get("judge", {}).get("total", 0)}
            for d in report["details"]
            if d.get("judge", {}).get("total", 0) < 3.0
        ]
    return report


def _save_report(report: dict):
    """落盘 output/brain_eval/YYYYMMDD_HHMMSS.json + .md。"""
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = _REPORT_DIR / f"{ts}.json"
    md_path = _REPORT_DIR / f"{ts}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    # 人读 MD 摘要
    lines = [
        f"# VERA 大脑评估报告 — {ts}",
        f"模式: {report.get('mode', 'v1')}",
        f"总题数: {report['total']}",
    ]
    if "regex_pass_rate" in report:
        lines.append(f"正则通过率: {report['regex_pass_rate']:.0%}")
    if report.get("overall_avg"):
        lines.append(f"Judge 整体均分: {report['overall_avg']:.1f}/5")
        dims = report.get("dim_avg", {})
        lines.append("| 维度 | 均分 |")
        lines.append("|------|------|")
        for d, s in dims.items():
            lines.append(f"| {d} | {s:.1f} |")
    if report.get("weak_cases"):
        lines.append("\n## 弱项（<3.0）")
        for w in report["weak_cases"]:
            lines.append(f"- {w['id']}: {w['overall']:.1f}")
    if report.get("judge_caveat"):
        lines.append(f"\n> ⚠ {report['judge_caveat']}")
    md_path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m brain.eval",
                                 description="golden set 回答质量评估")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--v2", action="store_true", help="使用 golden_set_v2.json（30 题）")
    ap.add_argument("--judge", action="store_true", help="启用 LLM judge 五维度打分（需 --v2）")
    args = ap.parse_args(argv)

    if args.v2:
        if args.judge:
            print("（注意：--judge 模式将对每题调用一次 claude CLI 打分，30 题 ≈ 60 次 CLI 调用，预计耗时数分钟）")
        report = run_eval_v2(limit=args.limit, timeout=args.timeout, with_judge=args.judge)
    else:
        report = run_eval(limit=args.limit, timeout=args.timeout)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    _save_report(report)

    if "error" in report:
        return 2
    if args.v2:
        return 0  # v2 不设硬性门槛，建基线
    return 0 if report["gate_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
