"""第三轮 — 放大仓位(100K)、放宽止损、修复vol_ratio bug。"""
import sys, os, itertools
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
import numpy as np
from core.connector import TdxConnector
from utils.logger import get_logger
from backtest.engine import _simulate_core_v3

logger = get_logger(__name__)

def _ma(df, w):
    return df.rolling(w, min_periods=w).mean()

def align_all(*dfs):
    cols = dfs[0].columns; idx = dfs[0].index
    for d in dfs[1:]:
        cols = cols.intersection(d.columns)
        idx = idx.intersection(d.index)
    return tuple(d.loc[idx, cols] for d in dfs)

def strategy_vol_price_fixed(close, open_df, high, low, vol, vol_ratio=0.8):
    """量价共振: 连续N天量比>=vol_ratio且创新高."""
    close, open_df, high, low, vol = align_all(close, open_df, high, low, vol)
    vr = vol / _ma(vol, 5).shift(1).replace(0, np.nan)
    # vol_ratio 作为量比阈值
    cond = vr > vol_ratio
    cond &= vr >= vr.rolling(10, min_periods=1).max().shift(1)  # 量比创新高
    ma5 = _ma(close, 5); ma10 = _ma(close, 10); ma20 = _ma(close, 20)
    cond &= (ma5 > ma10) & (ma10 > ma20) & (close > _ma(close, 60))
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26; dea = dif.ewm(span=9, adjust=False).mean()
    cond &= (dif > 0) & (dea > 0)
    cond &= open_df / close.shift(1) <= 1.05
    cond &= close > open_df
    return cond.fillna(False).shift(1).fillna(False).infer_objects(copy=False)

def strategy_golden_trend(close, open_df, high, low, vol, vol_mul=1.5, strength_pct=0.03):
    close, open_df, high, low, vol = align_all(close, open_df, high, low, vol)
    ma1 = _ma(close,5); ma2 = _ma(close,10); ma3 = _ma(close,20); ma4 = _ma(close,60)
    cond = (ma1>ma2) & (ma2>ma3) & (ma3>ma4) & (ma1>ma1.shift(1))
    cond &= (close>ma1) & ((close-ma4)/ma4*100 < 30)
    cond &= vol/vol.shift(1) > vol_mul
    cond &= close/close.shift(1) > (1+strength_pct)
    cond &= close > open_df
    return cond.fillna(False).shift(1).fillna(False).infer_objects(copy=False)

def strategy_ma_stick(close, open_df, high, low, vol, ma_range=3.0, vol_mul=1.8, trend_days=5):
    close, open_df, high, low, vol = align_all(close, open_df, high, low, vol)
    ma5=_ma(close,5); ma10=_ma(close,10); ma20=_ma(close,20); ma30=_ma(close,30); ma60=_ma(close,60)
    max_ma = pd.concat([ma5,ma10,ma20,ma30],axis=1).max(axis=1)
    min_ma = pd.concat([ma5,ma10,ma20,ma30],axis=1).min(axis=1)
    s = (max_ma-min_ma)/ma60.replace(0,np.nan)*100
    cond = (s<ma_range) & (close>max_ma) & (vol>_ma(vol,5)*vol_mul)
    cond &= ma20>ma20.shift(trend_days) & (close>ma60)
    return cond.fillna(False).shift(1).fillna(False).infer_objects(copy=False)

def strategy_lotus(close, open_df, high, low, vol, vol_mul_short=3.0, vol_mul_long=2.0, body_pct=0.02):
    close, open_df, high, low, vol = align_all(close, open_df, high, low, vol)
    ma5=_ma(close,5); ma10=_ma(close,10); ma20=_ma(close,20); ma60=_ma(close,60)
    cond = (open_df<ma5)&(open_df<ma10)&(open_df<ma20)
    cond &= (close>ma5)&(close>ma10)&(close>ma20)&(close>ma60)
    cond &= (close-open_df).abs()/open_df.replace(0,np.nan) > body_pct
    cond &= (vol>_ma(vol,5)*vol_mul_short) & (vol>_ma(vol,60)*vol_mul_long)
    ema12=close.ewm(span=12,adjust=False).mean(); ema26=close.ewm(span=26,adjust=False).mean()
    dif=ema12-ema26; dea=dif.ewm(span=9,adjust=False).mean()
    cond &= (dif>dea)&(dif.shift(1)<=dea.shift(1))
    return cond.fillna(False).shift(1).fillna(False).infer_objects(copy=False)


def run_sim(close_df, entries_df, stop_cfg, capital=1_000_000, max_buy=100000):
    common = sorted(set(close_df.columns) & set(entries_df.columns))
    c = close_df[common].ffill().bfill()
    e = entries_df.reindex(index=c.index, columns=common, fill_value=False)
    n_sig = int(e.sum().sum())
    if n_sig < 3:
        return None
    cost=stop_cfg.get("cost_stop",{}); trail=stop_cfg.get("trailing_stop",{})
    ladder=stop_cfg.get("ladder_tp",{}); time_s=stop_cfg.get("time_stop",{})
    cond_t=stop_cfg.get("cond_time_stop",{})
    lv=sorted(ladder.get("levels",[]),key=lambda x:x.get("profit",0))
    lp=np.array([lv[i]["profit"] for i in range(len(lv))],dtype=np.float64)
    lr=np.array([lv[i]["sell_ratio"] for i in range(len(lv))],dtype=np.float64)
    ea,rt=_simulate_core_v3(
        c.values.astype(np.float64),e.values,
        float(capital),0.0003,5000.0,float(max_buy),100,1,
        cost.get("enabled",True),float(cost.get("threshold",-0.06)),
        trail.get("enabled",True),float(trail.get("activation",0.05)),
        float(trail.get("drawdown",0.03)),
        ladder.get("enabled",True),lp,lr,len(lv),
        time_s.get("enabled",True),int(time_s.get("max_hold_days",10)),
        cond_t.get("enabled",False),int(cond_t.get("days",7)),float(cond_t.get("profit",0.01)),
    )
    if len(rt)==0: return None
    cr=(ea[-1]-capital)/capital
    wr=sum(1 for t in rt if t[7]>0)/len(rt)
    peak=np.maximum.accumulate(ea); mdd=float(np.min((ea-peak)/peak))
    ny=len(ea)/244.0
    ann=(1+cr)**(1/ny)-1 if ny>0 and cr>-1 else cr
    return {"trades":len(rt),"return":cr,"annual":ann,"win_rate":wr,"max_dd":mdd,"signals":n_sig}


def main():
    logger.info("第三轮 — 大仓位+宽止损")
    TdxConnector.initialize()
    from tqcenter import tq

    raw = tq.get_stock_list("50", list_type=1)
    codes = [s["Code"] for s in raw if isinstance(s, dict)]
    codes = [c for c in codes if not (
        'ST' in c or c.startswith('688') or c.startswith('920') or
        c.startswith('430') or c.startswith('873') or
        c.startswith('8') or c.startswith('4')
    ) and c.endswith(('.SH','.SZ'))]
    logger.info(f"股票池: {len(codes)} 只")

    cp,op,hp,lp,vp=[],[],[],[],[]
    for i in range(0,len(codes),300):
        batch=codes[i:i+300]
        try:
            r=tq.get_market_data(field_list=[],stock_list=batch,
                start_time="20260101",end_time="20260430",
                period="1d",dividend_type="front",count=-1)
            if r and "Close" in r:
                cp.append(r["Close"]); op.append(r["Open"])
                hp.append(r["High"]); lp.append(r["Low"]); vp.append(r["Volume"])
        except Exception as e: logger.warning("get_kline failed: %s", e)
    close=pd.concat(cp,axis=1).ffill().bfill()
    open_df=pd.concat(op,axis=1).ffill().bfill()
    high=pd.concat(hp,axis=1).ffill().bfill()
    low=pd.concat(lp,axis=1).ffill().bfill()
    vol=pd.concat(vp,axis=1).ffill().bfill()
    logger.info(f"数据: {close.shape[0]}bar × {close.shape[1]}股")

    # 止损配置 — 核心改变: 放大仓位、放宽止损
    STOPS = {
        "宽A": {"cost_stop":{"enabled":True,"threshold":-0.12},
                "trailing_stop":{"enabled":True,"activation":0.10,"drawdown":0.06},
                "ladder_tp":{"enabled":True,"levels":[
                    {"profit":0.08,"sell_ratio":0.25},
                    {"profit":0.20,"sell_ratio":0.35},
                    {"profit":0.40,"sell_ratio":1.00}]},
                "time_stop":{"enabled":True,"max_hold_days":20}},
        "宽B": {"cost_stop":{"enabled":True,"threshold":-0.15},
                "trailing_stop":{"enabled":True,"activation":0.12,"drawdown":0.08},
                "ladder_tp":{"enabled":True,"levels":[
                    {"profit":0.10,"sell_ratio":0.20},
                    {"profit":0.25,"sell_ratio":0.30},
                    {"profit":0.50,"sell_ratio":1.00}]},
                "time_stop":{"enabled":True,"max_hold_days":30}},
        "无时限宽A": {"cost_stop":{"enabled":True,"threshold":-0.12},
                "trailing_stop":{"enabled":True,"activation":0.08,"drawdown":0.05},
                "ladder_tp":{"enabled":True,"levels":[
                    {"profit":0.05,"sell_ratio":0.25},
                    {"profit":0.15,"sell_ratio":0.35},
                    {"profit":0.30,"sell_ratio":1.00}]},
                "time_stop":{"enabled":False,"max_hold_days":999}},
    }

    STRATEGIES = {
        "量价共振v2": (strategy_vol_price_fixed,
            {"vol_ratio": [0.5, 0.8, 1.0, 1.2, 1.5]}),
        "均线多头起爆": (strategy_golden_trend,
            {"vol_mul": [1.2, 1.3, 1.5, 1.8, 2.0],
             "strength_pct": [0.015, 0.02, 0.025, 0.03]}),
        "均线粘合起爆": (strategy_ma_stick,
            {"ma_range": [2.0, 3.0, 4.0],
             "vol_mul": [1.5, 1.8, 2.0, 2.5],
             "trend_days": [3, 5, 8]}),
        "出水芙蓉": (strategy_lotus,
            {"vol_mul_short": [2.0, 3.0, 4.0],
             "vol_mul_long": [1.5, 2.0, 2.5],
             "body_pct": [0.015, 0.02, 0.025]}),
    }

    CAPITALS = [1_000_000, 2_000_000]

    all_results = []
    max_trials = 12

    for sname, (fn, grid) in STRATEGIES.items():
        keys = list(grid.keys())
        combos = list(itertools.product(*grid.values()))
        if len(combos)>max_trials:
            np.random.seed(42)
            idx=np.random.choice(len(combos),max_trials,replace=False)
            combos=[combos[i] for i in idx]

        for combo in combos:
            params=dict(zip(keys,combo))
            try:
                entries=fn(close,open_df,high,low,vol,**params)
            except Exception as e:
                continue

            for stop_name,stop_cfg in STOPS.items():
                for cap in CAPITALS:
                    max_buy = cap * 0.10  # 10% per trade
                    m=run_sim(close,entries,stop_cfg,capital=cap,max_buy=max_buy)
                    if m is None: continue
                    ret=m["return"]
                    all_results.append({"strategy":sname,"stop":stop_name,"capital":cap,
                                        "params":params,**m})
                    flag="★★★" if ret>=0.30 else ("★★" if ret>=0.15 else ("★" if ret>=0.10 else " "))
                    logger.info(f"  {flag} [{sname}][{stop_name}] cap={cap/10000:.0f}w "
                                f"收益={ret:.2%} 年化={m['annual']:.2%} "
                                f"胜率={m['win_rate']:.1%} 回撤={m['max_dd']:.2%} "
                                f"交易={m['trades']} {params}")

    all_results.sort(key=lambda x:x["return"],reverse=True)

    logger.info(f"\n{'='*60}")
    logger.info("TOP 20")
    logger.info(f"{'='*60}")
    for i,r in enumerate(all_results[:20]):
        logger.info(f"  #{i+1} [{r['strategy']}][{r['stop']}] cap={r['capital']/10000:.0f}w "
                    f"收益={r['return']:.2%} 年化={r['annual']:.2%} "
                    f"胜率={r['win_rate']:.1%} 回撤={r['max_dd']:.2%} "
                    f"交易={r['trades']} {r['params']}")

    import json
    save=[{k:(float(v) if isinstance(v,(np.floating,))else v)for k,v in r.items()}for r in all_results]
    with open("optimization_results_v3.json","w")as f:
        json.dump(save,f,ensure_ascii=False,indent=2,default=str)
    logger.info("保存完成")
    TdxConnector.close()

if __name__=="__main__":
    main()
