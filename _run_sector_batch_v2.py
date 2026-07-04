"""
128 行业板块批量回测 — 后台版, 每完成一板块实时落盘
公式: QUANTQQ, 时间: 2026.1.1 ~ 2026.7.4
输出:
  output/_sector_batch_progress.jsonl  每板块一行 (实时)
  output/_sector_batch_ranking.json    最终排名 (完成后)
"""
import sys, os, json, time, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np
from datetime import datetime

from core.connector import TdxConnector
from core.data_fetcher import DataFetcher
from core.formula_runner import FormulaRunner
from backtest.engine import BacktestEngine
from utils.config_loader import ConfigLoader

START, END = "20260101", "20260704"
FORMULA_NAME, FORMULA_ARG = "QUANTQQ", ""

PROGRESS_PATH = "output/_sector_batch_progress.jsonl"
RANKING_PATH = "output/_sector_batch_ranking.json"

defaults = ConfigLoader.load_defaults()
STOP_CONFIG = defaults.get("stop_loss", {})
BT_CONFIG = defaults.get("backtest", {})

t_start_total = time.time()

# 初始化 TDX
TdxConnector.initialize()
sectors = DataFetcher.get_sector_list()
total = len(sectors)
print(f"[BATCH-START] sectors={total}, formula={FORMULA_NAME}, range={START}~{END}", flush=True)

# 清空 progress 文件
with open(PROGRESS_PATH, "w", encoding="utf-8") as f:
    pass

done_count = 0
for idx, sec in enumerate(sectors):
    code, name = sec["code"], sec["name"]
    t0 = time.time()
    record = {
        "idx": idx + 1, "total": total,
        "code": code, "name": name,
        "status": "running", "ts": datetime.now().strftime("%H:%M:%S"),
    }
    try:
        members = DataFetcher.get_sector_stocks(code)
        if not members:
            record.update({"status": "empty_members", "elapsed_s": round(time.time()-t0, 1)})
            with open(PROGRESS_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            done_count += 1
            continue

        picks = FormulaRunner.run_stock_selection_with_dates(
            formula_name=FORMULA_NAME, formula_arg=FORMULA_ARG,
            stock_list=members, start_time=START, end_time=END,
            stock_period="1d",
        )
        if picks.empty:
            record.update({
                "status": "no_signals", "members": len(members),
                "elapsed_s": round(time.time()-t0, 1),
            })
            with open(PROGRESS_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            done_count += 1
            continue

        engine = BacktestEngine(BT_CONFIG)
        bt = engine.run(selections=picks, start_time=START, end_time=END, stop_config=STOP_CONFIG)
        metrics = bt.get("metrics", {})
        record.update({
            "status": "ok",
            "members": len(members),
            "signals": len(picks),
            # 修复 trades 读取: engine 返回键是 "metrics" 内的 "total_trades", 不是顶层 "trade_count"
            "trades": int(metrics.get("total_trades", 0)),
            "cumulative_return": round(metrics.get("cumulative_return", 0), 4),
            "annualized_return": round(metrics.get("annualized_return", 0), 4),
            "max_drawdown": round(metrics.get("max_drawdown", 0), 4),
            "sharpe_ratio": round(metrics.get("sharpe_ratio", 0), 4),
            "win_rate": round(metrics.get("win_rate", 0), 4),
            "elapsed_s": round(time.time()-t0, 1),
        })
    except Exception as e:
        record.update({
            "status": "error", "error": str(e)[:200],
            "traceback": traceback.format_exc()[-400:],
            "elapsed_s": round(time.time()-t0, 1),
        })

    # 落盘这一条
    with open(PROGRESS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    done_count += 1

    # 进度日志
    cumret = record.get("cumulative_return", "n/a")
    print(f"[{done_count}/{total}] {name} {code} signals={record.get('signals', 'n/a')} cumret={cumret} {record['elapsed_s']}s", flush=True)

TdxConnector.close()

# 最终排名
results = []
with open(PROGRESS_PATH, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        results.append(json.loads(line))

ok_results = [r for r in results if r.get("status") == "ok"]
ok_results.sort(key=lambda x: x.get("cumulative_return", 0), reverse=True)

with open(RANKING_PATH, "w", encoding="utf-8") as f:
    json.dump({
        "total": total, "completed": done_count, "elapsed_total_s": round(time.time()-t_start_total, 1),
        "formula": FORMULA_NAME, "range": f"{START}~{END}",
        "ranking": ok_results,
        "all_results": results,
    }, f, ensure_ascii=False, indent=2)

print(f"\n[BATCH-DONE] {done_count}/{total} 总耗时 {time.time()-t_start_total:.1f}s", flush=True)
print(f"[RANK] ranking file: {RANKING_PATH}")
