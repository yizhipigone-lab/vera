"""
三因子评分模块(生产级,从冲烟重构)

per-row select_date:每个信号用自己的信号日判断前视(trade_date < select_date),
替代冲烟的全局 SD 常量(审计 M1)。

评分规则(与冲烟一致):
    - mf_score:     主力净额 net_mf_amount 分档 +2 / +1 / -1 / -2
    - dragon_score: 龙虎榜机构席位汇总净买 +1 / 净卖 -1 / 无 0
    - block_score:  大宗买方机构笔数 - 卖方机构笔数,+1 / -1 / 0
    - total_score:  三者之和,范围 [-4, +4]

前视防护:只用 trade_date < select_date 的因子数据(T-1 及更早)。
"""
import pandas as pd


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


def score_selections(selections: pd.DataFrame,
                     mf: pd.DataFrame,
                     top_inst: pd.DataFrame,
                     block_trade: pd.DataFrame) -> pd.DataFrame:
    """
    给 selections 每行算三因子评分(per-row select_date)。

    Args:
        selections: DataFrame,必须含 stock_code, select_date
        mf / top_inst / block_trade: 历史因子数据,必须含 trade_date(ts_code)
    Returns:
        selections 加 mf_score / dragon_score / block_score / total_score 列
    """
    sel = selections.copy()
    sel["select_date"] = pd.to_datetime(sel["select_date"], errors="coerce")

    mf = mf.copy()
    mf["trade_date"] = pd.to_datetime(mf["trade_date"], errors="coerce")
    ti = top_inst.copy()
    ti["trade_date"] = pd.to_datetime(ti["trade_date"], errors="coerce")
    bt = block_trade.copy()
    bt["trade_date"] = pd.to_datetime(bt["trade_date"], errors="coerce")

    # 预排序,加速 tail(1)
    mf_sorted = mf.sort_values(["ts_code", "trade_date"])
    ti_valid = ti[ti["exalter"].str.contains("机构", na=False)]

    mf_scores, dragon_scores, block_scores = [], [], []
    for _, row in sel.iterrows():
        code = row["stock_code"]
        sd = row["select_date"]

        # mf: 该股 trade_date < sd 的最近一条净额
        mf_v = mf_sorted[(mf_sorted["ts_code"] == code) & (mf_sorted["trade_date"] < sd)]
        mf_amt = mf_v["net_mf_amount"].iloc[-1] if len(mf_v) > 0 else None
        mf_scores.append(mf_to_score(mf_amt))

        # dragon: 该股 trade_date<sd 的机构席位 net_buy 汇总
        inst = ti_valid[(ti_valid["ts_code"] == code) & (ti_valid["trade_date"] < sd)]
        if len(inst) > 0:
            net = inst["net_buy"].sum()
            dragon_scores.append(1 if net > 0 else -1)
        else:
            dragon_scores.append(0)

        # block: 该股 trade_date<sd 的买方机构 - 卖方机构
        bv = bt[(bt["ts_code"] == code) & (bt["trade_date"] < sd)]
        buy_n = bv["buyer"].str.contains("机构", na=False).sum() if "buyer" in bv.columns else 0
        sell_n = bv["seller"].str.contains("机构", na=False).sum() if "seller" in bv.columns else 0
        diff = buy_n - sell_n
        block_scores.append(1 if diff > 0 else (-1 if diff < 0 else 0))

    sel["mf_score"] = mf_scores
    sel["dragon_score"] = dragon_scores
    sel["block_score"] = block_scores
    sel["total_score"] = sel["mf_score"] + sel["dragon_score"] + sel["block_score"]
    return sel
