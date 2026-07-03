"""
4 个公式长周期回测 (先生 2026-07-04)

先生要点:
- 公式: GP1014 / RZZT / 10日内涨停过 / 短线之王
- 区间: 2020.01.01 ~ 2026.07.03
- 参数: 先生前端最新参数 (cost_stop -4.6% / trailing 3.9%/1.7% / ladder 3%:20%/13%:60% / time 12d / cond_time 7d/1.5% / first_day OFF)
- 串行: TDX PYPlugins 不支持并发, 必须串行
- 输出: output/gs_top4_longterm/*.json + .md
"""
import json, os, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CFG_TEMPLATE = ROOT / "config" / "strategy_top4.yaml"
OUT_DIR = ROOT / "output" / "gs_top4_longterm"
OUT_DIR.mkdir(parents=True, exist_ok=True)

FORMULAS = [
    ("GP1014", "GP1014"),
    ("RZZT", "RZZT"),
    ("10日内涨停过", "10日内涨停过"),
    ("短线之王", "短线之王"),
]


def make_yaml(formula_name):
    """每次生成一份独立的 yaml, 改 formula_name"""
    text = CFG_TEMPLATE.read_text(encoding="utf-8")
    # 在 formula_name: "<>" 这行替换
    import re
    pattern = r'(formula_name:\s*")[^"]*(")'
    new_text = re.sub(pattern, rf'\g<1>{formula_name}\g<2>', text)
    return new_text


def run_one(formula_name, label):
    """调 main.py 跑单个公式"""
    yaml_path = ROOT / "config" / f"_tmp_top4_{label}.yaml"
    yaml_path.write_text(make_yaml(formula_name), encoding="utf-8")

    log_path = OUT_DIR / f"{label}_run.log"
    t0 = time.time()
    try:
        result = subprocess.run(
            [sys.executable, "-u", "-X", "utf8", "main.py",
             "--config", str(yaml_path),
             "--defaults", str(ROOT / "config" / "default.yaml")],
            capture_output=True, text=True, timeout=900,  # 15 分钟单公式上限
            encoding="utf-8", cwd=str(ROOT),
        )
        elapsed = time.time() - t0
        log_path.write_text(result.stdout + "\n--- STDERR ---\n" + result.stderr, encoding="utf-8")
        yaml_path.unlink()  # 删临时 yaml

        # 解析 VERA-CLI 输出 (格式: "  累计收益率:   -98.68%")
        m = {}
        for ln in result.stdout.split("\n"):
            # INFO 行去掉前缀
            body = ln.split("VERA-CLI |", 1)[1] if "VERA-CLI |" in ln else ln
            body = body.strip()
            if body.startswith("累计收益率:"):
                m["cumret"] = float(body.split(":", 1)[1].strip().rstrip("%").replace(" ", "")) / 100
            elif body.startswith("年化收益率:"):
                m["annret"] = float(body.split(":", 1)[1].strip().rstrip("%").replace(" ", "")) / 100
            elif body.startswith("最大回撤:"):
                m["maxdd"] = float(body.split(":", 1)[1].strip().rstrip("%").replace(" ", "")) / 100
            elif body.startswith("夏普比率:"):
                m["sharpe"] = float(body.split(":", 1)[1].strip().replace(" ", ""))
            elif body.startswith("胜率:"):
                m["winrate"] = float(body.split(":", 1)[1].strip().rstrip("%").replace(" ", "")) / 100
            elif body.startswith("交易笔数:"):
                m["trades"] = int(body.split(":", 1)[1].strip().replace(" ", ""))
        return {"ok": True, "elapsed_s": round(elapsed, 1), "metrics": m, "log": str(log_path)}

    except subprocess.TimeoutExpired:
        yaml_path.unlink(missing_ok=True)
        return {"ok": False, "error": "15min 超时", "log": str(log_path)}
    except Exception as e:
        yaml_path.unlink(missing_ok=True)
        return {"ok": False, "error": str(e)[:200]}


def main():
    print("=" * 80, flush=True)
    print("  4 公式长周期回测 (2020.1.1 ~ 2026.7.3, 前端参数, 串行)", flush=True)
    print("=" * 80, flush=True)

    # 先生已跑完 GP1014, 直接从日志重新解析作为第一个结果
    results = []
    gp_log = OUT_DIR / "GP1014_run.log"
    if gp_log.exists():
        print(f"\n[1/4] 复用 GP1014 已跑日志 ({gp_log})...", flush=True)
        m = {}
        for ln in gp_log.read_text(encoding="utf-8").split("\n"):
            body = ln.split("VERA-CLI |", 1)[1] if "VERA-CLI |" in ln else ln
            body = body.strip()
            if body.startswith("累计收益率:"):
                m["cumret"] = float(body.split(":", 1)[1].strip().rstrip("%").replace(" ", "")) / 100
            elif body.startswith("年化收益率:"):
                m["annret"] = float(body.split(":", 1)[1].strip().rstrip("%").replace(" ", "")) / 100
            elif body.startswith("最大回撤:"):
                m["maxdd"] = float(body.split(":", 1)[1].strip().rstrip("%").replace(" ", "")) / 100
            elif body.startswith("夏普比率:"):
                m["sharpe"] = float(body.split(":", 1)[1].strip().replace(" ", ""))
            elif body.startswith("胜率:"):
                m["winrate"] = float(body.split(":", 1)[1].strip().rstrip("%").replace(" ", "")) / 100
            elif body.startswith("交易笔数:"):
                m["trades"] = int(body.split(":", 1)[1].strip().replace(" ", ""))
        results.append({"ok": True, "elapsed_s": 284, "metrics": m,
                        "formula": "GP1014", "label": "GP1014"})
        print(f"    解析成功: cum={m.get('cumret', 0) * 100:+.2f}% ann={m.get('annret', 0) * 100:+.2f}% "
              f"dd={m.get('maxdd', 0) * 100:.2f}% sh={m.get('sharpe', 0):.2f} "
              f"wr={m.get('winrate', 0) * 100:.1f}% trd={m.get('trades', 0)}", flush=True)

    # 跑剩下 3 个
    for i, (formula_name, label) in enumerate(FORMULAS[1:], 2):
        print(f"\n[{i}/{len(FORMULAS)}] 跑 {formula_name} → {label} ...", flush=True)
        r = run_one(formula_name, label)
        r["formula"] = formula_name
        r["label"] = label
        results.append(r)

        if r["ok"]:
            m = r["metrics"]
            print(f"    完成 ({r['elapsed_s']}s): "
                  f"cum={m.get('cumret', 0) * 100:+.2f}% ann={m.get('annret', 0) * 100:+.2f}% "
                  f"dd={m.get('maxdd', 0) * 100:.2f}% sh={m.get('sharpe', 0):.2f} "
                  f"wr={m.get('winrate', 0) * 100:.1f}% trd={m.get('trades', 0)}", flush=True)
        else:
            print(f"    ERR: {r.get('error', '?')[:100]}", flush=True)

    # 汇总 JSON
    with open(OUT_DIR / "summary.json", "w", encoding="utf-8") as f:
        json.dump({"config": {"interval": "20200101~20260703", "stop_loss": "先生前端最新参数"},
                   "results": results}, f, ensure_ascii=False, indent=2)

    # 汇总 MD
    with open(OUT_DIR / "summary.md", "w", encoding="utf-8") as f:
        f.write("# 4 公式长周期回测 (2020.1.1 ~ 2026.7.3, 前端参数)\n\n")
        f.write("| 公式 | 累计 | 年化 | 回撤 | 夏普 | 胜率 | 交易 |\n")
        f.write("|---|---:|---:|---:|---:|---:|---:|\n")
        for r in results:
            if r["ok"]:
                m = r["metrics"]
                f.write(f"| {r['formula']} "
                        f"| {m.get('cumret', 0) * 100:+.2f}% "
                        f"| {m.get('annret', 0) * 100:+.2f}% "
                        f"| {m.get('maxdd', 0) * 100:.2f}% "
                        f"| {m.get('sharpe', 0):.2f} "
                        f"| {m.get('winrate', 0) * 100:.1f}% "
                        f"| {m.get('trades', 0)} |\n")
            else:
                f.write(f"| {r['formula']} | ERR: {r.get('error', '')[:50]} | | | | | |\n")

    print(f"\n完成. 报告: {OUT_DIR}", flush=True)


if __name__ == "__main__":
    main()
