# -*- coding: utf-8 -*-
"""识别框：常驻的半透明区域指示与交互。

设计要点
--------
1. **中间必须是真镂空**。用「顶栏 + 四条边框」共 5 个无边框窗口拼出矩形轮廓，
   矩形内部不覆盖任何窗口，因此点击/滚动会自然穿透到下面的页面。
   （若改用单窗口 + 透明色键，Tk 的 `-alpha` 与 `-transparentcolor` 会互相
   覆盖同一套分层窗口标志位，行为依赖系统实现；拼条是确定可行的做法。）
2. **所有部件都画在扫描区域之外**，边框贴在外沿，所以永远不会被 OCR 拍进去。
3. 拖动/缩放只更新本对象的 region 并回调主进程，主进程写入共享内存后
   截屏进程下一拍就跟随，不需要重启识别。
4. 鼠标滚轮在框上滚动即可调整透明度，配合控制面板的滑杆使用。
"""

from __future__ import annotations

import tkinter as tk
from typing import Any, Callable, Dict, Optional, Tuple

from .theme import Theme

Region = Tuple[int, int, int, int]

# 边框拖拽模式 -> 鼠标指针
_EDGES = (
    ("top", "sb_v_double_arrow"),
    ("bottom", "sb_v_double_arrow"),
    ("left", "sb_h_double_arrow"),
    ("right", "sb_h_double_arrow"),
)

_ALPHA_STEP = 0.05


class CaptureFrame:
    """可拖动、可缩放、可调透明度的常驻识别框。"""

    MIN_W = 48
    MIN_H = 24

    def __init__(self, root: tk.Misc, theme: Theme, cfg: Dict[str, Any],
                 on_region_change: Optional[Callable[[Region, bool], None]] = None,
                 on_close: Optional[Callable[[], None]] = None):
        self.root = root
        self.theme = theme
        self._on_region_change = on_region_change
        self._on_close = on_close

        c = theme.colors
        self._border_color = str(cfg.get("color") or c["primary"])
        self._alpha = self._clamp_alpha(cfg.get("alpha", 0.75))
        self._border = max(4, min(30, int(cfg.get("border", 10))))
        self._header_h = max(18, min(40, int(cfg.get("header_height", 24))))

        try:
            region = tuple(int(v) for v in (cfg.get("region") or (220, 220, 520, 180)))
        except Exception:
            region = (220, 220, 520, 180)
        self._region: Region = region if len(region) == 4 else (220, 220, 520, 180)

        self._visible = False
        self._drag: Optional[Tuple[str, int, int, Region]] = None
        self._state = "idle"          # idle | running | paused
        self._windows: Dict[str, tk.Toplevel] = {}
        self._build()
        self._layout()
        self._apply_alpha()

    # ================================================================ 构建

    @staticmethod
    def _clamp_alpha(value: Any) -> float:
        try:
            return max(0.15, min(1.0, float(value)))
        except Exception:
            return 0.75

    def _make_window(self, name: str, cursor: str, bg: Optional[str] = None) -> tk.Toplevel:
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.configure(bg=bg or self._border_color)
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        try:
            win.attributes("-alpha", self._alpha)
        except Exception:
            pass
        win.configure(cursor=cursor)
        win.withdraw()
        self._windows[name] = win
        return win

    def _build(self) -> None:
        c = self.theme.colors

        # ---- 顶栏：拖动 + 状态显示 + 隐藏按钮 ----
        header = self._make_window("header", "fleur", self._header_bg())
        self._info = tk.Label(
            header, text="", bg=self._header_bg(), fg="#FFFFFF",
            font=self.theme.font(9, "bold"), anchor="w", padx=8,
        )
        self._info.pack(side="left", fill="both", expand=True)

        self._close = tk.Label(
            header, text="✕", bg=self._header_bg(), fg="#FFFFFF",
            font=self.theme.font(10, "bold"), padx=8, cursor="hand2",
        )
        self._close.pack(side="right", fill="y")

        # 顶栏本体（含状态文字）负责拖动；关闭按钮单独绑，不参与拖动。
        # 注意：<Button-1> 与 <ButtonPress-1> 在 Tk 里是同一个事件序列，
        # 先绑 <Button-1> 再绑 <ButtonPress-1> 会把前者覆盖掉，所以只绑一次。
        for widget in (header, self._info):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._on_motion)
            widget.bind("<ButtonRelease-1>", self._end_drag)
            widget.bind("<MouseWheel>", self._on_wheel)
        self._close.bind("<ButtonPress-1>", self._on_close_press)
        self._close.bind("<MouseWheel>", self._on_wheel)

        # ---- 四条边框：缩放（同时也支持滚轮调透明度）----
        for name, cursor in _EDGES:
            win = self._make_window(name, cursor)
            win.bind("<ButtonPress-1>", lambda e, m=name: self._start_resize(m, e))
            win.bind("<B1-Motion>", self._on_motion)
            win.bind("<ButtonRelease-1>", self._end_drag)
            win.bind("<MouseWheel>", self._on_wheel)

        self._update_info()

    def _header_bg(self) -> str:
        c = self.theme.colors
        if self._state == "running":
            return self._border_color
        if self._state == "paused":
            return c["warn"]
        return c["subtext"]

    # ================================================================ 布局

    def _layout(self) -> None:
        x, y, w, h = self._region
        b = self._border
        hh = self._header_h
        geo = {
            # 顶栏放在上边框之外
            "header": f"{max(120, w)}x{hh}+{x}+{y - b - hh}",
            "top": f"{w}x{b}+{x}+{y - b}",
            "bottom": f"{w}x{b}+{x}+{y + h}",
            "left": f"{b}x{h}+{x - b}+{y}",
            "right": f"{b}x{h}+{x + w}+{y}",
        }
        for name, geometry in geo.items():
            win = self._windows.get(name)
            if win is None:
                continue
            try:
                win.geometry(geometry)
            except Exception:
                pass

    def _bounds(self) -> Region:
        """虚拟桌面范围，用于把框限制在屏幕内。"""
        try:
            import mss

            factory = getattr(mss, "MSS", None) or mss.mss
            with factory() as sct:
                mon = sct.monitors[0]
                return (int(mon["left"]), int(mon["top"]),
                        int(mon["width"]), int(mon["height"]))
        except Exception:
            return (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())

    def _normalize(self, region: Region) -> Region:
        """限制最小尺寸并把框夹在屏幕内。"""
        x, y, w, h = (int(v) for v in region)
        w = max(self.MIN_W, w)
        h = max(self.MIN_H, h)
        left, top, vw, vh = self._bounds()
        # 允许边框条部分出屏，但保证可拖拽区可见
        max_w = max(self.MIN_W, vw - 2 * self._border)
        max_h = max(self.MIN_H, vh - 2 * self._border - self._header_h)
        w = min(w, max_w)
        h = min(h, max_h)
        x = max(left + self._border, min(x, left + vw - w - self._border))
        y = max(top + self._border + self._header_h,
                min(y, top + vh - h - self._border))
        return (x, y, w, h)

    # ================================================================ 显隐

    def show(self) -> None:
        if self._visible:
            return
        self._visible = True
        self._layout()
        for name in ("top", "bottom", "left", "right", "header"):
            win = self._windows.get(name)
            if win is None:
                continue
            try:
                win.deiconify()
                win.lift()
                if name == "header" or name in ("top", "left", "right", "bottom"):
                    win.attributes("-topmost", True)
            except Exception:
                pass
        self._update_info()

    def hide(self) -> None:
        if not self._visible:
            return
        self._visible = False
        for win in self._windows.values():
            try:
                win.withdraw()
            except Exception:
                pass

    def toggle(self) -> None:
        self.hide() if self._visible else self.show()

    @property
    def visible(self) -> bool:
        return self._visible

    def destroy(self) -> None:
        self.hide()
        for win in self._windows.values():
            try:
                win.destroy()
            except Exception:
                pass
        self._windows.clear()

    # ================================================================ 外观

    def _apply_alpha(self) -> None:
        for win in self._windows.values():
            try:
                win.attributes("-alpha", self._alpha)
            except Exception:
                pass

    def set_alpha(self, value: Any) -> float:
        self._alpha = self._clamp_alpha(value)
        self._apply_alpha()
        self._update_info()
        return self._alpha

    @property
    def alpha(self) -> float:
        return self._alpha

    def cycle_alpha(self) -> float:
        steps = [0.35, 0.55, 0.75, 0.9, 1.0]
        current = min(steps, key=lambda s: abs(s - self._alpha))
        nxt = steps[(steps.index(current) + 1) % len(steps)]
        return self.set_alpha(nxt)

    def set_border(self, value: Any) -> int:
        try:
            self._border = max(4, min(30, int(float(value))))
        except Exception:
            return self._border
        self._layout()
        return self._border

    def set_state(self, state: str) -> None:
        """state: running | paused | idle，顶栏颜色随之变化。"""
        self._state = state if state in ("running", "paused", "idle") else "idle"
        bg = self._header_bg()
        for name, win in self._windows.items():
            try:
                if name == "header":
                    win.configure(bg=bg)
                elif name in ("top", "bottom", "left", "right"):
                    win.configure(bg=self._border_color if state != "paused"
                                  else self.theme.colors["warn"])
            except Exception:
                pass
        try:
            self._info.configure(bg=bg)
            self._close.configure(bg=bg)
        except Exception:
            pass
        self._update_info()

    def _update_info(self) -> None:
        x, y, w, h = self._region
        pct = int(round(self._alpha * 100))
        label = {"running": "● 识别中", "paused": "❚❚ 已暂停", "idle": "○ 待机"}[self._state]
        if w >= 430:
            text = (f"{label}   {w}×{h}   透明度 {pct}%   "
                    f"拖动移动 · 边缘缩放 · 滚轮透明")
        else:
            text = f"{label}  {w}×{h}  {pct}%"
        try:
            self._info.configure(text=text)
        except Exception:
            pass

    # ================================================================ 区域

    @property
    def region(self) -> Region:
        return self._region

    def set_region(self, region: Region, notify: bool = False, final: bool = False) -> None:
        self._region = self._normalize(tuple(int(v) for v in region))
        self._layout()
        self._update_info()
        if notify and self._on_region_change:
            try:
                self._on_region_change(self._region, final)
            except Exception:
                pass

    # ================================================================ 交互

    def _on_close_press(self, _event=None) -> str:
        """点 ✕：交由主进程决定（默认停识别 + 收起识别框 + 唤回主面板）。"""
        if self._on_close:
            try:
                self._on_close()
            except Exception:
                pass
        else:
            self.hide()
        return "break"

    def _start_drag(self, event) -> None:
        self._drag = ("move", int(event.x_root), int(event.y_root), self._region)

    def _start_resize(self, mode: str, event) -> None:
        self._drag = (mode, int(event.x_root), int(event.y_root), self._region)

    def _on_motion(self, event) -> None:
        if self._drag is None:
            return
        mode, x0, y0, region = self._drag
        dx = int(event.x_root) - x0
        dy = int(event.y_root) - y0
        x, y, w, h = region

        if mode == "move":
            nx, ny, nw, nh = x + dx, y + dy, w, h
        elif mode == "left":
            dx = min(dx, w - self.MIN_W)          # 不允许拖过右边
            nx, nw, ny, nh = x + dx, w - dx, y, h
        elif mode == "right":
            nx, nw, ny, nh = x, max(self.MIN_W, w + dx), y, h
        elif mode == "top":
            dy = min(dy, h - self.MIN_H)          # 不允许拖过下边
            nx, nw, ny, nh = x, w, y + dy, h - dy
        elif mode == "bottom":
            nx, nw, ny, nh = x, w, y, max(self.MIN_H, h + dy)
        else:
            return

        self.set_region((nx, ny, nw, nh), notify=True, final=False)

    def _end_drag(self, _event=None) -> None:
        if self._drag is None:
            return
        self._drag = None
        # 松手时补发一次最终区域，主进程据此触发一次立即识别
        self.set_region(self._region, notify=True, final=True)

    def _on_wheel(self, event) -> str:
        delta = getattr(event, "delta", 0) or 0
        if delta == 0:
            return "break"
        steps = max(1, abs(delta) // 120)
        step = _ALPHA_STEP * steps * (1 if delta > 0 else -1)
        self.set_alpha(self._alpha + step)
        if self._on_region_change:
            try:
                self._on_region_change(self._region, True)
            except Exception:
                pass
        return "break"
