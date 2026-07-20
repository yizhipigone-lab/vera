# -*- coding: utf-8 -*-
"""公式因子体检实验室(L2) — 一条命令跑完 S0→S5, 产出"该公式该配什么过滤"的判定报告。

计划书: docs/plan/2026-07-19_公式因子体检实验室_计划书.md(v2)
方法论: docs/公式因子体检方法论.md

    S0 前置检查 → S1/S2 IC 筛选(subprocess factor_ic_screen, 双窗口)
    → S3 族归纳自动选臂(纯函数, 预注册规则) → S4 A/B 终审(subprocess overheat_ab_test)
    → S5 报告(docs/audit/{日期}_{formula}_{tag}_{tag2}_因子体检报告.md)

用法:
    python tools/formula_lab.py --formula QUANTQQ --tag 20250719_20260718 --tag2 20230719_20260718
    python tools/formula_lab.py --formula UPN --tag 20250719_20260718   # 单窗口降级(报告标"待复核")
"""
import sys
import os
import argparse
import subprocess
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent

IC_MIN, ICIR_MIN = 0.03, 0.3   # 与 factor_ic_screen 预注册门槛同一把尺
HORIZON = 10                    # S3 选臂用 fwd10
TOP_FAMILIES = 2
MIN_SIGNALS, MIN_DAYS, MIN_PER_DAY = 200, 40, 5


# ═══════════════════════════════════════════════════════════════
# S0 前置检查(纯逻辑, 可测)
# ═══════════════════════════════════════════════════════════════

def precheck(sel: pd.DataFrame, min_signals=MIN_SIGNALS, min_days=MIN_DAYS,
             min_per_day=MIN_PER_DAY) -> tuple[bool, str]:
    """信号量与截面密度是否够做 IC。不够就拒跑, 不产出伪结论。"""
    if len(sel) < min_signals:
        return False, f"信号总量 {len(sel)} < {min_signals}"
    per_day = sel.groupby("select_date").size()
    valid_days = int((per_day >= min_per_day).sum())
    if valid_days < min_days:
        return False, f"有效截面日(≥{min_per_day}条/日) {valid_days} < {min_days}"
    return True, f"OK(信号 {len(sel)} 条, 有效截面日 {valid_days} 天)"


# ═══════════════════════════════════════════════════════════════
# S3 族归纳 + 自动选臂(纯函数, 预注册规则, 可测)
# ═══════════════════════════════════════════════════════════════

def select_arms(ic_by_win: dict, family_of: dict, primary: str | None = None,
                ic_min=IC_MIN, icir_min=ICIR_MIN, top_n=TOP_FAMILIES) -> list:
    """ic_by_win: {tag: 该窗口 fwd10 的 IC 表}; primary: 证据窗(长窗, 缺省取最后一个 tag)。
    规则(v2.1 校准): 阈值只在证据窗卡(|IC|≥ic_min 且 |ICIR|≥icir_min),
    其余窗口只要求 IC 符号一致;双窗口一致性由 S4 A/B 终审强制执行,不在 S3 重复卡阈值
    (QUANTQQ 实战: 1y 窗效应系统性偏弱,双窗同阈值会把已知能过 A/B 的族错杀)。
    族代表 = 证据窗内族成员 |ICIR| 最大者;族按代表强度降序取前 top_n。
    返回 [{family, factor, ic_sign, strength, per_win}]。"""
    if not ic_by_win:
        return []
    primary = primary or list(ic_by_win)[-1]

    # 每因子跨窗记录
    per_factor: dict[str, dict] = {}
    for tag, df in ic_by_win.items():
        d = df.dropna(subset=["icir", "ic_mean"])
        for _, r in d.iterrows():
            per_factor.setdefault(r["factor"], {})[tag] = (r["ic_mean"], r["icir"])

    # 过滤: 全窗口有值 + 符号一致 + 证据窗过线
    passed = []
    for factor, rec in per_factor.items():
        if family_of.get(factor) is None or len(rec) != len(ic_by_win) or primary not in rec:
            continue
        signs = {int(v[0] > 0) - int(v[0] < 0) for v in rec.values()}
        if len(signs) != 1:
            continue
        pim, pir = rec[primary]
        if abs(pim) < ic_min or abs(pir) < icir_min:
            continue
        passed.append({"factor": factor, "family": family_of[factor],
                       "ic_sign": signs.pop(), "primary_icir": abs(pir),
                       "per_win": {t: {"ic_mean": float(v[0]), "icir": float(v[1])}
                                   for t, v in rec.items()}})

    # 族代表 = 证据窗 |ICIR| 最大成员; 族按代表强度降序
    best_by_family: dict[str, dict] = {}
    for p in passed:
        cur = best_by_family.get(p["family"])
        if cur is None or p["primary_icir"] > cur["primary_icir"]:
            best_by_family[p["family"]] = p
    quals = sorted(best_by_family.values(), key=lambda q: -q["primary_icir"])
    for q in quals:
        q["strength"] = q.pop("primary_icir")
    return quals[:top_n]


def arms_spec(quals: list) -> str:
    """选臂结果 → overheat_ab_test --arms 字符串。负 IC 剔高端, 正 IC 剔低端。
    ≥2 族时追加同款档位的组合臂(2026-07-20 用户拍板: 组合也要终审)。"""
    parts = []
    for q in quals:
        side = "top" if q["ic_sign"] < 0 else "bottom"
        parts += [f"{q['factor']}:{side}10", f"{q['factor']}:{side}20"]
    if len(quals) >= 2:
        sides = ["top" if q["ic_sign"] < 0 else "bottom" for q in quals[:2]]
        parts += [f"{quals[0]['factor']}:{sides[0]}10+{quals[1]['factor']}:{sides[1]}10",
                  f"{quals[0]['factor']}:{sides[0]}20+{quals[1]['factor']}:{sides[1]}20"]
    return ",".join(parts)


# ═══════════════════════════════════════════════════════════════
# S5b 规则 JSON(前端因子过滤区的数据源, 按公式动态渲染)
# ═══════════════════════════════════════════════════════════════

FACTOR_LABEL = {
    "turnover_rate": "换手率", "dist_ma20": "偏离MA20", "intraday20": "日内涨幅",
    "vol20": "20日波动率", "mom5": "5日动量", "mom20": "20日动量",
    "circ_mv": "流通市值", "total_mv": "总市值", "pv_corr20": "量价相关性",
    "rsi14": "RSI14", "volr5_20": "量比", "turnover5": "活跃度",
}
RULE_LABEL = {"top10": "最高10%", "top20": "最高20%", "bottom10": "最低10%", "bottom20": "最低20%"}


def build_rules_json(formula: str, tags: list, yaml_path: str, quals: list,
                     ab_by_win: dict, started: str) -> dict:
    """由 S3 选臂 + S4 终审表生成机器可读规则文件(前端动态渲染用)。"""
    rules = []
    for q in quals:
        side = "top" if q["ic_sign"] < 0 else "bottom"
        for cut in ("10", "20"):
            rid = f"{q['factor']}:{side}{cut}"
            arm_name = f"{q['factor']}_{side}{cut}"
            v_by_win, s_by_win = {}, {}
            for tag, df in ab_by_win.items():
                row = df[df["arm"] == arm_name]
                if len(row):
                    r = row.iloc[0]
                    v_by_win[tag] = r["verdict"]
                    s_by_win[tag] = {"annret": float(r["annret"]), "maxdd": float(r["maxdd"]),
                                     "calmar": float(r["calmar"]) if pd.notna(r["calmar"]) else None}
            adopted = (len(tags) >= 2 and len(v_by_win) >= len(tags)
                       and all(str(v).startswith("PASS") for v in v_by_win.values()))
            pending_review = (not adopted) and any(str(v).startswith("PASS")
                                                   for v in v_by_win.values())
            fl = FACTOR_LABEL.get(q["factor"], q["factor"])
            rules.append({
                "id": rid, "factor": q["factor"], "family": q["family"], "rule": f"{side}{cut}",
                "label": f"剔除{fl}{RULE_LABEL[f'{side}{cut}']}" if side == "top"
                         else f"剔除{fl}{RULE_LABEL[f'{side}{cut}']}",
                "ic_per_win": q["per_win"], "verdict_by_win": v_by_win,
                "stats_by_win": s_by_win, "adopted": adopted,
                "pending_review": pending_review,
            })
    # 组合臂终审结果(2026-07-20 用户拍板: 组合也要终审)
    combos = []
    combo_ids = sorted({a for df in ab_by_win.values() for a in df["arm"] if str(a).startswith("combo_")})
    for cid in combo_ids:
        v_by_win = {}
        s_by_win = {}
        for tag, df in ab_by_win.items():
            row = df[df["arm"] == cid]
            if len(row):
                r = row.iloc[0]
                v_by_win[tag] = r["verdict"]
                s_by_win[tag] = {"annret": float(r["annret"]), "maxdd": float(r["maxdd"]),
                                 "calmar": float(r["calmar"]) if pd.notna(r["calmar"]) else None}
        combos.append({
            "arm": cid,
            "label": cid.replace("combo_", "").replace("_", " ").replace("+", " + "),
            "verdict_by_win": v_by_win, "stats_by_win": s_by_win,
            "adopted": (len(tags) >= 2 and len(v_by_win) >= len(tags)
                        and all(str(v).startswith("PASS") for v in v_by_win.values())),
        })
    return {
        "formula": formula, "generated_at": started, "tags": tags,
        "strategy_yaml": yaml_path, "rules": rules, "combos": combos,
        "note": "规则仅对本公式信号池成立(IC 海选 + A/B 终审双窗口判定); "
                "止损参数变更后需在新基线上复跑终审。",
    }


# ═══════════════════════════════════════════════════════════════
# 编排(subprocess 复用现有两工具)
# ═══════════════════════════════════════════════════════════════

def run(cmd: list, label: str) -> None:
    print(f"\n[{label}] {' '.join(cmd)}")
    t0 = time.time()
    p = subprocess.run([sys.executable] + cmd, cwd=ROOT)
    if p.returncode != 0:
        raise SystemExit(f"[FAIL] {label} 失败(rc={p.returncode})")
    print(f"[{label}] 完成 ({int(time.time() - t0)}s)")


def ic_csv(formula: str, tag: str) -> Path:
    return ROOT / "output" / "reports" / f"factor_ic_{formula}_{tag}.csv"


def ab_csv(formula: str, tag: str) -> Path:
    return ROOT / "output" / "reports" / f"overheat_ab_{formula}_{tag}.csv"


def render_report(formula: str, tags: list, yaml: str, s0_notes: dict, quals: list,
                  ab_tables: dict, final: list, started: str) -> str:
    """S5 报告(固定模板, 含可信度警告)。"""
    lines = [
        f"# {formula} 公式因子体检报告",
        "",
        f"> 生成: {started} | 驱动: `tools/formula_lab.py` | 配置: `{yaml}`",
        f"> 窗口: {', '.join(tags)}{' ⚠️ 单窗口, 结论待复核' if len(tags) < 2 else ''}",
        f"> 方法: docs/公式因子体检方法论.md(S0-S5, 五条纪律)",
        "",
        "## S0 前置检查",
        ""]
    lines += [f"- {t}: {n}" for t, n in s0_notes.items()]
    lines += ["", "## S3 族归纳选臂(预注册规则)", ""]
    if not quals:
        lines.append("**零族达标** — 两个窗口 fwd10 均满足 |IC|≥0.03 且 |ICIR|≥0.3 的族不存在。"
                     "结论: 无可行动因子, 建议保持现状, 不加过滤。")
    else:
        lines += [f"- 族 `{q['family']}` 代表 `{q['factor']}` "
                  f"(IC 符号 {'负→剔高端' if q['ic_sign'] < 0 else '正→剔低端'}, "
                  f"min|ICIR|={q['strength']:.2f}, "
                  f"分窗: {', '.join(f'{t}: IC {v['ic_mean']:+.3f}/ICIR {v['icir']:+.2f}' for t, v in q['per_win'].items())})"
                  for q in quals]
    lines += ["", "## S4 A/B 终审(同 stop_config)", ""]
    for tag, tb in ab_tables.items():
        lines += [f"### {tag}", "", "```", tb, "```", ""]
    lines += ["## 判定结论", ""]
    if final:
        lines.append("**双窗口均 PASS 的臂(可纳入生产候选)**:")
        lines += [f"- ✅ `{a}`" for a in final]
        lines.append("")
        lines.append("纳入前注意: 判定相对当前 stop_config 成立; 若生产止损参数变更, 需在新基线上复跑终审。")
    else:
        lines.append("**无臂双窗口通过** — 保持现状, 不加过滤(负结果同样有价值)。")
    lines += [
        "",
        "## 可信度警告(五条纪律, 必读)",
        "",
        "1. 以上结论**仅对该公式的信号池成立**, 不外推到其他公式/全市场;",
        "2. 回测系统性乐观(CLAUDE.md 7.5/10), 相对排序可参考, 绝对年化不可全信;",
        "3. IC 是相关性海选, A/B 是终审; 本报告任何 IC 数字不构成进策略的依据;",
        "4. 批量跑多个公式挑最优组合 = 又一层多重检验, 跨公式结论需样外验证;",
        "5. 数据边界: 北向日频 2024-08 起断供; 两融/陆股通仅覆盖子集; 事件因子仅近 1 年窗。",
    ]
    return "\n".join(lines) + "\n"


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--formula", required=True)
    ap.add_argument("--tag", required=True, help="短窗(主窗口)")
    ap.add_argument("--tag2", default=None, help="长窗(复核窗口); 缺省 = 单窗口降级")
    ap.add_argument("--strategy-yaml", default=None,
                    help="策略 yaml; 缺省自动解析: current.yaml(前端保存) > strategy_QUANTQQ.yaml")
    ap.add_argument("--report-only", action="store_true",
                    help="只重建报告+规则JSON(复用已有 IC/A/B CSV, 不跑回测)")
    args = ap.parse_args()
    from utils.config_loader import resolve_strategy_yaml
    args.strategy_yaml = resolve_strategy_yaml(args.strategy_yaml)
    tags = [t for t in (args.tag, args.tag2) if t]
    started = datetime.now().strftime("%Y-%m-%d %H:%M")

    # S0 前置检查(两窗口分别)
    s0_notes = {}
    for tag in tags:
        p = ROOT / "data" / "baseline" / f"{args.formula}_selections_{tag}.parquet"
        if not p.exists():
            raise SystemExit(f"[S0 FAIL] baseline 不存在: {p}(先用 run_upn_baseline 生成)")
        sel = pd.read_parquet(p)
        ok, note = precheck(sel)
        s0_notes[tag] = note
        if not ok:
            raise SystemExit(f"[S0 FAIL] {tag}: {note} — 信号量不足, 拒跑(不产出伪结论)")
        print(f"[S0] {tag}: {note}")

    # S1/S2 IC 筛选(事件因子 parquet 缺失的窗口自动 --skip-event-factors)
    for tag in tags:
        matrix = ROOT / "output" / "gs_filter" / f"factor_matrix_{args.formula}_{tag}.parquet"
        # IC 排行榜与因子矩阵必须同时存在;矩阵是 S4 的因子值来源(缺则重跑 S2)
        if args.report_only or (ic_csv(args.formula, tag).exists() and matrix.exists()):
            print(f"[S2] {tag}: IC 结果 + 因子矩阵已存在, 复用")
            continue
        cmd = ["tools/factor_ic_screen.py", "--formula", args.formula, "--tag", tag]
        if not (ROOT / "data" / "factors" / f"moneyflow_{tag}.parquet").exists():
            cmd.append("--skip-event-factors")
        run(cmd, f"S2 IC筛选 {tag}")

    # S3 选臂
    from factor_ic_screen import FAMILY_OF
    ic_by_win = {}
    for tag in tags:
        df = pd.read_csv(ic_csv(args.formula, tag))
        ic_by_win[tag] = df[df["horizon"] == HORIZON]
    quals = select_arms(ic_by_win, FAMILY_OF, primary=tags[-1])
    print(f"[S3] 选臂: {[(q['family'], q['factor'], q['ic_sign']) for q in quals] or '零族达标'}")

    # S4 A/B 终审(有臂才跑; --report-only 复用已有结果)
    ab_tables, ab_by_win, final = {}, {}, []
    if quals:
        spec = arms_spec(quals)
        for tag in tags:
            if not args.report_only:
                run(["tools/overheat_ab_test.py", "--tag", tag, "--formula", args.formula,
                     "--strategy-yaml", args.strategy_yaml, "--arms", spec], f"S4 A/B {tag}")
            tb = pd.read_csv(ab_csv(args.formula, tag))
            ab_by_win[tag] = tb
            ab_tables[tag] = tb.to_string(index=False)
        # 双窗口判定(纪律 2: 单窗口一律"待复核", 不算通过 — 2026-07-20 UPN 冒烟抓出)
        cand = set(ab_by_win[tags[0]]["arm"]) - {"base"}
        for arm in sorted(cand):
            oks = [str(ab_by_win[tag].set_index("arm").loc[arm, "verdict"]).startswith("PASS")
                   for tag in tags]
            if len(tags) >= 2 and all(oks):
                final.append(arm)
            elif any(oks):
                print(f"[S4] {arm}: 单窗口通过, 标'待复核'")

    # S5 报告 + S5b 规则 JSON(前端数据源)
    out = (ROOT / "docs" / "audit"
           / f"{datetime.now().strftime('%Y-%m-%d')}_{args.formula}_{'_'.join(tags)}_因子体检报告.md")
    out.write_text(render_report(args.formula, tags, args.strategy_yaml, s0_notes,
                                 quals, ab_tables, final, started), encoding="utf-8")
    import json as _json
    rules_path = ROOT / "output" / "reports" / f"{args.formula}_filter_rules.json"
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text(_json.dumps(
        build_rules_json(args.formula, tags, args.strategy_yaml, quals, ab_by_win, started),
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[S5] 报告已生成: {out}")
    print(f"[S5b] 规则 JSON: {rules_path}(前端因子过滤区数据源)")
    print(f"[判定] {'双窗口通过: ' + ', '.join(final) if final else '无臂通过 / 无可行动因子'}")


if __name__ == "__main__":
    main()
