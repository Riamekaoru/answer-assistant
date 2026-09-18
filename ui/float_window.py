# -*- coding: utf-8 -*-
"""答案浮窗。

设计取舍：
    1. 用 overrideredirect 自绘标题栏 —— 系统级无边框窗口不会抢焦点，
       这样在答题页面上打字时浮窗弹出不会打断输入，也不会进入 Alt-Tab。
    2. 正文用 Text + tag 渲染，既能富文本高亮选项答案，又天然支持滚动与选字复制。
    3. 多候选以单行列表展示，点击即可切换查看，兼顾"答错了要换一个"的场景。
"""

from __future__ import annotations

import tkinter as tk
from typing import Any, Dict, List, Optional

from .theme import Theme

_HINT = "F2 开始/暂停   F1 重划区域   F3 隐藏浮窗   F4 立即识别   F7 透明度   F8 退出"


class FloatWindow:
    """置顶答案浮窗。"""

    def __init__(self, root: tk.Misc, theme: Theme, cfg: Dict[str, Any],
                 on_close=None, on_geometry=None):
        self.root = root
        self.theme = theme
        self.cfg = cfg
        self.on_close = on_close
        self.on_geometry = on_geometry

        self._alpha = float(cfg.get("alpha", 0.95))
        self._font_size = int(cfg.get("font_size", 11))
        self._topmost = bool(cfg.get("topmost", True))
        self._visible = False
        self._matches: List[Dict[str, Any]] = []
        self._index = 0
        self._drag_origin = (0, 0)
        self._resize_origin = None

        self._build()
        self._apply_fonts()

    # ---------------------------------------------------------------- 构建

    def _build(self) -> None:
        c = self.theme.colors
        cfg = self.cfg

        win = tk.Toplevel(self.root)
        self.win = win
        win.overrideredirect(True)
        win.configure(bg=c["border_strong"])
        win.attributes("-topmost", self._topmost)
        if not self._topmost:
            win.attributes("-topmost", False)
        try:
            win.attributes("-alpha", self._alpha)
        except Exception:
            pass

        width = int(cfg.get("width", 470))
        height = int(cfg.get("height", 400))
        x = cfg.get("x")
        y = cfg.get("y")
        if x is None or y is None:
            sw = win.winfo_screenwidth()
            x = max(20, sw - width - 40)
            y = 90
        win.geometry(f"{width}x{height}+{int(x)}+{int(y)}")

        # 外框（1px 描边效果）
        outer = tk.Frame(win, bg=c["panel"])
        outer.pack(fill="both", expand=True, padx=1, pady=1)

        # ---- 标题栏 ----
        header = tk.Frame(outer, bg=c["primary"], height=30)
        header.pack(fill="x")
        header.pack_propagate(False)

        self.dot = tk.Canvas(header, width=14, height=14, bg=c["primary"],
                             highlightthickness=0)
        self.dot.pack(side="left", padx=(9, 4))
        self._dot_id = self.dot.create_oval(3, 3, 11, 11, fill="#FFFFFF",
                                            outline="", tags="dot")

        self.title_label = tk.Label(header, text="答题助手", bg=c["primary"],
                                    fg="#FFFFFF", font=self.theme.font(10, "bold"))
        self.title_label.pack(side="left")

        self.score_label = tk.Label(header, text="", bg=c["primary"], fg="#FFFFFF",
                                    font=self.theme.font(10, "bold"))
        self.score_label.pack(side="left", padx=10)

        close_btn = tk.Label(header, text="✕", bg=c["primary"], fg="#FFFFFF",
                             font=self.theme.font(11, "bold"), padx=10, cursor="hand2")
        close_btn.pack(side="right")
        close_btn.bind("<Button-1>", lambda _e: self.hide())

        pin_btn = tk.Label(header, text="⤓", bg=c["primary"], fg="#FFFFFF",
                           font=self.theme.font(11, "bold"), padx=8, cursor="hand2")
        pin_btn.pack(side="right")
        pin_btn.bind("<Button-1>", lambda _e: self._toggle_pin())

        for widget in (header, self.title_label, self.score_label, self.dot):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag)

        # ---- 正文 ----
        body = tk.Frame(outer, bg=c["panel"])
        body.pack(fill="both", expand=True)

        self.text = tk.Text(
            body, wrap="word", bg=c["panel"], fg=c["text"], relief="flat",
            bd=0, padx=12, pady=10, cursor="arrow", insertwidth=0,
            highlightthickness=0, spacing2=2, spacing3=3,
            selectbackground=c["primary_soft"], selectforeground=c["text"],
        )
        self.scroll = tk.Scrollbar(body, command=self.text.yview, width=10,
                                   relief="flat", bd=0, troughcolor=c["panel_alt"],
                                   bg=c["scrollbar"], activebackground=c["border_strong"],
                                   highlightthickness=0)
        self.text.configure(yscrollcommand=self.scroll.set)
        self.scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.configure(state="disabled")

        # ---- 底栏 ----
        footer = tk.Frame(outer, bg=c["panel_alt"], height=34)
        footer.pack(fill="x", side="bottom")
        footer.pack_propagate(False)

        self.status_label = tk.Label(footer, text="待机", bg=c["panel_alt"],
                                     fg=c["subtext"], font=self.theme.font(9),
                                     anchor="w")
        self.status_label.pack(side="left", padx=10)

        self.grip = tk.Label(footer, text="◢", bg=c["panel_alt"], fg=c["muted"],
                             font=self.theme.font(11), cursor="sizing")
        self.grip.pack(side="right", padx=4)
        self.grip.bind("<ButtonPress-1>", self._start_resize)
        self.grip.bind("<B1-Motion>", self._resize)

        self.next_btn = tk.Label(footer, text="下一个候选", bg=c["panel_alt"],
                                 fg=c["primary"], font=self.theme.font(9, "bold"),
                                 cursor="hand2")
        self.next_btn.pack(side="right", padx=8)
        self.next_btn.bind("<Button-1>", lambda _e: self.next_candidate())

        self.copy_btn = tk.Label(footer, text="复制答案", bg=c["panel_alt"],
                                 fg=c["primary"], font=self.theme.font(9, "bold"),
                                 cursor="hand2")
        self.copy_btn.pack(side="right", padx=8)
        self.copy_btn.bind("<Button-1>", lambda _e: self.copy_answer())

        self.win.withdraw()

    # ---------------------------------------------------------------- 主题/字体

    def _apply_fonts(self) -> None:
        c = self.theme.colors
        size = self._font_size
        t = self.text
        t.configure(bg=c["panel"], fg=c["text"])
        t.tag_configure("hint", font=self.theme.font(size - 1), foreground=c["subtext"])
        t.tag_configure("muted", font=self.theme.font(size - 2), foreground=c["muted"])
        t.tag_configure("section", font=self.theme.font(size - 1, "bold"),
                        foreground=c["primary"], spacing1=8, spacing3=2)
        t.tag_configure("question", font=self.theme.font(size + 1), foreground=c["text"],
                        spacing1=2, spacing3=4)
        t.tag_configure("option", font=self.theme.font(size), foreground=c["subtext"],
                        lmargin1=12, lmargin2=24, spacing3=1)
        t.tag_configure("option_hit", font=self.theme.font(size, "bold"),
                        foreground=c["success"], lmargin1=12, lmargin2=24,
                        background=c["success_soft"], spacing3=1)
        t.tag_configure("answer", font=self.theme.font(size + 4, "bold"),
                        foreground=c["success"], spacing1=4, spacing3=4)
        t.tag_configure("analysis", font=self.theme.font(size - 1), foreground=c["subtext"],
                        lmargin1=8, lmargin2=8, spacing3=3)
        t.tag_configure("cand", font=self.theme.font(size - 1), foreground=c["subtext"],
                        spacing3=2)
        t.tag_configure("cand_sel", font=self.theme.font(size - 1, "bold"),
                        foreground=c["primary"])
        t.tag_configure("score_hi", font=self.theme.font(size, "bold"), foreground=c["success"])
        t.tag_configure("score_mid", font=self.theme.font(size, "bold"), foreground=c["warn"])
        t.tag_configure("score_lo", font=self.theme.font(size, "bold"), foreground=c["error"])
        t.tag_configure("warn", font=self.theme.font(size - 1), foreground=c["warn"])
        t.tag_configure("ocr", font=self.theme.mono_font(size - 2), foreground=c["muted"],
                        lmargin1=8, lmargin2=8)
        t.tag_configure("divider", font=self.theme.font(4), spacing1=6, spacing3=2)

    def set_font_size(self, size: int) -> None:
        self._font_size = max(8, min(22, int(size)))
        self._apply_fonts()
        if self._matches:
            self._render()

    # ---------------------------------------------------------------- 显隐

    def show(self) -> None:
        if self._visible:
            return
        self._visible = True
        try:
            self.win.deiconify()
            self.win.lift()
            if self._topmost:
                self.win.attributes("-topmost", True)
        except Exception:
            pass

    def hide(self) -> None:
        if not self._visible:
            return
        self._visible = False
        try:
            self.win.withdraw()
        except Exception:
            pass
        self._notify_geometry()

    def toggle(self) -> None:
        self.hide() if self._visible else self.show()

    def raise_up(self) -> None:
        """把浮窗重新提到最前（识别框显示后调用，避免答案被压住）。"""
        if not self._visible:
            return
        try:
            self.win.lift()
            if self._topmost:
                self.win.attributes("-topmost", True)
        except Exception:
            pass

    @property
    def visible(self) -> bool:
        return self._visible

    def _toggle_pin(self) -> None:
        self._topmost = not self._topmost
        try:
            self.win.attributes("-topmost", self._topmost)
        except Exception:
            pass
        self.set_status("已置顶" if self._topmost else "已取消置顶")

    def set_alpha(self, value: float) -> None:
        self._alpha = max(0.25, min(1.0, float(value)))
        try:
            self.win.attributes("-alpha", self._alpha)
        except Exception:
            pass

    def cycle_alpha(self) -> float:
        steps = [0.55, 0.75, 0.88, 0.95, 1.0]
        current = min(steps, key=lambda s: abs(s - self._alpha))
        idx = (steps.index(current) + 1) % len(steps)
        self.set_alpha(steps[idx])
        self.set_status(f"透明度 {int(self._alpha * 100)}%")
        return self._alpha

    def set_size(self, width: int, height: int) -> None:
        x, y, _, _ = self.geometry()
        try:
            self.win.geometry(f"{int(width)}x{int(height)}+{x}+{y}")
        except Exception:
            pass

    def set_topmost(self, value: bool) -> None:
        self._topmost = bool(value)
        try:
            self.win.attributes("-topmost", self._topmost)
        except Exception:
            pass

    def move_to_corner(self) -> None:
        """把浮窗复位到主屏右上角。"""
        width = max(300, self.win.winfo_width())
        sw = self.win.winfo_screenwidth()
        try:
            self.win.geometry(f"{width}x{self.win.winfo_height()}+{sw - width - 40}+90")
        except Exception:
            pass
        self._notify_geometry()

    @property
    def alpha(self) -> float:
        return self._alpha

    # ---------------------------------------------------------------- 拖动 / 缩放

    def _start_drag(self, event) -> None:
        self._drag_origin = (event.x_root - self.win.winfo_x(),
                             event.y_root - self.win.winfo_y())

    def _drag(self, event) -> None:
        dx, dy = self._drag_origin
        self.win.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

    def _start_resize(self, event) -> None:
        self._resize_origin = (event.x_root, event.y_root,
                               self.win.winfo_width(), self.win.winfo_height())

    def _resize(self, event) -> None:
        if not self._resize_origin:
            return
        x0, y0, w0, h0 = self._resize_origin
        w = max(280, w0 + (event.x_root - x0))
        h = max(160, h0 + (event.y_root - y0))
        self.win.geometry(f"{w}x{h}")

    def geometry(self) -> tuple:
        return (self.win.winfo_x(), self.win.winfo_y(),
                self.win.winfo_width(), self.win.winfo_height())

    def _notify_geometry(self) -> None:
        if self.on_geometry:
            try:
                self.on_geometry(self.geometry())
            except Exception:
                pass

    # ---------------------------------------------------------------- 渲染

    def set_status(self, text: str) -> None:
        try:
            self.status_label.configure(text=text)
        except Exception:
            pass

    def set_indicator(self, state: str) -> None:
        """state: running | paused | error | idle"""
        colors = {"running": "#3FCF8E", "paused": "#FFC53D",
                  "error": "#F26D6D", "idle": "#C6CBD4"}
        try:
            self.dot.itemconfigure(self._dot_id, fill=colors.get(state, "#C6CBD4"))
        except Exception:
            pass

    def clear(self) -> None:
        self._matches = []
        self._index = 0
        self._write([("暂无匹配结果\n", "hint"),
                     ("按 F2 开始识别，或按 F4 立即识别一次。\n", "hint"),
                     (f"操作提示：{_HINT}\n", "muted")])
        self.score_label.configure(text="")

    def set_result(self, payload: Dict[str, Any]) -> None:
        """渲染一次识别结果。"""
        self._matches = payload.get("matches") or []
        self._index = 0
        self._last_payload = payload

        if not self._matches:
            self._render_empty(payload)
            return

        self._render()

    def _render_empty(self, payload: Dict[str, Any]) -> None:
        self.score_label.configure(text="")
        raw = (payload.get("text") or "").strip()
        quality = payload.get("quality") or ""
        parts = [("未在题库中匹配到题目\n", "warn")]
        if quality:
            parts.append((f"{quality}\n", "muted"))
        if raw:
            parts.append(("识别到的文字：\n", "section"))
            parts.append((raw[:600] + ("…" if len(raw) > 600 else ""), "ocr"))
        else:
            parts.append(("本次未识别到任何文字，请检查扫描区域是否覆盖到题干。\n", "muted"))
        parts.append(("\n处理耗时：", "muted"))
        parts.append((f"OCR {payload.get('ocr_ms', 0)}ms", "muted"))
        self._write(parts)
        self.set_status("无匹配")

    def _score_tag(self, score: float) -> str:
        if score >= 0.85:
            return "score_hi"
        if score >= 0.7:
            return "score_mid"
        return "score_lo"

    def _render(self) -> None:
        m = self._matches[self._index]
        q = m.get("question") or {}
        score = float(m.get("score", 0.0))
        parts: List[tuple] = []

        self.score_label.configure(text=f"匹配度 {score * 100:.1f}%")

        # 头部信息
        qtype = q.get("qtype") or "未分类"
        parts.append((f"{qtype}", "section"))
        if len(self._matches) > 1:
            parts.append((f"   ·   候选 {self._index + 1}/{len(self._matches)}", "muted"))
        parts.append(("\n", "divider"))

        # 题目
        parts.append(("题目\n", "section"))
        parts.append(((q.get("question") or "").strip() + "\n", "question"))

        # 选项
        options = q.get("options") or []
        if options:
            letters = set((m.get("answer_letters") or q.get("answer_letters") or "").upper())
            parts.append(("选项\n", "section"))
            for opt in options:
                head = str(opt)[:1].upper()
                if head in letters:
                    parts.append((f"✓ {opt}\n", "option_hit"))
                else:
                    parts.append((f"　{opt}\n", "option"))

        # 答案
        parts.append(("答案\n", "section"))
        ans = (m.get("answer_display") or q.get("answer") or "（题库未提供答案）").strip()
        parts.append((ans + "\n", "answer"))

        # 解析
        analysis = (q.get("analysis") or "").strip()
        if analysis:
            parts.append(("解析\n", "section"))
            parts.append((analysis + "\n", "analysis"))

        # 其他候选：每行挂一个独立标签，点击即可切换
        if len(self._matches) > 1:
            parts.append(("\n", "divider"))
            parts.append(("其他候选（点击切换）\n", "section"))
            for i, cand in enumerate(self._matches):
                if i == self._index:
                    continue
                cq = cand.get("question") or {}
                text = (cq.get("question") or "").replace("\n", " ")
                if len(text) > 42:
                    text = text[:42] + "…"
                # 同时挂公共样式标签与行级可点击标签
                parts.append((f"[{i + 1}] {cand.get('score', 0) * 100:.0f}%  {text}\n",
                              ("cand", f"cand_line{i}")))

        # 过程信息
        payload = self._last_payload or {}
        parts.append(("\n", "divider"))
        parts.append((f"OCR {payload.get('ocr_ms', 0)}ms  ·  "
                      f"匹配 {payload.get('match_ms', 0)}ms  ·  "
                      f"{payload.get('engine', '')}\n", "muted"))

        self._write(parts)
        self.set_status(f"已匹配 {len(self._matches)} 条")

    def _write(self, parts: List[tuple]) -> None:
        t = self.text
        try:
            t.configure(state="normal")
            t.delete("1.0", "end")
            for text, tag in parts:
                t.insert("end", text, tag)
            t.configure(state="disabled")
            t.yview_moveto(0.0)
        except Exception:
            pass

    # ---------------------------------------------------------------- 交互

    def select_candidate(self, index: int) -> None:
        if 0 <= index < len(self._matches):
            self._index = index
            self._render()

    def next_candidate(self) -> None:
        if len(self._matches) > 1:
            self._index = (self._index + 1) % len(self._matches)
            self._render()
        elif not self._matches:
            self.set_status("暂无候选")

    def copy_answer(self) -> None:
        """把当前候选的答案与题干复制到剪贴板。"""
        if not self._matches:
            self.set_status("暂无可复制内容")
            return
        m = self._matches[self._index]
        q = m.get("question") or {}
        payload = (f"题目：{(q.get('question') or '').strip()}\n"
                   f"答案：{(m.get('answer_display') or '').strip()}")
        try:
            self.win.clipboard_clear()
            self.win.clipboard_append(payload)
            self.set_status("已复制到剪贴板")
        except Exception as exc:
            self.set_status(f"复制失败：{exc}")

    def destroy(self) -> None:
        self._notify_geometry()
        try:
            self.win.destroy()
        except Exception:
            pass
