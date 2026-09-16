"""
年化 > 9% 公式重新回测（2019-01-01 ~ 2026-08-02）
"""
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.farm_rules import TARGET_ANN  # noqa: E402  统计口径与达标线同源 (2026-09-15 审计收口)

RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gs_run_one.py")
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "output", "gs_top9pct")
RESULT_FILE = os.path.join(OUT_DIR, "results.jsonl")
REPORT_MD = os.path.join(OUT_DIR, "report.md")

START, END = "20190101", "20260802"
TIMEOUT_PER = 300

os.makedirs(OUT_DIR, exist_ok=True)

# 61 formulas with >9% annret (excluding 绝底/ZXNH)
FORMULAS = [
    "C1288", "C1290", "QUANTQQ", "下引线", "C1287", "C1289", "沿着5天线走",
    "TPZJ", "枪挑小梁王", "航海突破", "粉漂亮", "永良选股3",
    "GUPIAO_070", "青云加仓", "十指金叉", "预警", "逢高减",
    "六指金叉", "成交组合", "专找小牛", "起攀选股", "趋势搜寻上升",
    "决策参考", "GUPIAO_044", "GUPIAO_074", "波段底部", "XPJS2",
    "黑马摇篮之小", "沿途打劫", "出手就赢", "终极黄金", "GP1017",
    "GUPIAO_016", "琪新波段", "B点信号", "大黑马", "海风二号选股",
    "GUPIAO_043", "青云强势", "GUPIAO_009", "短快进", "GUPIAO_014",
    "GP1039T", "黑马起步选股1", "超准", "ZTXG", "黑马摇篮之大",
    "MACD金叉", "一年四倍", "招财猫买", "GP1001", "GP1001B",
    "招财猫", "GP1009", "短中精", "极地上涨2", "黑马起步选股2",
    "拉升力", "波段全仓", "GUPIAO_015", "明日见阴止损",
]


def run_one(formula: str) -> dict:
    t0 = time.time()
    try:
        p = subprocess.run(
            [sys.executable, "-X", "utf8", RUNNER, formula, START, END],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=TIMEOUT_PER,
        )
        elapsed = round(time.time() - t0, 1)
        stdout = (p.stdout or "").strip()

        last_line = ""
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                last_line = line
        if not last_line:
            return {"formula": formula, "status": "no_json_output", "elapsed": elapsed}

        result = json.loads(last_line)
        result["formula"] = formula
        result["elapsed"] = elapsed
        return result
    except subprocess.TimeoutExpired:
        return {"formula": formula, "status": "timeout", "elapsed": round(time.time() - t0, 1)}
    except json.JSONDecodeError:
        return {"formula": formula, "status": "json_parse_error", "elapsed": round(time.time() - t0, 1)}
    except Exception as e:
        return {"formula": formula, "status": "error",
                "err": f"{type(e).__name__}: {str(e)[:120]}",
                "elapsed": round(time.time() - t0, 1)}


def build_report(results: list[dict]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ok = [r for r in results if r.get("status") == "ok"]
    no_sig = [r for r in results if r.get("status") == "no_signals"]
    too_many = [r for r in results if r.get("status") == "too_many_signals"]
    errs = [r for r in results if r.get("status") not in ("ok", "no_signals", "too_many_signals")]

    lines = []
    def w(s=""): lines.append(s)

    w("# 年化 > 9% 公式回测报告")
    w()
    w(f"> 生成时间：{now}  |  区间：2019-01-01 ~ 2026-08-02（至今）")
    w(f"> 精度：日线  |  止盈止损：config/default.yaml 当前配置  |  股票池：沪深300")
    w()
    w("## 总览")
    w()
    w("| | 数量 |")
    w("|---|---|")
    w(f"| 总公式 | {len(results)} |")
    w(f"| ✅ 有交易 | {len(ok)} |")
    w(f"| ⚪ 无信号 | {len(no_sig)} |")
    w(f"| 🔶 信号过多 | {len(too_many)} |")
    w(f"| ❌ 错误 | {len(errs)} |")
    w()

    if ok:
        sorted_ok = sorted(ok, key=lambda r: r.get("annret", 0) or 0, reverse=True)
        w("## 排名（按年化收益降序）")
        w()
        w("| # | 公式 | 交易数 | 年化收益 | 最大回撤 | 夏普 | 胜率 | 耗时 |")
        w("|---|------|--------|----------|----------|------|------|------|")
        for i, r in enumerate(sorted_ok, 1):
            w(f"| {i} | {r['formula']} | {r.get('trades','?')} | "
              f"{r.get('annret',0):.2%} | {r.get('maxdd',0):.2%} | "
              f"{r.get('sharpe',0):.2f} | {r.get('winrate',0):.1%} | {r.get('elapsed','?')}s |")
        w()

        annrets = [r.get("annret", 0) or 0 for r in sorted_ok]
        maxdds = [abs(r.get("maxdd", 0) or 0) for r in sorted_ok]
        sharpes = [r.get("sharpe", 0) or 0 for r in sorted_ok]
        winrates = [r.get("winrate", 0) or 0 for r in sorted_ok]

        w("## 统计")
        w()
        w(f"- 年化收益均值: {sum(annrets)/len(annrets):.2%}")
        w(f"- 年化收益中位数: {sorted(annrets)[len(annrets)//2]:.2%}")
        w(f"- 最大回撤均值: {sum(maxdds)/len(maxdds):.2%}")
        w(f"- 夏普均值: {sum(sharpes)/len(sharpes):.2f}")
        w(f"- 胜率均值: {sum(winrates)/len(winrates):.1%}")
        w(f"- 年化 > 20%: {sum(1 for v in annrets if v > 0.20)} 个")
        w(f"- 年化 > 15%: {sum(1 for v in annrets if v > TARGET_ANN)} 个")
        w(f"- 夏普 > 2.0: {sum(1 for v in sharpes if v > 2.0)} 个")
        w(f"- 夏普 > 1.5: {sum(1 for v in sharpes if v > 1.5)} 个")
        w()

    if no_sig:
        w("## 无信号")
        for r in no_sig:
            w(f"- {r['formula']}")
        w()

    if too_many:
        w("## 信号过多")
        for r in too_many:
            w(f"- {r['formula']} ({r.get('signals','?')})")
        w()

    if errs:
        w("## 错误")
        for r in errs:
            w(f"- {r['formula']}: {r.get('err', r.get('status','?'))}")
        w()

    return "\n".join(lines)


def main():
    # 读已有结果
    done = {}
    if os.path.exists(RESULT_FILE):
        with open(RESULT_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        r = json.loads(line)
                        done[r["formula"]] = r
                    except Exception:
                        pass

    todo = [f for f in FORMULAS if f not in done]
    print(f"总: {len(FORMULAS)} | 已完成: {len(done)} | 待跑: {len(todo)}")

    if not todo:
        print("全部完成！生成报告...")
        all_results = [done[f] for f in FORMULAS]
        md = build_report(all_results)
        with open(REPORT_MD, "w", encoding="utf-8") as fh:
            fh.write(md)
        print(f"报告 → {REPORT_MD}")
        return

    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
    except Exception:
        pass

    t_start = time.time()
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(run_one, name): name for name in todo}
        for fut in as_completed(futs):
            r = fut.result()
            done[r["formula"]] = r
            with open(RESULT_FILE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            n = len(done)
            print(f"[{n}/{len(FORMULAS)}] {r['formula']} → {r.get('status','?')} "
                  f"({r.get('elapsed','?')}s)", flush=True)

    elapsed_min = (time.time() - t_start) / 60
    print(f"\n[DONE] {len(todo)} 个, 用时 {elapsed_min:.1f}min")

    all_results = [done[f] for f in FORMULAS]
    md = build_report(all_results)
    with open(REPORT_MD, "w", encoding="utf-8") as fh:
        fh.write(md)
    print(f"报告 → {REPORT_MD}")


if __name__ == "__main__":
    main()
