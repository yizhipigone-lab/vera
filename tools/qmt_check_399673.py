"""qmt_check_399673.py — 诊断 QMT xtdata 能否取到 399673.SZ 日线。

用途: 在 QMT(miniQMT) 环境跑, 排查 ETF 轮动信号取数为空 (实际 0 根) 的问题。
用法: python tools/qmt_check_399673.py
输出: 各种取数姿势各试一遍, 打印每种的根数, 一眼定位根因。
"""
from __future__ import annotations


def main() -> None:
    try:
        from xtquant import xtdata
    except ImportError:
        print("未安装 xtquant —— 请在 QMT(miniQMT) 环境运行本脚本")
        return

    code = "399673.SZ"
    print(f"=== 检查 {code} 日线取数 ===\n")

    # 1) 直接 get_market_data_ex: 试 字段列表 × count 的组合
    print("[1] 直接 get_market_data_ex")
    for field_list in ([], ["close"]):
        for count in (500, -1):
            tag = f"field_list={field_list!r}, count={count}"
            try:
                raw = xtdata.get_market_data_ex(
                    field_list, [code], "1d", "", "", count, "none", False)
                df = (raw or {}).get(code)
                if df is None or df.empty:
                    print(f"  {tag}: 0 根  (raw keys={list((raw or {}).keys()) if raw else raw})")
                else:
                    cols = list(df.columns)
                    last = float(df[cols[-1]].iloc[-1]) if cols else None
                    print(f"  {tag}: {len(df)} 根, 列={cols}, 末根={last}")
            except Exception as e:
                print(f"  {tag}: 异常 {e!r}")

    # 2) 尝试下载历史数据后重取 (判断是否"未下载"导致)
    print("\n[2] download_history_data 后重取")
    try:
        rc = xtdata.download_history_data(code, "1d", "20240101", "")
        print(f"  download rc={rc}")
        raw = xtdata.get_market_data_ex([], [code], "1d", "", "", 500, "none", False)
        df = (raw or {}).get(code)
        if df is None or df.empty:
            print("  下载后仍 0 根")
        else:
            print(f"  下载后 {len(df)} 根, 末根 close={float(df['close'].iloc[-1])}")
    except Exception as e:
        print(f"  download 异常: {e!r}")

    # 3) 代码格式备选 (判断是否代码后缀问题)
    print("\n[3] 代码格式备选")
    for alt in ("399673", "SZ399673", "399673.399673", "399006.SZ"):
        try:
            raw = xtdata.get_market_data_ex([], [alt], "1d", "", "", 500, "none", False)
            df = (raw or {}).get(alt)
            n = 0 if df is None or df.empty else len(df)
            print(f"  {alt!r}: {n} 根"
                  + (f" (末根 close={float(df['close'].iloc[-1])})" if n else ""))
        except Exception as e:
            print(f"  {alt!r}: 异常 {e!r}")

    print("\n=== 结论指引 ===")
    print("- [1] 若 field_list=[] + count=500 有根数 → 代码已修好, 轮动可正常取数")
    print("- [1] 全 0 但 [2] 下载后有根数 → 需在取数前先 download_history_data")
    print("- [3] 某备选格式有根数 → 代码里的 399673.SZ 后缀格式不对, 需换")


if __name__ == "__main__":
    main()
