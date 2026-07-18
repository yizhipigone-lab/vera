"""
限售解禁利空因子注入 selections 冲烟测试(腿 B 注入机制 + 前视防护验证)

选这个因子的理由:
    - 逻辑单向(只剔除,不加分),冲烟最简
    - 前视防护典型:解禁公告日(ann_date) vs 解禁日(float_date)
    - 利空避雷,符合 VERA "中等回撤" 风险偏好

验证目标:
    1. 注入函数能读 selections + lockup_df,输出过滤后 selections
    2. 过滤逻辑:T 日后 horizon 天内解禁的股被剔除
    3. 前视防护:T 日选股只用 ann_date <= T 的解禁(不偷看 ann_date > T 的未来公告)

测试样本:
    (a) 真实数据:已公告(ann<=T)且 horizon 内解禁  → 应剔除
    (b) 真实数据:已公告但 horizon 外解禁           → 应保留
    (c) 合成数据:ann_date 人为设到 T 之后(未来公告)→ 应保留(前视防护核心验证)

数据特性(tushare share_float 实测):
    - ann_date 100% 提前于 float_date(中位数提前 183 天)
    - 故真实数据里"未公告就解禁"几乎不存在,(c) 类需合成才能覆盖前视防护反例

用法:
    python tools/smoke_lockup_filter.py
"""
import os
import sys
import json
from pathlib import Path

import pandas as pd

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def load_token() -> str:
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if token:
        return token
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("TUSHARE_TOKEN"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def filter_by_lockup(selections: pd.DataFrame, lockup: pd.DataFrame,
                     horizon_days: int = 5):
    """
    从 selections 剔除:select_date 后 horizon_days 天内、且 select_date 时已公告的解禁股。

    前视防护:只用 ann_date <= select_date 的解禁记录(不偷看未来公告)。

    Args:
        selections: DataFrame[stock_code, select_date] (VERA 格式)
        lockup:     DataFrame[ts_code, ann_date, float_date, ...]
        horizon_days: 解禁危险窗口(选股日后 N 天内解禁则剔除)

    Returns:
        (kept_df, removed_df) — 列同 selections
    """
    lk = lockup.copy()
    lk["ann_date"] = pd.to_datetime(lk["ann_date"], format="%Y%m%d", errors="coerce")
    lk["float_date"] = pd.to_datetime(lk["float_date"], format="%Y%m%d", errors="coerce")
    lk = lk.dropna(subset=["ann_date", "float_date"])

    sel = selections.copy()
    sel["select_date"] = pd.to_datetime(sel["select_date"], format="%Y%m%d", errors="coerce")
    sel["horizon_end"] = sel["select_date"] + pd.Timedelta(days=horizon_days)

    removed_mask = pd.Series(False, index=sel.index)
    # 注:iterrows 用于冲烟(数据量小)。生产应向量化 join。
    for idx, row in sel.iterrows():
        code = row["stock_code"]
        sd = row["select_date"]
        he = row["horizon_end"]
        # 已公告(ann_date <= sd) 且 解禁日在 (sd, he]
        hits = lk[(lk["ts_code"] == code)
                  & (lk["ann_date"] <= sd)
                  & (lk["float_date"] > sd)
                  & (lk["float_date"] <= he)]
        if len(hits) > 0:
            removed_mask[idx] = True

    removed = sel[removed_mask].drop(columns=["horizon_end"])
    kept = sel[~removed_mask].drop(columns=["horizon_end"])
    return kept, removed


def main() -> None:
    token = load_token()
    if not token:
        print("[FAIL] 未找到 TUSHARE_TOKEN")
        sys.exit(1)
    import tushare as ts
    pro = ts.pro_api(token)

    SELECT_DATE = "20240208"   # 让 2024-03 解禁落在 30 天 horizon 内
    HORIZON = 30
    sd = pd.Timestamp("2024-02-08")
    he = sd + pd.Timedelta(days=HORIZON)

    # 1. 拉真实解禁数据
    print("=== 1. 拉限售解禁数据 ===")
    lockup = pro.share_float(start_date="20240101", end_date="20240331")
    print(f"真实解禁记录: {len(lockup)} 条")

    lk = lockup.copy()
    lk["ann_dt"] = pd.to_datetime(lk["ann_date"], format="%Y%m%d", errors="coerce")
    lk["float_dt"] = pd.to_datetime(lk["float_date"], format="%Y%m%d", errors="coerce")

    # 2. 真实样本池
    a_pool = lk[(lk["ann_dt"] <= sd) & (lk["float_dt"] > sd) & (lk["float_dt"] <= he)]
    b_pool = lk[lk["float_dt"] > he]
    print(f"\n=== 2. 样本池(select_date={SELECT_DATE}, horizon={HORIZON}天, 到 {he.date()})===")
    print(f"(a) 真实 已公告且 horizon 内解禁: {a_pool['ts_code'].nunique()} 只 → 应剔除")
    print(f"(b) 真实 horizon 外解禁:         {b_pool['ts_code'].nunique()} 只 → 应保留")

    # 3. 构造测试 selections + 合成 (a)/(c) 反例
    #    真实 a_pool=0(float_date 都在 horizon 外),故 (a) 用合成保证剔除分支被触发
    b_all = b_pool["ts_code"].drop_duplicates().tolist()
    a_codes = b_all[0:3]   # 合成 (a):已公告 + horizon 内 → 应剔除
    c_codes = b_all[3:6]   # 合成 (c):未公告 + horizon 内 → 应保留(前视防护)
    b_codes = b_all[6:9]   # 真实 (b):horizon 外 → 应保留

    samples = []
    synth_rows = []
    for code in a_codes:
        samples.append({"stock_code": code, "select_date": SELECT_DATE, "类别": "(a)应剔除-合成horizon内"})
        synth_rows.append({
            "ts_code": code,
            "ann_date": (sd - pd.Timedelta(days=100)).strftime("%Y%m%d"),  # T 之前公告
            "float_date": (sd + pd.Timedelta(days=15)).strftime("%Y%m%d"), # horizon 内解禁
            "share_type": "合成-应剔除",
        })
    for code in b_codes:
        samples.append({"stock_code": code, "select_date": SELECT_DATE, "类别": "(b)应保留-horizon外"})
    for code in c_codes:
        samples.append({"stock_code": code, "select_date": SELECT_DATE, "类别": "(c)应保留-合成未来公告"})
        synth_rows.append({
            "ts_code": code,
            "ann_date": (sd + pd.Timedelta(days=10)).strftime("%Y%m%d"),   # 未来公告(T 之后)
            "float_date": (sd + pd.Timedelta(days=15)).strftime("%Y%m%d"), # horizon 内解禁
            "share_type": "合成-前视防护",
        })
    selections = pd.DataFrame(samples)
    print(f"(a) 合成 已公告+horizon内: {len(a_codes)} 只 → 应剔除")
    print(f"(b) 真实 horizon 外:      {len(b_codes)} 只 → 应保留")
    print(f"(c) 合成 未公告+horizon内: {len(c_codes)} 只 → 应保留(前视防护)")

    # 合并真实 + 合成 lockup
    lockup_full = pd.concat([lockup, pd.DataFrame(synth_rows)], ignore_index=True)
    print(f"\n测试 selections: {len(selections)} 只")
    print(selections.to_string(index=False))

    # 4. 应用过滤
    print(f"\n=== 3. 应用过滤(真实+合成 lockup,{len(lockup_full)} 条)===")
    kept, removed = filter_by_lockup(
        selections[["stock_code", "select_date"]], lockup_full, HORIZON)
    print(f"保留: {len(kept)} 只,剔除: {len(removed)} 只")
    if len(removed) > 0:
        print("\n剔除清单:")
        print(removed.to_string(index=False))

    # 5. 验证三类
    print(f"\n=== 4. 验证 ===")
    removed_set = set(removed["stock_code"])

    a_removed = [c for c in a_codes if c in removed_set]
    a_pass = len(a_removed) == len(a_codes)
    print(f"[{'PASS' if a_pass else 'FAIL'}] (a) 合成应剔除 {len(a_codes)} 只 → "
          f"实际剔除 {len(a_removed)}/{len(a_codes)}")

    b_removed = [c for c in b_codes if c in removed_set]
    b_pass = len(b_removed) == 0
    print(f"[{'PASS' if b_pass else 'FAIL'}] (b) horizon 外 {len(b_codes)} 只 → "
          f"{'全保留' if b_pass else f'误剔除 {b_removed}'}")

    c_removed = [c for c in c_codes if c in removed_set]
    c_pass = len(c_removed) == 0
    print(f"[{'PASS' if c_pass else 'FAIL'}] (c) 合成未来公告 {len(c_codes)} 只 → "
          f"{'全保留(前视防护生效,没偷看 ann_date>T)' if c_pass else f'误剔除 {c_removed}(偷看了未来公告!)'}")

    all_pass = a_pass and b_pass and c_pass
    result = {
        "select_date": SELECT_DATE, "horizon_days": HORIZON,
        "lockup_records_real": int(len(lockup)),
        "lockup_records_with_synth": int(len(lockup_full)),
        "test_selections": int(len(selections)),
        "kept": int(len(kept)), "removed": int(len(removed)),
        "filter_logic_pass": bool(a_pass),
        "front_look_protection_pass": bool(c_pass),
        "all_pass": bool(all_pass),
    }
    print(f"\n{json.dumps(result, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
