# -*- coding: utf-8 -*-
"""屏幕采集与帧变化检测。

要点：
1. DPI 感知必须在创建 Tk 根窗口之前设置，否则高缩放屏（125%/150%）下
   Tk 报告的逻辑坐标与 mss 抓取的物理像素不一致，选区会整体偏移。
2. mss 实例不能跨进程/跨线程共享，每个使用者自建一个。
3. 变化检测用灰度均值差（MAE）而不是逐像素哈希 —— 对视频/光标闪烁更宽容，
   同时能识别"同一页面轻微抖动"从而跳过重复 OCR。
"""

from __future__ import annotations

import sys
from typing import Optional, Tuple

import numpy as np

try:  # 仅在 Windows 下生效
    import ctypes
except Exception:  # pragma: no cover
    ctypes = None  # type: ignore

Region = Tuple[int, int, int, int]  # (left, top, width, height)


# ---------------------------------------------------------------- DPI

_dpi_ready = False


def enable_dpi_awareness() -> None:
    """开启进程 DPI 感知（幂等）。必须在 Tk 初始化前调用。"""
    global _dpi_ready
    if _dpi_ready:
        return
    if sys.platform != "win32" or ctypes is None:
        _dpi_ready = True
        return
    try:
        # Windows 8.1+ : PER_MONITOR_AWARE_V2
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    _dpi_ready = True


def primary_dpi_scale() -> float:
    """返回主屏缩放比例（1.0 / 1.25 / 1.5 / 2.0 ...）。"""
    if sys.platform != "win32" or ctypes is None:
        return 1.0
    try:
        hdc = ctypes.windll.user32.GetDC(0)
        try:
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
        finally:
            ctypes.windll.user32.ReleaseDC(0, hdc)
        return max(1.0, round(dpi / 96.0, 3))
    except Exception:
        return 1.0


# ---------------------------------------------------------------- 采集

class ScreenCapture:
    """mss 封装：支持多显示器虚拟桌面坐标。

    通道顺序说明：mss 的 ScreenShot.raw 是 **BGRA** 四字节像素，
    取其前三个通道即得到 OpenCV 需要的 BGR，无需再做反转。
    （`ScreenShot.rgb` 是三通道 RGB，长度不同，直接按四通道 reshape 会报错。）
    """

    def __init__(self) -> None:
        import mss  # 延迟导入，避免无显示环境导入即崩

        self._mss = mss
        factory = getattr(mss, "MSS", None) or mss.mss
        self._sct = factory()

    def close(self) -> None:
        try:
            self._sct.close()
        except Exception:
            pass

    # -- 显示器信息 ---------------------------------------------------

    def monitors(self):
        return list(self._sct.monitors)

    def virtual_bounds(self) -> Region:
        """整个虚拟桌面（所有显示器并集）。"""
        mon = self._sct.monitors[0]
        return (int(mon["left"]), int(mon["top"]), int(mon["width"]), int(mon["height"]))

    def monitor_count(self) -> int:
        return max(0, len(self._sct.monitors) - 1)

    # -- 抓取 ---------------------------------------------------------

    def grab(self, region: Region) -> np.ndarray:
        """抓取指定区域，返回 BGR ndarray (H, W, 3)。"""
        left, top, width, height = (int(v) for v in region)
        width = max(1, width)
        height = max(1, height)
        raw = self._sct.grab({"left": left, "top": top, "width": width, "height": height})

        # 优先用 raw.raw（BGRA），退回 bgra；再退回 rgb（三通道）
        buf = getattr(raw, "raw", None)
        channels_hint = 4
        if buf is None:
            buf = getattr(raw, "bgra", None)
        if buf is None:
            buf = getattr(raw, "rgb", None)
            channels_hint = 3

        size = getattr(raw, "size", None)
        h = int(getattr(size, "height", None) or getattr(raw, "height", height))
        w = int(getattr(size, "width", None) or getattr(raw, "width", width))

        arr = np.frombuffer(buf, dtype=np.uint8)
        if arr.size < h * w:
            raise ValueError(f"截屏缓冲区尺寸异常：{arr.size} < {h}x{w}")

        bpp = max(1, arr.size // (h * w))
        arr = arr.reshape((h, w, bpp))

        if bpp >= 4:
            bgr = arr[:, :, :3]                    # BGRA -> BGR
        elif bpp == 3:
            bgr = arr[:, :, ::-1]                  # RGB -> BGR
        else:
            bgr = np.repeat(arr[:, :, :1], 3, axis=2)
        _ = channels_hint
        return np.ascontiguousarray(bgr)

    def grab_full(self) -> np.ndarray:
        return self.grab(self.virtual_bounds())


def clamp_region(region: Optional[Region], bounds: Region) -> Optional[Region]:
    """把区域裁剪到桌面范围内；完全越界返回 None。"""
    if not region:
        return None
    try:
        left, top, width, height = (int(v) for v in region)
    except Exception:
        return None
    if width <= 4 or height <= 4:
        return None

    bl, bt, bw, bh = bounds
    br, bb = bl + bw, bt + bh

    left = max(left, bl)
    top = max(top, bt)
    right = min(left + width, br)
    bottom = min(top + height, bb)

    if right - left <= 4 or bottom - top <= 4:
        return None
    return (left, top, right - left, bottom - top)


def frame_diff(a: Optional[np.ndarray], b: np.ndarray) -> float:
    """两帧的平均绝对差（0-255）。形状不一致时返回一个很大的值。"""
    if a is None or a.shape != b.shape:
        return 255.0
    ga = a
    gb = b
    if a.ndim == 3:
        ga = a.mean(axis=2)
        gb = b.mean(axis=2)
    return float(np.mean(np.abs(ga.astype(np.float32) - gb.astype(np.float32))))
