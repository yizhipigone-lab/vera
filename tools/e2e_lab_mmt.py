"""体检修复 Playwright 端到端验证 (2026-07-25): UI 提交 MMT, 盯到 S0 通过。

用法: python tools/e2e_lab_mmt.py
"""
import json
import time
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8080"


def lab_status():
    with urllib.request.urlopen(BASE + "/api/lab/status", timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.goto(BASE, wait_until="networkidle")
        page.click("#tabBtnLab")
        page.wait_for_timeout(500)
        page.fill("#labFormulas", "MMT")
        page.click("#btnLabSubmit")
        print("已提交 MMT 体检 (默认 近1年 + 近3年)")
        page.wait_for_timeout(1500)
        page.screenshot(path=".playwright-mcp/lab_submit.png")

        last_stage = None
        t0 = time.time()
        s0_passed = False
        while time.time() - t0 < 1500:  # 最长盯 25 分钟
            time.sleep(5)
            try:
                st = lab_status()
            except Exception:
                continue
            tasks = ([st.get("current")] if st.get("current") else []) + st.get("queue", [])
            mmt = next((t for t in tasks if t and "MMT" in t.get("formulas", [])), None)
            if not mmt:
                continue
            stage = f'{mmt["status"]}|{mmt.get("stage", "")}'
            if stage != last_stage:
                print(f'[{time.time()-t0:6.0f}s] {stage}  {mmt.get("error", "")[:120]}')
                last_stage = stage
            if mmt["status"] == "failed":
                print("RESULT: FAILED —", mmt.get("error", "")[:300])
                break
            if "S0" in str(mmt.get("stage", "")) and "FAIL" not in str(mmt.get("error", "")):
                s0_passed = True
            stage_s = str(mmt.get("stage", ""))
            if any(k in stage_s for k in ("S2", "S3", "S4", "S5")):
                if not s0_passed:
                    s0_passed = True
                    print(">>> S0 已通过 (进入 IC 阶段)")
            if mmt["status"] == "done":
                print("RESULT: DONE 全部完成")
                break
        page.screenshot(path=".playwright-mcp/lab_final.png")
        browser.close()
        print("S0 通过证据:" , "YES" if s0_passed else "NO")


if __name__ == "__main__":
    main()
