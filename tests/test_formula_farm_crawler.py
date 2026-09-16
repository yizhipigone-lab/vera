# -*- coding: utf-8 -*-
"""公式农场 v1 爬虫 — 离线单元测试(纯 helper, 不联网)。

fixture 为固定 HTML 片段; 网页结构/验证码墙/说明文字混入三风险都覆盖。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools.formula_farm import crawl_gupang  # noqa: E402

# ---- 纯选股文章(meta 行后直接是源码) ----
PAGE_SELECT = """<html><head><title>通达信资金天机选股指标公式-通达信公式-股旁网</title></head>
<body><div>当前位置: 股旁网 &gt; 通达信公式</div>
<div class="content">
通达信资金天机选股指标公式<br>
来源：Internet，编辑：股旁网，2026-09-04<br>
N:=20;<br>
主力成本:=MA(C,N);<br>
XG:N AND C&gt;主力成本;<br>
</div>
<div>相关文章</div><div>xxx之选股指标公式</div></body></html>"""

# ---- 验证码墙(选股部分被隐藏) ----
PAGE_WALL = """<html><head><title>某主图指标公式-通达信公式-股旁网</title></head>
<body><div class="content">
本文指标含有选股公式如下：此处内容已被隐藏，请输入验证码查看内容。<br>
STICKLINE(C&gt;O,H,L,0,0),COLORRED;<br>
</div></body></html>"""


def test_is_code_line():
    assert crawl_gupang.is_code_line("N:=20;")
    assert crawl_gupang.is_code_line("主力成本:=MA(C,N);")
    assert crawl_gupang.is_code_line("XG:crOSS(C,主力成本);")
    assert crawl_gupang.is_code_line("STICKLINE(C>O,H,L,0,0),COLORRED;")
    # 说明性文字不混入
    assert not crawl_gupang.is_code_line("该指标用于捕捉主力吸筹完毕后的启动点。")
    assert not crawl_gupang.is_code_line("用法：当红柱出现时关注次日走势")


def test_extract_select_page():
    rec = crawl_gupang.extract_article("https://www.gupang.com/202609/99999.html",
                                       PAGE_SELECT)
    assert rec["id"] == 99999
    assert rec["date"] == "2026-09-04"
    assert rec["title"].startswith("通达信资金天机选股指标公式")
    assert rec["wall"] is False
    assert "主力成本:=MA(C,N);" in rec["code"]
    assert "XG:N AND C>主力成本;" in rec["code"]
    # 说明性/导航文字不进 code
    assert "相关文章" not in rec["code"]
    assert "当前位置" not in rec["code"]
    # html 实体还原(&gt; -> >)
    assert "C>主力成本" in rec["code"]


def test_extract_wall_page():
    rec = crawl_gupang.extract_article("https://www.gupang.com/202609/99998.html",
                                       PAGE_WALL)
    assert rec["wall"] is True          # 验证码墙 → 应跳过, 不给可误用的"源码"
    assert rec["code"] == ""            # 隐藏内容不进入 code


def test_list_links():
    lst = crawl_gupang.parse_list_links(PAGE_SELECT, "https://www.gupang.com/xuangu/")
    # 上述 page 是详情样例; 用详情页里嵌的链接构造最小列表再验
    mini = ('<a href="https://www.gupang.com/202609/70670.html">通达信高量异突破主图指标公式</a>'
            '<a href="https://www.gupang.com/202609/70667.html">通达信资金天机选股指标公式</a>')
    arts = crawl_gupang.parse_list_links(mini, "https://www.gupang.com/xuangu/")
    assert [a["id"] for a in arts] == [70670, 70667]
    assert arts[0]["title"] == "通达信高量异突破主图指标公式"
