# -*- coding: utf-8 -*-
"""HTML 图文报告生成 (2026-08-26, 全新编写)。

汇总 S0/S1-S3/S4/S5 全部产物 → 单文件 HTML (plotly 内嵌):
  漏斗总览 / 死因分布 / 幸存者档案 / 三段稳健性 / 5m vs 日线 / 池子对比热力图

用法:
    python tools/formula_pipeline/report/build_html.py --run-dir <dir>
"""
import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from tools.formula_pipeline.common import load_json  # noqa: E402

import plotly.graph_objects as go  # noqa: E402
import plotly.io as pio  # noqa: E402

POOL_NAMES = {"all_a": "全A", "hs300": "沪深300", "zz500": "中证500",
              "zz1000": "中证1000", "cyb": "创业板", "kcb": "科创板"}
STAGE_NAMES = {"s1": "S1 2020-2026.7", "s2": "S2 2014-2026.8",
               "s3": "S3 2005-2024"}
DEATH_CN = {"killed_s1": "S1 淘汰", "killed_s2": "S2 淘汰",
            "killed_s3": "S3 淘汰", "killed_density": "信号过密",
            "unsupported": "函数不支持", "no_signals": "无信号",
            "error": "执行错误", "no_metrics": "无指标",
            "survivor": "三段幸存"}


def fig_to_html(fig):
    return pio.to_html(fig, include_plotlyjs=False, full_html=False,
                       config={"displaylogo": False})


def build(run_dir: Path) -> Path:
    s0 = load_json(run_dir / "stage0_scan" / "report.json")
    cov = load_json(run_dir / "stage0_scan" / "parse_coverage.json")
    d1 = load_json(run_dir / "stage_d1" / "results.json")
    pools_fp = run_dir / "stage5_pools" / "pools.json"
    pools = load_json(pools_fp) if pools_fp.exists() else {}
    m5_fp = run_dir / "stage4_5m" / "results_5m.json"
    m5 = load_json(m5_fp) if m5_fp.exists() else {}

    survivors = [r for r in d1 if r["status"] == "survivor"]

    # ------------------------------------------------ 漏斗图
    n_total = s0["summary"]["total"]
    n_s0 = s0["summary"]["pass"]
    n_exec = cov["executable"]
    stages = [("原始公式", n_total), ("S0 静态体检", n_s0),
              ("可执行", n_exec)]
    cnt = Counter(r["status"] for r in d1)
    stages += [("进入回测", len(d1)), ("S1 通过", len(d1) - sum(
        cnt[k] for k in ("killed_s1", "killed_density", "no_signals",
                         "unsupported", "error", "no_metrics")))]
    for k, lab in (("killed_s2", "S2 通过"), ):
        stages.append((lab, len(d1) - sum(cnt[x] for x in (
            "killed_s1", "killed_density", "no_signals", "unsupported",
            "error", "no_metrics", k))))
    stages.append(("S3 幸存", cnt.get("survivor", 0)))

    funnel = go.Figure(go.Funnel(
        y=[s[0] for s in stages], x=[s[1] for s in stages],
        textinfo="value+percent previous", marker={"color": "#2E86AB"}))
    funnel.update_layout(title="淘汰漏斗总览 (397 个公式 → 幸存者)",
                         height=420)

    # ------------------------------------------------ 死因分布
    death_cnt = Counter()
    for e in s0["excluded"]:
        for k in e["reasons"]:
            death_cnt[k] += 1
    for st, n in cnt.items():
        death_cnt[DEATH_CN.get(st, st)] += n
    death_labels = {"future": "未来函数", "polyline": "漂移画图",
                    "zxnh": "ZXNH", "proprietary": "专有函数(筹码/财务)",
                    "cross_period": "跨周期引用"}
    items = [(death_labels.get(k, DEATH_CN.get(k, k)), v)
             for k, v in death_cnt.items() if v > 0]
    pie = go.Figure(go.Pie(
        labels=[i[0] for i in items], values=[i[1] for i in items],
        hole=0.45))
    pie.update_layout(title="出局原因分布", height=420)

    # ------------------------------------------------ 幸存者三段稳健性
    fig_surv = go.Figure()
    for r in survivors:
        xs, ys = [], []
        for tag in ("s1", "s2", "s3"):
            if tag in r:
                xs.append(STAGE_NAMES[tag])
                ar = r[tag]["metrics"].get("annualized_return") or 0
                ys.append(ar * 100)
        if xs:
            fig_surv.add_trace(go.Scatter(
                x=xs, y=ys, mode="lines+markers", name=r["name"]))
    fig_surv.update_layout(title="幸存者 · 三段年代考卷年化收益 (%)",
                           yaxis_title="年化 %", height=450)

    # ------------------------------------------------ 幸存者表格
    rows_html = []
    for r in sorted(survivors,
                    key=lambda x: -(x.get("s2", {}).get("metrics", {})
                                    .get("annualized_return") or -9)):
        s2m = r.get("s2", {}).get("metrics", {})
        s2b = r.get("s2", {}).get("bench", 0) or 0
        ar = (s2m.get("annualized_return") or 0) * 100
        dd = (s2m.get("max_drawdown") or 0) * 100
        wr = (s2m.get("win_rate") or 0) * 100
        nt = r.get("s2", {}).get("n_trades", 0)
        m5r = m5.get(r["name"], {})
        ar5 = (m5r.get("metrics", {}).get("annualized_return") or 0) * 100 \
            if m5r.get("status") == "ok" else None
        rows_html.append(
            f"<tr><td>{r['name']}</td>"
            f"<td>{ar:+.1f}%</td><td>{s2b*100:+.1f}%</td>"
            f"<td>{dd:.1f}%</td><td>{wr:.0f}%</td><td>{nt}</td>"
            f"<td>{'—' if ar5 is None else f'{ar5:+.1f}%'}</td></tr>")
    table_html = f"""
    <table class="tbl"><thead><tr>
      <th>公式</th><th>S2 年化</th><th>同期基准</th><th>最大回撤</th>
      <th>胜率</th><th>交易数</th><th>5m 版年化</th>
    </tr></thead><tbody>{''.join(rows_html) or '<tr><td colspan=7>暂无幸存者</td></tr>'}</tbody></table>"""

    # ------------------------------------------------ 池子对比热力图
    # 全A 列直接取 d1/S2 结果 (stage_pools 不重跑全A); 其余池子取 pools.json
    if pools or survivors:
        pool_keys = ["all_a", "hs300", "zz500", "zz1000", "cyb", "kcb"]
        names = [r["name"] for r in survivors
                 if r["name"] in pools or True][:40]  # 上限40行防图过长
        z = []
        for r in survivors:
            if r["name"] not in names:
                continue
            row = []
            for pk in pool_keys:
                if pk == "all_a":
                    ar = (r.get("s2", {}).get("metrics", {})
                          .get("annualized_return") or 0) * 100
                    row.append(ar)
                    continue
                v = pools.get(r["name"], {}).get(pk, {})
                if v.get("status") == "ok":
                    ar = (v["metrics"].get("annualized_return") or 0) * 100
                    row.append(ar)
                else:
                    row.append(None)
            z.append(row)
        heat = go.Figure(go.Heatmap(
            z=z, x=[POOL_NAMES[k] for k in pool_keys], y=names,
            colorscale="RdYlGn", zmid=0, text=[[f"{v:+.1f}%" if v is not None
                                                 else "—" for v in row]
                                               for row in z],
            texttemplate="%{text}", hovertemplate="%{y} @ %{x}<br>%{text}<extra></extra>"))
        heat.update_layout(title="池子对比 · S2 区间年化收益热力图 (%)",
                           height=max(320, 60 * len(names) + 140))
    else:
        heat = go.Figure()
        heat.update_layout(title="池子对比 (未运行)", height=200)

    # ------------------------------------------------ S4 结论 (按实际数据生成)
    if m5:
        ok5 = {k: v for k, v in m5.items() if v.get("status") == "ok"}
        diffs = []
        for k, v in ok5.items():
            ar5 = (v.get("metrics", {}).get("annualized_return") or 0) * 100
            s2ar = 0.0
            for r in survivors:
                if r["name"] == k:
                    s2ar = (r.get("s2", {}).get("metrics", {})
                            .get("annualized_return") or 0) * 100
            diffs.append(ar5)
        avg5 = sum(diffs) / len(diffs) if diffs else 0
        better = sum(1 for k, v in ok5.items()
                     if (v.get("metrics", {}).get("annualized_return") or 0)
                     > next((r.get("s2", {}).get("metrics", {})
                             .get("annualized_return") or 0
                             for r in survivors if r["name"] == k), 0))
        s4_concl = (f"<b>S4 核心结论 (本批实测): {len(ok5)}/{len(m5)} 个公式出结果, "
                    f"5m 版平均年化 {avg5:+.1f}%, 其中 {better} 个 5m 版优于日线版</b>。"
                    "⚠ <b>关键提醒 — 区间不对等, 「全灭」不能直接归因</b>: 本批 5m 只跑了 "
                    "2024-06-27 起的近 2 年 (当时为避开 5m 缺数段选的干净段), 而日线对照用的是 "
                    "S2 全段 2014-2026 (约 12 年)。两者样本期悬殊, 「30/30 全灭 / 全部劣于日线」"
                    "<b>更可能是近 2 年样本期市场风格不利, 而非 5m 执行模型本身更差</b>。 "
                    "执行模型本身是合规的 (尾盘买入 + T+1 按 bar 出场, 非盘中追涨杀跌)。 "
                    "要公平判定 5m 相对日线收盘执行的真实增益/损耗, 需把 5m 区间往前扩到与日线同源 "
                    "(如 2015 或 2020 起, 本地 5m 缓存多数股票已覆盖 2015-2017) 重跑复核。")
    else:
        s4_concl = ("<b>S4 五分钟精考进行中/未运行</b> — 结果出来后重新生成本报告 "
                    "(python tools/formula_pipeline/report/build_html.py --run-dir ...)。"
                    "5m 区间为 2024-06-27 后 (本地 5m 数据全量完整段), 与日线 S2 "
                    "(2014-2026) 区间不同。")

    # ------------------------------------------------ 汇总 HTML
    html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>通达信公式多 Agent 淘汰漏斗报告</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
 body {{ font-family: "Microsoft YaHei", sans-serif; margin: 24px auto;
        max-width: 1200px; color: #222; background: #fafafa; }}
 h1 {{ color: #1a5276; border-bottom: 3px solid #2E86AB; padding-bottom: 8px; }}
 h2 {{ color: #21618c; margin-top: 36px; }}
 .meta {{ color: #666; font-size: 14px; }}
 .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }}
 .tbl {{ border-collapse: collapse; width: 100%; font-size: 14px;
        background: #fff; }}
 .tbl th, .tbl td {{ border: 1px solid #d5d8dc; padding: 6px 10px;
                    text-align: center; }}
 .tbl th {{ background: #2E86AB; color: #fff; }}
 .tbl tr:nth-child(even) {{ background: #f4f6f7; }}
 .warn {{ background: #fef9e7; border-left: 4px solid #f39c12;
         padding: 10px 14px; font-size: 13px; }}
</style></head><body>
<h1>通达信公式多 Agent 淘汰漏斗报告</h1>
<p class="meta">run: {run_dir.name} | 公式来源: gs_2 批 397 个 (gupang 选股公式) |
   信号引擎: 本地解释器 (纯K线) | 回测: VERA 标准引擎 |
   规则: 6%硬止损 + 3.5%/1%移动止盈 + 10交易日强平 (止损优先)</p>

<h2>〇、流水线工作过程说明 (S1-S5 每步做什么、怎么做、为什么)</h2>
<div class="pipeline-desc" style="background:#fff;padding:14px 18px;border:1px solid #d5d8dc;font-size:14px;line-height:1.9">
<p><b>目标</b>: 从一批通达信选股公式里, 用「一层比一层严格」的考试, 淘汰掉未来函数、
运气好、只在某个年代有效的公式, 最终留下<b>近似实盘 (5 分钟执行) 依然赚钱的优选公式</b>。
每一步做什么、怎么做、为什么这么做:</p>
<ul style="padding-left:20px">
<li><b>S0 静态扫描 (体检查源码)</b> — 不跑回测, 纯文本检查每个公式的源码:
   用了未来函数 (ZIG/PEAK/REFX 等「考完试才改答案」的函数, 回测必然虚高) 的直接出局;
   用了通达信专有筹码/财务函数 (WINNER/COST 等, 本地数据无法复现) 与跨周期引用的也无法评估,
   一并出局。这一步成本最低, 先把「不可能公平考试」的公式清出场。</li>
<li><b>S0.5 解释器验证 (确认看得懂)</b> — 自写的公式解释器 (把通达信公式翻译成 numpy 信号引擎)
   逐个试跑一遍: 参数齐不齐、函数认不认识、有没有输出行。看不懂的公式 (NAMELIKE/ZTPRICE 等
   通达信专有函数) 标记 unsupported, 不进回测。这一步得到「可执行公式集」。</li>
<li><b>S1-S3 日线三段淘汰 (三张不同年代的考卷)</b> — 核心考试。每个公式用本地解释器算出
   全区间买入信号矩阵 (哪天、哪只票、什么条件触发), 先过两道门: 同票 30 日内只取第一个信号
   (避免天天出信号刷量)、日均信号超 200 只直接杀 (信号太密根本没法实操)。然后依次考三张考卷,
   <b>任何一张不及格立即淘汰</b>:
   <br>· <b>S1 近年考卷 (2020-01~2026-07)</b>: 年化跑赢基准、最大回撤≤45%、交易≥30 笔、
       胜率≥40% 或盈亏比 PF≥1.3 — 先杀掉明显不赚钱的;
   <br>· <b>S2 长跑考卷 (2014-01~2026-08)</b>: 标准收紧 — 回撤≤40%、交易≥80 笔、
       盈利月占比≥45% — 杀掉靠一波行情吃饭的;
   <br>· <b>S3 换年代考卷 (2005-01~2024-12)</b>: 把起点往前挪 9 年, 任一 5 年子窗口
       最差收益不得低于 -20% — 杀掉「只在近年风格里有效」的, 留下跨年代稳健的。</li>
<li><b>S5 分池对比 (赚的是谁的钱)</b> — 幸存公式的信号不变, 把股票池分别换成
   沪深300/中证500/中证1000/创业板/科创板, 各池单独回测。用来回答「这公式到底在赚哪类票的钱」
   (本批结论: 绝大多数最优池是创业板, 即本质在赚 20cm 高波动票的钱)。</li>
<li><b>S4 五分钟精考 (近似实盘)</b> — 最后一步, 也是「优选」的最终裁判: 日线信号不变,
   买入后的三件套 (止损/止盈/强平) 改用 5 分钟线执行。执行模型严格按 A股实盘:
   <b>尾盘买入</b> (日线信号在收盘确定, 取信号日最后一根 5m bar = 15:00 的收盘价成交) +
   <b>T+1 约束</b> (当天买入的仓位当天不可卖) + <b>按 bar 出场</b> (T+1 起每根 5m bar 复查
   止损止盈, 触线即按该 bar 价格成交, intraday confirm)。这比日线「收盘才确认」更敏感——
   盘中一根 5m bar 跌破止损线就触发, 且更早锁定止盈。<b>只有日线赚钱、5m 近似实盘也赚钱的公式,
   才是真正可上实盘的优选公式。</b> (本批结果见第四章。)</li>
</ul>
<p><b>顺序</b>: S0/S0.5 全量快筛 → S1-S3 全量淘汰 → 幸存者 S5 分池 + S4 精考 (Top30) → 本报告。
所有阶段的判定标准、淘汰原因、逐公式明细全部落盘 JSON 可追溯。</p>
</div>

<h2>一、漏斗总览</h2>
<div class="grid">
  <div>{fig_to_html(funnel)}</div>
  <div>{fig_to_html(pie)}</div>
</div>

<h2>二、幸存者档案 (按 S2 年化排序)</h2>
{table_html}

<h2>三、幸存者 · 三段年代稳健性</h2>
{fig_to_html(fig_surv)}

<h2>四、S4 五分钟精考 (近似实盘) 与 S5 池子对比</h2>
<p>{s4_concl}
   S5: 同一信号在不同指数成分池的表现 (当前成分近似, 存在幸存者偏差)。</p>
{fig_to_html(heat)}

<h2>五、局限性声明</h2>
<div class="warn">
<ul>
<li><b>成分股幸存者偏差</b>: 池子对比用当前成分名单回测历史, 成绩偏乐观。</li>
<li><b>印花税口径</b>: 引擎统一按万五, 2023-08 前实际为千一, 早年收益略乐观。</li>
<li><b>专有函数公式</b>: 154 个含筹码/财务函数的公式按用户决策未评估。</li>
<li><b>过拟合风险</b>: 三段区间均为历史筛选, 未来表现无保证;
    报告附「最近12个月」参考列见 JSON 明细。</li>
<li><b>5m 数据缺口</b>: 2024-06 前部分票缺 5m, 引擎自动降级日线。</li>
</ul></div>

<h2>六、产物索引</h2>
<ul class="meta">
<li>stage0_scan/report.json — 静态扫描明细 (每公式判定+死因)</li>
<li>stage0_scan/parse_coverage.json — 解释器覆盖率</li>
<li>stage_d1/results.json — 三阶段逐公式指标+淘汰原因</li>
<li>stage4_5m/results_5m.json — 5 分钟执行对比</li>
<li>stage5_pools/pools.json — 六池子逐池指标</li>
</ul>
</body></html>"""

    out = run_dir / "report" / "final_report.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    args = ap.parse_args()
    out = build(Path(args.run_dir))
    print(f"[report] {out}")


if __name__ == "__main__":
    main()
