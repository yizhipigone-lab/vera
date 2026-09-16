# -*- coding: utf-8 -*-
"""gui_onboard: pywinauto 控件路径驱动 TDX 公式管理器上架/删除公式(v1, 实测成功)。

2026-09-06 实测定论:
  - 公式管理器 V6.06 是标准 Win32 对话框(#32770), 树=SysTreeView32, 按钮全部可达;
  - 工具栏按钮要用 click_input(真实鼠标点控件矩形中心), 消息 click 在窗口被压时不稳定;
  - click_input 前必须 force_foreground(AttachThreadInput), 否则点到覆盖窗口;
  - 编辑器代码区是自绘 Static, 只能"click_input 聚焦 + Ctrl+A + Ctrl+V"粘贴;
  - **源码必须 CRLF 换行**(LF 会被显示成 ■);
  - 删除确认弹窗标题 TCalc64, 按钮『是(&Y)』(用 startswith('是') 匹配);
  - 删除后树里留"已删除"影子节点, 管理器重开即消失(无害)。

与 image-based-gui-automation 技能关系: 保留其 force_foreground/DPI 心法,
但本模块走控件路径(更稳), 图像匹配仅留作后备。
"""
import argparse
import ctypes
import os
import sys
import time
from ctypes import wintypes

import pyautogui
import pyperclip
from pywinauto import Application, Desktop

HERE = os.path.dirname(os.path.abspath(__file__))
SHOT_DIR = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                        "scratch", "formula_farm_v0", "gui_captures")
MGR_TITLE = "公式管理器V6.06"
EDITOR_TITLE = "条件选股公式编辑器"
TREE_PATH = ["条件选股公式", "其他类型"]


def log(s):
    print(s, flush=True)


def set_dpi():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def force_foreground(hwnd):
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    HWND, DWORD, BOOL = wintypes.HWND, wintypes.DWORD, wintypes.BOOL
    u.GetForegroundWindow.restype = HWND
    u.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(DWORD)]
    u.GetWindowThreadProcessId.restype = DWORD
    k.GetCurrentThreadId.restype = DWORD
    u.AttachThreadInput.argtypes = [DWORD, DWORD, BOOL]
    u.AttachThreadInput.restype = BOOL
    u.ShowWindow.argtypes = [HWND, ctypes.c_int]
    u.ShowWindow.restype = BOOL
    u.BringWindowToTop.argtypes = [HWND]
    u.BringWindowToTop.restype = BOOL
    u.SetForegroundWindow.argtypes = [HWND]
    u.SetForegroundWindow.restype = BOOL
    u.IsIconic.argtypes = [HWND]
    u.IsIconic.restype = BOOL
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, 9)
    fg = u.GetForegroundWindow()
    cur_tid, fg_tid = k.GetCurrentThreadId(), u.GetWindowThreadProcessId(fg, None)
    att = bool(fg_tid and fg_tid != cur_tid and u.AttachThreadInput(cur_tid, fg_tid, True))
    try:
        u.BringWindowToTop(hwnd)
        u.SetForegroundWindow(hwnd)
    finally:
        if att:
            u.AttachThreadInput(cur_tid, fg_tid, False)
    time.sleep(0.4)


def _find_window(title):
    for w in Desktop(backend="win32").windows():
        try:
            if w.window_text() == title:
                return w
        except Exception:
            pass
    return None


def keep_awake():
    """批量上架防休眠(TDXDLDATA 同款): 禁止系统/显示器睡眠直到进程结束。"""
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000003)  # CONTINUOUS|SYSTEM|DISPLAY
    except Exception:
        pass


def _dismiss_stray_dialogs():
    """收掉上架过程中的意外弹窗(错误提示/询问框)。

    排除: 公式管理器/编辑器/主窗口。按钮偏好: 取消 > 否 > 关闭 > 确定(错误框)。
    """
    for w in Desktop(backend="win32").windows():
        try:
            t = w.window_text() or ""
            if w.class_name() != "#32770" or t in (MGR_TITLE, EDITOR_TITLE) \
                    or MGR_TITLE[:4] in t or EDITOR_TITLE[:4] in t or "通达信金融终端" in t:
                continue
            if not w.is_visible():
                continue
            aw = Application(backend="win32").connect(handle=w.handle).window(handle=w.handle)
            for want in ("取  消", "否(&N)", "否", "关  闭", "确  定", "确定"):
                hit = False
                for b in aw.children(class_name="Button"):
                    if (b.window_text() or "").replace(" ", "") == want.replace(" ", ""):
                        force_foreground(w.handle)
                        b.click_input()
                        log("   🧹 收掉弹窗『%s』(点 %s)" % (t or w.class_name(), want))
                        hit = True
                        break
                if hit:
                    break
        except Exception:
            pass


def _editor(timeout=8.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        w = _find_window(EDITOR_TITLE)
        if w:
            return Application(backend="win32").connect(handle=w.handle).window(handle=w.handle)
        time.sleep(0.4)
    return None


def _shot(tag):
    os.makedirs(SHOT_DIR, exist_ok=True)
    p = os.path.join(SHOT_DIR, tag + ".png")
    pyautogui.screenshot().save(p)
    return p


def _manager():
    app = Application(backend="win32").connect(title=MGR_TITLE)
    win = app.window(title=MGR_TITLE)
    force_foreground(win.handle)
    return win


def _click_button(win, want_text, timeout=6):
    t0 = time.time()
    while time.time() - t0 < timeout:
        for b in win.children(class_name="Button"):
            if (b.window_text() or "").replace(" ", "") == want_text.replace(" ", ""):
                b.click_input()
                return True
        time.sleep(0.3)
    return False


def add_formula(name, code, desc=""):
    """新建+粘贴+确定。返回 (ok, 信息)。name 建议短 ASCII 或中文短名。

    批量化加固(2026-09-06): keep_awake 防休眠; 前后收意外弹窗;
    点确定后编辑器不收 = 编译失败 → 自动点『取消』收场, 不堵下一条。
    """
    set_dpi()
    keep_awake()
    code = code.replace("\r\n", "\n").replace("\n", "\r\n")  # CRLF 铁律
    _dismiss_stray_dialogs()
    win = _manager()
    tv = win.child_window(title="Tree1", class_name="SysTreeView32")
    tv.get_item(TREE_PATH).select()
    time.sleep(0.5)
    _click_button(win, "新  建")
    ed = _editor()
    if not ed:
        _shot("add_fail_no_editor")
        return False, "编辑器没弹出来(已截图 add_fail_no_editor)"
    ed.child_window(class_name="Edit", found_index=0).set_edit_text(name)
    if desc:
        ed.child_window(class_name="Edit", found_index=2).set_edit_text(desc)
    # 代码区=最大的 Static(自绘区), 控件矩形中心聚焦
    statics = [s for s in ed.children(class_name="Static")]
    big = max(statics, key=lambda s: s.rectangle().width() * s.rectangle().height())
    big.click_input()
    time.sleep(0.3)
    pyperclip.copy(code)
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.15)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.4)
    _shot("add_before_ok_%s" % name)
    _click_button(ed, "确  定")
    time.sleep(1.2)
    if _editor(timeout=2.5) is not None:
        _shot("add_fail_still_open_%s" % name)
        _dismiss_stray_dialogs()
        # 编辑器还开着 → 编译失败, 点『取消』收场, 不堵下一条
        ed2 = _editor(timeout=2)
        if ed2 is not None:
            _click_button(ed2, "取  消")
            time.sleep(0.8)
        if _editor(timeout=2) is not None:
            return False, "编译失败且取消也关不掉编辑器(截图 add_fail_still_open_%s)" % name
        return False, "编译失败(已自动取消, 截图 add_fail_still_open_%s)" % name
    return True, "上架成功(编辑器已关闭)"


def find_node(name):
    win = _manager()
    tv = win.child_window(title="Tree1", class_name="SysTreeView32")
    parent = tv.get_item(TREE_PATH)
    for k in parent.children():
        if name in (k.text() or ""):
            return k
    return None


def delete_formula(name):
    """删除公式(只删自己的, 按名称匹配)。返回 (ok, 信息)。"""
    set_dpi()
    node = find_node(name)
    if not node:
        return False, "树里没找到 %s" % name
    node.select()
    time.sleep(0.5)
    win = _manager()
    _click_button(win, "删  除")
    time.sleep(0.8)
    # 确认弹窗 TCalc64 → 『是(&Y)』
    dlg = _find_window("TCalc64")
    if dlg:
        force_foreground(dlg.handle)
        aw = Application(backend="win32").connect(handle=dlg.handle).window(handle=dlg.handle)
        hit = False
        for b in aw.children(class_name="Button"):
            if (b.window_text() or "").startswith("是"):
                b.click_input()
                hit = True
                break
        if not hit:
            pyautogui.hotkey("alt", "y")
        time.sleep(1.0)
        log("删除确认已点")
    # 复核: 影子节点(已删除标记)可能被枚举到, 用截图留痕由人/视觉复核
    still = find_node(name)
    _shot("after_delete_%s" % name)
    if still:
        return True, "已执行删除(树里可能留'已删除'影子节点, 管理器重开消失; 截图 after_delete_%s.png)" % name
    return True, "删除并复核通过"


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    a1 = sub.add_parser("add-one")
    a1.add_argument("--name", required=True)
    a1.add_argument("--code-file", required=True)
    a1.add_argument("--desc", default="")
    d1 = sub.add_parser("delete")
    d1.add_argument("--name", required=True)
    args = ap.parse_args()
    if args.cmd == "add-one":
        code = open(args.code_file, encoding="utf-8").read().strip()
        ok, msg = add_formula(args.name, code, args.desc)
        log(("✓ " if ok else "✗ ") + msg)
        raise SystemExit(0 if ok else 1)
    elif args.cmd == "delete":
        ok, msg = delete_formula(args.name)
        log(("✓ " if ok else "✗ ") + msg)
        raise SystemExit(0 if ok else 1)
    ap.print_help()


if __name__ == "__main__":
    main()
