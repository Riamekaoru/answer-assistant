# -*- coding: utf-8 -*-
"""区域选择遮罩。

全屏半透明遮罩覆盖整个虚拟桌面（含多显示器），拖拽出矩形后返回
(left, top, width, height)，坐标为物理像素，与 mss 抓屏坐标一致。

实现要点：
    - 遮罩窗口用 overrideredirect，不抢焦点，Esc / 右键取消
    - 选区用 stipple 填充做出"挖空"观感，透过遮罩仍能看清目标内容
    - 实时显示尺寸，便于精确框选
"""

from __future__ import annotations

import tkinter as tk
from typing import Optional, Tuple

from .theme import Theme

Region = Tuple[int, int, int, int]

_MIN_SIZE = 12


class RegionSelector:
    """模态区域选择器。"""

    def __init__(self, root: tk.Misc, theme: Theme):
        self.root = root
        self.theme = theme
        self.result: Optional[Region] = None
        self._win: Optional[tk.Toplevel] = None
        self._canvas: Optional[tk.Canvas] = None
        self._start: Optional[Tuple[int, int]] = None
        self._origin = (0, 0)
        self._rect_id: Optional[int] = None
        self._hint_id: Optional[int] = None

    # ---------------------------------------------------------------- 对外

    def select(self) -> Optional[Region]:
        """阻塞式选取，返回所选区域或 None。"""
        bounds = self._virtual_bounds()
        left, top, width, height = bounds

        win = tk.Toplevel(self.root)
        self._win = win
        win.overrideredirect(True)
        win.geometry(f"{width}x{height}+{left}+{top}")
        win.attributes("-topmost", True)
        try:
            win.attributes("-alpha", 0.35)
        except Exception:
            pass
        win.configure(bg="#000000")
        win.lift()

        canvas = tk.Canvas(win, bg="#000000", highlightthickness=0, cursor="crosshair")
        canvas.pack(fill="both", expand=True)
        self._canvas = canvas
        self._origin = (left, top)

        c = self.theme.colors
        hint = ("拖动鼠标框选要识别的区域　·　Esc 或右键取消　·　建议只框住单道题干，识别更快更准")
        self._hint_id = canvas.create_text(
            width // 2, 40, text=hint, fill="#FFFFFF",
            font=self.theme.font(12, "bold"), anchor="n",
        )
        canvas.create_text(
            width // 2, 72, text="（框选范围请避开浏览器标签栏、状态栏等无关内容）",
            fill="#C9CDD4", font=self.theme.font(10), anchor="n",
        )

        canvas.bind("<ButtonPress-1>", self._on_press)
        canvas.bind("<B1-Motion>", self._on_drag)
        canvas.bind("<ButtonRelease-1>", self._on_release)
        canvas.bind("<ButtonPress-3>", lambda _e: self._cancel())
        win.bind("<Escape>", lambda _e: self._cancel())

        try:
            win.grab_set()
        except Exception:
            pass

        win.focus_force()
        canvas.focus_set()
        self.root.wait_window(win)
        return self.result

    # ---------------------------------------------------------------- 内部

    def _virtual_bounds(self) -> Region:
        """获取虚拟桌面范围；失败时退回主屏。"""
        try:
            import mss

            with mss.mss() as sct:
                mon = sct.monitors[0]
                return (int(mon["left"]), int(mon["top"]),
                        int(mon["width"]), int(mon["height"]))
        except Exception:
            return (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())

    def _cancel(self) -> None:
        self.result = None
        self._close()

    def _close(self) -> None:
        win = self._win
        self._win = None
        if win is not None:
            try:
                win.grab_release()
            except Exception:
                pass
            try:
                win.destroy()
            except Exception:
                pass

    def _on_press(self, event) -> None:
        self._start = (event.x, event.y)
        c = self.theme.colors
        if self._rect_id is not None:
            self._canvas.delete(self._rect_id)
        self._rect_id = self._canvas.create_rectangle(
            event.x, event.y, event.x, event.y,
            outline=c["primary"], width=2, fill="#FFFFFF", stipple="gray12",
        )
        try:
            self._canvas.itemconfigure(self._hint_id, state="hidden")
        except Exception:
            pass

    def _on_drag(self, event) -> None:
        if self._start is None or self._rect_id is None:
            return
        x0, y0 = self._start
        self._canvas.coords(self._rect_id, x0, y0, event.x, event.y)

        w = abs(event.x - x0)
        h = abs(event.y - y0)
        label = f"{w} × {h}"
        if self._hint_id is not None:
            self._canvas.itemconfigure(self._hint_id, state="normal", text=label)
            self._canvas.coords(self._hint_id, (x0 + event.x) // 2,
                                max(20, min(y0, event.y) - 22))

    def _on_release(self, event) -> None:
        if self._start is None:
            return
        x0, y0 = self._start
        left = min(x0, event.x)
        top = min(y0, event.y)
        width = abs(event.x - x0)
        height = abs(event.y - y0)

        if width < _MIN_SIZE or height < _MIN_SIZE:
            # 尺寸过小视为误触，继续选择
            if self._rect_id is not None:
                self._canvas.delete(self._rect_id)
                self._rect_id = None
            self._start = None
            if self._hint_id is not None:
                self._canvas.itemconfigure(self._hint_id, state="normal",
                                           text="区域太小，请重新框选")
            return

        ox, oy = self._origin
        self.result = (ox + left, oy + top, width, height)
        self._close()
