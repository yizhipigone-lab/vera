"""Phase timing probe — NO production code changes, monkey-patch timers only.

Prints per-phase wall-clock for daily-line and 5m configs (cold + warm each),
so we can see where the wall-clock actually goes:
  select(step1) / fetch(network) / core-loop / matrix+postproc / benchmark / report

Usage: python tools/bench_phases.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from backtest.loop.loop import BacktestLoop
from core import data_fetcher
from core.connector import TdxConnector
from pipeline.pipeline import Pipeline

# ── shared timers ──
fetch_t = [0.0]; fetch_n = [0]; loop_t = [0.0]

# ── monkey-patch data fetching (network I/O) ──
_gk = data_fetcher.DataFetcher.get_kline
_gw = data_fetcher.DataFetcher.get_kline_windowed
def _pk(*a, **k):
    t = time.perf_counter(); r = _gk(*a, **k)
    fetch_t[0] += time.perf_counter() - t; fetch_n[0] += 1; return r
def _pw(*a, **k):
    t = time.perf_counter(); r = _gw(*a, **k)
    fetch_t[0] += time.perf_counter() - t; fetch_n[0] += 1; return r
data_fetcher.DataFetcher.get_kline = _pk
data_fetcher.DataFetcher.get_kline_windowed = _pw

# ── monkey-patch core loop ──
_lr = BacktestLoop.run
def _pl(*a, **k):
    t = time.perf_counter(); r = _lr(*a, **k)
    loop_t[0] += time.perf_counter() - t; return r
BacktestLoop.run = _pl


def run_once(yaml_path, label):
    fetch_t[0] = 0.0; fetch_n[0] = 0; loop_t[0] = 0.0
    print(f"\n{'='*64}\n[{label}]  cfg={yaml_path}\n{'='*64}", flush=True)
    pipe = Pipeline(yaml_path)
    T0 = time.perf_counter()
    t = time.perf_counter(); TdxConnector.initialize(); c = time.perf_counter() - t
    t = time.perf_counter(); sel = pipe.step1_select(); s = time.perf_counter() - t
    if sel is None or (hasattr(sel, 'empty') and sel.empty):
        print(f"  [!] selection EMPTY — pipeline stops. conn={c:.2f}s sel={s:.2f}s", flush=True)
        return None
    n_sel = len(sel)
    t = time.perf_counter(); bt = pipe.step2_backtest(sel); b = time.perf_counter() - t
    f = fetch_t[0]; lp = loop_t[0]
    t = time.perf_counter(); bm = pipe.step3_benchmark(bt); bm_t = time.perf_counter() - t
    t = time.perf_counter(); rp = pipe.step4_report(bt, bm); r = time.perf_counter() - t
    tot = time.perf_counter() - T0
    other = b - f - lp
    print("\n  ---- phase timing (seconds) ----", flush=True)
    print(f"  select(step1)      : {s:7.2f}   signals={n_sel}", flush=True)
    print(f"  backtest(step2)    : {b:7.2f}", flush=True)
    print(f"    fetch(network)   : {f:7.2f}   calls={fetch_n[0]}", flush=True)
    print(f"    core-loop        : {lp:7.2f}", flush=True)
    print(f"    matrix+postproc  : {other:7.2f}", flush=True)
    print(f"  benchmark(step3)   : {bm_t:7.2f}", flush=True)
    print(f"  report(step4)      : {r:7.2f}", flush=True)
    print(f"  TOTAL(inc conn)    : {tot:7.2f}", flush=True)
    sys.stdout.flush()
    return dict(sel=s, fetch=f, loop=lp, other=other, bench=bm_t, rep=r, total=tot, n_sel=n_sel)


if __name__ == "__main__":
    configs = [
        ("config/current.yaml", "DAILY-cold"),
        ("config/current.yaml", "DAILY-warm"),
        ("config/test_5m.yaml", "5M-cold"),
        ("config/test_5m.yaml", "5M-warm"),
    ]
    results = {}
    for path, label in configs:
        try:
            results[label] = run_once(path, label)
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"  [!] {label} FAILED: {e}", flush=True)
            results[label] = None
    try:
        TdxConnector.close()
    except Exception:
        pass
    print("\n\n==== SUMMARY (seconds) ====", flush=True)
    hdr = f"{'phase':<14}" + "".join(f"{lab:>13}" for lab in ['DAILY-cold','DAILY-warm','5M-cold','5M-warm'])
    print(hdr, flush=True)
    for k in ['sel', 'fetch', 'loop', 'other', 'bench', 'rep', 'total']:
        row = f"{k:<14}"
        for lab in ['DAILY-cold', 'DAILY-warm', '5M-cold', '5M-warm']:
            v = results.get(lab) or {}
            row += f"{v.get(k, 0):>13.2f}"
        print(row, flush=True)
    sys.stdout.flush()
