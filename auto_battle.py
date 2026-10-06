# -*- coding: utf-8 -*-
"""
auto_battle.py —— “我的防线”自动化战斗脚本。

流程：
  1. 找到“小游戏”窗口，把左上角对齐到屏幕 (0,0)；
  2. 进入主循环：每秒对游戏画面找色，判断当前场景，再按场景执行对应逻辑；
  3. 运行中随时按 F5 键可立即停止脚本。

当前已实现的场景检测规则（坐标 = 窗口内物理像素；窗口对齐到 (0,0) 后即屏幕坐标）：
  开始界面:   (303, 1299) 颜色 == #4DBDA5  → 点击该坐标（开始按钮）
  选择卡牌:   (357, 297)  颜色 == #7BFFE3  → 先按优先级识图，唯一命中直接点；同图多张命中时用 OCR 判定
  挑战失败:   (453, 401)  颜色 == #BAC0D2  → 点击 (504, 1250)
  额外机会:   (408, 234)  颜色 == #7BFFE3  → 点击 (159,795)，再按 (515,1437) 色值分支点击

用法：
    python auto_battle.py            # 运行检测循环（按 F5 停止）
    python auto_battle.py --probe    # 单次探测：打印规则色值 + 找图匹配分（不点击）
    python auto_battle.py --probe --threshold 0.7   # 用指定阈值探测匹配分
    python auto_battle.py --threshold 0.7            # 用指定阈值运行检测循环
    python auto_battle.py --cut x1 y1 x2 y2 输出.png   # 抓当前屏幕裁一块模板图

依赖：ctypes（Windows 自带）+ numpy + opencv-python + rapidocr_onnxruntime（OCR）。
"""

import ctypes
import sys
import time
from ctypes import wintypes
from typing import NamedTuple, Tuple

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


class SceneRule(NamedTuple):
    """一条场景识别规则：在某坐标点找指定颜色，命中即判定为对应场景。

    字段:
        scene: 场景名（对应 SCENE_* 常量）。
        x:     检测点横坐标（窗口内物理像素）。
        y:     检测点纵坐标。
        color: 期望的 RGB 颜色元组，如 (0x4D, 0xBD, 0xA5)。
    """
    scene: str
    x: int
    y: int
    color: Tuple[int, int, int]


# 开始界面：开始按钮坐标（物理像素，窗口已对齐到屏幕 (0,0)）。
START_BTN = (303, 1299)

# 挑战失败场景
FAILED_POINT = (453, 401)                 # 检测点坐标
FAILED_COLOR = (0xBA, 0xC0, 0xD2)         # 期望色 #BAC0D2
FAILED_CLICK = (504, 1250)                # 点击坐标

# 额外机会场景
EXTRA_POINT = (408, 234)                  # 检测点坐标
EXTRA_COLOR = (0x7B, 0xFF, 0xE3)          # 期望色 #7BFFE3
EXTRA_CLICK_FIRST = (159, 795)            # 第一步点击
EXTRA_CHECK_POINT = (515, 1437)           # 第二步判断坐标
EXTRA_CHECK_COLOR = (0xCF, 0xB9, 0x5A)    # 期望色 #CFB95A
EXTRA_CLICK_ALT = (423, 786)              # 未命中时的替代点击

# 选择卡牌/等级提升场景
CHOOSE_CARD_POINT = (357, 297)            # 检测点坐标
CHOOSE_CARD_COLOR = (0x7B, 0xFF, 0xE3)    # 期望色 #7BFFE3

# 找图（识图）：三张卡牌的卡面图在这块区域内按优先级查找。
CARD_SEARCH_BOX = (45, 758, 812, 924)     # 找图范围 (x1, y1, x2, y2)

# 卡牌图列表：按优先级顺序（索引越小越优先），顺序必须与 CARD_NAME_LIST 一致。
CARD_IMAGE_LIST = [
    r"find_pic\蜂巢-组合.png",
    r"find_pic\蜂巢.png",
    r"find_pic\蜂巢-进阶.png",
    r"find_pic\干扰.png",
    r"find_pic\干扰-进阶.png",
    r"find_pic\电磁炮.png",
    r"find_pic\电磁炮-进阶.png",
]

MATCH_THRESHOLD = 0.85                    # 模板匹配阈值，越低越容易命中

# 三个卡牌识别区域（OCR），格式 (x1, y1, x2, y2)，对应三张可选的卡。
CARD_OCR_REGIONS = [
    (41, 700, 249, 745),    # 卡牌1 文字区域
    (317, 700, 528, 745),   # 卡牌2 文字区域
    (588, 700, 809, 745),   # 卡牌3 文字区域
]

# 与上面三个区域一一对应的点击坐标。
CARD_CLICK_POINTS = [
    (152, 835),
    (426, 835),
    (702, 835),
]

# 卡牌优先级数组（OCR 用）：索引越小越优先；顺序必须与 CARD_IMAGE_LIST 一致。
# TODO: 内容仍为占位，等你确认实际的优先级顺序。
CARD_NAME_LIST = [
    # 蜂巢
    "密集蜂群",
    "强化连射",
    "电流蜂群",
    "黄蜂齐射",
    # 干扰
    "传送增伤",
    "范围瓦解",
    "瓦解爆发",
    "回溯传送",
    "电磁力场",
    "瓦解之力",
    "随机扰动",
    "力场持续",
    "力场扩展",
    # 电磁炮
    "陷阱电网",
    "强化电网",
    "电磁雷网",
    "电磁连锁",
    # 其他炮台
    "反重力装置",
    "高射轰",
    "燃爆炮",
]

CARD_FALLBACK_CLICK = (150, 840)          # 一张都识别不到时的兜底点击

# 通关场景
CLEAR_POINT = (216, 300)                  # 检测点坐标
CLEAR_COLOR = (0x96, 0x4E, 0x1D)          # 期望色 #964E1D
CLEAR_CLICK = (294, 1281)                 # 点击坐标

# 找色容差：每个通道相差不超过该值即视为同色，容忍渲染抖动。
COLOR_TOL = 3

# 场景识别规则表：按顺序检测，命中第一条即返回对应场景。
# 后续新增场景时，往这里加一条 SceneRule。
RULES = [
    SceneRule(SCENE_START, START_BTN[0], START_BTN[1], (0x4D, 0xBD, 0xA5)),  # 开始界面 #4DBDA5
    SceneRule(SCENE_CHOOSE_CARD, CHOOSE_CARD_POINT[0], CHOOSE_CARD_POINT[1], CHOOSE_CARD_COLOR),
    SceneRule(SCENE_FAILED, FAILED_POINT[0], FAILED_POINT[1], FAILED_COLOR),
    SceneRule(SCENE_EXTRA, EXTRA_POINT[0], EXTRA_POINT[1], EXTRA_COLOR),
    SceneRule(SCENE_CLEAR, CLEAR_POINT[0], CLEAR_POINT[1], CLEAR_COLOR),
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


def wait_scene_change(hwnd, from_scene, timeout=2.0):
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


def color_close(got, target, tol=COLOR_TOL):
    """三个通道都在容差内则视为同色（容忍渲染抖动）。"""
    return all(abs(g - t) <= tol for g, t in zip(got, target))


def detect_scene(buf, w, h):
    """按规则表逐条找色，命中则返回场景名，否则返回“未知”。"""
    for scene, x, y, rgb in RULES:
        if 0 <= x < w and 0 <= y < h and color_close(pixel_color(buf, w, x, y), rgb):
            return scene
    return SCENE_UNKNOWN


def dump_rule_colors(w, h, buf):
    """打印所有规则坐标处的实际色值（用于定位“未知”画面到底是什么）。"""
    for scene, x, y, rgb in RULES:
        if 0 <= x < w and 0 <= y < h:
            got = pixel_color(buf, w, x, y)
            exp = "#%02X%02X%02X" % rgb
            act = "#%02X%02X%02X" % got
            print(f"    [调试] 规则[{scene}] ({x},{y}) 期望 {exp} 实际 {act}  "
                  f"{'✅' if color_close(got, rgb) else '❌'}")
        else:
            print(f"    [调试] 规则[{scene}] ({x},{y}) 超出截图范围 {w}x{h}")


# ---- 找图（识图）----
def imread_unicode(path):
    """读取图片（支持中文路径），返回 BGR numpy 数组；失败返回 None。"""
    try:
        data = np.fromfile(path, dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def find_image(buf, w, h, tpl_path, x1, y1, x2, y2, threshold=None):
    """在截图 [x1,y1,x2,y2] 区域内找模板，返回 (x, y, 匹配分)。

    x,y 为 None 表示未达阈值；模板尺寸超过搜索区时返回 None。
    threshold 为 None 时使用全局 MATCH_THRESHOLD（可由 --threshold 覆盖）。
    """
    if threshold is None:
        threshold = MATCH_THRESHOLD
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


def find_all_images(buf, w, h, tpl_path, x1, y1, x2, y2, threshold=None):
    """在截图 [x1,y1,x2,y2] 区域内找出模板的【所有】匹配位置。

    返回:  绝对坐标 (x, y) 列表（模板左上角）；无匹配返回空列表。
    同一张卡只报一个点：按匹配分从高到低取互不重叠的峰值，避免相邻像素重复命中。
    threshold 为 None 时使用全局 MATCH_THRESHOLD（可由 --threshold 覆盖）。
    """
    if threshold is None:
        threshold = MATCH_THRESHOLD
    tpl = imread_unicode(tpl_path)
    if tpl is None:
        return []
    th, tw = tpl.shape[:2]

    img = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)[:, :, :3]  # BGRA -> BGR
    roi = img[y1:y2, x1:x2]
    if roi.shape[0] < th or roi.shape[1] < tw:
        return []

    res = cv2.matchTemplate(roi, tpl, cv2.TM_CCOEFF_NORMED)
    ys, xs = np.where(res >= threshold)
    if len(xs) == 0:
        return []

    # 按匹配分从高到低排序，逐个取与已选位置不重叠的峰值
    candidates = sorted(zip(res[ys, xs], xs, ys), reverse=True)
    picks = []
    for _score, x, y in candidates:
        if all(abs(x - px) >= tw or abs(y - py) >= th for px, py in picks):
            picks.append((x, y))
    return [(x + x1, y + y1) for x, y in picks]


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
    time.sleep(0.7)
    foreground_click(hwnd, START_BTN[0], START_BTN[1])


def _keep_chinese(s):
    """只保留中文字符：去掉空格、+ 号、罗马数字、数字等 OCR 附带的无关注缀。"""
    return "".join(ch for ch in s if "一" <= ch <= "鿿")


# CARD_NAME_LIST 的中文归一化版本，匹配时用，避免每次重复计算。
_CARD_NAME_NORM = [_keep_chinese(n) for n in CARD_NAME_LIST]


def best_card(texts):
    """在识别出的文字里挑优先级最高的卡牌。

    texts: 三个区域分别识别出的字符串（与 CARD_OCR_REGIONS / CARD_CLICK_POINTS 一一对应）。
    返回:  (CARD_NAME_LIST 索引, 对应点击坐标)；全都匹配不到则返回 (None, None)。
    """
    best_idx = None
    best_click = None
    for text, click in zip(texts, CARD_CLICK_POINTS):
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
    texts = ocr_utils.recognize_regions(img, CARD_OCR_REGIONS)
    print(f"  → OCR 结果：{texts}")
    idx, click = best_card(texts)
    if idx is not None:
        print(f"  → OCR 选中最优先卡牌「{CARD_NAME_LIST[idx]}」，点击 {click}")
        click_card(hwnd, click[0], click[1])
        return True
    return False


def on_choose_card(hwnd, buf, w, h):
    """选择卡牌：优先识图，识图唯一命中直接点；同一张图命中多张时转 OCR 判定。

    选择三张卡牌中优先级最高的一张：
      1. 按 CARD_IMAGE_LIST 顺序识图，某张图恰好命中一处 → 直接点击；
      2. 同一张图命中多处（画面里有重复卡面/相似卡面）→ OCR 按名字优先级挑一张；
      3. 所有图都没命中 → OCR 兜底；OCR 也不中 → 点兜底坐标。
    """
    print("  → 检测到选择卡牌场景，先识图、必要时 OCR 判定…")
    x1, y1, x2, y2 = CARD_SEARCH_BOX
    # 卡片有翻转动画：重新截当前画面，并最多重试几次，等卡片翻到正面
    for attempt in range(4):
        cap = capture_window(hwnd)
        if cap is None:
            print("  → 截图失败")
            break
        w, h, buf = cap
        img = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)[:, :, :3]  # BGRA -> BGR

        # 1) 按优先级顺序识图
        for path in CARD_IMAGE_LIST:
            r = find_image(buf, w, h, path, x1, y1, x2, y2)
            score = r[2] if r else 0.0
            locs = find_all_images(buf, w, h, path, x1, y1, x2, y2)
            print(f"  → 模板 {path} 最高匹配分 {score:.3f}，命中 {len(locs)} 处")
            if not locs:
                continue  # 这种卡不在画面上，看下一优先级
            if len(locs) == 1:
                x, y = locs[0]
                print(f"  → 识图命中 {path}（唯一），点击 ({x},{y})")
                click_card(hwnd, x, y)
                return
            # 同一张图命中多处，图片区分不了，转 OCR 按名字优先级挑
            print(f"  → 识图命中 {path} 共 {len(locs)} 处，转 OCR 判定…")
            if ocr_pick(hwnd, img):
                return
            # OCR 也没区分出来：都是同一张卡，点第一处命中即可
            x, y = locs[0]
            print(f"  → OCR 未区分，退回点击第一处 ({x},{y})")
            click_card(hwnd, x, y)
            return

        # 2) 所有图都没命中，OCR 兜底一次
        print("  → 识图未命中任何卡牌，转 OCR 兜底…")
        if ocr_pick(hwnd, img):
            return

        if attempt < 3 and sleep_check(0.5):
            return
    print(f"  → 识图和 OCR 都没结果，点击兜底坐标 {CARD_FALLBACK_CLICK}")
    click_card(hwnd, CARD_FALLBACK_CLICK[0], CARD_FALLBACK_CLICK[1])


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
    if c is not None and color_close(c, EXTRA_CHECK_COLOR):
        print(f"  → ({EXTRA_CHECK_POINT[0]},{EXTRA_CHECK_POINT[1]}) 命中，点击")
        foreground_click(hwnd, EXTRA_CHECK_POINT[0], EXTRA_CHECK_POINT[1])
        sleep_check(0.3)
        foreground_click(hwnd, EXTRA_CHECK_POINT[0], EXTRA_CHECK_POINT[1])
    else:
        print(f"  → ({EXTRA_CHECK_POINT[0]},{EXTRA_CHECK_POINT[1]}) 未命中（实际 {c}），点击 {EXTRA_CLICK_ALT}")
        foreground_click(hwnd, EXTRA_CLICK_ALT[0], EXTRA_CLICK_ALT[1])
        if sleep_check(1.0):
            return
        print(f"  → 再点击 ({EXTRA_CHECK_POINT[0]},{EXTRA_CHECK_POINT[1]})")
        foreground_click(hwnd, EXTRA_CHECK_POINT[0], EXTRA_CHECK_POINT[1])
        sleep_check(0.3)
        foreground_click(hwnd, EXTRA_CHECK_POINT[0], EXTRA_CHECK_POINT[1])


def on_clear(hwnd, buf, w, h):
    print(f"  → 检测到通关场景，点击 {CLEAR_CLICK}")
    sleep_check(0.5)
    foreground_click(hwnd, CLEAR_CLICK[0], CLEAR_CLICK[1])
    sleep_check(0.5)


HANDLERS = {
    SCENE_START: on_start,
    SCENE_CHOOSE_CARD: on_choose_card,
    SCENE_FAILED: on_failed,
    SCENE_EXTRA: on_extra,
    SCENE_CLEAR: on_clear,
}


def probe(hwnd):
    """单次探测：打印规则色值 + OCR 识别结果（不点击），用于校准坐标/阈值。"""
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
                  f"{'✅ 匹配' if color_close(got, rgb) else '❌ 不匹配'}")
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
            locs = find_all_images(buf, w, h, path, x1, y1, x2, y2)
            hit = "✅ 命中" if x is not None else "❌ 未达阈值"
            print(f"  [ ] {path} 模板 {tw}x{th}  最高匹配分 {score:.3f}  "
                  f"命中 {len(locs)} 处 {locs}  {hit}")

    print("[*] OCR dry-run（不点击）：")
    import ocr_utils
    img = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)[:, :, :3]  # BGRA -> BGR
    texts = ocr_utils.recognize_regions(img, CARD_OCR_REGIONS)
    idx, click = best_card(texts)
    for i, (text, reg, pt) in enumerate(zip(texts, CARD_OCR_REGIONS, CARD_CLICK_POINTS)):
        print(f"  [ ] 区域{i} {reg} 识别为「{text}」，点击坐标 {pt}")
    if idx is not None:
        print(f"  [+] 命中优先级最高的卡牌「{CARD_NAME_LIST[idx]}」，点击 {click}")
    else:
        print("  [!] 三个区域都没命中优先级数组里的卡牌，将点击兜底坐标")


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
    unknown_start = None
    unknown_dumped = False
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
                # 进入场景后延迟 2 秒再执行，等画面稳定
                if sleep_check(2):
                    break
                handler(hwnd, buf, w, h)
                # 场景脚本结束后先停 1.5 秒，再检测其他场景
                if sleep_check(1.5):
                    break
                # 动作执行后快速轮询，等画面切走（转场动画），
                # 结束后重置 last_scene，让下一个画面（无论是否与当前同场景）
                # 都被重新识别并处理
                wait_scene_change(hwnd, scene)
                last_scene = None

        # 新增调试：未知画面持续超过 3 秒时，打印各规则点实际色值，定位未建模的画面
        if scene == SCENE_UNKNOWN:
            if unknown_start is None:
                unknown_start = time.time()
            elif not unknown_dumped and time.time() - unknown_start >= 3.0:
                print(f"[调试] 未知画面已持续 {time.time() - unknown_start:.1f} 秒，各规则点实际色值：")
                dump_rule_colors(w, h, buf)
                unknown_dumped = True
        else:
            unknown_start = None
            unknown_dumped = False

        if sleep_check(1.0):
            break


def _parse_threshold(argv):
    """从命令行解析 --threshold <浮点值>，覆盖全局 MATCH_THRESHOLD。

    返回 (threshold, argv_剩余不含该参数)。未提供时 threshold 为 None。
    """
    args = list(argv)
    val = None
    if "--threshold" in args:
        i = args.index("--threshold")
        if i + 1 < len(args):
            try:
                val = float(args[i + 1])
            except ValueError:
                print(f"[!] --threshold 后应跟数字，忽略：{args[i + 1]}")
                val = None
            del args[i:i + 2]
        else:
            del args[i]
    return val, args


if __name__ == "__main__":
    _set_dpi_aware()
    thr, argv = _parse_threshold(sys.argv[1:])
    if thr is not None:
        MATCH_THRESHOLD = thr
        print(f"[*] 匹配阈值已设为 {MATCH_THRESHOLD}")

    if len(argv) > 0 and argv[0] == "--probe":
        hwnd = find_window("小游戏")
        if hwnd is None:
            print('[!] 未找到“小游戏”窗口，请先打开游戏。')
            sys.exit(1)
        probe(hwnd)
    elif len(argv) >= 6 and argv[0] == "--cut":
        # 用法: python auto_battle.py --cut x1 y1 x2 y2 输出.png
        hwnd = find_window("小游戏")
        if hwnd is None:
            print('[!] 未找到“小游戏”窗口，请先打开游戏。')
            sys.exit(1)
        x1, y1, x2, y2 = map(int, argv[1:5])
        cut_template(hwnd, x1, y1, x2, y2, argv[5])
    else:
        main()
