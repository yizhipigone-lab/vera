"""交易明细分页 e2e (2026-07-26): 加载 9.2 万笔历史回测, 验证不再 OOM。

流程: 打开页面 → 历史回测选 20260726_003543 (91934 笔, 32.7MB) →
等渲染 → 断言表格 200 行/页码条 460 页 → 翻页 → 筛选重置页码 → JS 堆内存采样。
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8080"
RESULT_ID = "20260726_003543"   # 91934 笔, 32.7MB
EXPECT_PAGES = (91934 + 199) // 200


def heap_mb(page):
    return page.evaluate("performance.memory ? performance.memory.usedJSHeapSize/1048576 : -1")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)[:200]))
        page.goto(BASE, wait_until="networkidle")

        # 历史下拉填充后选大结果
        page.wait_for_function(
            "document.getElementById('historySelect').options.length > 1", timeout=30000)
        page.select_option("#historySelect", RESULT_ID)
        print("已选择历史回测:", RESULT_ID)

        # 等交易表渲染 (大 JSON 拉取+渲染需要几秒)
        page.wait_for_function(
            "document.getElementById('tradeTableBody').children.length > 0",
            timeout=120000)
        rows = page.locator("#tradeTableBody tr").count()
        pager = page.locator("#tradePager").inner_text()
        heap1 = heap_mb(page)
        print(f"渲染完成: 表格 {rows} 行 | 页码条: {pager.strip()} | JS堆 {heap1:.0f}MB")
        assert rows == 200, f"应渲染 200 行, 实际 {rows}"
        assert f"第 1 / {EXPECT_PAGES} 页" in pager, f"页码条异常: {pager}"

        # 翻页
        page.click("#tradePageNext")
        page.wait_for_timeout(500)
        pager2 = page.locator("#tradePager").inner_text()
        first_no = page.locator("#tradeTableBody tr td").first.inner_text()
        print(f"翻页后: {pager2.strip()} | 首行序号 {first_no}")
        assert f"第 2 / {EXPECT_PAGES} 页" in pager2
        assert first_no == str(91934 - 200)

        # 筛选 → 回第一页
        page.select_option("#tradeFilter", "win")
        page.wait_for_timeout(600)
        pager3 = page.locator("#tradePager").inner_text()
        rows3 = page.locator("#tradeTableBody tr").count()
        heap2 = heap_mb(page)
        print(f"仅盈利筛选: {pager3.strip()} | {rows3} 行 | JS堆 {heap2:.0f}MB")
        assert "第 1 /" in pager3
        assert rows3 <= 200

        page.screenshot(path=".playwright-mcp/trade_pager.png")
        browser.close()
        assert not errors, f"页面 JS 错误: {errors}"
        print("TRADE PAGER E2E PASS (未崩溃, 页码/翻页/筛选正常)")


if __name__ == "__main__":
    main()
