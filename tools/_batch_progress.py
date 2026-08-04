import json
from collections import Counter

with open("output/gs_402_batch/results.jsonl", encoding="utf-8") as f:
    results = [json.loads(line) for line in f if line.strip()]

statuses = Counter(r["status"] for r in results)
print(f"已完成: {len(results)}/402")
print()
print("状态分布:")
for s, c in statuses.most_common():
    print(f"  {s}: {c}")

ok = [r for r in results if r.get("status") == "ok"]
no_sig = [r for r in results if r.get("status") == "no_signals"]
errors = [r for r in results if r.get("status") == "error"]
too_many = [r for r in results if r.get("status") == "too_many_signals"]

if ok:
    annrets = [r.get("annret", 0) or 0 for r in ok]
    print(f"\n有交易 {len(ok)} 个:")
    print(f"  年化收益均值: {sum(annrets)/len(annrets):.2%}")
    print(f"  年化收益中位数: {sorted(annrets)[len(annrets)//2]:.2%}")
    top5 = sorted(ok, key=lambda r: r.get("annret", 0) or 0, reverse=True)[:5]
    print("  Top5:")
    for r in top5:
        print(f"    {r['formula']} 年化{r.get('annret',0):.2%} 回撤{r.get('maxdd',0):.2%} 夏普{r.get('sharpe',0):.2f}")
    # count thresholds
    print(f"  年化>10%: {sum(1 for v in annrets if v > 0.10)}")
    print(f"  年化>20%: {sum(1 for v in annrets if v > 0.20)}")
    print(f"  夏普>1.0: {sum(1 for r in ok if (r.get('sharpe',0) or 0) > 1.0)}")

if errors:
    err_msgs = Counter(r.get("msg", "")[:80] for r in errors)
    print(f"\n错误 {len(errors)} 个:")
    for m, c in err_msgs.most_common(5):
        print(f"  [{c}] {m}")

if no_sig:
    print(f"\n无信号: {len(no_sig)}")

if too_many:
    print(f"\n信号过多: {len(too_many)}")
