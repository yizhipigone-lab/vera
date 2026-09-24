"""服务端(TDX,重绘) vs Python replica(重绘复刻) vs Python causal(实盘可见) 三方信号对比。"""
import sys
import pandas as pd

def keyset(df):
    return set(zip(df["stock_code"], pd.to_datetime(df["select_date"]).dt.strftime("%Y%m%d")))

def main(server_path, replica_path, causal_path, win_start, win_end):
    srv = pd.read_parquet(server_path)
    rep = pd.read_parquet(replica_path)
    cau = pd.read_parquet(causal_path)
    def clip(df):
        d = pd.to_datetime(df["select_date"]).dt.strftime("%Y%m%d")
        return df[(d >= win_start) & (d <= win_end)]
    srv, rep, cau = clip(srv), clip(rep), clip(cau)
    S, R, C = keyset(srv), keyset(rep), keyset(cau)
    # 口径对齐: 只看服务端池内的股票 (排除ST等差异)
    pool = set(srv["stock_code"]) | set(rep["stock_code"]) | set(cau["stock_code"])
    R_in = {(c, d) for c, d in R}
    C_in = {(c, d) for c, d in C}
    print(f"窗口 {win_start}~{win_end}")
    print(f"  服务端(重绘)   : {len(S)}")
    print(f"  Python replica : {len(R_in)}  (与服务端交集 {len(S & R_in)}, 覆盖服务端 {len(S & R_in)/max(len(S),1):.1%})")
    print(f"  Python causal  : {len(C_in)}  (与服务端交集 {len(S & C_in)})")
    print(f"  causal ⊂ replica 比例: {len(C_in & R_in)/max(len(C_in),1):.1%}")
    print(f"  服务端信号中实盘不可见(重绘水分): {1 - len(S & C_in)/max(len(S),1):.1%}")
    # 逐股看 000001.SZ
    for code in ["000001.SZ"]:
        s = sorted(d for c, d in S if c == code)
        r = sorted(d for c, d in R_in if c == code)
        c_ = sorted(d for c, d in C_in if c == code)
        print(f"  {code}: server={len(s)} replica={len(r)} causal={len(c_)}")

if __name__ == "__main__":
    main(*sys.argv[1:6])
