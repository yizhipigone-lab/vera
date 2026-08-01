"""批次 3a · A5 快照基线生成器 (2026-08-01)。

重跑 tests/test_snapshot_parity.py 里的全部场景, 覆写 tests/snapshots/*.json。
测试只读不写; 只有确认行为变更合法后才运行本脚本重建基线。

用法:
    python -X utf8 tools/gen_snapshot_parity.py           # 重新生成全部快照
    python -X utf8 tools/gen_snapshot_parity.py --check   # 只校验不写 (等价跑测试对比)
"""

from __future__ import annotations

import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_snapshot_parity import (  # noqa: E402
    SCENARIOS,
    SNAPSHOT_DIR,
    build_snapshot_doc,
    equity_to_json,
    trades_to_json,
)


def main():
    check_only = "--check" in sys.argv
    generated_at = datetime.datetime.now().isoformat(timespec="seconds")
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    n_written = 0
    n_diff = 0
    for name in SCENARIOS:
        path = os.path.join(SNAPSHOT_DIR, f"{name}.json")
        if check_only:
            eq, tr, _ = SCENARIOS[name]()
            with open(path, "r", encoding="utf-8") as f:
                snap = json.load(f)
            same = (equity_to_json(eq) == snap["equity"]
                    and trades_to_json(tr) == snap["trades"])
            print(f"[{'OK' if same else 'DIFF'}] {name}")
            n_diff += 0 if same else 1
            continue
        doc = build_snapshot_doc(name, generated_at)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
            f.write("\n")
        print(f"[WRITE] {path}  (equity={len(doc['equity'])}, trades={len(doc['trades'])})")
        n_written += 1
    if check_only:
        print(f"--check 完成: {len(SCENARIOS) - n_diff}/{len(SCENARIOS)} 一致")
        sys.exit(1 if n_diff else 0)
    print(f"生成完成: {n_written} 个快照 → {SNAPSHOT_DIR}")


if __name__ == "__main__":
    main()
