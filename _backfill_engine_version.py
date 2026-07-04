"""一次性脚本: 给所有旧回测结果回填 engine_version 标记, 区分旧 (t+1-open) 和新 (signal-day-close).

F2 (审计发现): 历史结果无版本标记会被前端误用为新结果.
执行: python _backfill_engine_version.py [--dry-run]
"""
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_RESULTS = _ROOT / "output" / "results"

OLD_VERSION = "t+1-open"
OLD_BASIS = "open_on_t1_next_day"


def backfill_one(path: Path, dry_run: bool) -> str:
    """返回 backfill 状态: 'new' / 'old->marked' / 'already-marked' / 'skipped'"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return f"error:{e}"

    if not isinstance(data, dict):
        return "skipped"

    ev = data.get("engine_version")
    if ev in ("signal-day-close", "t+1-open"):
        return "already-marked"

    # 未标记 → 视为旧 (T+1 开盘)
    if not dry_run:
        data["engine_version"] = OLD_VERSION
        data["entry_price_basis"] = OLD_BASIS
        if "meta" in data and isinstance(data["meta"], dict):
            data["meta"]["engine_version"] = OLD_VERSION
            data["meta"]["entry_price_basis"] = OLD_BASIS
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, default=str)
    return "old->marked"


def main():
    dry_run = "--dry-run" in sys.argv
    if not _RESULTS.exists():
        print(f"无结果目录: {_RESULTS}")
        return

    counts = {"new": 0, "old->marked": 0, "already-marked": 0, "skipped": 0, "error": 0}

    # 单独回填 last_result.json (顶层不是 {meta: ..., data: ...} 结构)
    last_path = _ROOT / "output" / "last_result.json"
    if last_path.exists():
        try:
            with open(last_path, encoding="utf-8") as f:
                last = json.load(f)
            if isinstance(last, dict) and "engine_version" not in last:
                if not dry_run:
                    last["engine_version"] = OLD_VERSION
                    last["entry_price_basis"] = OLD_BASIS
                    with open(last_path, "w", encoding="utf-8") as f:
                        json.dump(last, f, ensure_ascii=False, default=str)
                counts["old->marked"] += 1
        except Exception as e:
            counts["error"] += 1
            print(f"[ERROR] {last_path}: {e}")

    # 遍历 results/*.json
    for p in sorted(_RESULTS.glob("*.json")):
        if p.name == "index.json":
            continue
        status = backfill_one(p, dry_run)
        counts[status] = counts.get(status, 0) + 1

    # index.json 也加 version 给最后 50 条
    idx_path = _RESULTS / "index.json"
    if idx_path.exists():
        try:
            with open(idx_path, encoding="utf-8") as f:
                idx = json.load(f)
            changed = 0
            for item in idx if isinstance(idx, list) else []:
                if "engine_version" not in item:
                    if not dry_run:
                        item["engine_version"] = OLD_VERSION
                        item["entry_price_basis"] = OLD_BASIS
                    changed += 1
            if changed and not dry_run:
                with open(idx_path, "w", encoding="utf-8") as f:
                    json.dump(idx, f, ensure_ascii=False)
            counts["old->marked"] += changed
        except Exception as e:
            print(f"[ERROR] {idx_path}: {e}")

    print(f"Dry-run: {dry_run}")
    for k, v in counts.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
