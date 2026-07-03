"""
gs_txt 重跑 error 公式 (先生 2026-07-03)

读取 output/gs_rerun_list.json, 串行重跑, 150s 超时.
"""
import sys, os, re, json, time, subprocess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

START = '20260101'
END = '20261231'
TIMEOUT = 150
SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_gs_run_one.py')

def run_one(formula):
    try:
        r = subprocess.run(
            [sys.executable, '-X', 'utf8', SCRIPT, formula, START, END],
            capture_output=True, text=True, timeout=TIMEOUT, encoding='utf-8',
        )
    except subprocess.TimeoutExpired:
        return {'status': 'selection_timeout'}
    out = r.stdout if r.stdout else ''
    for line in out.split('\n'):
        line = line.strip()
        if line.startswith('{') and '"status"' in line:
            try: return json.loads(line)
            except: pass
    return {'status': 'error', 'msg': 'no json'}

def main():
    with open('output/gs_rerun_list.json', encoding='utf-8') as f:
        items = json.load(f)
    n = len(items)
    print(f'重跑 {n} 个 error 公式')
    t0 = time.time()
    results = []
    ok = 0
    for i, item in enumerate(items, 1):
        fn, fm, hd = item['file'], item['formula'], item['header']
        elapsed = time.time() - t0
        speed = i / max(elapsed, 1) * 60
        eta = (n - i) / max(speed, 0.01)
        print(f'[{i}/{n}] {fm} ({hd}) ~{speed:.1f}/min ETA {eta:.0f}min', flush=True)
        r = run_one(fm)
        r['file'] = fn; r['formula'] = fm; r['header'] = hd
        results.append(r)
        if r['status'] == 'ok':
            ok += 1
            print(f'  OK {r["signals"]}sig {r["stocks"]}stk {r["trades"]}trd {r["cumret"]*100:+.2f}% ann={r["annret"]*100:+.2f}% dd={r["maxdd"]*100:.2f}% wr={r["winrate"]*100:.1f}%', flush=True)
        elif r['status'] == 'too_many_signals':
            print(f'  TOOMANY {r["signals"]}', flush=True)
        else:
            print(f'  {r["status"]}', flush=True)
        # 每 10 存一次
        if i % 10 == 0:
            with open('output/gs_rerun_results.json', 'w', encoding='utf-8') as f2:
                json.dump(results, f2, ensure_ascii=False, indent=1, default=str)
            with open('output/gs_rerun_progress.log', 'w', encoding='utf-8') as f2:
                for rr in results:
                    if rr['status'] == 'ok':
                        f2.write(f'{rr["formula"]},{rr["header"]},ok,{rr["signals"]},{rr["stocks"]},{rr["trades"]},{rr["cumret"]:.4f},{rr["annret"]:.4f},{rr["maxdd"]:.4f},{rr["sharpe"]:.2f},{rr["winrate"]:.4f}\n')
                    else:
                        f2.write(f'{rr["formula"]},{rr["header"]},{rr["status"]}\n')
    total = time.time() - t0
    print(f'\n完成: OK={ok}/{n} 用时 {total:.0f}s ({total/60:.1f}min)')
    with open('output/gs_rerun_results.json', 'w', encoding='utf-8') as f2:
        json.dump(results, f2, ensure_ascii=False, indent=1, default=str)

if __name__ == '__main__':
    main()
