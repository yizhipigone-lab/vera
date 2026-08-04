"""
402 公式批量回测调度器
- 日线精度, 2019-01-01 ~ 2026-07-01
- 使用 config/default.yaml 当前止盈止损配置
- 2 并发 (TDX 上限), 断点续跑
- 结果输出到 output/gs_402_batch/
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gs_run_one.py")
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "output", "gs_402_batch")
RESULT_FILE = os.path.join(OUT_DIR, "results.jsonl")
SUMMARY_MD = os.path.join(OUT_DIR, "summary.md")

START, END = "20190101", "20260701"
MAX_WORKERS = 2
TIMEOUT_PER = 300  # 每个公式最多 5 分钟

os.makedirs(OUT_DIR, exist_ok=True)


def formula_name_from_file(fname: str) -> str:
    """gs_0_芳香DMI.txt → 芳香DMI"""
    base = os.path.splitext(fname)[0]
    return re.sub(r"^gs_\d+_", "", base)


def run_one(formula: str) -> dict:
    """调用 gs_run_one.py，返回结果 dict。"""
    t0 = time.time()
    try:
        p = subprocess.run(
            [sys.executable, "-X", "utf8", RUNNER, formula, START, END],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=TIMEOUT_PER,
        )
        elapsed = round(time.time() - t0, 1)
        stdout = (p.stdout or "").strip()
        stderr = (p.stderr or "").strip()

        # 取最后一行 JSON
        last_line = ""
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                last_line = line
        if not last_line:
            return {
                "formula": formula, "status": "no_json_output",
                "elapsed": elapsed, "stderr": stderr[-200:],
            }

        result = json.loads(last_line)
        result["formula"] = formula
        result["elapsed"] = elapsed
        if stderr and result.get("status") == "ok":
            result["_stderr_tail"] = stderr[-200:]
        return result
    except subprocess.TimeoutExpired:
        return {"formula": formula, "status": "timeout",
                "elapsed": round(time.time() - t0, 1)}
    except json.JSONDecodeError:
        return {"formula": formula, "status": "json_parse_error",
                "elapsed": round(time.time() - t0, 1),
                "stdout_tail": (stdout or "")[-200:]}
    except Exception as e:
        return {"formula": formula, "status": "error",
                "err": f"{type(e).__name__}: {str(e)[:120]}",
                "elapsed": round(time.time() - t0, 1)}


def load_done(result_path: str) -> dict[str, dict]:
    """读取已有结果，返回 {formula_name: result}。"""
    done = {}
    if os.path.exists(result_path):
        with open(result_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        r = json.loads(line)
                        done[r["formula"]] = r
                    except Exception:
                        pass
    return done


def read_formula_list() -> list[tuple[str, str]]:
    """从扫描报告读取通过公式列表（仅 gs_1_，gs_0_ TDX 不认），返回 [(formula_name, filename), ...]"""
    scan_md = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "docs", "gs_formula_signal_scan.md")
    formulas = []
    in_table = False
    with open(scan_md, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("| # | 公式名 |"):
                in_table = True
                continue
            if in_table:
                if line.startswith("|---"):
                    continue
                if not line.startswith("|"):
                    break
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 4:
                    fname = parts[3].strip("`")
                    # 仅 gs_1_ 开头的公式在 TDX 运行时可识别
                    if fname.startswith("gs_1_") and fname.endswith(".txt"):
                        formulas.append((formula_name_from_file(fname), fname))
    return formulas


def build_summary(results: list[dict]) -> str:
    """从 results.jsonl 生成汇总 MD。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ok = [r for r in results if r.get("status") == "ok"]
    no_sig = [r for r in results if r.get("status") == "no_signals"]
    too_many = [r for r in results if r.get("status") == "too_many_signals"]
    errs = [r for r in results if r.get("status") not in ("ok", "no_signals", "too_many_signals")]

    def avg(key, items):
        vals = [r.get(key, 0) for r in items if r.get(key) is not None]
        return sum(vals) / len(vals) if vals else 0

    lines = []
    def w(s=""): lines.append(s)

    w("# 402 公式批量回测汇总")
    w()
    w(f"> 生成时间：{now}  |  区间：{START[:4]}-{START[4:6]}-{START[6:]} ~ {END[:4]}-{END[4:6]}-{END[6:]}")
    w(f"> 精度：日线  |  止盈止损：config/default.yaml 当前配置")
    w()
    w("## 总览")
    w()
    w("| | 数量 | 占比 |")
    w("|---|---|---|")
    w(f"| 总公式 | {len(results)} | 100% |")
    w(f"| ✅ 有交易 | {len(ok)} | {len(ok)/max(len(results),1)*100:.1f}% |")
    w(f"| ⚪ 无信号 | {len(no_sig)} | {len(no_sig)/max(len(results),1)*100:.1f}% |")
    w(f"| 🔶 信号过多 | {len(too_many)} | {len(too_many)/max(len(results),1)*100:.1f}% |")
    w(f"| ❌ 错误 | {len(errs)} | {len(errs)/max(len(results),1)*100:.1f}% |")
    w()

    if ok:
        w("## 有交易公式  —  按年化收益排序")
        w()
        sorted_ok = sorted(ok, key=lambda r: r.get("annret", 0) or 0, reverse=True)
        w("| # | 公式 | 信号数 | 交易数 | 年化收益 | 最大回撤 | 夏普 | 胜率 | 耗时 |")
        w("|---|------|--------|--------|----------|----------|------|------|------|")
        for i, r in enumerate(sorted_ok, 1):
            w(f"| {i} | {r['formula']} | {r.get('signals','?')} | {r.get('trades','?')} | "
              f"{r.get('annret',0):.2%} | {r.get('maxdd',0):.2%} | "
              f"{r.get('sharpe',0):.2f} | {r.get('winrate',0):.1%} | {r.get('elapsed','?')}s |")
        w()

    w("## 关键统计（有交易公式）")
    w()
    if ok:
        annrets = [r.get("annret", 0) or 0 for r in ok]
        maxdds = [abs(r.get("maxdd", 0) or 0) for r in ok]
        sharpes = [r.get("sharpe", 0) or 0 for r in ok]
        winrates = [r.get("winrate", 0) or 0 for r in ok]
        w(f"- 年化收益均值: {sum(annrets)/len(annrets):.2%}")
        w(f"- 年化收益中位数: {sorted(annrets)[len(annrets)//2]:.2%}")
        w(f"- 最大回撤均值: {sum(maxdds)/len(maxdds):.2%}")
        w(f"- 夏普均值: {sum(sharpes)/len(sharpes):.2f}")
        w(f"- 胜率均值: {sum(winrates)/len(winrates):.1%}")
        w(f"- 年化 > 10%: {sum(1 for v in annrets if v > 0.10)} 个")
        w(f"- 年化 > 20%: {sum(1 for v in annrets if v > 0.20)} 个")
        w(f"- 夏普 > 1.0: {sum(1 for v in sharpes if v > 1.0)} 个")
    w()

    if no_sig:
        w("## 无信号公式")
        w()
        for r in sorted(no_sig, key=lambda x: x["formula"]):
            w(f"- {r['formula']} ({r.get('elapsed','?')}s)")
        w()

    if too_many:
        w("## 信号过多公式（>50k，未回测）")
        w()
        for r in sorted(too_many, key=lambda x: x["formula"]):
            w(f"- {r['formula']} signals={r.get('signals','?')}")
        w()

    if errs:
        w("## 错误")
        w()
        for r in sorted(errs, key=lambda x: x["formula"]):
            detail = r.get("err") or r.get("stderr") or r.get("stdout_tail") or r.get("status")
            w(f"- {r['formula']}: {detail}")
        w()

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=MAX_WORKERS)
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个（调试用）")
    ap.add_argument("--skip-done", action="store_true", default=True)
    ap.add_argument("--no-skip", action="store_true")
    args = ap.parse_args()

    formulas = read_formula_list()
    if not formulas:
        print("[FATAL] 未从扫描报告读取到公式列表")
        return

    if args.limit:
        formulas = formulas[:args.limit]

    done = load_done(RESULT_FILE) if args.skip_done and not args.no_skip else {}
    todo = [(name, fname) for name, fname in formulas if name not in done]
    print(f"总: {len(formulas)} | 已完成: {len(done)} | 待跑: {len(todo)} | workers: {args.workers}")

    if not todo:
        print("全部完成！生成汇总...")
        all_results = list(done.values())
        md = build_summary(all_results)
        with open(SUMMARY_MD, "w", encoding="utf-8") as fh:
            fh.write(md)
        print(f"汇总 → {SUMMARY_MD}")
        return

    # 防休眠
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
    except Exception:
        pass

    total = len(formulas)
    ok_count = sum(1 for r in done.values() if r.get("status") == "ok")
    t_start = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(run_one, name): name for name, _ in todo}
        for fut in as_completed(futs):
            r = fut.result()
            done[r["formula"]] = r

            # 增量追加到 JSONL
            with open(RESULT_FILE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

            if r.get("status") == "ok":
                ok_count += 1
            n = len(done)
            print(f"[{n}/{total}] {r['formula']} → {r.get('status','?')} "
                  f"({r.get('elapsed','?')}s) | 累计ok={ok_count}", flush=True)

    elapsed_min = (time.time() - t_start) / 60
    print(f"\n[DONE] 本轮 {len(todo)} 个, 用时 {elapsed_min:.1f}min")

    # 生成汇总
    all_results = list(done.values())
    md = build_summary(all_results)
    with open(SUMMARY_MD, "w", encoding="utf-8") as fh:
        fh.write(md)
    print(f"汇总 → {SUMMARY_MD}")


if __name__ == "__main__":
    main()
