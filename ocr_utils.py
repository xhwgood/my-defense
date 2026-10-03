# -*- coding: utf-8 -*-
"""
ocr_utils.py —— 通用 OCR 文字识别工具（基于 PaddleOCR）。

供 auto_battle.py 及其他脚本复用：传入图片和识别区域，返回识别出的文字。

依赖：
    pip install paddleocr

用法：
    import ocr_utils
    text  = ocr_utils.recognize(img)                      # 识别整张图
    text  = ocr_utils.recognize(img, (x1, y1, x2, y2))    # 只识别指定区域
    texts = ocr_utils.recognize_regions(img, [r1, r2, r3])  # 识别多个区域
"""

import logging
import os
import sys
import warnings
from contextlib import contextmanager

import cv2
import numpy as np
from paddleocr import PaddleOCR

# 引擎懒加载并缓存：首次识别较慢（加载模型），之后复用同一个实例。
_engine = None


def _silence_paddle_noise():
    """压掉 PaddleOCR / PaddleX 在初始化时打印的无用日志与警告。"""
    # paddlex 默认 INFO，会打印 "Creating model ..." / "Model files already exist ..."
    for name in ("paddlex", "paddleocr", "paddle"):
        logging.getLogger(name).setLevel(logging.ERROR)
    # 未安装 ccache 时的 UserWarning（本机必然出现）
    warnings.filterwarnings("ignore", message=".*ccache.*")


@contextmanager
def _redirect_stderr():
    """把标准错误重定向到 devnull，用于吞掉一次性初始化噪声。

    Paddle 在探测 ccache 时会执行 `where ccache`（子进程直接继承 stderr 文件描述符），
    中文 Windows 上会漏出「信息: 用提供的模式无法找到文件。」——这行不经过 Python logging，
    只能靠重定向 stderr（文件描述符 2）来压掉。
    """
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    saved_fd = os.dup(2)
    os.dup2(devnull_fd, 2)
    try:
        yield
    finally:
        sys.stderr.flush()
        os.dup2(saved_fd, 2)
        os.close(saved_fd)
        os.close(devnull_fd)


def get_engine():
    """返回全局共享的 PaddleOCR 引擎（首次调用时初始化）。"""
    global _engine
    if _engine is None:
        _silence_paddle_noise()
        # 首次初始化会触发模型加载并打印大量噪声，重定向 stderr 一次性吞掉。
        with _redirect_stderr():
            # enable_mkldnn=False：部分 AMD CPU 上 oneDNN 推理会崩（ConvertPirAttribute 未实现）。
            # 关闭文档方向/矫正/文本行方向预处理：卡牌名是规整的横排文字，用不上还更慢、易误判。
            _engine = PaddleOCR(
                lang="ch",
                enable_mkldnn=False,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
            )
            # 用一张空白图做一次预测，把 det/rec 模型真正加载起来（"Creating model" 日志就在这一步），
            # 这样真正的第一次 recognize() 就不会再刷屏。
            try:
                blank = np.zeros((320, 320, 3), dtype=np.uint8)
                if hasattr(_engine, "predict"):
                    _engine.predict(blank)
                else:
                    _engine.ocr(blank)
            except Exception:
                pass  # 预热失败不影响真实识别
        # 初始化内部可能又把日志级别改回 INFO，再压一次。
        _silence_paddle_noise()
    return _engine


def _to_texts(result):
    """把 PaddleOCR 的返回结果统一解析成文字列表，兼容 2.x / 3.x 两种格式。"""
    if not result:
        return []

    first = result[0]

    # 3.x：predict() 返回 dict / OCRResult 对象列表，字段名 rec_texts
    rec = None
    if isinstance(first, dict):
        rec = first.get("rec_texts")
    elif hasattr(first, "rec_texts"):
        rec = getattr(first, "rec_texts")
    if rec is not None:
        texts = []
        for item in result:
            r = item.get("rec_texts") if isinstance(item, dict) else getattr(item, "rec_texts", None)
            r = r or []
            if isinstance(r, str):
                texts.append(r)
            else:
                texts.extend(str(t) for t in r)
        return texts

    # 2.x：ocr() 返回 [[box, (text, score)], ...]，单图时外层再套一层
    dets = first if isinstance(first, (list, tuple)) else result
    texts = []
    for d in dets:
        if isinstance(d, (list, tuple)) and len(d) >= 2:
            second = d[1]
            if isinstance(second, (list, tuple)):
                texts.append(str(second[0]))
            else:
                texts.append(str(second))
    return texts


def recognize(img, region=None, scale=2):
    """识别 img 中的文字，返回拼接后的字符串。

    img:    numpy 数组，BGR 彩色图。
    region: 可选识别区域 (x1, y1, x2, y2)；为 None 时识别整张图。
    scale:  识别前把图片放大的倍数（小字放大后识别率更高）；1 表示不放大。
    返回:   识别出的文本（多块文字用空格拼接）；识别失败返回空字符串。
    """
    if region is not None:
        x1, y1, x2, y2 = region
        img = img[y1:y2, x1:x2]
        if img.size == 0:
            return ""
    if scale and scale != 1:
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

    engine = get_engine()
    # 3.x 用 predict()，2.x 用 ocr()
    out = engine.predict(img) if hasattr(engine, "predict") else engine.ocr(img)
    return " ".join(_to_texts(out))


def recognize_regions(img, regions, scale=2):
    """识别多个区域，返回与 regions 等长的字符串列表。"""
    return [recognize(img, r, scale) for r in regions]
