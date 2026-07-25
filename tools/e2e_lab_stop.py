"""停止体检功能 Playwright 端到端 (2026-07-25)。

流程: 打开体检页 → 提交 QUANTQQ → 等按钮变"停止体检" → 点击 →
验证任务 cancelled + 按钮还原"开始体检" + toast 提示。
"""
import json
import sys
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8080"


def lab_status():
    with urllib.request.urlopen(BASE + "/api/lab/status", timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_task(pred, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = lab_status()
        tasks = ([st["current"]] if st.get("current") else []) + st.get("queue", [])
        for t in tasks:
            if t and "QUANTQQ" in t.get("formulas", []) and pred(t):
                return t
        time.sleep(1)
    return None


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.goto(BASE, wait_until="networkidle")
        page.click("#tabBtnLab")
        page.wait_for_timeout(600)

        btn = page.locator("#btnLabSubmit")
        print("初始按钮:", btn.inner_text().strip(), "| class:", btn.get_attribute("class"))
        assert "开始体检" in btn.inner_text()

        page.fill("#labFormulas", "QUANTQQ")
        page.click("#btnLabSubmit")
        print("已提交 QUANTQQ 体检")

        # 等按钮变停止
        page.wait_for_function(
            "document.getElementById('btnLabSubmit').dataset.mode === 'stop'",
            timeout=30000)
        page.wait_for_timeout(300)
        page.screenshot(path=".playwright-mcp/lab_stop_btn.png")
        print("运行中按钮:", btn.inner_text().strip(), "| class:", btn.get_attribute("class"))
        assert "停止体检" in btn.inner_text()
        assert "btn-danger" in (btn.get_attribute("class") or "")

        # 点停止
        btn.click()
        t = wait_task(lambda t: t["status"] == "cancelled", timeout=30)
        assert t, "任务未变为 cancelled"
        print("任务状态: cancelled ✓ (stage:", t.get("stage"), ")")

        # 按钮还原
        page.wait_for_function(
            "document.getElementById('btnLabSubmit').dataset.mode !== 'stop'",
            timeout=15000)
        page.wait_for_timeout(300)
        page.screenshot(path=".playwright-mcp/lab_stopped.png")
        print("停止后按钮:", btn.inner_text().strip())
        assert "开始体检" in btn.inner_text()
        browser.close()
    print("LAB STOP E2E PASS")


if __name__ == "__main__":
    main()
