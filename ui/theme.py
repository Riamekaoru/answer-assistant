# -*- coding: utf-8 -*-
"""统一视觉主题。

全部使用浅色配色 + 高对比文字，浮窗额外提供深色方案以便在深色屏幕上叠加。
字体优先微软雅黑，缺失时逐级降级。
"""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Any, Dict, Optional

# ---------------------------------------------------------------- 配色

LIGHT: Dict[str, str] = {
    "bg": "#F2F4F7",
    "panel": "#FFFFFF",
    "panel_alt": "#F7F8FA",
    "border": "#DFE3E8",
    "border_strong": "#C6CBD4",
    "text": "#1F2329",
    "subtext": "#646A73",
    "muted": "#8F959E",
    "primary": "#0052D9",
    "primary_hover": "#366EF4",
    "primary_soft": "#EAF1FF",
    "success": "#00A870",
    "success_soft": "#E8F8F2",
    "warn": "#E37318",
    "warn_soft": "#FFF3E8",
    "error": "#D54941",
    "error_soft": "#FDECEC",
    "shadow": "#D9DDE3",
    "scrollbar": "#C6CBD4",
}

DARK: Dict[str, str] = {
    "bg": "#1A1D21",
    "panel": "#23272E",
    "panel_alt": "#2A2F37",
    "border": "#353B45",
    "border_strong": "#4A515C",
    "text": "#F2F4F7",
    "subtext": "#B6BCC6",
    "muted": "#8A919C",
    "primary": "#4B8BFF",
    "primary_hover": "#6BA1FF",
    "primary_soft": "#1E2A40",
    "success": "#3FCF8E",
    "success_soft": "#16302A",
    "warn": "#F0A24A",
    "warn_soft": "#33261A",
    "error": "#F26D6D",
    "error_soft": "#331E1E",
    "shadow": "#121417",
    "scrollbar": "#4A515C",
}

THEMES = {"light": LIGHT, "dark": DARK}


def pick_theme(name: str) -> Dict[str, str]:
    return THEMES.get((name or "light").lower(), LIGHT)


# ---------------------------------------------------------------- 字体

_CJK_CANDIDATES = ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
                   "Noto Sans CJK SC", "SimHei", "SimSun")
_MONO_CANDIDATES = ("Cascadia Mono", "Consolas", "JetBrains Mono", "Courier New")


def resolve_family(candidates, fallback: str = "TkDefaultFont") -> str:
    try:
        available = set(tkfont.families())
    except Exception:
        return fallback
    for name in candidates:
        if name in available:
            return name
    return fallback


class Theme:
    """主题对象：持有配色、字体名与尺寸，供各窗口共用。"""

    def __init__(self, root: tk.Misc, mode: str = "light", scale: float = 1.0):
        self.mode = mode if mode in THEMES else "light"
        self.colors = pick_theme(self.mode)
        self.family = resolve_family(_CJK_CANDIDATES)
        self.mono = resolve_family(_MONO_CANDIDATES, "Courier New")
        self.scale = max(1.0, float(scale))
        self.root = root
        self._dpi_applied = False

    # -- 字体 ---------------------------------------------------------

    def font(self, size: int = 10, weight: str = "normal") -> tuple:
        return (self.family, size, weight)

    def mono_font(self, size: int = 9) -> tuple:
        return (self.mono, size)

    # -- 应用 ---------------------------------------------------------

    def apply(self) -> None:
        """设置 Tk 全局缩放与 ttk 样式（幂等）。"""
        if not self._dpi_applied:
            try:
                # tk scaling 影响"点"字号的实际像素，几何尺寸仍按物理像素，故安全
                self.root.tk.call("tk", "scaling", self.scale * 96.0 / 72.0)
            except Exception:
                pass
            self._dpi_applied = True
        self._style_ttk()

    def _style_ttk(self) -> None:
        c = self.colors
        try:
            style = ttk.Style(self.root)
            if "clam" in style.theme_names():
                style.theme_use("clam")

            style.configure(".", background=c["bg"], foreground=c["text"],
                            font=self.font(10), borderwidth=0, focuscolor=c["primary"])
            style.configure("TFrame", background=c["bg"])
            style.configure("Panel.TFrame", background=c["panel"])
            style.configure("TLabel", background=c["bg"], foreground=c["text"])
            style.configure("Panel.TLabel", background=c["panel"], foreground=c["text"])
            style.configure("Sub.TLabel", background=c["bg"], foreground=c["subtext"],
                            font=self.font(9))
            style.configure("PanelSub.TLabel", background=c["panel"],
                            foreground=c["subtext"], font=self.font(9))
            style.configure("H1.TLabel", background=c["bg"], foreground=c["text"],
                            font=self.font(14, "bold"))
            style.configure("H2.TLabel", background=c["panel"], foreground=c["text"],
                            font=self.font(11, "bold"))

            style.configure("TNotebook", background=c["bg"], borderwidth=0)
            style.configure("TNotebook.Tab", background=c["panel_alt"],
                            foreground=c["subtext"], padding=(16, 8),
                            font=self.font(10))
            style.map("TNotebook.Tab",
                      background=[("selected", c["panel"])],
                      foreground=[("selected", c["primary"])])

            style.configure("TEntry", fieldbackground=c["panel"], foreground=c["text"],
                            bordercolor=c["border"], lightcolor=c["border"],
                            darkcolor=c["border"], insertcolor=c["text"], padding=6)
            style.configure("TCombobox", fieldbackground=c["panel"], background=c["panel"],
                            foreground=c["text"], arrowcolor=c["subtext"],
                            bordercolor=c["border"], padding=5)
            style.map("TCombobox",
                      fieldbackground=[("readonly", c["panel"])],
                      foreground=[("readonly", c["text"])])
            self.root.option_add("*TCombobox*Listbox.background", c["panel"])
            self.root.option_add("*TCombobox*Listbox.foreground", c["text"])
            self.root.option_add("*TCombobox*Listbox.selectBackground", c["primary"])
            self.root.option_add("*TCombobox*Listbox.selectForeground", "#FFFFFF")
            self.root.option_add("*TCombobox*Listbox.font", self.font(10))

            style.configure("TCheckbutton", background=c["panel"], foreground=c["text"],
                            font=self.font(10))
            style.map("TCheckbutton", background=[("active", c["panel"])],
                      indicatorcolor=[("selected", c["primary"]),
                                      ("!selected", c["panel"])])

            style.configure("TScale", background=c["panel"], troughcolor=c["panel_alt"],
                            bordercolor=c["border"], lightcolor=c["primary"],
                            darkcolor=c["primary"])
            style.configure("Vertical.TScrollbar", background=c["scrollbar"],
                            troughcolor=c["panel_alt"], bordercolor=c["panel_alt"],
                            arrowcolor=c["subtext"], width=11)
            style.map("Vertical.TScrollbar",
                      background=[("active", c["border_strong"])])
            style.configure("TSeparator", background=c["border"])
            style.configure("TProgressbar", background=c["primary"],
                            troughcolor=c["panel_alt"], bordercolor=c["panel_alt"],
                            lightcolor=c["primary"], darkcolor=c["primary"], thickness=4)
        except Exception:
            pass

    # -- 便捷构件 -----------------------------------------------------

    def button(self, parent, text: str, command=None, kind: str = "default",
               width: Optional[int] = None, **kw) -> tk.Button:
        c = self.colors
        palettes = {
            "primary": (c["primary"], "#FFFFFF", c["primary_hover"]),
            "default": (c["panel"], c["text"], c["panel_alt"]),
            "ghost": (c["bg"], c["subtext"], c["panel_alt"]),
            "danger": (c["error"], "#FFFFFF", "#E0655D"),
        }
        bg, fg, hover = palettes.get(kind, palettes["default"])
        btn = tk.Button(
            parent, text=text, command=command,
            bg=bg, fg=fg, activebackground=hover, activeforeground=fg,
            relief="flat", bd=0, highlightthickness=1,
            highlightbackground=c["border"] if kind == "default" else bg,
            highlightcolor=c["primary"],
            font=self.font(10, "bold" if kind == "primary" else "normal"),
            cursor="hand2", padx=12, pady=6, **kw,
        )
        if width:
            btn.configure(width=width)
        btn.bind("<Enter>", lambda _e, b=btn, h=hover: b.configure(bg=h))
        btn.bind("<Leave>", lambda _e, b=btn, bg0=bg: b.configure(bg=bg0))
        return btn

    def badge(self, parent, bg_key: str = "primary_soft", fg_key: str = "primary",
              **kw) -> tk.Label:
        c = self.colors
        return tk.Label(parent, bg=c[bg_key], fg=c[fg_key], font=self.font(9),
                        padx=8, pady=2, **kw)
