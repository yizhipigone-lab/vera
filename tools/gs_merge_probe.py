"""合并 6 分片信号探测结果 -> 有信号公式清单 (阶段A -> 阶段B 桥接). 2026-07-18"""
import json
import glob

IN_GLOB = "output/gs_filter/signals_probe_shard*of6.json"
OUT = "output/gs_filter/signals_probe_all.json"


def main():
    frames = [json.load(open(p, encoding="utf-8"))
              for p in sorted(glob.glob(IN_GLOB))]
    if not frames:
        print("[ERR] no shard files yet"); return
    has, zero, err = [], [], []
    for f in frames:
        has += f["has_signals"]; zero += f["zero_signals"]; err += f["errors"]
    all_res = {
        "n_shards": len(frames),
        "total_has": len(has), "total_zero": len(zero), "total_err": len(err),
        "has_signals_sorted": sorted(has, key=lambda x: -x["signals"]),
        "zero_signals": zero, "errors": err,
    }
    json.dump(all_res, open(OUT, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"[MERGE] shards={len(frames)} has={len(has)} zero={len(zero)} err={len(err)}")
    print(f"[OUT] {OUT}")
    print("top10 by signals:")
    for h in all_res["has_signals_sorted"][:10]:
        print(f"  {h['name']}  signals={h['signals']} stocks={h['stocks']}")


if __name__ == "__main__":
    main()
