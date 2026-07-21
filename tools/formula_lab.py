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
    "vol20": "20日波动率", "mom5": "5日动量", "mom20": "20日动量", "mom60": "60日动量",
    "circ_mv": "流通市值", "total_mv": "总市值", "pv_corr20": "量价相关性",
    "rsi14": "RSI14", "volr5_20": "量比", "turnover5": "活跃度",
    "volume_ratio": "量比(官方)", "dist_high20": "距20日高点", "dist_high250": "距年高点",
    "ret1": "昨日涨幅", "maxret20": "彩票因子", "macd_hist": "MACD柱",
    "boll_pct": "布林位置", "kdj_k": "KDJ-K", "price": "价格水平",
    "amihud20": "非流动性", "ret_skew20": "收益偏度", "volvol20": "量能波动",
    "overnight20": "隔夜收益", "corr_index20": "跟盘程度", "beta60": "贝塔",
    "amt20": "日均成交额", "sector_heat5": "板块热度5日", "sector_heat20": "板块热度20日",
    "pe_ttm": "市盈率", "pb": "市净率", "ps_ttm": "市销率", "dv_ratio": "股息率",
    "rzye_ratio": "融资占比", "hk_ratio": "北向占比",
    "mf_score": "资金流评分", "dragon_score": "龙虎榜评分",
    "block_score": "大宗评分", "total_score": "综合评分",
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


# ═══════════════════════════════════════════════════════════════
# S5 报告(大白话版, 2026-07-21 用户要求: 看不懂术语, 全部大白话 + 因子排名)
# ═══════════════════════════════════════════════════════════════

FACTOR_DESC = {
    "turnover_rate": "当天买卖活跃程度(换手率)", "volume_ratio": "量比(官方口径)",
    "dist_ma20": "股价比20日均线高多少(涨过头程度)", "intraday20": "当天盘中涨了多少(追高程度)",
    "vol20": "股价波动大小", "mom5": "最近5天涨了多少", "mom20": "最近20天涨了多少",
    "mom60": "最近60天涨了多少", "volr5_20": "最近放量倍数", "turnover5": "近期成交活跃度变化",
    "dist_high20": "离20日最高点有多远", "dist_high250": "离一年最高点有多远",
    "ret1": "昨天一天涨了多少", "maxret20": "最近20天单日最大涨幅(彩票感)",
    "rsi14": "技术指标RSI(超买超卖)", "macd_hist": "技术指标MACD柱",
    "boll_pct": "股价在布林通道里的位置", "kdj_k": "技术指标KDJ的K值",
    "price": "股价的绝对高低", "amihud20": "流动性(小成交就能推动价格)",
    "ret_skew20": "暴涨暴跌倾向(收益偏度)", "volvol20": "成交量的波动大小",
    "pv_corr20": "量价齐升/背离程度", "overnight20": "隔夜跳空收益(开盘缺口)",
    "corr_index20": "跟随大盘的程度", "beta60": "对大盘的敏感度(贝塔)",
    "amt20": "日均成交额", "sector_heat5": "所属板块最近5天热度", "sector_heat20": "所属板块最近20天热度",
    "pe_ttm": "市盈率(贵不贵)", "pb": "市净率", "ps_ttm": "市销率", "dv_ratio": "股息率",
    "total_mv": "总市值", "circ_mv": "流通市值",
    "rzye_ratio": "融资盘占比(杠杆资金)", "hk_ratio": "北向资金持股占比",
    "mf_score": "主力资金流评分", "dragon_score": "龙虎榜机构评分",
    "block_score": "大宗交易评分", "total_score": "三因子综合评分",
}


def _strength(ic: float) -> str:
    a = abs(ic)
    return "强" if a >= 0.10 else ("中" if a >= 0.05 else "弱")


def _stability(icir: float) -> str:
    a = abs(icir)
    return "很稳" if a >= 0.5 else ("稳" if a >= 0.3 else "一般")


def _verdict_cn(v: str) -> str:
    s = str(v)
    if s.startswith("PASS"):
        return "✅ 通过(更赚钱)"
    if s == "BASE":
        return "基准"
    return "❌ 没通过"


def render_report(formula: str, tags: list, yaml: str, s0_notes: dict, quals: list,
                  ab_by_win: dict, final: list, started: str,
                  ic_primary: pd.DataFrame | None = None) -> str:
    """S5 报告(大白话版): 一句话结论 + 裸跑表现 + 因子排名 + 终审对决 + 判定 + 提醒。"""
    L = [f"# {formula} 体检报告(大白话版)", ""]
    L.append(f"> 体检时间: {started} | 用的配置: `{yaml}` | 窗口: {' + '.join(tags)}"
             f"{' ⚠️ 只有一个窗口, 结论先记着别当真(待复核)' if len(tags) < 2 else ''}")
    L.append("")

    # ── 一句话结论 ──
    L.append("## 一句话结论")
    L.append("")
    if final:
        L.append(f"**这个公式建议加过滤:** {'、'.join(final)} —— 两个窗口都验证过, 确实更赚钱。")
    elif quals:
        L.append("**有候选过滤规则, 但两个窗口没全通过 —— 先别用, 继续观察。**")
    else:
        L.append("**没什么好加的 —— 这个公式按现状跑就行, 别折腾过滤。**")
    L.append("")

    # ── 裸跑表现 ──
    L.append("## 这个公式本身(不加任何过滤)赚不赚钱")
    L.append("")
    L.append("| 窗口 | 年化收益 | 最大回撤 | 收益回撤比(越大越好) |")
    L.append("|---|---|---|---|")
    for tag, df in ab_by_win.items():
        b = df[df["arm"] == "base"]
        if len(b):
            r = b.iloc[0]
            L.append(f"| {tag} | {r['annret']:+.1%} | {r['maxdd']:.1%} | {r['calmar']:.2f} |")
    L.append("")

    # ── 因子排名 ──
    if ic_primary is not None and len(ic_primary):
        L += ["## 因子排名(哪些因子和'买入后 10 天涨不涨'关系最大, 前 15 名)", "",
              "| 排名 | 因子 | 大白话意思 | 方向 | 关系强度 | 稳定度 |",
              "|---|---|---|---|---|---|"]
        top = ic_primary.dropna(subset=["icir", "ic_mean"]).sort_values(
            "icir", key=abs, ascending=False).head(15)
        for i, (_, r) in enumerate(top.iterrows(), 1):
            f = r["factor"]
            direction = "越高越跌(要躲)" if r["ic_mean"] < 0 else "越高越涨"
            L.append(f"| {i} | {FACTOR_LABEL.get(f, f)} | {FACTOR_DESC.get(f, f)} | "
                     f"{direction} | {_strength(r['ic_mean'])} | {_stability(r['icir'])} |")
        L.append("")
        L.append("> 读法: 排名靠前又'很稳'的因子, 就是这个公式信号池里最硬的规律。"
                 "'越高越跌'的因子可以拿来躲(剔除最高的); '越高越涨'的可以拿来挑。")
        L.append("")

    # ── 终审对决 ──
    L.append("## 终审对决(同一段历史跑两遍: 裸跑 vs 加过滤)")
    L.append("")
    for tag, df in ab_by_win.items():
        L.append(f"### 窗口 {tag}")
        L.append("")
        L.append("| 过滤方案 | 年化收益 | 最大回撤 | 收益回撤比 | 判定 |")
        L.append("|---|---|---|---|---|")
        for _, r in df.iterrows():
            L.append(f"| {r['arm']} | {r['annret']:+.1%} | {r['maxdd']:.1%} | "
                     f"{r['calmar']:.2f} | {_verdict_cn(r['verdict'])} |")
        L.append("")

    # ── 最终判定 ──
    L.append("## 最终判定")
    L.append("")
    if final:
        L.append("**✅ 这两个窗口都验证通过的规则(可以上生产):**")
        L += [f"- {a}" for a in final]
        L.append("")
        L.append("注意: 判定是相对当前这套止损参数说的; 以后改了止损参数, 要重新终审一遍。")
    else:
        L.append("**❌ 没有规则两个窗口都通过 —— 保持现状不加过滤(没通过也是值钱的信息)。**")
    L.append("")

    # ── 大白话提醒 ──
    L += ["## 必须知道的提醒(大白话)", "",
          "1. 以上结论**只对这个公式的信号池成立**——换个公式, 答案可能完全不一样, 别外推;",
          "2. 回测算出来的数字**整体偏乐观**(成交价假设往好了算), 看'谁比谁好'可以, 别全信绝对年化;",
          "3. 因子排名是'相关性海选', 终审对决才是'真刀真枪'; 排名好看 ≠ 能进策略;",
          "4. 如果你拿很多公式都跑一遍再挑最好的用, 那又多了一层'挑数据'风险, 留点心;",
          "5. 数据边界: 北向资金 2024-08 后没有日度数据; 两融/陆股通只覆盖部分股票; 事件因子只有近一年。"]
    return "\n".join(L) + "\n"


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
    _explicit_yaml = args.strategy_yaml          # 用户显式指定的优先, 不被溯源覆盖
    args.strategy_yaml = resolve_strategy_yaml(args.strategy_yaml)
    # --report-only 配置溯源: 数字来自上次终审用的配置, 以 rules JSON 记录为准
    # (仅未显式指定时启用; 兜底规则若已变更, 防止报告头部张冠李戴)
    if args.report_only and _explicit_yaml is None:
        import json as _json0
        _rj = ROOT / "output" / "reports" / f"{args.formula}_filter_rules.json"
        if _rj.exists():
            try:
                _prev = _json0.loads(_rj.read_text(encoding="utf-8")).get("strategy_yaml")
                if _prev:
                    args.strategy_yaml = _prev
            except Exception:
                pass
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
    ab_by_win, final = {}, []
    if quals:
        spec = arms_spec(quals)
        for tag in tags:
            if not args.report_only:
                run(["tools/overheat_ab_test.py", "--tag", tag, "--formula", args.formula,
                     "--strategy-yaml", args.strategy_yaml, "--arms", spec], f"S4 A/B {tag}")
            ab_by_win[tag] = pd.read_csv(ab_csv(args.formula, tag))
        # 双窗口判定(纪律 2: 单窗口一律"待复核", 不算通过 — 2026-07-20 UPN 冒烟抓出)
        cand = set(ab_by_win[tags[0]]["arm"]) - {"base"}
        for arm in sorted(cand):
            oks = [str(ab_by_win[tag].set_index("arm").loc[arm, "verdict"]).startswith("PASS")
                   for tag in tags]
            if len(tags) >= 2 and all(oks):
                final.append(arm)
            elif any(oks):
                print(f"[S4] {arm}: 单窗口通过, 标'待复核'")

    # S5 报告(大白话版, 含因子排名: 用证据窗 fwd10 IC 表) + S5b 规则 JSON(前端数据源)
    out = (ROOT / "docs" / "audit"
           / f"{datetime.now().strftime('%Y-%m-%d')}_{args.formula}_{'_'.join(tags)}_因子体检报告.md")
    out.write_text(render_report(args.formula, tags, args.strategy_yaml, s0_notes,
                                 quals, ab_by_win, final, started,
                                 ic_primary=ic_by_win.get(tags[-1])), encoding="utf-8")
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
