# -*- coding: utf-8 -*-
"""
auto_battle.py —— “我的防线”自动化战斗脚本。

流程：
  1. 找到“小游戏”窗口，把左上角对齐到屏幕 (0,0)；
  2. 进入主循环：每秒对游戏画面找色，判断当前场景，再按场景执行对应逻辑；
  3. 运行中随时按 F5 键可立即停止脚本。

当前已实现的场景检测规则（坐标 = 窗口内物理像素；窗口对齐到 (0,0) 后即屏幕坐标）：
  开始界面:   (303, 1299) 颜色 == #4DBDA5  → 点击该坐标（开始按钮）
  选择卡牌:   (357, 297)  颜色 == #7BFFE3  → 在 (45,758)-(812,924) 内按顺序找图并点击
  挑战失败:   (453, 401)  颜色 == #BAC0D2  → 点击 (434, 1250)
  额外机会:   (408, 234)  颜色 == #7BFFE3  → 点击 (159,795)，再按 (515,1437) 色值分支点击

用法：
    python auto_battle.py            # 运行检测循环（按 F5 停止）
    python auto_battle.py --probe    # 单次探测：打印规则色值 + 找图匹配分（不点击）
    python auto_battle.py --cut x1 y1 x2 y2 输出.png   # 抓当前屏幕裁一块模板图

依赖：ctypes（Windows 自带）+ numpy + opencv-python（找图）。
"""

import ctypes
import sys
import time
from ctypes import wintypes

import cv2
import numpy as np

# 修复 Windows 控制台中文乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---- Win32 常量 ----
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SW_RESTORE = 9
PW_RENDERFULLCONTENT = 0x00000002
DIB_RGB_COLORS = 0
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
VK_MENU = 0x12  # Alt 键，SetForegroundWindow 失败时兜底
KEYEVENTF_KEYUP = 0x0002
VK_F5 = 0x74  # 按 F5 立即停止脚本

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wintypes.UINT]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                               wintypes.DWORD, ctypes.c_ulong]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short

EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


# ---- 场景定义 ----
SCENE_UNKNOWN = "未知"
SCENE_START = "开始界面"
SCENE_CHOOSE_CARD = "选择卡牌"
SCENE_FAILED = "挑战失败"
SCENE_EXTRA = "额外机会"
SCENE_CLEAR = "通关"

# 开始按钮坐标（物理像素，窗口已对齐到屏幕 (0,0)）
START_BTN = (303, 1299)

# 挑战失败场景
FAILED_POINT = (453, 401)
FAILED_COLOR = (0xBA, 0xC0, 0xD2)               # #BAC0D2
FAILED_CLICK = (434, 1250)

# 额外机会场景
EXTRA_POINT = (408, 234)
EXTRA_COLOR = (0x7B, 0xFF, 0xE3)                # #7BFFE3
EXTRA_CLICK_FIRST = (159, 795)                  # 第一步点击
EXTRA_CHECK_POINT = (515, 1437)                 # 第二步判断坐标
EXTRA_CHECK_COLOR = (0xCF, 0xB9, 0x5A)          # #CFB95A
EXTRA_CLICK_ALT = (423, 786)                    # 未命中时的替代点击

# 选择卡牌/等级提升场景
CHOOSE_CARD_POINT = (357, 297)
CHOOSE_CARD_COLOR = (0x7B, 0xFF, 0xE3)          # #7BFFE3
CARD_SEARCH_BOX = (45, 758, 812, 924)           # 找图范围 (x1, y1, x2, y2)
CARD_IMAGE_LIST = [                             # 按顺序查找
    r"find_pic\蜂巢-组合.png",
    r"find_pic\蜂巢.png",
    r"find_pic\蜂巢-进阶.png",
    r"find_pic\干扰.png",
    r"find_pic\干扰-进阶.png",
    r"find_pic\电磁炮.png",
    r"find_pic\电磁炮-进阶.png",
]
CARD_FALLBACK_CLICK = (150, 840)                # 都找不到时的兜底点击
MATCH_THRESHOLD = 0.85                          # 模板匹配阈值

# 通关场景
CLEAR_POINT = (216, 300)
CLEAR_COLOR = (0x96, 0x4D, 0x1D)                 # #964D1D
CLEAR_CLICK = (294, 1281)

# 场景识别规则：(场景名, x, y, RGB 元组)。后续新增场景往这里加。
RULES = [
    (SCENE_START, START_BTN[0], START_BTN[1], (0x4D, 0xBD, 0xA5)),       # #4DBDA5
    (SCENE_CHOOSE_CARD, CHOOSE_CARD_POINT[0], CHOOSE_CARD_POINT[1], CHOOSE_CARD_COLOR),
    (SCENE_FAILED, FAILED_POINT[0], FAILED_POINT[1], FAILED_COLOR),
    (SCENE_EXTRA, EXTRA_POINT[0], EXTRA_POINT[1], EXTRA_COLOR),
    (SCENE_CLEAR, CLEAR_POINT[0], CLEAR_POINT[1], CLEAR_COLOR),
]


# ---- 基础工具 ----
def _set_dpi_aware():
    """让本进程 DPI 感知，保证截图尺寸与屏幕物理像素一致。"""
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def find_window(keyword):
    """枚举所有可见顶层窗口，返回标题包含 keyword 的第一个句柄，找不到返回 None。"""
    found = []

    def callback(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value and keyword in buf.value:
                found.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(callback), 0)
    return found[0] if found else None


def move_to_origin(hwnd):
    """把窗口左上角移动到屏幕 (0,0)，保持大小和 z 序不变。返回移动后的 (left, top)。"""
    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOZORDER)
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top


_stop = False


def check_f5():
    """查询 F5 是否被按下；按下则置位停止标记 _stop（含按下沿与按住）。"""
    global _stop
    if (user32.GetAsyncKeyState(VK_F5) & 0x8001) != 0:
        _stop = True


def sleep_check(seconds):
    """分片休眠（每 50ms 检查一次 F5），期间按下 F5 则打印提示并返回 True。"""
    steps = max(1, int(seconds / 0.05))
    for _ in range(steps):
        check_f5()
        if _stop:
            print("[!] 检测到 F5，停止脚本")
            return True
        time.sleep(0.05)
    return False


def wait_scene_change(hwnd, from_scene, timeout=5.0):
    """快速轮询（每 0.1 秒），直到画面离开 from_scene（转场动画）或超时。
    返回离开后的场景名；超时或按 F5 停止则返回 from_scene。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        check_f5()
        if _stop:
            return from_scene
        cap = capture_window(hwnd)
        if cap is None:
            time.sleep(0.1)
            continue
        w, h, buf = cap
        s = detect_scene(buf, w, h)
        if s != from_scene:
            return s
        time.sleep(0.1)
    return from_scene


def capture_window(hwnd):
    """后台截图，返回 (宽, 高, BGRA 字节串)，失败返回 None。"""
    if user32.IsIconic(hwnd):
        return None

    rect = RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None

    hdc = user32.GetDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(hdc_mem, bmp)

    ok = user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)

    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth = w
    bmi.biHeight = -h  # top-down
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0  # BI_RGB

    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(hdc_mem, bmp, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(hwnd, hdc)

    if not ok:
        return None
    return w, h, buf.raw


def cut_template(hwnd, x1, y1, x2, y2, out_path):
    """抓当前屏幕，把 [x1,y1,x2,y2] 区域裁出来存成模板图（支持中文路径）。"""
    cap = capture_window(hwnd)
    if cap is None:
        print("[!] 截图失败（窗口可能被最小化）")
        return
    w, h, buf = cap
    img = np.frombuffer(buf, np.uint8).reshape(h, w, 4)[:, :, :3]
    crop = img[y1:y2, x1:x2]
    ok, data = cv2.imencode(".png", crop)
    if not ok:
        print("[!] 编码失败")
        return
    with open(out_path, "wb") as f:
        f.write(data.tobytes())
    print(f"[+] 已保存模板 {out_path}  尺寸 {crop.shape[1]}x{crop.shape[0]}")


def pixel_color(buf, w, x, y):
    """读取截图缓冲区内 (x, y) 的像素，返回 (R, G, B) 元组。"""
    off = (y * w + x) * 4
    return buf[off + 2], buf[off + 1], buf[off]  # BGRA -> RGB


def color_at(hwnd, x, y):
    """重新截图，读取窗口内 (x, y) 的像素色值，返回 (R,G,B) 或 None。"""
    cap = capture_window(hwnd)
    if cap is None:
        return None
    w, h, buf = cap
    if not (0 <= x < w and 0 <= y < h):
        return None
    return pixel_color(buf, w, x, y)


def detect_scene(buf, w, h):
    """按规则表逐条找色，命中则返回场景名，否则返回“未知”。"""
    for scene, x, y, rgb in RULES:
        if 0 <= x < w and 0 <= y < h and pixel_color(buf, w, x, y) == rgb:
            return scene
    return SCENE_UNKNOWN


# ---- 找图 ----
def imread_unicode(path):
    """读取图片（支持中文路径），返回 BGR numpy 数组；失败返回 None。"""
    try:
        data = np.fromfile(path, dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def find_image(buf, w, h, tpl_path, x1, y1, x2, y2, threshold=MATCH_THRESHOLD):
    """在截图 [x1,y1,x2,y2] 区域内找模板，返回 (x, y, 匹配分)。

    x,y 为 None 表示未达阈值；模板尺寸超过搜索区时返回 None。
    """
    tpl = imread_unicode(tpl_path)
    if tpl is None:
        return None
    th, tw = tpl.shape[:2]

    img = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)[:, :, :3]  # BGRA -> BGR
    roi = img[y1:y2, x1:x2]
    if roi.shape[0] < th or roi.shape[1] < tw:
        return None

    res = cv2.matchTemplate(roi, tpl, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    if max_val >= threshold:
        return max_loc[0] + x1, max_loc[1] + y1, float(max_val)
    return None, None, float(max_val)


def find_first_image(buf, w, h, image_list, x1, y1, x2, y2, threshold=MATCH_THRESHOLD):
    """按顺序查找图片列表，返回第一张命中的 (路径, x, y)；都未命中返回 (None, None, None)。"""
    for path in image_list:
        r = find_image(buf, w, h, path, x1, y1, x2, y2, threshold)
        if r is not None and r[0] is not None:
            return path, r[0], r[1]
    return None, None, None


# ---- 前台鼠标点击 ----
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


def foreground_click(hwnd, x, y):
    """前台真实鼠标点击。坐标是物理像素（本进程已 DPI 感知）。"""
    check_f5()
    if _stop:
        return
    bring_to_foreground(hwnd)
    user32.SetCursorPos(x, y)
    time.sleep(0.1)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.08)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


# ---- 各场景的处理逻辑（后续按需补充）----
def on_start(hwnd, buf, w, h):
    print(f"  → 检测到开始界面，点击开始按钮 {START_BTN}")
    foreground_click(hwnd, START_BTN[0], START_BTN[1])


def on_choose_card(hwnd, buf, w, h):
    print("  → 检测到选择卡牌场景，开始找图…")
    x1, y1, x2, y2 = CARD_SEARCH_BOX
    # 卡片有翻转动画：重新截当前画面，并最多重试几次，等卡片翻到正面
    for attempt in range(4):
        cap = capture_window(hwnd)
        if cap is None:
            print("  → 截图失败")
            break
        w, h, buf = cap
        path, x, y = find_first_image(buf, w, h, CARD_IMAGE_LIST, x1, y1, x2, y2)
        if path is not None:
            print(f"  → 找到 {path}，左上角 ({x},{y})，点击")
            foreground_click(hwnd, x, y)
            # 再次点击，跳过卡牌进阶的动画
            time.sleep(0.3)
            foreground_click(hwnd, x, y)
            return
        if attempt < 3 and sleep_check(0.5):
            return
    print(f"  → 未找到任何卡牌图，点击兜底坐标 {CARD_FALLBACK_CLICK}")
    foreground_click(hwnd, CARD_FALLBACK_CLICK[0], CARD_FALLBACK_CLICK[1])
    # 再次点击，跳过卡牌进阶的动画
    time.sleep(0.3)
    foreground_click(hwnd, CARD_FALLBACK_CLICK[0], CARD_FALLBACK_CLICK[1])


def on_failed(hwnd, buf, w, h):
    print(f"  → 检测到挑战失败，点击 {FAILED_CLICK}")
    foreground_click(hwnd, FAILED_CLICK[0], FAILED_CLICK[1])


def on_extra(hwnd, buf, w, h):
    print("  → 检测到额外机会场景")
    print(f"  → 点击 {EXTRA_CLICK_FIRST}")
    foreground_click(hwnd, EXTRA_CLICK_FIRST[0], EXTRA_CLICK_FIRST[1])
    if sleep_check(1.0):
        return

    c = color_at(hwnd, EXTRA_CHECK_POINT[0], EXTRA_CHECK_POINT[1])
    if c == EXTRA_CHECK_COLOR:
        print(f"  → ({EXTRA_CHECK_POINT[0]},{EXTRA_CHECK_POINT[1]}) 命中，点击")
        foreground_click(hwnd, EXTRA_CHECK_POINT[0], EXTRA_CHECK_POINT[1])
    else:
        print(f"  → ({EXTRA_CHECK_POINT[0]},{EXTRA_CHECK_POINT[1]}) 未命中（实际 {c}），点击 {EXTRA_CLICK_ALT}")
        foreground_click(hwnd, EXTRA_CLICK_ALT[0], EXTRA_CLICK_ALT[1])
        if sleep_check(1.0):
            return
        print(f"  → 再点击 ({EXTRA_CHECK_POINT[0]},{EXTRA_CHECK_POINT[1]})")
        foreground_click(hwnd, EXTRA_CHECK_POINT[0], EXTRA_CHECK_POINT[1])


def on_clear(hwnd, buf, w, h):
    print(f"  → 检测到通关场景，点击 {CLEAR_CLICK}")
    sleep_check(2.0)
    foreground_click(hwnd, CLEAR_CLICK[0], CLEAR_CLICK[1])
    sleep_check(2.0)


HANDLERS = {
    SCENE_START: on_start,
    SCENE_CHOOSE_CARD: on_choose_card,
    SCENE_FAILED: on_failed,
    SCENE_EXTRA: on_extra,
    SCENE_CLEAR: on_clear,
}


def probe(hwnd):
    """单次探测：打印规则色值 + 找图匹配分（不点击），用于校准坐标/阈值。"""
    cap = capture_window(hwnd)
    if cap is None:
        print("[!] 截图失败（窗口可能被最小化）")
        return
    w, h, buf = cap
    print(f"[+] 截图尺寸: {w}x{h}")

    for scene, x, y, rgb in RULES:
        exp = "#%02X%02X%02X" % rgb
        if 0 <= x < w and 0 <= y < h:
            got = pixel_color(buf, w, x, y)
            act = "#%02X%02X%02X" % got
            print(f"[+] 规则[{scene}] 坐标 ({x},{y}) 期望 {exp} 实际 {act}  "
                  f"{'✅ 匹配' if got == rgb else '❌ 不匹配'}")
        else:
            print(f"[!] 规则[{scene}] 坐标 ({x},{y}) 超出截图范围 {w}x{h}")

    print("[*] 找图 dry-run（不点击）：")
    x1, y1, x2, y2 = CARD_SEARCH_BOX
    for path in CARD_IMAGE_LIST:
        tpl = imread_unicode(path)
        if tpl is None:
            print(f"  [!] 无法读取模板 {path}")
            continue
        th, tw = tpl.shape[:2]
        r = find_image(buf, w, h, path, x1, y1, x2, y2)
        if r is None:
            print(f"  [!] {path} 模板 {tw}x{th} 超过搜索区 {x2 - x1}x{y2 - y1}，无法匹配")
        else:
            x, y, score = r
            hit = "✅ 命中" if x is not None else "❌ 未达阈值"
            print(f"  [ ] {path} 模板 {tw}x{th}  最高匹配分 {score:.3f}  "
                  f"位置 ({x},{y})  {hit}")


def main():
    _set_dpi_aware()

    hwnd = find_window("小游戏")
    if hwnd is None:
        print('[!] 未找到“小游戏”窗口，请先打开游戏。')
        sys.exit(1)

    left, top = move_to_origin(hwnd)
    print(f'[+] 找到窗口 (句柄 {hwnd:#010x})，左上角已对齐到 ({left}, {top})')

    print("[*] 开始场景检测（每秒一次，按 F5 停止）...")
    last_scene = None
    while True:
        check_f5()
        if _stop:
            print("[!] 检测到 F5，停止脚本")
            break

        cap = capture_window(hwnd)
        if cap is None:
            if last_scene != "截图中断":
                print("[!] 截图失败（窗口可能被最小化），继续等待…")
                last_scene = "截图中断"
            if sleep_check(1.0):
                break
            continue

        w, h, buf = cap
        scene = detect_scene(buf, w, h)
        if scene != last_scene:
            print(f"[场景] → {scene}")
            last_scene = scene
            # 只在进入新场景时执行一次动作，避免每秒重复点击
            handler = HANDLERS.get(scene)
            if handler:
                # 进入场景后延迟一秒再执行，等画面稳定
                if sleep_check(1.0):
                    break
                handler(hwnd, buf, w, h)
                # 动作执行后快速轮询，等画面切走（转场动画），
                # 并把 last_scene 更新成新画面，避免错过紧挨着的同场景
                next_scene = wait_scene_change(hwnd, scene)
                if next_scene != scene:
                    print(f"[场景] → {next_scene}")
                    last_scene = next_scene
        if sleep_check(1.0):
            break


if __name__ == "__main__":
    _set_dpi_aware()
    if len(sys.argv) > 1 and sys.argv[1] == "--probe":
        hwnd = find_window("小游戏")
        if hwnd is None:
            print('[!] 未找到“小游戏”窗口，请先打开游戏。')
            sys.exit(1)
        probe(hwnd)
    elif len(sys.argv) >= 7 and sys.argv[1] == "--cut":
        # 用法: python auto_battle.py --cut x1 y1 x2 y2 输出.png
        hwnd = find_window("小游戏")
        if hwnd is None:
            print('[!] 未找到“小游戏”窗口，请先打开游戏。')
            sys.exit(1)
        x1, y1, x2, y2 = map(int, sys.argv[2:6])
        cut_template(hwnd, x1, y1, x2, y2, sys.argv[6])
    else:
        main()
