# -*- coding: utf-8 -*-
"""Stage 0 静态扫描 (2026-08-26, 全新编写)。

扫 gs_1_*.txt 546 个公式, 排除 (命中即出局, 记死因):
  ① 未来函数  ② 漂移画图 POLYLINE  ③ ZXNH  ④ 专有数据函数  ⑤ 跨周期引用
纯装饰画图 (STICKLINE/DRAWTEXT 等) 不排除 (用户 00:37 规则)。

输出: <run>/stage0_scan/report.json
  - 每公式: status(ok/excluded/error) + reasons + params + 输出结构 + 函数清单
  - 汇总: 各死因分布、函数使用频次 Top、参数 P1/P2 引用率 (供解释器决策)

用法:
    python tools/formula_pipeline/stage0_scan.py [--run-dir <dir>]
"""
import argparse
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from tools.formula_pipeline.common import (gs_files, 
    DECORATIVE_DRAW_FUNCS, formula_display_name, new_run_dir,
    read_formula_txt, save_json, scan_static,
)

DRAW_LINE_RE = re.compile(
    r"^\s*(" + "|".join(DECORATIVE_DRAW_FUNCS) + r")\s*\(", re.IGNORECASE)


def output_structure(source: str) -> dict:
    """统计输出结构: 具名输出行数 / 末行是否裸表达式 / 是否无有效输出。

    具名输出 = `NAME:expr` (NAME:= 为赋值不算); 画图行不算输出。
    """
    lines = [l.strip() for l in source.splitlines() if l.strip()]
    named = []
    for i, l in enumerate(lines):
        if DRAW_LINE_RE.match(l):
            continue
        m = re.match(r"^([\w\u4e00-\u9fa5]+)\s*:(?!=)", l)
        if m:
            named.append((i, m.group(1)))
    bare_tail = bool(lines) and not DRAW_LINE_RE.match(lines[-1]) \
        and not re.match(r"^([\w\u4e00-\u9fa5]+)\s*:=(\s|$)", lines[-1]) \
        and not re.match(r"^([\w\u4e00-\u9fa5]+)\s*:(?!=)", lines[-1])
    return {
        "named_outputs": [n for _, n in named],
        "named_count": len(named),
        "bare_expr_tail": bare_tail,
        "has_output": len(named) > 0 or bare_tail,
    }


FUNC_CALL_RE = re.compile(r"\b([A-Z][A-Z0-9_]*)\s*\(", re.IGNORECASE)
BUILTIN_VARS = {"C", "O", "H", "L", "V", "VOL", "CLOSE", "OPEN", "HIGH",
                "LOW", "AMOUNT", "AMO", "ADVANCE", "DECLINE", "INDEXC"}


def used_functions(source: str) -> list:
    """源码中调用的函数清单 (去重排序, 供解释器预判可解析性)。"""
    funcs = {m.group(1).upper() for m in FUNC_CALL_RE.finditer(source)}
    return sorted(funcs - BUILTIN_VARS)


def param_usage(source: str) -> list:
    """统计 P1..P9 参数引用 (txt 参数行只有值没有名, 默认按 P1..Pn 绑定)。"""
    return sorted({m for m in re.findall(r"\bP([1-9])\b", source)})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", default=None, help="复用已有 run 目录")
    ap.add_argument("--batch", default="gs_1_", help="批次前缀 (gs_0_/gs_1_/...)")
    args = ap.parse_args()

    run_dir = Path(args.run_dir) if args.run_dir else new_run_dir()
    out_dir = run_dir / "stage0_scan"
    out_dir.mkdir(parents=True, exist_ok=True)

    files = gs_files(args.batch)
    report = {"total": len(files), "ok": [], "excluded": [], "errors": [],
              "func_freq": Counter(), "param_p_freq": Counter(),
              "no_output": []}
    for p in files:
        try:
            f = read_formula_txt(p)
            hits = scan_static(f["source"])
            name = f["name"] or formula_display_name(p.name)
            if hits:
                report["excluded"].append({
                    "file": p.name, "name": name, "reasons": hits})
            else:
                ostr = output_structure(f["source"])
                entry = {"file": p.name, "name": name,
                         "params": f["params"], "output": ostr,
                         "functions": used_functions(f["source"]),
                         "param_refs": param_usage(f["source"])}
                report["ok"].append(entry)
                for fn in entry["functions"]:
                    report["func_freq"][fn] += 1
                for pr in entry["param_refs"]:
                    report["param_p_freq"][pr] += 1
                if not ostr["has_output"]:
                    report["no_output"].append(name)
        except Exception as e:
            report["errors"].append({"file": p.name, "err": str(e)[:120]})

    summary = {
        "total": report["total"],
        "pass": len(report["ok"]),
        "excluded": len(report["excluded"]),
        "errors": len(report["errors"]),
        "reason_dist": Counter(),
        "no_output": len(report["no_output"]),
        "func_top30": report["func_freq"].most_common(30),
        "param_p_freq": dict(report["param_p_freq"]),
    }
    for e in report["excluded"]:
        for k in e["reasons"]:
            summary["reason_dist"][k] += 1
    summary["reason_dist"] = dict(summary["reason_dist"])

    save_json(out_dir / "report.json", {
        "total": report["total"],
        "summary": {k: v for k, v in summary.items()},
        "ok": report["ok"], "excluded": report["excluded"],
        "errors": report["errors"],
    })

    print(f"[S0] total={summary['total']} pass={summary['pass']} "
          f"excluded={summary['excluded']} errors={summary['errors']} "
          f"no_output={summary['no_output']}")
    print(f"[S0] reason_dist={summary['reason_dist']}")
    print(f"[S0] param_P refs={summary['param_p_freq']}")
    print(f"[S0] func_top10={summary['func_top30'][:10]}")
    print(f"[OUT] {out_dir / 'report.json'}")


if __name__ == "__main__":
    main()
