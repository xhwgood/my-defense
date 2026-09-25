# -*- coding: utf-8 -*-
"""
前台鼠标自动化：找到“小游戏”窗口，把窗口左上角对齐到屏幕 (0,0)，
之后用真实鼠标按“游戏逻辑坐标”点击（前台模式）。

重要：显示器可能开启了 DPI 缩放（本项目实测 150%）。游戏的逻辑分辨率是
568x1067，而屏幕物理像素是 852x1601。脚本会自动检测缩放比例，把逻辑坐标
换算成物理像素后再移动鼠标，所以你只需按 568x1067 的坐标系给坐标即可。

用法：
    python find_and_click.py            # 找窗口 + 对齐到 (0,0)
    python find_and_click.py 420 810    # 找窗口 + 对齐 + 点击逻辑坐标 (420,810)

依赖：仅 Windows 自带的 user32.dll（ctypes），无需第三方库。
"""

import ctypes
import time
import sys
from ctypes import wintypes

# 修复 Windows 控制台中文乱码（Git Bash / UTF-8 终端下输出正常中文）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---- Win32 常量 ----
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SW_RESTORE = 9
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
VK_MENU = 0x12  # Alt 键，SetForegroundWindow 失败时兜底
KEYEVENTF_KEYUP = 0x0002

user32 = ctypes.windll.user32
EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

# 显式声明参数类型，避免 64 位下句柄/坐标被截断
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wintypes.UINT]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                               wintypes.DWORD, ctypes.c_ulong]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.GetDpiForWindow.argtypes = [wintypes.HWND]
user32.GetDpiForWindow.restype = wintypes.UINT


def find_windows(keyword):
    """枚举所有可见的顶层窗口，返回标题包含 keyword 的 (句柄, 标题) 列表。"""
    found = []

    def callback(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            if title and keyword.lower() in title.lower():
                found.append((hwnd, title))
        return True

    user32.EnumWindows(EnumProc(callback), 0)
    return found


def list_all_windows():
    """列出所有可见窗口标题，用于排查窗口名。"""
    titles = []

    def callback(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value:
                titles.append((hwnd, buf.value))
        return True

    user32.EnumWindows(EnumProc(callback), 0)
    return titles


def get_scale(hwnd):
    """返回窗口 DPI 缩放比例（物理像素 / 逻辑像素），如 150% 则返回 1.5。"""
    try:
        dpi = user32.GetDpiForWindow(hwnd)
        if dpi and dpi != 96:
            return dpi / 96.0
    except Exception:
        pass
    return 1.0


def move_to_origin(hwnd, scale):
    """把窗口左上角移动到屏幕 (0,0)，保持大小和 z 序不变。"""
    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOZORDER)
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    pw, ph = r.right - r.left, r.bottom - r.top
    print(f"  窗口位置: 左上角 ({r.left}, {r.top})")
    print(f"  物理像素: {pw}x{ph}   逻辑像素: {round(pw/scale)}x{round(ph/scale)}")
    print(f"  DPI 缩放: {scale:.1%}")


def bring_to_foreground(hwnd):
    """把窗口切到前台（最小化则先还原），失败时用 Alt 键兜底。"""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.3)
    if not user32.SetForegroundWindow(hwnd):
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)


def foreground_click(hwnd, x, y, scale):
    """前台真实鼠标点击。x, y 为游戏逻辑坐标，内部换算成物理像素。"""
    bring_to_foreground(hwnd)
    px = round(x * scale)
    py = round(y * scale)
    print(f"  逻辑坐标 ({x}, {y}) -> 物理像素 ({px}, {py})")
    user32.SetCursorPos(px, py)
    time.sleep(0.1)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.08)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def main():
    # 让本进程 DPI 感知，保证窗口坐标与屏幕物理坐标一致
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

    keyword = "小游戏"

    windows = find_windows(keyword)
    if not windows:
        print(f'[!] 未找到标题包含 “{keyword}” 的窗口。')
        print("当前可见窗口列表（请检查游戏窗口的实际标题）：")
        for hwnd, title in list_all_windows():
            print(f"  {hwnd:#010x}  {title}")
        sys.exit(1)

    hwnd, title = windows[0]
    print(f'[+] 找到窗口: {title} (句柄 {hwnd:#010x})')

    scale = get_scale(hwnd)
    move_to_origin(hwnd, scale)
    print("[+] 窗口已对齐到屏幕 (0,0)")

    # 若命令行给了 x y 参数，则顺手点击一次
    if len(sys.argv) >= 3:
        x, y = int(sys.argv[1]), int(sys.argv[2])
        print(f"[*] 前台点击逻辑坐标 ({x}, {y}) ...")
        foreground_click(hwnd, x, y, scale)
        print("[+] 点击完成。")


if __name__ == "__main__":
    main()
