# -*- coding: utf-8 -*-
"""LLM Agent 钩子 (2026-08-26, 全新编写)。

流水线的 AI 复核角色。两级实现:
  1. 规则版 (零依赖兜底): 基于 JSON 产物的确定性检查 — 边缘带识别、
     异常统计、淘汰名单合理性。永远可用, LLM 不可用时唯一执行体。
  2. LLM 增强版 (可选): 调用本项目 brain/ 通道逐批复核, 输出意见存档。

角色映射 (计划书 §6):
  Auditor  → review_s0      静态扫描复核 (误杀/漏杀抽查)
  Warden   → review_d1      三阶段质检 (边缘带+异常清单)
  Bull/Bear/RiskManager → review_borderline 边缘带生死辩论
  Writer   → 报告生成时调用 (build_html 已含规则版解读)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.formula_pipeline.common import load_json, save_json  # noqa: E402

# 边缘带定义: 差及格线 10% 以内 (相对值)
BORDERLINE_REL = 0.10


def _is_borderline(judge: dict, metrics: dict) -> bool:
    """任一失败项差距 <10% → 边缘带 (可能被误杀)。"""
    if judge.get("pass"):
        return False
    m = metrics or {}
    ar = m.get("annualized_return") or 0
    dd = m.get("max_drawdown") or 0
    nt = m.get("total_trades") or 0
    wr = m.get("win_rate") or 0
    for r in judge.get("fail_reasons", []):
        if "≤基准" in r and ar > 0:
            return True          # 正收益但没跑赢基准 — 争议带
        if "回撤" in r and abs(dd) <= 0.45 * (1 + BORDERLINE_REL):
            return True
        if "交易" in r and nt >= 25:
            return True
        if "胜率" in r and wr >= 0.38:
            return True
    return False


def review_d1(run_dir: Path) -> dict:
    """Warden: 三阶段结果质检 → 边缘带名单 + 异常统计。"""
    results = load_json(run_dir / "stage_d1" / "results.json")
    borderline, anomalies = [], []
    for r in results:
        for tag in ("s1", "s2", "s3"):
            st = r.get(tag)
            if st and not st["judge"]["pass"] and _is_borderline(
                    st["judge"], st["metrics"]):
                borderline.append({
                    "name": r["name"], "stage": tag,
                    "reasons": st["judge"]["fail_reasons"],
                    "annualized": (st["metrics"].get("annualized_return")
                                   or 0) * 100,
                })
        # 异常: 交易数爆炸 / 信号密度极端
        dens = r.get("signal_density")
        if dens and dens > 100:
            anomalies.append({"name": r["name"], "type": "density",
                              "value": dens})
        if r.get("status") == "error":
            anomalies.append({"name": r["name"], "type": "error",
                              "error": r.get("error", "")[:100]})

    # 统计概览
    from collections import Counter
    stat = Counter(r["status"] for r in results)
    out = {
        "role": "Warden (数据质检)",
        "n_results": len(results),
        "status_dist": dict(stat),
        "borderline": borderline,
        "n_borderline": len(borderline),
        "anomalies": anomalies,
        "verdict_rule": (
            f"边缘带 {len(borderline)} 个 (差及格线<10%, 可考虑人工复核); "
            f"异常 {len(anomalies)} 个。机械阈值淘汰为主, "
            f"{'建议对边缘带逐个人工过目' if len(borderline) > 0 else '无需额外复核'}。"
        ),
    }
    save_json(run_dir / "agents" / "warden_d1.json", out)
    return out


def review_s0(run_dir: Path) -> dict:
    """Auditor: 静态扫描复核 — 死因分布合理性 + 双重命中统计。"""
    s0 = load_json(run_dir / "stage0_scan" / "report.json")
    cov = load_json(run_dir / "stage0_scan" / "parse_coverage.json")
    excl = s0["excluded"]
    both = [e for e in excl if len(e["reasons"]) >= 2]
    out = {
        "role": "Auditor (公式审查)",
        "total": s0["summary"]["total"],
        "excluded": len(excl),
        "multi_reason": len(both),
        "coverage_pct": cov["coverage_pct"],
        "exec_errors": cov["exec_errors"],
        "verdict_rule": (
            f"排除 {len(excl)} 个 (其中 {len(both)} 个多重命中); "
            f"解释器覆盖率 {cov['coverage_pct']}% "
            f"({'达标' if cov['coverage_pct'] >= 55 else '异常偏低需检查'}); "
            f"执行错误 {len(cov['exec_errors'])} 个已出局。"
        ),
    }
    save_json(run_dir / "agents" / "auditor_s0.json", out)
    return out


def try_llm_review(run_dir: Path) -> dict:
    """LLM 增强复核 (brain 通道可用时)。失败 → 规则版结论原样返回。"""
    base = review_d1(run_dir)
    try:
        prompt = (
            "你是量化策略质检员。以下是一条通达信公式三阶段回测淘汰漏斗的"
            "边缘带名单与统计, 请给出: 1) 哪些边缘带公式值得人工复核 "
            "2) 淘汰标准是否有系统性误杀。200字内。\n"
            + json.dumps(base, ensure_ascii=False)[:3000]
        )
        from brain.llm_client import chat
        reply = chat(prompt)
        base["verdict_llm"] = str(reply)[:1500]
        base["llm_available"] = True
    except Exception as e:
        base["llm_available"] = False
        base["llm_error"] = str(e)[:150]
    save_json(run_dir / "agents" / "warden_d1.json", base)
    return base


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--with-llm", action="store_true")
    args = ap.parse_args()
    rd = Path(args.run_dir)
    a = review_s0(rd)
    print(f"[agent:Auditor] {a['verdict_rule']}")
    w = try_llm_review(rd) if args.with_llm else review_d1(rd)
    print(f"[agent:Warden] {w['verdict_rule']}")
    if w.get("llm_available"):
        print(f"[agent:Warden:LLM] {w.get('verdict_llm', '')[:300]}")
    else:
        print("[agent:Warden:LLM] brain 通道不可用, 规则版结论生效")
