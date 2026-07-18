"""
tushare 事件因子接口可得性探测(腿 B 数据源摸底 —— "能拿多少拿多少")

目的:
    一次性探测 FinanceMCP 工具对应的所有事件因子接口,
    输出每个接口的:可得性 / 行数 / 关键字段 / 失败原因(含积分要求)

用法:
    python tools/verify_tushare_events.py [trade_date YYYYMMDD]

token 来源:环境变量 TUSHARE_TOKEN 或项目根 .env
"""
import os
import sys
import json
from pathlib import Path

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


def main() -> None:
    trade_date = sys.argv[1] if len(sys.argv) > 1 else "20240105"
    token = load_token()
    if not token:
        print("[FAIL] 未找到 TUSHARE_TOKEN")
        sys.exit(1)

    import tushare as ts
    pro = ts.pro_api(token)
    print(f"[INFO] 探测交易日: {trade_date}\n")

    # (接口名, 中文描述, 调用 lambda, FinanceMCP 对应工具)
    probes = [
        ("top_list",         "龙虎榜每日明细",   lambda: pro.top_list(trade_date=trade_date),         "dragon_tiger_inst"),
        ("top_inst",         "龙虎榜机构席位",   lambda: pro.top_inst(trade_date=trade_date),         "dragon_tiger_inst"),
        ("block_trade",      "大宗交易",         lambda: pro.block_trade(trade_date=trade_date),      "block_trade"),
        ("moneyflow",        "个股资金流向",     lambda: pro.moneyflow(trade_date=trade_date),        "money_flow"),
        ("moneyflow_hsgt",   "沪深港通资金流",   lambda: pro.moneyflow_hsgt(trade_date=trade_date),   "money_flow"),
        ("margin",           "融资融券",         lambda: pro.margin(trade_date=trade_date),           "margin_trade"),
        ("stk_holdernumber", "股东户数",         lambda: pro.stk_holdernumber(trade_date=trade_date), "company_performance"),
        ("repurchase",       "股票回购",         lambda: pro.repurchase(trade_date=trade_date),       "(公告因子)"),
        ("share_float",      "限售股解禁",       lambda: pro.share_float(trade_date=trade_date),      "(公告因子)"),
        ("hold_list",        "沪深港通持股",     lambda: pro.hk_hold(trade_date=trade_date),          "(北向资金)"),
    ]

    results = []
    for name, desc, fn, mcp in probes:
        try:
            df = fn()
            rows = len(df)
            cols = list(df.columns) if rows > 0 else []
            ok = rows > 0
            status = "OK" if ok else "EMPTY"
            sample = ""
            if ok:
                sample = f"字段({len(cols)}): {cols[:6]}"
            print(f"[{status}] {name:18s} {desc:14s} | {rows:5d} 行 | {sample}")
            results.append({"iface": name, "desc": desc, "ok": ok, "rows": int(rows),
                            "cols_count": len(cols), "mcp_tool": mcp})
        except Exception as e:
            msg = str(e)
            # 提取积分要求(如有)
            print(f"[FAIL] {name:18s} {desc:14s} | {msg[:80]}")
            results.append({"iface": name, "desc": desc, "ok": False, "rows": 0,
                            "error": msg[:120], "mcp_tool": mcp})

    ok_count = sum(1 for r in results if r["ok"])
    print(f"\n=== 汇总: {ok_count}/{len(results)} 个接口可用 ===")
    print(json.dumps({"trade_date": trade_date, "ok": ok_count, "total": len(results),
                      "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
