# -*- coding: utf-8 -*-
"""
auto_battle.py —— “我的防线”自动化战斗脚本。

流程：
  1. 找到“小游戏”或“抖音”窗口，把左上角对齐到屏幕 (0,0)；
  2. 进入主循环：每秒对游戏画面 OCR，判断当前场景，再按场景执行对应逻辑；
  3. 运行中随时按 F6 键可立即停止脚本。

当前已实现的场景检测：统一在 (48,111)-(705,342) 区域 OCR，未命中再在 (213,1407)-(632,1503) OCR 一次；命中对应关键词即判定为对应场景：
  开始界面:   文本含“开始”
  选择卡牌:   文本含“选择卡牌”或“等级提升”
  挑战失败:   文本含“失败”
  额外机会:   文本含“额外”或“机会”
  通关:       文本含“通关”或“胜利”

  支持两个客户端窗口：「小游戏」与「抖音」，各有一套窗口定位与点击坐标（见下方 CLIENT_XIAO / CLIENT_DOUYIN）。

用法：
    python auto_battle.py            # 运行检测循环（按 F6 停止）
    python auto_battle.py forever    # 启用开始界面的额外点击
    python auto_battle.py --probe    # 单次探测：打印规则色值 + OCR 识别结果（不点击）
    python auto_battle.py --cut x1 y1 x2 y2 输出.png   # 抓当前屏幕裁一块模板图

依赖：ctypes（Windows 自带）+ numpy + opencv-python + rapidocr_onnxruntime（OCR）。
"""

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes
from typing import NamedTuple, Tuple

import cv2
import numpy as np

from card_constants import CARD_NAME_LIST

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
VK_F6 = 0x75  # 按 F6 立即停止脚本
GW_CHILD = 5  # GetWindow 取第一个子窗口

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
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.ClientToScreen.restype = wintypes.BOOL
user32.IsZoomed.argtypes = [wintypes.HWND]
user32.IsZoomed.restype = wintypes.BOOL
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int

EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class RECT(ctypes.Structure):
    """Windows RECT 结构：窗口/客户区的矩形边界（left/top/right/bottom）。"""
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    """BITMAPINFOHEADER 结构：描述 GetDIBits 读取像素位图的格式。"""
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


# ---- 场景定义 ----
# 场景名常量：detect_scene() 的返回值，同时是 HANDLERS 字典的键。
SCENE_UNKNOWN = "未知"
SCENE_START = "开始界面"
SCENE_CHOOSE_CARD = "选择卡牌"
SCENE_FAILED = "挑战失败"
SCENE_EXTRA = "额外机会"
SCENE_CLEAR = "通关"


# 统一场景检测：先 OCR 主区域，未命中再用备选区域 OCR 一次；命中对应关键词即判定为对应场景。
SCENE_OCR_REGION = (48, 111, 705, 342)
SCENE_OCR_REGION_FALLBACK = (213, 1334, 632, 1503)

# 场景名 → 触发关键词（顺序越靠前优先级越高；OCR 文本命中任一关键词即判定为该场景）。
SCENE_KEYWORDS = (
    (SCENE_START, ("开始",)),
    (SCENE_CHOOSE_CARD, ("选择卡牌", "等级提升", "临时")),
    (SCENE_FAILED, ("失败",)),
    (SCENE_EXTRA, ("额外", "机会")),
    (SCENE_CLEAR, ("通关", "胜利", "完成")),
)

# 同一场景连续检测达到该次数时发送告警邮件（见 email_notify.py）。
SCENE_REPEAT_ALERT_COUNT = 10

# 未知画面持续超过该秒数时发送一次告警邮件。
UNKNOWN_ALERT_SECONDS = 180

# 统一动作坐标（两个客户端通用，不再按客户端区分）。
START_BTN = (303, 1346)                        # 开始按钮点击
EXTRA_CLICK_FIRST = (159, 795)                 # 额外机会 第一步点击
EXTRA_CHECK_POINT = (515, 1502)                # 额外机会 第二步判断
EXTRA_CLICK_ALT = (423, 786)                   # 额外机会 替代点击


# ---- 客户端查找配置 ----
# 两个客户端（“小游戏”/“抖音”）各一套窗口定位与点击坐标；keyword 用于定位窗口。
# 坐标 = 窗口内物理像素；窗口对齐到 (0,0) 后即屏幕坐标。
class ClientConfig(NamedTuple):
    """一个客户端的窗口定位与点击坐标（场景检测与通用动作坐标已统一，不再按客户端区分）。"""
    keyword: str
    window_class: str                                     # 窗口类名（空串表示不按类名过滤）
    first_child: bool                                     # 是否用第一个子控件作为游戏画面
    failed_click: Tuple[int, int]                         # 挑战失败 点击
    card_ocr_regions: Tuple[Tuple[int, int, int, int], ...]   # 三个卡牌 OCR 区域
    card_click_points: Tuple[Tuple[int, int], ...]        # 与 OCR 区域对应的点击坐标
    card_fallback_click: Tuple[int, int]                  # 一张都识别不到时的兜底点击
    clear_click: Tuple[int, int]                          # 通关 点击


# 客户端「小游戏」：当前生效的查找配置。
CLIENT_XIAO = ClientConfig(
    keyword="小游戏",
    window_class="",
    first_child=False,
    failed_click=(531, 1287),
    card_ocr_regions=(
        (41, 700, 249, 745),    # 卡牌1 文字区域
        (317, 700, 528, 745),   # 卡牌2 文字区域
        (588, 700, 809, 745),   # 卡牌3 文字区域
    ),
    card_click_points=(
        (152, 835),
        (426, 835),
        (702, 835),
    ),
    card_fallback_click=(150, 840),
    clear_click=(294, 1317),
)

# 客户端「抖音」：坐标暂与「小游戏」相同，待按抖音实际画面修改。
CLIENT_DOUYIN = ClientConfig(
    keyword="抖音",
    window_class="Qt5152QWindowIcon",
    first_child=True,
    failed_click=(504, 1311),
    card_ocr_regions=(
        (41, 730, 287, 775),
        (317, 730, 584, 775),
        (626, 730, 888, 775),
    ),
    card_click_points=(
        (152, 835),
        (426, 835),
        (702, 835),
    ),
    card_fallback_click=(150, 840),
    clear_click=(284, 1317),
)

# 当前生效的客户端配置：入口（main / --probe / --cut）按找到的窗口关键字选定。
CFG = CLIENT_XIAO

# ---- 基础工具 ----
def _set_dpi_aware():
    """让本进程 DPI 感知，保证截图尺寸与屏幕物理像素一致。"""
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def find_window(keyword, wclass=None):
    """枚举可见顶层窗口，返回标题含 keyword（且类名含 wclass，若给定）的第一个句柄。"""
    found = []

    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        if wclass is not None:
            cb = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cb, 256)
            if wclass not in cb.value:
                return True
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if buf.value and keyword in buf.value:
            found.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(callback), 0)
    return found[0] if found else None


def resolve_game_window(cfg):
    """按配置找到窗口，返回 (game, top)。

    game：截图/坐标基准窗口（抖音为其第一个子控件）；top：切前台/移动用的顶层窗口。
    找不到返回 (None, None)。
    """
    top = find_window(cfg.keyword, cfg.window_class or None)
    if top is None:
        return None, None
    game = top
    if cfg.first_child:
        child = user32.GetWindow(top, GW_CHILD)
        if child:
            game = child
    return game, top


def find_client_window():
    """按“小游戏”→“抖音”顺序查找可用客户端窗口，返回 (game, top, ClientConfig)。

    都找不到时返回 (None, None, None)。
    """
    for cfg in (CLIENT_XIAO, CLIENT_DOUYIN):
        game, top = resolve_game_window(cfg)
        if game is not None:
            return game, top, cfg
    return None, None, None


def _client_screen_origin(hwnd):
    """返回 hwnd 客户区左上角在屏幕上的坐标 (x, y)。"""
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y


def move_to_origin(top_hwnd, game_hwnd):
    """把 game_hwnd（游戏画面）客户区左上角对齐到屏幕 (0,0)，返回对齐后的客户区原点。

    移动的是顶层窗口 top_hwnd（子控件随之移动）：先还原最小化/最大化，再按
    game 客户区原点相对 top 窗口原点的偏移移动，补偿标题栏/边框及子控件偏移。
    """
    if user32.IsIconic(top_hwnd):
        user32.ShowWindow(top_hwnd, SW_RESTORE)
        time.sleep(0.3)
    if user32.IsZoomed(top_hwnd):
        user32.ShowWindow(top_hwnd, SW_RESTORE)
        time.sleep(0.3)

    gx, gy = _client_screen_origin(game_hwnd)
    r = RECT()
    user32.GetWindowRect(top_hwnd, ctypes.byref(r))
    user32.SetWindowPos(top_hwnd, 0, r.left - gx, r.top - gy, 0, 0, SWP_NOSIZE | SWP_NOZORDER)
    return _client_screen_origin(game_hwnd)


_stop = False
_foreground_hwnd = None   # 切前台用的顶层窗口句柄
_click_origin = (0, 0)    # 游戏画面客户区在屏幕上的原点，点击时加该偏移


def check_f6():
    """查询 F6 是否被按下；按下则置位停止标记 _stop（含按下沿与按住）。"""
    global _stop
    if (user32.GetAsyncKeyState(VK_F6) & 0x8001) != 0:
        _stop = True


def sleep_check(seconds):
    """分片休眠（每 50ms 检查一次 F6），期间按下 F6 则打印提示并返回 True。"""
    steps = max(1, int(seconds / 0.05))
    for _ in range(steps):
        check_f6()
        if _stop:
            print("[!] 检测到 F6，停止脚本")
            return True
        time.sleep(0.05)
    return False


def wait_scene_change(hwnd, from_scene, timeout=2.0):
    """快速轮询（每 0.1 秒），直到画面离开 from_scene（转场动画）或超时。
    返回离开后的场景名；超时或按 F6 停止则返回 from_scene。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        check_f6()
        if _stop:
            return from_scene
        cap = capture_window(hwnd)
        if cap is None:
            time.sleep(0.1)
            continue
        w, h, buf = cap
        s, _ = detect_scene(buf, w, h)
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


def save_frame_png(w, h, buf, out_path):
    """把截图缓冲区的整帧保存成 PNG（支持中文路径），返回是否成功。"""
    img = np.frombuffer(buf, np.uint8).reshape(h, w, 4)[:, :, :3]
    ok, data = cv2.imencode(".png", img)
    if not ok:
        return False
    with open(out_path, "wb") as f:
        f.write(data.tobytes())
    return True


def _ocr_region(buf, w, h, region):
    """OCR 识别指定区域 region，返回文本；区域越界或识别失败返回空串。"""
    x1, y1, x2, y2 = region
    if not (0 <= x1 and 0 <= y1 and x2 <= w and y2 <= h):
        return ""
    import ocr_utils
    img = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)[:, :, :3]  # BGRA -> BGR
    return ocr_utils.recognize(img, region)


def _match_scene(text):
    """按 SCENE_KEYWORDS 把 OCR 文本映射成场景名；匹配不到返回 SCENE_UNKNOWN。"""
    for scene, keywords in SCENE_KEYWORDS:
        if any(k in text for k in keywords):
            return scene
    return SCENE_UNKNOWN


def detect_scene(buf, w, h):
    """统一场景检测：先 OCR 主区域，未命中再用备选区域 OCR 一次。

    返回 (scene, text)：scene 为场景名，text 为命中的 OCR 文本（未命中时为空串）。
    本函数静默，不打印——打印交给调用方在「场景变化」时进行，避免每秒刷屏。
    """
    text = _ocr_region(buf, w, h, SCENE_OCR_REGION)
    scene = _match_scene(text)
    if scene != SCENE_UNKNOWN:
        return scene, text
    text2 = _ocr_region(buf, w, h, SCENE_OCR_REGION_FALLBACK)
    scene2 = _match_scene(text2)
    return scene2, text2


# ---- OCR 识别：封装在 ocr_utils.py（recognize / recognize_regions），供多个脚本复用 ----


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
    """前台真实鼠标点击。x,y 是截图（客户区）坐标，点击时加上游戏画面的屏幕原点偏移。"""
    check_f6()
    if _stop:
        return
    bring_to_foreground(_foreground_hwnd)
    ox, oy = _click_origin
    user32.SetCursorPos(ox + x, oy + y)
    time.sleep(0.1)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.08)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


# ---- 各场景的处理逻辑（后续按需补充）----
def on_start(hwnd, buf, w, h, forever=False):
    print(f"  → 检测到开始界面，点击开始按钮 {START_BTN}")
    time.sleep(0.7)
    foreground_click(hwnd, START_BTN[0], START_BTN[1])
    if forever:
        time.sleep(0.4)
        foreground_click(hwnd, 842, 305)


# OCR 常把卡牌名尾部的罗马数字等级（Ⅰ/Ⅱ/Ⅲ）误识别成这些形近汉字（如“强化连射Ⅲ”→“强化连射川”）；
# 卡牌名本身不含这些字，归一化时直接剔除。
_LEVEL_SUFFIX_LOOKALIKES = set("川州洲丨")


def _keep_chinese(s):
    """只保留中文字符：去掉空格、+ 号、罗马数字、数字等 OCR 附带的无关注缀。

    另剔除形近罗马数字的汉字（川/州/洲/丨）——它们只会作为等级后缀被误识别出来。
    """
    return "".join(ch for ch in s if "一" <= ch <= "鿿" and ch not in _LEVEL_SUFFIX_LOOKALIKES)


# CARD_NAME_LIST 的中文归一化版本，匹配时用，避免每次重复计算。
_CARD_NAME_NORM = [_keep_chinese(n) for n in CARD_NAME_LIST]


def best_card(texts):
    """在识别出的文字里挑优先级最高的卡牌。

    texts: 三个区域分别识别出的字符串（与 CFG.card_ocr_regions / CFG.card_click_points 一一对应）。
    返回:  (CARD_NAME_LIST 索引, 对应点击坐标)；全都匹配不到则返回 (None, None)。
    """
    best_idx = None
    best_click = None
    for text, click in zip(texts, CFG.card_click_points):
        t = _keep_chinese(text)
        if t in _CARD_NAME_NORM:
            idx = _CARD_NAME_NORM.index(t)
            if best_idx is None or idx < best_idx:
                best_idx = idx
                best_click = click
    return best_idx, best_click


def click_card(hwnd, x, y):
    """点击某张卡牌，并连点两次（跳过卡牌进阶动画）。"""
    foreground_click(hwnd, x, y)
    time.sleep(0.3)
    foreground_click(hwnd, x, y)


def ocr_pick(hwnd, img):
    """用 OCR 识别三张卡牌文字并按优先级挑一张，返回是否成功点击。"""
    import ocr_utils
    texts = ocr_utils.recognize_regions(img, CFG.card_ocr_regions)
    print(f"  → OCR 结果：{texts}")
    idx, click = best_card(texts)
    if idx is not None:
        print(f"  → OCR 选中最优先卡牌「{CARD_NAME_LIST[idx]}」，点击 {click}")
        click_card(hwnd, click[0], click[1])
        return True
    return False


def on_choose_card(hwnd, buf, w, h):
    """选择卡牌：用 OCR 识别三张卡牌文字，按名字优先级挑一张，点不到则点兜底坐标。"""
    print("  → 检测到选择卡牌场景，OCR 识别卡牌文字…")
    # 卡片有翻转动画：重新截当前画面，并最多重试几次，等卡片翻到正面
    for attempt in range(2):
        cap = capture_window(hwnd)
        if cap is None:
            print("  → 截图失败")
            break
        w, h, buf = cap
        img = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)[:, :, :3]  # BGRA -> BGR

        if ocr_pick(hwnd, img):
            return

        if attempt < 3 and sleep_check(0.5):
            return
    print(f"  → OCR 未识别出卡牌，点击兜底坐标 {CFG.card_fallback_click}")
    click_card(hwnd, CFG.card_fallback_click[0], CFG.card_fallback_click[1])


def on_failed(hwnd, buf, w, h):
    print(f"  → 检测到挑战失败，点击 {CFG.failed_click}")
    foreground_click(hwnd, CFG.failed_click[0], CFG.failed_click[1])


def on_extra(hwnd, buf, w, h):
    print("  → 检测到额外机会场景")
    print(f"  → 点击 {EXTRA_CLICK_FIRST}")
    foreground_click(hwnd, EXTRA_CLICK_FIRST[0], EXTRA_CLICK_FIRST[1])
    if sleep_check(0.3):
        return
    print(f"  → 点击 {EXTRA_CHECK_POINT}")
    foreground_click(hwnd, EXTRA_CHECK_POINT[0], EXTRA_CHECK_POINT[1])
    if sleep_check(0.3):
        return
    print(f"  → 点击 {EXTRA_CLICK_ALT}")
    foreground_click(hwnd, EXTRA_CLICK_ALT[0], EXTRA_CLICK_ALT[1])
    if sleep_check(0.3):
        return
    print(f"  → 点击 {EXTRA_CHECK_POINT}")
    foreground_click(hwnd, EXTRA_CHECK_POINT[0], EXTRA_CHECK_POINT[1])


def on_clear(hwnd, buf, w, h):
    print(f"  → 检测到通关场景，点击 {CFG.clear_click}")
    sleep_check(0.5)
    foreground_click(hwnd, CFG.clear_click[0], CFG.clear_click[1])
    sleep_check(0.5)


HANDLERS = {
    SCENE_START: on_start,
    SCENE_CHOOSE_CARD: on_choose_card,
    SCENE_FAILED: on_failed,
    SCENE_EXTRA: on_extra,
    SCENE_CLEAR: on_clear,
}


def probe(hwnd):
    """单次探测：OCR 识别场景区域 + 三张卡牌文字（不点击），用于校准关键词/坐标。"""
    cap = capture_window(hwnd)
    if cap is None:
        print("[!] 截图失败（窗口可能被最小化）")
        return
    w, h, buf = cap
    print(f"[+] 截图尺寸: {w}x{h}")

    print("[*] 场景区域 OCR（不点击）：")
    text = _ocr_region(buf, w, h, SCENE_OCR_REGION)
    print(f"  [ ] 主区域 {SCENE_OCR_REGION} 识别为「{text}」→ {_match_scene(text)}")
    text2 = _ocr_region(buf, w, h, SCENE_OCR_REGION_FALLBACK)
    print(f"  [ ] 备选区域 {SCENE_OCR_REGION_FALLBACK} 识别为「{text2}」→ {_match_scene(text2)}")

    print("[*] 卡牌 OCR dry-run（不点击）：")
    import ocr_utils
    img = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)[:, :, :3]  # BGRA -> BGR
    texts = ocr_utils.recognize_regions(img, CFG.card_ocr_regions)
    idx, click = best_card(texts)
    for i, (text, reg, pt) in enumerate(zip(texts, CFG.card_ocr_regions, CFG.card_click_points)):
        print(f"  [ ] 区域{i} {reg} 识别为「{text}」，点击坐标 {pt}")
    if idx is not None:
        print(f"  [+] 命中优先级最高的卡牌「{CARD_NAME_LIST[idx]}」，点击 {click}")
    else:
        print("  [!] 三个区域都没命中优先级数组里的卡牌，将点击兜底坐标")


def _is_admin():
    """当前进程是否以管理员权限运行（即 High 完整性级别）。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _relaunch_as_admin():
    """以管理员身份重新启动本脚本（弹出 UAC）。成功返回 True，此时当前进程应退出。"""
    try:
        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        script = os.path.abspath(sys.argv[0])
        params = subprocess.list2cmdline(sys.argv[1:])
        cmd = f'"{script}"' + (f' {params}' if params else '')
        ret = shell32.ShellExecuteW(None, "runas", sys.executable, cmd, os.getcwd(), 1)
        # ShellExecute 成功时返回值 > 32
        return bool(ret) and ret > 32
    except Exception:
        return False


def main(forever=False):
    global CFG, _foreground_hwnd, _click_origin
    started_at = time.time()
    started_monotonic = time.monotonic()

    def format_elapsed(seconds):
        hours, remainder = divmod(int(seconds), 3600)
        minutes, seconds = divmod(remainder, 60)
        hours_text = f"{hours}小时" if hours else ""
        return f"{hours_text}{minutes}分{seconds}秒"

    def log_stopped():
        stopped_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        elapsed = time.monotonic() - started_monotonic
        print(f"[*] 脚本停止时间：{stopped_at}，共运行 {format_elapsed(elapsed)}")

    def send_alert_email(email_notify, subject, body):
        sending_at = time.time()
        elapsed = time.monotonic() - started_monotonic
        sending_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sending_at))
        print(f"[*] 邮件发送时间：{sending_time}，脚本已运行 {format_elapsed(elapsed)}")
        body += (
            f"\n脚本启动时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started_at))}\n"
            f"邮件发送时间：{sending_time}\n"
            f"脚本已运行：{format_elapsed(elapsed)}"
        )
        return email_notify.send_alert_email(subject, body)

    print(f"[*] 脚本启动时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started_at))}")
    _set_dpi_aware()

    game_hwnd, top_hwnd, cfg = find_client_window()
    if game_hwnd is None:
        print('[!] 未找到“小游戏”或“抖音”窗口，请先打开游戏。')
        log_stopped()
        sys.exit(1)
    CFG = cfg
    _foreground_hwnd = top_hwnd

    _click_origin = move_to_origin(top_hwnd, game_hwnd)
    print(f'[+] 找到窗口「{CFG.keyword}」(顶层句柄 {top_hwnd:#010x}，画面句柄 {game_hwnd:#010x})，客户区原点 {_click_origin}')
    if _click_origin != (0, 0):
        print(f'[!] 游戏画面未对齐到 (0,0)，点击将按偏移 {_click_origin} 补偿')

    hwnd = game_hwnd

    print("[*] 开始场景检测（每秒一次，按 F6 停止）...")
    last_scene = None
    previous_detected_scene = None
    scene_counts = {SCENE_FAILED: 0, SCENE_CLEAR: 0}
    repeated_scene = None
    repeated_scene_count = 0
    repeated_scene_alert_sent = False
    unknown_start = None
    unknown_dumped = False
    unknown_last_log = None
    unknown_email_sent = False
    while True:
        check_f6()
        if _stop:
            print("[!] 检测到 F6，停止脚本")
            break

        cap = capture_window(hwnd)
        if cap is None:
            if last_scene != "截图中断":
                print("[!] 截图失败（窗口可能被最小化），继续等待…")
                last_scene = "截图中断"
            repeated_scene = None
            repeated_scene_count = 0
            repeated_scene_alert_sent = False
            if sleep_check(1.0):
                break
            continue

        w, h, buf = cap
        scene, scene_text = detect_scene(buf, w, h)
        if scene != previous_detected_scene:
            if scene in scene_counts:
                scene_counts[scene] += 1
                print(
                    f"[统计] 挑战失败 {scene_counts[SCENE_FAILED]} 次，"
                    f"通关 {scene_counts[SCENE_CLEAR]} 次"
                )
            previous_detected_scene = scene

        if scene == repeated_scene:
            repeated_scene_count += 1
        else:
            repeated_scene = scene
            repeated_scene_count = 1
            repeated_scene_alert_sent = False

        if (repeated_scene_count >= SCENE_REPEAT_ALERT_COUNT
                and not repeated_scene_alert_sent):
            import email_notify
            subject = f"【自动战斗告警】场景「{scene}」已连续检测 {repeated_scene_count} 次"
            body = (
                f"自动战斗脚本连续 {repeated_scene_count} 次检测到场景「{scene}」。\n"
                f"最近一次 OCR 识别结果：「{scene_text}」\n"
                f"挑战失败次数：{scene_counts[SCENE_FAILED]}\n"
                f"通关次数：{scene_counts[SCENE_CLEAR]}"
            )
            email_sent = send_alert_email(email_notify, subject, body)
            repeated_scene_alert_sent = True
            # 任一告警邮件发送成功后立即停止，避免未知画面告警等再次触发导致重复发信。
            if email_sent:
                print("[!] 告警邮件发送成功，停止脚本")
                break

        if scene != last_scene:
            print(f"[场景] → {scene}（OCR「{scene_text}」）")
            last_scene = scene
            # 只在进入新场景时执行一次动作，避免每秒重复点击
            handler = HANDLERS.get(scene)
            if handler:
                # 进入场景后延迟 2 秒再执行，等画面稳定
                if sleep_check(2):
                    break
                if scene == SCENE_START:
                    handler(hwnd, buf, w, h, forever=forever)
                else:
                    handler(hwnd, buf, w, h)
                # 场景脚本结束后先停 1.5 秒，再检测其他场景
                if sleep_check(1.5):
                    break
                # 动作执行后快速轮询，等画面切走（转场动画），
                # 结束后重置 last_scene，让下一个画面（无论是否与当前同场景）
                # 都被重新识别并处理
                new_scene = wait_scene_change(hwnd, scene)
                # 额外机会是固定点击序列，画面文字易残留；超时未切走时保留 last_scene 防止重复触发。
                # 其余场景（如选择卡牌）动作未使画面切走时需要重试，因此清空 last_scene。
                last_scene = scene if (scene == SCENE_EXTRA and new_scene == scene) else None

        # 未知画面：持续超过 3 秒时打印各规则点实际色值并保存整屏截图，方便定位未建模的画面；
        # 之后每 10 秒再打一次，提示脚本仍在运行（未知场景无对应动作，会一直停留）。
        if scene == SCENE_UNKNOWN:
            if unknown_start is None:
                unknown_start = time.time()
            else:
                elapsed = time.time() - unknown_start
                if not unknown_dumped and elapsed >= 3.0:
                    print(f"[调试] 未知画面已持续 {elapsed:.1f} 秒，场景区域 OCR 结果：「{_ocr_region(buf, w, h, SCENE_OCR_REGION)}」")
                    if save_frame_png(w, h, buf, "_debug_unknown.png"):
                        print("[调试] 已保存未知画面整屏截图 _debug_unknown.png，可打开确认当前场景")
                    unknown_dumped = True
                    unknown_last_log = time.time()
                elif unknown_dumped and time.time() - unknown_last_log >= 10.0:
                    print(f"[调试] 仍在未知画面，已持续 {elapsed:.0f} 秒（无对应动作，等待人工处理）")
                    unknown_last_log = time.time()

                # 未知画面持续超过阈值时发送一次告警邮件（避免每秒重复发送）。
                if not unknown_email_sent and elapsed >= UNKNOWN_ALERT_SECONDS:
                    import email_notify
                    subject = "【自动战斗告警】检测到未知画面"
                    body = (
                        f"挑战失败次数：{scene_counts[SCENE_FAILED]}\n"
                        f"通关次数：{scene_counts[SCENE_CLEAR]}"
                    )
                    email_sent = send_alert_email(email_notify, subject, body)
                    unknown_email_sent = True
                    if email_sent:
                        print("[!] 告警邮件发送成功，停止脚本")
                        break

        else:
            unknown_start = None
            unknown_dumped = False
            unknown_email_sent = False

        if sleep_check(1.0):
            break

    log_stopped()


if __name__ == "__main__":
    _set_dpi_aware()
    # 抖音窗口以 High 完整性运行，普通（Medium）进程被 UIPI 拦截，无法移动/前置它；
    # 先尝试提权到管理员（High），否则抖音窗口永远移不到 (0,0)、也无法可靠置前。
    if not _is_admin():
        print("[*] 当前非管理员权限，尝试以管理员身份重新启动（请在弹出的 UAC 窗口点“是”）…")
        if _relaunch_as_admin():
            sys.exit(0)
        print("[!] 提权失败或被取消，继续以普通权限运行（抖音窗口可能无法移动/置前）")

    argv = sys.argv[1:]

    if len(argv) > 0 and argv[0] == "--probe":
        game_hwnd, top_hwnd, cfg = find_client_window()
        if game_hwnd is None:
            print('[!] 未找到“小游戏”或“抖音”窗口，请先打开游戏。')
            sys.exit(1)
        CFG = cfg
        _foreground_hwnd = top_hwnd
        _click_origin = move_to_origin(top_hwnd, game_hwnd)
        probe(game_hwnd)
    elif len(argv) >= 6 and argv[0] == "--cut":
        # 用法: python auto_battle.py --cut x1 y1 x2 y2 输出.png
        game_hwnd, top_hwnd, cfg = find_client_window()
        if game_hwnd is None:
            print('[!] 未找到“小游戏”或“抖音”窗口，请先打开游戏。')
            sys.exit(1)
        CFG = cfg
        x1, y1, x2, y2 = map(int, argv[1:5])
        cut_template(game_hwnd, x1, y1, x2, y2, argv[5])
    else:
        main(forever="forever" in argv)
