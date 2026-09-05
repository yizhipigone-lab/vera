"""research/_diag_timing.py — 分阶段计时诊断 (临时脚本, 用完删)"""
import sys, time
from pathlib import Path
import yaml
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from research.run_quantqq_2014_5m_trailing import build_stop_config

START = sys.argv[1] if len(sys.argv) > 1 else "20240102"
END = sys.argv[2] if len(sys.argv) > 2 else "20240110"

def main():
    from pipeline.pipeline import Pipeline
    cfg = yaml.safe_load(open("config/strategy_QUANTQQ.yaml", encoding="utf-8"))
    cfg["time_range"]["start"] = START
    cfg["time_range"]["end"] = END
    cfg["backtest"]["period"] = "5m"
    cfg["backtest"]["entry_price_mode"] = "close_t"
    cfg["backtest"]["degrade_5m"] = True
    cfg["stop_loss"] = build_stop_config()
    tmp = Path("output/quantqq_2014_5m_trailing/_tmp_diag.yaml")
    tmp.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")

    pipe = Pipeline(str(tmp))
    t0 = time.time()
    print(f"[diag] step1_select 开始 {START}~{END}", flush=True)
    sel = pipe.step1_select()
    t1 = time.time()
    print(f"[diag] step1_select 完成: {len(sel)} 信号, 耗时 {t1-t0:.1f}s", flush=True)
    print(f"[diag] 选股样本:\n{sel.head(10).to_string()}", flush=True)
    t2 = time.time()
    print(f"[diag] step2_backtest 开始", flush=True)
    bt = pipe.step2_backtest(sel)
    t3 = time.time()
    print(f"[diag] step2_backtest 完成: 交易 {bt.get('metrics',{}).get('total_trades')} 笔, 耗时 {t3-t2:.1f}s", flush=True)
    print(f"[diag] === 总耗时 {t3-t0:.1f}s ===", flush=True)

if __name__ == "__main__":
    main()
