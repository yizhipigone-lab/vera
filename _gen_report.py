"""生成黑马起步深度分析 HTML 汇总报告 (一个页面看全部)."""
import sys
from pathlib import Path
import pandas as pd

OUT = Path('output/mcap_analysis')
small = pd.read_csv('output/heima_compare_4pool.csv', encoding='utf-8-sig')
big = pd.read_csv('output/heima_bigpos_4pool.csv', encoding='utf-8-sig')
fine = pd.read_csv(OUT / 'bucket_fine.csv', encoding='utf-8-sig')

# 4池对照表
m = small.merge(big, on='池子', suffixes=('_小', '_大'))
def row4(r):
    return (f"<tr><td><b>{r['池子']}</b></td>"
            f"<td>{r['累计收益_小']}</td><td>{r['累计收益_大']}</td>"
            f"<td>{r['年化收益_小']}</td><td>{r['年化收益_大']}</td>"
            f"<td>{r['最大回撤_小']}</td><td>{r['最大回撤_大']}</td>"
            f"<td>{r['交易笔数_小']}</td><td>{r['交易笔数_大']}</td></tr>")
tbl4 = "<table><tr><th>池子</th><th>小仓位累计</th><th>大仓位累计</th><th>小年化</th><th>大年化</th><th>小回撤</th><th>大回撤</th><th>小交易</th><th>大交易</th></tr>" + "".join(row4(r) for _, r in m.iterrows()) + "</table>"

# 细分桶表
def rowb(r):
    return (f"<tr><td><b>{r['bucket']}</b></td><td>{int(r['笔数'])}</td>"
            f"<td class='pos'>{r['平均%']:.2f}%</td><td>{r['中位%']:.2f}%</td>"
            f"<td>{r['胜率%']:.1f}%</td><td>{r['盈亏比']:.2f}</td>"
            f"<td class='pos'>{r['暴涨>50%占比']:.2f}%</td></tr>")
tblb = "<table><tr><th>市值桶</th><th>笔数</th><th>平均收益</th><th>中位数</th><th>胜率</th><th>盈亏比</th><th>暴涨>50%占比</th></tr>" + "".join(rowb(r) for _, r in fine.iterrows()) + "</table>"

# 分时段表(硬编码自 _plot_final)
decay_rows = [
    ('2019-2020', '13.7%', '3.4%', '+10.4pp', '5.86%/0.97%'),
    ('2021-2022', '6.4%', '3.0%', '+3.4pp', '2.12%/0.18%'),
    ('2023-2024', '4.1%', '3.6%', '+0.5pp', '0.15%/0.19%'),
    ('2025-2026', '3.5%', '2.7%', '+0.8pp', '0.05%/0.00%'),
]
tbl_decay = "<table><tr><th>时段</th><th>小盘<50亿</th><th>大盘>300亿</th><th>溢价</th><th>暴涨(小/大)</th></tr>" + "".join(f"<tr><td><b>{a}</b></td><td class='pos'>{b}</td><td>{c}</td><td><b>{d}</b></td><td>{e}</td></tr>" for a, b, c, d, e in decay_rows) + "</table>"

# 多agent验证表
agent_rows = [
    ('统计显著性', '显著但效应小', "p&lt;1e-7 极显著，但 Cohen's d=0.16(小效应)；剔除暴涨后均值差 4.24%→1.25%"),
    ('幸存者偏差', '高估小盘(中)', '退市股(~200-250只,全小盘)被池子排除；小盘收益打0.5-2折'),
    ('市值代理偏差', '高估历史市值', '股本只增不减→机械高估；2024-2026无偏样本溢价 2.64→0.38(消失)'),
    ('稳健性', '方向稳/强度脆弱', '分桶稳；时段脆弱：2019-2020 +10.4 → 2023-2024 +0.5，衰减95%'),
]
tbl_agent = "<table><tr><th>验证角度</th><th>判定</th><th>关键发现</th></tr>" + "".join(f"<tr><td><b>{a}</b></td><td>{b}</td><td>{c}</td></tr>" for a, b, c in agent_rows) + "</table>"

html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>黑马起步策略 · 深度分析报告</title>
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;max-width:1100px;margin:0 auto;padding:40px 24px;color:#1a1a2e;background:linear-gradient(135deg,#fafafa,#f4f4f7);line-height:1.65}}
  h1{{color:#5b5bd6;border-bottom:3px solid #5b5bd6;padding-bottom:12px;font-size:26px}}
  h2{{margin-top:44px;border-left:5px solid #5b5bd6;padding-left:14px;font-size:19px}}
  table{{border-collapse:collapse;width:100%;margin:14px 0;background:#fff;box-shadow:0 1px 3px rgba(16,18,32,.08);border-radius:8px;overflow:hidden}}
  th{{background:#5b5bd6;color:#fff;padding:11px 10px;text-align:left;font-size:12px;letter-spacing:.3px}}
  td{{padding:9px 10px;border-bottom:1px solid #e6e6eb;font-size:13px}}
  tr:hover td{{background:#f4f4f7}}
  .pos{{color:#d1242f;font-weight:600}}
  img{{max-width:100%;border:1px solid #e6e6eb;border-radius:10px;margin:14px 0;box-shadow:0 4px 16px rgba(16,18,32,.08)}}
  .card{{background:#fff;border:1px solid #e6e6eb;border-radius:12px;padding:20px 22px;margin:16px 0;box-shadow:0 1px 3px rgba(16,18,32,.06)}}
  .warning{{background:#fff8e8;border-left:5px solid #f5a623;padding:16px 20px;border-radius:8px;margin:16px 0;font-size:14px}}
  .key{{background:#f0f0ff;border-left:5px solid #5b5bd6;padding:16px 20px;border-radius:8px;margin:16px 0;font-size:15px}}
  small{{color:#6b6b7b}}
  .tag{{display:inline-block;background:#5b5bd6;color:#fff;padding:3px 11px;border-radius:11px;font-size:11px;margin:3px 4px 3px 0}}
  .meta{{color:#6b6b7b;font-size:13px;margin-top:-8px;margin-bottom:24px}}
</style></head>
<body>
<h1>黑马起步策略 · 深度分析报告</h1>
<div class="meta">日线 / 全A 5534只 / 2019-06-01 ~ 2026-07-10 · 22329笔交易 · 生成于 2026-07-10</div>

<div class="key"><b>🎯 一句话结论：</b>黑马起步的"小盘效应"<b>方向真实但强度脆弱</b>——由 <span class="tag">2019-2021小盘牛市</span><span class="tag">幸存者偏差</span><span class="tag">暴涨尾部</span> 三者放大；近3年(2023-2026)几乎消失。本质是"中小盘右尾更肥"的<b>历史描述，非稳定可交易alpha</b>。</div>

<h2>1️⃣ 四池对比：小仓位 vs 大仓位</h2>
<p><small>小仓位每笔2千-2万；大仓位每笔5万-20万。收益鸿沟(全A/沪深300) 从12倍→11倍<b>基本不变</b> → 不是纯复利，是 <b>笔数多(16倍) × 小盘单笔略高 × 复利</b> 三者复合。大仓位下回撤才真实(-10%~-12%)。</small></p>
{tbl4}

<h2>2️⃣ 小盘效应：流通市值细分桶</h2>
<img src="final_bucket.png">
{tblb}
<p><small>微盘&lt;30亿暴涨概率 <b>2.09%</b> 是大盘&gt;300亿 <b>0.27%</b> 的 <b>7.7倍</b>；但中位数各桶几乎一样(~6%)——差异在<b>尾部暴涨概率</b>，不在每笔赚多少。盈亏比仅微盘>1(1.05)。</small></p>

<h2>3️⃣ 核心发现：小盘溢价随时间衰减</h2>
<img src="final_decay.png">
{tbl_decay}
<p><small>溢价从 +10.4pp 衰减到 +0.5pp，<b>缩水95%</b>。2023-2024暴涨甚至微反转(大盘0.19%&gt;小盘0.15%)——小盘效应主要是<b>2019-2020小盘牛市的产物</b>，近3年基本消失。</small></p>

<h2>4️⃣ 暴涨股 / 王牌股画像</h2>
<div class="card">
<b>暴涨股(240只, 出现过单笔&gt;50%)：</b> 小盘(流通市值中位<b>41.7亿</b>) + 低价(中位<b>14.9元</b>) + 超短持有(<b>3.7天</b>) + 止盈退出(87%) + 平均单笔29.3% + 胜率88%<br><br>
<b>王牌股(463只, 平均收益&gt;10%)：</b> 更微盘(中位<b>35.6亿</b>) + 低价(16.2元) + 持有<b>2.8天</b> + 胜率中位<b>100%</b> + 平均22.1%<br><br>
<b>行业(不挑行业挑形态)：</b> 化学制品 · 汽车零部件 · 软件服务 · 光学光电 · 通用设备 · 半导体 · 专用设备 · 电网设备<br><br>
<b>代表股：</b> 603290(+218%) · 003031(+132%) · 601279(+100%) · 603176(+100%) · 600956(+98%)
</div>
<img src="bucket_box.png">

<h2>5️⃣ 多 Agent 对抗验证（4角度交叉印证）</h2>
{tbl_agent}
<p><small>两组agent<b>独立撞上同一发现</b>：① 代理偏差+稳健性 都指向"近3年消失" ② 统计+稳健性 都确认"靠尾部暴涨撑"(剔除后优势缩水60-70%)。结论收敛。</small></p>

<h2>6️⃣ 实盘警示</h2>
<div class="warning">
⚠️ 别把回测的"全A +705% / 创业板 +281%"当未来收益——含幸存者偏差(打0.5-2折) + 2019-2021牛市红利<br><br>
⚠️ 近3年(2023-2026)小盘超额几乎归零，现在上车小盘<b>没有历史显示的那种优势</b><br><br>
⚠️ 大仓位下回撤才真实(-10%~-12%)，小仓位的-2%~-6%是仓位假象<br><br>
⚠️ 市值代理用最新股本×历史价(取不到历史股本)，对增发解禁股有5-13%偏差
</div>

<p><small>数据源：TDX get_stock_info 流通股本×入场价 · 22329笔(全A有市值) · 多agent验证脚本 _audit_mcap_*.py / _mcap_robustness.py</small></p>
</body></html>"""

out = OUT / 'heima_report.html'
out.write_text(html, encoding='utf-8')
print(f"报告已生成: {out}", flush=True)
print(f"用浏览器打开: e:/1target/VERA/{out.as_posix()}", flush=True)
