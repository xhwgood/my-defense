import builtins
import ctypes
import os
import sys

RESET = "\033[0m"
FG = {
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "white": "\033[37m",
}


def enable_ansi_windows_console():
    """用于 Windows 控制台启用 ANSI 颜色支持。"""
    if os.name != "nt":
        return
    try:
        hwnd = ctypes.windll.kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetConsoleMode(hwnd, ctypes.byref(mode)):
            ctypes.windll.kernel32.SetConsoleMode(hwnd, mode.value | 0x0004)
    except Exception:
        pass


def colorize(text):
    """根据消息内容给文本附上颜色，便于区分输出类型。"""
    if not isinstance(text, str):
        text = str(text)

    if "[!]" in text or "失败" in text or "错误" in text or "未找到" in text:
        return f"{FG['red']}{text}{RESET}"
    if "[+]" in text or "已保存" in text or "找到窗口" in text or "成功" in text:
        return f"{FG['green']}{text}{RESET}"
    if "[*]" in text or "[统计]" in text or "[场景]" in text:
        return f"{FG['cyan']}{text}{RESET}"
    if "[调试]" in text or "[ ]" in text:
        return f"{FG['magenta']}{text}{RESET}"
    if "→" in text or "识别" in text or "点击" in text or "OCR" in text:
        return f"{FG['yellow']}{text}{RESET}"
    return text


def _color_print(*args, sep=" ", end="\n", file=None, flush=False):
    """带颜色的 print：按内容上色。"""
    if file is None:
        file = sys.stdout
    if file is None:
        return

    message = sep.join(str(arg) for arg in args)
    file.write(colorize(message) + end)
    if flush:
        file.flush()


def install_color_print():
    """把全局 print 替换为带颜色版，供其他文件复用。"""
    builtins.print = _color_print
    enable_ansi_windows_console()


__all__ = ["colorize", "install_color_print", "enable_ansi_windows_console"]
