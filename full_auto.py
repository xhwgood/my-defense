# -*- coding: utf-8 -*-
"""
full_auto.py —— 每日流程入口：先找到游戏窗口并对齐到屏幕 (0,0)，再按日期决定跑日常还是自动战斗。

流程：
  1. 记录当前系统时间；
  2. 找到“小游戏”或“抖音”窗口，把画面左上角对齐到屏幕 (0,0)；
  3. 若今天还未执行过日常脚本，则执行日常脚本（占位，待完善）；
     若今天已执行过，则执行 auto_battle。

用法：
    python full_auto.py                # 完整流程（默认）
    python full_auto.py full_auto      # 同上
    python full_auto.py auto_battle    # 跳过日常判断，只执行 auto_battle
    python full_auto.py auto_battle forever   # 单独执行 auto_battle，并启用 forever 参数

依赖：同 auto_battle.py（ctypes + numpy + opencv-python + rapidocr_onnxruntime）。
"""

import json
import sys
from datetime import datetime
from pathlib import Path

# 修复 Windows 控制台中文乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import auto_battle

# 日常脚本执行状态：记录最近一次执行日常的日期，用于判断今天是否已经跑过。
STATE_PATH = Path(__file__).with_name("logs") / "full_auto_state.json"


def load_state():
    """读取状态文件；不存在或损坏时返回空字典（视为今天还没执行过日常）。"""
    if not STATE_PATH.is_file():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[!] 读取 {STATE_PATH.name} 失败（{type(e).__name__}: {e}），按未执行过日常处理")
        return {}


def save_state(state):
    """把状态写回文件。"""
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[!] 保存 {STATE_PATH.name} 失败：{type(e).__name__}: {e}")


def run_daily_task():
    """每日日常脚本（占位，待完善）。返回 True 表示执行成功，成功后才记为“今天已执行”。"""
    print("[日常] 今天还未执行过日常脚本，开始执行…")
    # TODO: 在这里补充日常任务逻辑
    print("[日常] 占位流程完成")
    return True


def align_window():
    """找到游戏窗口并把画面对齐到屏幕 (0,0)，返回是否成功。"""
    game_hwnd, top_hwnd, cfg = auto_battle.find_client_window()
    if game_hwnd is None:
        print('[!] 未找到“小游戏”或“抖音”窗口，请先打开游戏。')
        return False

    auto_battle.CFG = cfg
    auto_battle._foreground_hwnd = top_hwnd
    origin = auto_battle.move_to_origin(top_hwnd, game_hwnd)
    auto_battle._click_origin = origin
    print(f'[+] 找到窗口「{cfg.keyword}」(顶层句柄 {top_hwnd:#010x}，画面句柄 {game_hwnd:#010x})，'
          f'客户区原点 {origin}')
    if origin != (0, 0):
        print(f'[!] 游戏画面未对齐到 (0,0)，点击将按偏移 {origin} 补偿')
    return True


def run_full_auto(forever=False):
    """完整流程：判断今天是否执行过日常，未执行则跑日常，否则跑 auto_battle。"""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    print(f"[*] 当前系统时间：{now.strftime('%Y-%m-%d %H:%M:%S')}")

    state = load_state()
    if state.get("last_daily_date") == today:
        state["last_run_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        save_state(state)
        print(f"[*] 今天（{today}）已执行过日常脚本，开始执行 auto_battle")
        auto_battle.main(forever=forever)
        return

    if run_daily_task():
        state["last_daily_date"] = today
        state["last_run_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        save_state(state)
        print(f"[*] 已记录今天（{today}）执行过日常脚本")
    else:
        print("[!] 日常脚本未成功执行，不记录今日状态")


def main():
    argv = sys.argv[1:]
    if argv and argv[0] in ("full_auto", "auto_battle"):
        command = argv[0]
        extra = argv[1:]
    else:
        command = "full_auto"
        extra = argv
    forever = "forever" in extra

    auto_battle._set_dpi_aware()

    # 抖音窗口以 High 完整性运行，普通（Medium）进程被 UIPI 拦截，无法移动/前置它；
    # 先尝试提权到管理员（High），否则窗口永远移不到 (0,0)、也无法可靠置前。
    if not auto_battle._is_admin():
        print("[*] 当前非管理员权限，尝试以管理员身份重新启动（请在弹出的 UAC 窗口点“是”）…")
        if auto_battle._relaunch_as_admin():
            sys.exit(0)
        print("[!] 提权失败或被取消，继续以普通权限运行（抖音窗口可能无法移动/置前）")

    if not align_window():
        sys.exit(1)

    if command == "auto_battle":
        print("[*] 单独执行 auto_battle")
        auto_battle.main(forever=forever)
    else:
        run_full_auto(forever=forever)


if __name__ == "__main__":
    main()
