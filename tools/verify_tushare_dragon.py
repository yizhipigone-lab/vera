"""
tushare 龙虎榜数据验证脚本(腿 B 事件因子数据源探查)

目的:
    1. 验证 tushare token + 积分是否够拉龙虎榜(需 2000 积分)
    2. 看龙虎榜数据格式(top_list 每日明细 / top_inst 机构明细)
    3. 试着转成 VERA selections 格式(stock_code, select_date)
    4. 确认日期对齐(前视防护:龙虎榜 T 日盘后数据,只能 T+1 用)

用法:
    python tools/verify_tushare_dragon.py [trade_date YYYYMMDD]

token 来源(按优先级):
    1. 环境变量 TUSHARE_TOKEN
    2. 项目根 .env 里的 TUSHARE_TOKEN

输出:打印观察数据 + 最后一行 JSON 结论
"""
import os
import sys
import json
from pathlib import Path

# Windows GBK 终端中文输出修复
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def load_token() -> str:
    """从环境变量或项目根 .env 加载 TUSHARE_TOKEN"""
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


def main() -> None:
    trade_date = sys.argv[1] if len(sys.argv) > 1 else "20240105"
    token = load_token()
    if not token:
        print("[FAIL] 未找到 TUSHARE_TOKEN,请 set TUSHARE_TOKEN 或写 .env")
        sys.exit(1)

    try:
        import tushare as ts
    except ImportError:
        print("[FAIL] tushare 未安装,请 pip install tushare")
        sys.exit(1)

    print(f"[INFO] tushare 版本: {ts.__version__}")
    print(f"[INFO] 目标交易日: {trade_date}")
    pro = ts.pro_api(token)

    # 1. 拉龙虎榜每日明细 top_list
    print(f"\n=== 1. 拉龙虎榜 top_list(trade_date={trade_date}) ===")
    top_list_ok = False
    df = None
    try:
        df = pro.top_list(trade_date=trade_date)
        print(f"行数: {len(df)}")
        print(f"字段: {list(df.columns)}")
        if len(df) > 0:
            print(df.head(3).to_string())
            top_list_ok = True
        else:
            print("[WARN] 该交易日无龙虎榜数据(可能非交易日或当日无触发)")
    except Exception as e:
        print(f"[FAIL] top_list 拉取失败: {e}")

    # 2. 拉龙虎榜机构买卖明细 top_inst
    print(f"\n=== 2. 拉龙虎榜机构明细 top_inst(trade_date={trade_date}) ===")
    top_inst_ok = False
    try:
        df_inst = pro.top_inst(trade_date=trade_date)
        print(f"行数: {len(df_inst)}")
        print(f"字段: {list(df_inst.columns)}")
        if len(df_inst) > 0:
            print(df_inst.head(3).to_string())
            top_inst_ok = True
    except Exception as e:
        print(f"[FAIL] top_inst 拉取失败: {e}")

    # 3. 试着转 VERA selections 格式
    print("\n=== 3. 转 VERA selections 格式(stock_code, select_date)===")
    if top_list_ok and df is not None and len(df) > 0:
        # tushare ts_code 如 '000001.SZ',与 VERA 的 stock_code 格式一致
        sel = df[["ts_code", "trade_date"]].copy()
        sel.columns = ["stock_code", "select_date"]
        print(f"可造 selections 行数: {len(sel)}")
        print(sel.head(3).to_string())
        print("\n[OK] ts_code 格式与 VERA stock_code 一致,可直接注入 selections")
    else:
        print("[SKIP] top_list 无数据,跳过格式转换")

    # 4. 前视偏差提示
    print("\n=== 4. 前视偏差提示(关键)===")
    print("龙虎榜是 T 日【盘后】数据,T 日收盘选股不能用(收盘时刻还没出)")
    print("VERA 铁律:信号日 T 收盘买入 → 龙虎榜因子只能用在 T+1 及之后的选股过滤")
    print("即:T 日收盘生成的 selections,只能用 T-1 及更早的龙虎榜过滤")

    # JSON 结论
    result = {
        "trade_date": trade_date,
        "top_list_ok": top_list_ok,
        "top_inst_ok": top_inst_ok,
        "top_list_rows": int(len(df)) if (top_list_ok and df is not None) else 0,
    }
    print(f"\n{json.dumps(result, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
