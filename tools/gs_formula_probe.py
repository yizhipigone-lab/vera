"""
gs_txt 公式名可解析性探测 (2026-07-18)

通达信运行时公式库 != gs_txt 导出文件 (实测: UPN 可解析, GUPIAO_001 不可解析)。
本脚本调 TQ 接口小批量探测每个公式名是否在通达信运行时存在, 不跑全A选股。

判断:
  error_id in (0,19) -> resolvable (公式存在, 无论有无信号)
  其他 error_id      -> missing (公式不存在 / 报错)

用法:
  python tools/gs_formula_probe.py --limit 10        # ok 清单前 10
  python tools/gs_formula_probe.py                   # 全量 519
  python tools/gs_formula_probe.py --names UPN,GUPIAO_001
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.formula_runner import FormulaRunner  # noqa: E402

REPORT = "output/gs_filter/report.json"
OUT = "output/gs_filter/probe.json"
# 小 batch 5 股 + 1 月区间 + count 100, 每公式 ~2s
PROBE_STOCKS = ["600000.SH", "000001.SZ", "300750.SZ", "601318.SH", "000858.SZ"]


def probe_one(tq, name):
    try:
        result = tq.formula_process_mul_xg(
            formula_name=name, formula_arg="", return_count=0, return_date=True,
            stock_list=PROBE_STOCKS, stock_period="1d",
            start_time="20240801", end_time="20240831", count=100, dividend_type=1)
        if not result:
            return {"status": "missing", "err": "result None"}
        error_id = str(result.get("ErrorId", "0"))
        error_msg = str(result.get("Error", ""))
        n = 0
        for sc, val in result.items():
            if sc == "ErrorId" or not isinstance(val, dict):
                continue
            for entries in val.values():
                if isinstance(entries, list):
                    n += sum(1 for e in entries
                             if isinstance(e, dict) and str(e.get("Value", "")) == "1")
                break
        if error_id in ("0", "19"):
            return {"status": "resolvable", "signals_1mo": n, "err": ""}
        return {"status": "missing", "err": f"id={error_id} msg={error_msg[:60]}"}
    except Exception as e:
        return {"status": "error", "err": f"{type(e).__name__}: {str(e)[:60]}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--names", default=None)
    args = ap.parse_args()

    if args.names:
        names = args.names.split(",")
    else:
        r = json.load(open(REPORT, encoding="utf-8"))
        names = [e["name"] for e in r["ok"]]
    if args.limit:
        names = names[: args.limit]

    FormulaRunner._ensure_ready()
    tq = FormulaRunner._connector().tq()

    res = {"total": len(names), "resolvable": [], "missing": [], "error": [],
           "details": []}
    t0 = time.time()
    for i, name in enumerate(names):
        r = probe_one(tq, name)
        r["name"] = name
        res["details"].append(r)
        bucket = r["status"] if r["status"] in ("resolvable", "missing", "error") else "error"
        res[bucket].append(name)
        if (i + 1) % 20 == 0 or i == len(names) - 1:
            print(f"[probe] {i+1}/{len(names)} resolvable={len(res['resolvable'])} "
                  f"missing={len(res['missing'])} ({(i+1)/(time.time()-t0):.1f}/s)",
                  flush=True)
    res["seconds"] = round(time.time() - t0, 1)
    json.dump(res, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[OK] total={res['total']} resolvable={len(res['resolvable'])} "
          f"missing={len(res['missing'])} error={len(res['error'])} {res['seconds']}s")
    print(f"[OUT] {OUT}")


if __name__ == "__main__":
    main()
