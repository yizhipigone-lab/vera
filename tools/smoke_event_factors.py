"""
事件因子冲烟测试:综合评分模型(资金流 + 龙虎榜 + 大宗)

设计:不过滤,三个因子统一打分,按总分排序选股
    - 资金流 mf_score: 按主力净额(net_mf_amount)分档 +2/+1/-1/-2
    - 龙虎榜 dragon_score: 机构席位汇总净买入 +1 / 净卖出 -1 / 无 0
    - 大宗 block_score: 买方机构 +1 / 卖方机构 -1 / 无 0
    - total = mf_score + dragon_score + block_score

前视防护(关键):三个都是 T 日盘后数据 → T 日收盘选股时还没出
    → 只用 trade_date < select_date 的数据

用法:python tools/smoke_event_factors.py
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


SELECT_DATE = "20240108"
SD = pd.Timestamp("2024-01-08")


def mf_to_score(amt) -> int:
    """主力净额 → 分档分数"""
    if amt is None or pd.isna(amt):
        return 0
    if amt > 10000:
        return 2
    if amt > 0:
        return 1
    if amt > -10000:
        return -1
    return -2


def add_mf_score(selections: pd.DataFrame, mf: pd.DataFrame) -> pd.DataFrame:
    """资金流评分。前视:只用 trade_date < select_date"""
    mf = mf.copy()
    mf["trade_date"] = pd.to_datetime(mf["trade_date"], format="%Y%m%d")
    mf_valid = mf[mf["trade_date"] < SD]
    latest = mf_valid.sort_values("trade_date").groupby("ts_code").tail(1)
    net_map = dict(zip(latest["ts_code"], latest["net_mf_amount"]))
    sel = selections.copy()
    sel["net_mf_amount"] = sel["stock_code"].map(net_map)
    sel["mf_score"] = sel["net_mf_amount"].apply(mf_to_score)
    return sel


def add_dragon_score(selections: pd.DataFrame, top_inst: pd.DataFrame) -> pd.DataFrame:
    """龙虎榜评分:机构席位汇总净买入 +1 / 净卖出 -1。前视:trade_date < select_date"""
    ti = top_inst.copy()
    ti["trade_date"] = pd.to_datetime(ti["trade_date"], format="%Y%m%d")
    ti_valid = ti[ti["trade_date"] < SD]
    inst = ti_valid[ti_valid["exalter"].str.contains("机构", na=False)]
    # 每只股所有机构席位的净买入汇总
    net_by_code = inst.groupby("ts_code")["net_buy"].sum()

    def score(c):
        if c in net_by_code.index:
            return 1 if net_by_code[c] > 0 else -1
        return 0
    sel = selections.copy()
    sel["dragon_score"] = sel["stock_code"].apply(score)
    return sel


def add_block_score(selections: pd.DataFrame, block_trade: pd.DataFrame) -> pd.DataFrame:
    """大宗评分:买方机构笔数 - 卖方机构笔数,正 +1 / 负 -1。前视:trade_date < select_date"""
    bt = block_trade.copy()
    bt["trade_date"] = pd.to_datetime(bt["trade_date"], format="%Y%m%d")
    bt_valid = bt[bt["trade_date"] < SD]
    buy_inst = bt_valid[bt_valid["buyer"].str.contains("机构", na=False)]
    sell_inst = bt_valid[bt_valid["seller"].str.contains("机构", na=False)]
    buy_cnt = buy_inst.groupby("ts_code").size()
    sell_cnt = sell_inst.groupby("ts_code").size()

    def score(c):
        b = buy_cnt.get(c, 0)
        s = sell_cnt.get(c, 0)
        diff = b - s
        if diff > 0:
            return 1
        if diff < 0:
            return -1
        return 0
    sel = selections.copy()
    sel["block_score"] = sel["stock_code"].apply(score)
    return sel


def main() -> None:
    token = load_token()
    if not token:
        print("[FAIL] 未找到 TUSHARE_TOKEN")
        sys.exit(1)
    import tushare as ts
    pro = ts.pro_api(token)

    print(f"[INFO] select_date={SELECT_DATE}(T),前视只用 trade_date < {SD.date()}")
    print("=== 拉三因子数据(trade_date=20240105, T-1 周五盘后)===")
    mf = pro.moneyflow(trade_date="20240105")
    ti = pro.top_inst(trade_date="20240105")
    bt = pro.block_trade(trade_date="20240105")
    print(f"资金流 {len(mf)} 条 | 龙虎榜机构明细 {len(ti)} 条 | 大宗 {len(bt)} 条")

    # 构造测试 selections
    print("\n=== 构造测试 selections ===")
    samples = []
    for _, r in mf.nsmallest(3, "net_mf_amount").iterrows():
        samples.append({"stock_code": r["ts_code"], "select_date": SELECT_DATE, "类别": "资金流大净流出"})
    for _, r in mf.nlargest(3, "net_mf_amount").iterrows():
        samples.append({"stock_code": r["ts_code"], "select_date": SELECT_DATE, "类别": "资金流大净流入"})
    inst_buy = ti[(ti["exalter"].str.contains("机构", na=False)) & (ti["net_buy"] > 0)]
    for code in inst_buy["ts_code"].drop_duplicates().head(3):
        samples.append({"stock_code": code, "select_date": SELECT_DATE, "类别": "龙虎榜机构净买入"})
    for code in bt[bt["buyer"].str.contains("机构", na=False)]["ts_code"].drop_duplicates().head(3):
        samples.append({"stock_code": code, "select_date": SELECT_DATE, "类别": "大宗买方机构"})
    selections = pd.DataFrame(samples).drop_duplicates(subset=["stock_code"]).reset_index(drop=True)
    print(f"测试 selections: {len(selections)} 只")

    # 综合评分
    print("\n=== 综合评分 ===")
    scored = add_mf_score(selections, mf)
    scored = add_dragon_score(scored, ti)
    scored = add_block_score(scored, bt)
    scored["total_score"] = scored["mf_score"] + scored["dragon_score"] + scored["block_score"]
    scored = scored.sort_values("total_score", ascending=False).reset_index(drop=True)
    show_cols = ["stock_code", "类别", "net_mf_amount", "mf_score", "dragon_score", "block_score", "total_score"]
    print(scored[show_cols].to_string(index=False))

    # 验证:矛盾信号股(龙虎榜机构买+资金流小出)应得中性分,不被粗暴剔除
    print("\n=== 关键验证:矛盾信号股的综合处理 ===")
    # 找既上榜(机构买)又资金流出的股
    codes_inst = set(inst_buy["ts_code"].unique())
    conflict = scored[(scored["stock_code"].isin(codes_inst)) & (scored["mf_score"] < 0)]
    if len(conflict) > 0:
        print(f"矛盾股(机构买 + 资金流净流出): {len(conflict)} 只 — 综合评分下未被直接剔除:")
        print(conflict[show_cols].to_string(index=False))
        print("[OK] 综合评分保留了矛盾股(机构买入的对冲了资金流出),不会被粗暴剔除")
    else:
        print("(本次无矛盾股样本)")

    # 前视防护验证
    print("\n=== 前视防护验证 ===")
    test_code = mf.nlargest(1, "net_mf_amount")["ts_code"].iloc[0]   # 净流入最大的股
    synth_mf = pd.DataFrame([{"ts_code": test_code, "trade_date": SELECT_DATE, "net_mf_amount": -99999}])
    mf_with_synth = pd.concat([mf, synth_mf], ignore_index=True)
    scored_synth = add_mf_score(
        pd.DataFrame([{"stock_code": test_code, "select_date": SELECT_DATE}]), mf_with_synth)
    fl_pass = scored_synth["mf_score"].iloc[0] == 2   # 应仍用真实 T-1 数据(+2),不是合成当天(-2)
    print(f"[{'PASS' if fl_pass else 'FAIL'}] {test_code} 合成 trade_date={SELECT_DATE} 净流出 → "
          f"mf_score={scored_synth['mf_score'].iloc[0]}(应=2,用 T-1 真实净流入)→ "
          f"{'前视防护生效' if fl_pass else '偷看了当天数据!'}")

    result = {
        "select_date": SELECT_DATE,
        "test_selections": int(len(selections)),
        "scoring_model": "mf分档+龙虎榜机构+大宗机构",
        "top_score": int(scored["total_score"].iloc[0]),
        "bottom_score": int(scored["total_score"].iloc[-1]),
        "conflict_stocks_kept": int(len(conflict)),
        "front_look_pass": bool(fl_pass),
    }
    print(f"\n{json.dumps(result, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
