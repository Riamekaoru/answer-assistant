# -*- coding: utf-8 -*-
"""主控制面板。

承担配置的所见即所得调整：任何控件变更都会立刻写入 Config 并推送到工作进程，
不需要重启程序（OCR 引擎切换除外，会在界面上明确提示）。

面板本身只是「视图 + 事件转发」，业务动作全部回调给 app（main.py 中的控制器），
避免 UI 与进程编排互相耦合。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Dict, Optional

from core import question_bank as qb
from core.ocr_engine import ENGINE_LABELS, detect_available
from .theme import Theme

_LANG_LABELS = {
    "ch": "中文（简体）",
    "ch_tra": "中文（繁体）",
    "en": "英文",
    "japan": "日文",
    "korean": "韩文",
}


class ControlPanel:
    """主窗口（Tk root）。"""

    def __init__(self, root: tk.Tk, theme: Theme, cfg: Any, app: Any):
        self.root = root
        self.theme = theme
        self.cfg = cfg
        self.app = app
        self._building = True          # 构建期间抑制回调

        self._vars: Dict[str, Any] = {}
        self.value_labels: Dict[str, tk.Label] = {}
        self._key_vars: Dict[str, tk.StringVar] = {}
        self._suppress = False

        self._build()
        self._load_from_config()
        self._building = False

    # ================================================================ 构建

    def _build(self) -> None:
        c = self.theme.colors
        root = self.root
        root.title("答题助手 · 控制面板")
        root.configure(bg=c["bg"])
        root.minsize(720, 560)

        # ---- 顶部工具条 ----
        top = tk.Frame(root, bg=c["panel"], height=58)
        top.pack(fill="x", side="top")
        top.pack_propagate(False)

        left = tk.Frame(top, bg=c["panel"])
        left.pack(side="left", padx=14)
        tk.Label(left, text="答题助手", bg=c["panel"], fg=c["text"],
                 font=self.theme.font(15, "bold")).pack(anchor="w")
        self.sub_label = tk.Label(left, text="划区 → 识别 → 秒出答案", bg=c["panel"],
                                  fg=c["muted"], font=self.theme.font(9))
        self.sub_label.pack(anchor="w")

        right = tk.Frame(top, bg=c["panel"])
        right.pack(side="right", padx=14)

        self.run_btn = self.theme.button(right, "▶ 开始识别", self.app.toggle_run,
                                        kind="primary")
        self.run_btn.pack(side="left", padx=4)
        self.theme.button(right, "立即识别", self.app.capture_once).pack(side="left", padx=4)
        self.theme.button(right, "隐藏面板", self.app.hide_panel,
                          kind="ghost").pack(side="left", padx=4)

        tk.Frame(root, bg=c["border"], height=1).pack(fill="x")

        # ---- 选项卡 ----
        style = ttk.Style(root)
        nb = ttk.Notebook(root)
        self.notebook = nb
        nb.pack(fill="both", expand=True, padx=10, pady=(8, 4))

        self.tab_bank = tk.Frame(nb, bg=c["panel"])
        self.tab_ocr = tk.Frame(nb, bg=c["panel"])
        self.tab_float = tk.Frame(nb, bg=c["panel"])
        self.tab_keys = tk.Frame(nb, bg=c["panel"])
        self.tab_log = tk.Frame(nb, bg=c["panel"])
        nb.add(self.tab_bank, text="  题库  ")
        nb.add(self.tab_ocr, text="  识别设置  ")
        nb.add(self.tab_float, text="  浮窗  ")
        nb.add(self.tab_keys, text="  快捷键  ")
        nb.add(self.tab_log, text="  运行日志  ")

        self._build_bank_tab()
        self._build_ocr_tab()
        self._build_float_tab()
        self._build_keys_tab()
        self._build_log_tab()

        # ---- 状态栏 ----
        tk.Frame(root, bg=c["border"], height=1).pack(fill="x")
        bar = tk.Frame(root, bg=c["panel_alt"], height=30)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self.status_label = tk.Label(bar, text="就绪", bg=c["panel_alt"], fg=c["subtext"],
                                     font=self.theme.font(9), anchor="w")
        self.status_label.pack(side="left", padx=12)
        self.perf_label = tk.Label(bar, text="", bg=c["panel_alt"], fg=c["muted"],
                                   font=self.theme.font(9), anchor="e")
        self.perf_label.pack(side="right", padx=12)

        root.protocol("WM_DELETE_WINDOW", self.app.quit)

        # 让 ttk 用上自定义样式
        _ = style

    # ---------------------------------------------------------------- 通用行

    def _section(self, parent, text: str) -> tk.Frame:
        c = self.theme.colors
        wrap = tk.Frame(parent, bg=c["panel"])
        wrap.pack(fill="x", padx=18, pady=(14, 0))
        tk.Label(wrap, text=text, bg=c["panel"], fg=c["primary"],
                 font=self.theme.font(10, "bold")).pack(anchor="w")
        tk.Frame(wrap, bg=c["border"], height=1).pack(fill="x", pady=(5, 8))
        return wrap

    def _row(self, parent, label: str, hint: str = "") -> tk.Frame:
        c = self.theme.colors
        row = tk.Frame(parent, bg=c["panel"])
        row.pack(fill="x", pady=4)
        tk.Label(row, text=label, bg=c["panel"], fg=c["text"],
                 font=self.theme.font(10), width=13, anchor="w").pack(side="left")
        holder = tk.Frame(row, bg=c["panel"])
        holder.pack(side="left", fill="x", expand=True)
        if hint:
            tk.Label(row, text=hint, bg=c["panel"], fg=c["muted"],
                     font=self.theme.font(8), anchor="e").pack(side="right")
        return holder

    def _scale(self, parent, key: str, lo: float, hi: float, step: float,
               fmt: Callable[[float], str] = lambda v: f"{v:g}") -> tk.Scale:
        c = self.theme.colors
        var = tk.DoubleVar()
        self._vars[key] = var

        def _on_change(raw: str) -> None:
            value = float(raw)
            if step >= 1:
                value = round(value)
            self.value_labels[key].configure(text=fmt(value))
            self._push(key, value)

        scale = tk.Scale(
            parent, variable=var, from_=lo, to=hi, resolution=step,
            orient="horizontal", showvalue=False, command=_on_change,
            bg=c["panel"], fg=c["text"], troughcolor=c["panel_alt"],
            activebackground=c["primary"], highlightthickness=0, bd=0,
            sliderrelief="flat", length=250, sliderlength=18,
        )
        scale.pack(side="left")
        self.value_labels[key] = tk.Label(parent, text=fmt(lo), bg=c["panel"],
                                          fg=c["subtext"], font=self.theme.font(9),
                                          width=8, anchor="w")
        self.value_labels[key].pack(side="left", padx=(8, 0))
        return scale

    def _check(self, parent, key: str, text: str) -> ttk.Checkbutton:
        var = tk.BooleanVar()
        self._vars[key] = var
        cb = ttk.Checkbutton(parent, text=text, variable=var,
                             command=lambda k=key, v=var: self._push(k, bool(v.get())))
        cb.pack(side="left")
        return cb

    def _combo(self, parent, key: str, values: list, width: int = 22) -> ttk.Combobox:
        var = tk.StringVar()
        self._vars[key] = var
        combo = ttk.Combobox(parent, textvariable=var, values=values,
                             state="readonly", width=width)
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>",
                   lambda _e, k=key, v=var: self._push(k, v.get()))
        return combo

    # ---------------------------------------------------------------- 题库页

    def _build_bank_tab(self) -> None:
        c = self.theme.colors
        sec = self._section(self.tab_bank, "题库文件")

        holder = self._row(sec, "题库路径", "支持 docx / doc / xlsx / csv / json / txt")
        self._vars["bank_path"] = tk.StringVar()
        entry = ttk.Entry(holder, textvariable=self._vars["bank_path"])
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda _e: self.app.import_bank(
            self._vars["bank_path"].get()))

        btns = tk.Frame(sec, bg=c["panel"])
        btns.pack(fill="x", pady=(8, 0))
        self.theme.button(btns, "选择文件…", self._browse_bank,
                          kind="primary").pack(side="left", padx=(0, 6))
        self.theme.button(btns, "重新加载", lambda: self.app.import_bank(
            self._vars["bank_path"].get())).pack(side="left", padx=6)
        self.theme.button(btns, "生成示例题库", self.app.make_sample_bank,
                          kind="ghost").pack(side="left", padx=6)
        self.theme.button(btns, "另存为 JSON", self.app.export_bank_json,
                          kind="ghost").pack(side="left", padx=6)

        sec2 = self._section(self.tab_bank, "题库概况")
        self.bank_stats = tk.Label(sec2, text="尚未导入题库", bg=c["panel"], fg=c["subtext"],
                                   font=self.theme.mono_font(9), justify="left", anchor="w")
        self.bank_stats.pack(fill="x")

        sec3 = self._section(self.tab_bank, "导入格式说明")
        tips = (
            "· Excel / CSV：表头需含「题目」「答案」，可选「选项A-D」「解析」「题型」。\n"
            "  标准考试模板中的「答案乱序 / 阅卷人 / 阅卷类型 / 难度设置」会被自动忽略。\n"
            "· Word：支持「1. 题干 / A. 选项 / 答案：A / 解析：xxx」的段落式结构，\n"
            "  也支持两列（题目 | 答案）表格。\n"
            "· JSON：{\"questions\":[{\"question\":\"…\",\"answer\":\"A\",\"options\":[…]}]}\n"
            "  或简写 {\"题干\": \"答案\"}。\n"
            "· 编码：UTF-8 / GBK 自动识别，无需手工转码。"
        )
        tk.Label(sec3, text=tips, bg=c["panel"], fg=c["subtext"],
                 font=self.theme.font(9), justify="left", anchor="w").pack(fill="x")

    # ---------------------------------------------------------------- 识别页

    def _build_ocr_tab(self) -> None:
        c = self.theme.colors
        sec = self._section(self.tab_ocr, "扫描区域")
        holder = self._row(sec, "当前区域", "选中后按 F1 可重划")
        self.region_label = tk.Label(holder, text="未设置", bg=c["panel"], fg=c["subtext"],
                                     font=self.theme.mono_font(9), anchor="w")
        self.region_label.pack(side="left", fill="x", expand=True)
        self.theme.button(holder, "划定区域", self.app.select_region,
                          kind="primary").pack(side="right", padx=(8, 0))
        self.theme.button(holder, "清除", self.app.clear_region,
                          kind="ghost").pack(side="right")

        sec2 = self._section(self.tab_ocr, "常驻识别框")
        self._check(self._row(sec2, "显示"), "capture_frame.show", "开始识别时自动显示")
        self._check(self._row(sec2, "主面板"), "main_window.minimize_on_run",
                    "开始识别时最小化到任务栏")
        self._scale(self._row(sec2, "不透明度"), "capture_frame.alpha", 0.15, 1.0, 0.05,
                    lambda v: f"{v * 100:.0f}%")
        self._scale(self._row(sec2, "边框粗细"), "capture_frame.border", 4, 30, 1,
                    lambda v: f"{int(v)} px")
        btns = tk.Frame(sec2, bg=c["panel"])
        btns.pack(fill="x", pady=(6, 0))
        self.theme.button(btns, "显示 / 隐藏识别框（F9）", self.app.toggle_frame,
                          kind="primary").pack(side="left", padx=(0, 6))
        self.theme.button(btns, "重置到屏幕中央", self.app.reset_frame_position,
                          kind="ghost").pack(side="left", padx=6)
        tk.Label(sec2,
                 text=("点「开始识别」后识别框才出现，同时主面板自动收进任务栏；\n"
                       "点识别框上的 ✕（或按 F9）即停识别、收起识别框并唤回主面板。\n"
                       "拖顶部横条移动 · 拖四条边缩放 · 在框上滚轮调透明度。\n"
                       "识别框中间是镂空的，边框画在扫描区之外，不会被 OCR 拍进去。"),
                 bg=c["panel"], fg=c["muted"], font=self.theme.font(8),
                 justify="left", anchor="w").pack(fill="x", pady=(0, 4))

        sec3 = self._section(self.tab_ocr, "节流与灵敏度")
        self._scale(self._row(sec3, "截屏间隔"), "capture_interval_ms", 50, 2000, 10,
                    lambda v: f"{int(v)} ms")
        self._scale(self._row(sec3, "画面变化阈值"), "change_threshold", 0.1, 12.0, 0.1,
                    lambda v: f"{v:.1f}")
        tk.Label(sec3, text=("间隔越小越跟手（默认 120ms，最低 50ms）。画面静止时帧差检测会跳过 OCR，\n"
                             "所以调小基本只增加抓屏开销；真正的延迟瓶颈是 OCR 识别本身。"),
                 bg=c["panel"], fg=c["muted"], font=self.theme.font(8),
                 justify="left", anchor="w").pack(fill="x", pady=(0, 4))

        sec4 = self._section(self.tab_ocr, "匹配策略")
        self._scale(self._row(sec4, "相似度阈值"), "similarity_threshold", 0.30, 0.95, 0.01,
                    lambda v: f"{v * 100:.0f}%")
        self._scale(self._row(sec4, "候选数量 Top-K"), "top_k", 1, 10, 1,
                    lambda v: f"{int(v)} 条")
        self._scale(self._row(sec4, "最短触发字数"), "min_question_len", 3, 30, 1,
                    lambda v: f"{int(v)} 字")

        sec5 = self._section(self.tab_ocr, "OCR 引擎")
        self._scale_engine_hint(sec5)
        holder = self._row(sec5, "引擎")
        self._combo(holder, "ocr_engine",
                    ["auto", "paddle", "rapid", "none"], width=26)
        holder2 = self._row(sec5, "识别语言")
        self._combo(holder2, "ocr_lang", list(_LANG_LABELS.keys()), width=26)
        self._vars["_lang_label"] = tk.StringVar()
        self._scale(self._row(sec5, "推理线程"), "ocr_threads", 0, 16, 1,
                    lambda v: "自动" if int(v) == 0 else f"{int(v)} 线程")
        tk.Label(sec5, text="切换引擎 / 语言 / 线程需要重启程序才会生效。\n"
                            "线程设为「自动」时 Paddle 会按核心数铺满线程，"
                            "降了识别间隔后容易让整机变卡，建议 4。",
                 bg=c["panel"], fg=c["warn"], font=self.theme.font(8),
                 justify="left", anchor="w").pack(fill="x", pady=(4, 0))

        sec6 = self._section(self.tab_ocr, "图像预处理")
        self._scale(self._row(sec6, "放大倍数"), "preprocess.scale", 1.0, 3.0, 0.1,
                    lambda v: f"{v:.1f}×")
        h = self._row(sec6, "增强")
        self._check(h, "preprocess.grayscale", "灰度化")
        self._check(h, "preprocess.sharpen", "锐化")
        self._check(h, "preprocess.binarize", "二值化")
        tk.Label(sec6, text="小字号建议放大 1.5~2.0 倍 + 锐化；深色底浅色字建议开启二值化。",
                 bg=c["panel"], fg=c["muted"], font=self.theme.font(8),
                 anchor="w").pack(fill="x")

    def _scale_engine_hint(self, parent) -> None:
        c = self.theme.colors
        available = detect_available()
        names = {"paddle": "PaddleOCR", "rapid": "RapidOCR"}
        if available:
            text = "本机可用引擎：" + "、".join(names.get(a, a) for a in available)
            color = c["success"]
        else:
            text = ("未检测到 OCR 引擎，请先运行 install.bat（或 pip install paddlepaddle paddleocr）")
            color = c["error"]
        tk.Label(parent, text=text, bg=c["panel"], fg=color,
                 font=self.theme.font(9), anchor="w").pack(fill="x", pady=(0, 6))

    # ---------------------------------------------------------------- 浮窗页

    def _build_float_tab(self) -> None:
        c = self.theme.colors
        sec = self._section(self.tab_float, "外观")
        self._scale(self._row(sec, "不透明度"), "float_window.alpha", 0.3, 1.0, 0.05,
                    lambda v: f"{v * 100:.0f}%")
        self._scale(self._row(sec, "正文字号"), "float_window.font_size", 8, 20, 1,
                    lambda v: f"{int(v)} pt")
        h = self._row(sec, "行为")
        self._check(h, "float_window.topmost", "始终置顶")

        sec2 = self._section(self.tab_float, "尺寸与位置")
        self._scale(self._row(sec2, "窗口宽度"), "float_window.width", 300, 900, 10,
                    lambda v: f"{int(v)} px")
        self._scale(self._row(sec2, "窗口高度"), "float_window.height", 180, 800, 10,
                    lambda v: f"{int(v)} px")

        btns = tk.Frame(sec2, bg=c["panel"])
        btns.pack(fill="x", pady=(10, 0))
        self.theme.button(btns, "显示 / 隐藏浮窗", self.app.toggle_float,
                          kind="primary").pack(side="left", padx=(0, 6))
        self.theme.button(btns, "重置到右上角", self.app.reset_float_position,
                          kind="ghost").pack(side="left", padx=6)

        sec3 = self._section(self.tab_float, "使用提醒")
        tk.Label(sec3,
                 text=("浮窗遮挡扫描区域会造成「自己识别自己」的死循环，\n"
                       "请把浮窗拖到扫描区域之外；程序检测到重叠时会在状态栏提示。\n"
                       "浮窗为无边框窗口，按住标题栏即可拖动，右下角 ◢ 可调整大小。"),
                 bg=c["panel"], fg=c["subtext"], font=self.theme.font(9),
                 justify="left", anchor="w").pack(fill="x")

    # ---------------------------------------------------------------- 快捷键页

    def _build_keys_tab(self) -> None:
        c = self.theme.colors
        sec = self._section(self.tab_keys, "全局快捷键")
        tk.Label(sec, text="格式示例：f2 / ctrl+alt+q / ctrl+shift+f1",
                 bg=c["panel"], fg=c["muted"], font=self.theme.font(8),
                 anchor="w").pack(fill="x", pady=(0, 8))

        self._key_vars: Dict[str, tk.StringVar] = {}
        from core.hotkeys import ACTION_LABELS, DEFAULT_BINDINGS

        grid = tk.Frame(sec, bg=c["panel"])
        grid.pack(fill="x")
        for i, (action, label) in enumerate(ACTION_LABELS.items()):
            tk.Label(grid, text=label, bg=c["panel"], fg=c["text"],
                     font=self.theme.font(10), anchor="w").grid(
                row=i, column=0, sticky="w", pady=3, padx=(0, 12))
            var = tk.StringVar(value=DEFAULT_BINDINGS.get(action, ""))
            self._key_vars[action] = var
            ttk.Entry(grid, textvariable=var, width=18).grid(
                row=i, column=1, sticky="w", pady=3)

        btns = tk.Frame(sec, bg=c["panel"])
        btns.pack(fill="x", pady=(12, 0))
        self.theme.button(btns, "应用快捷键", self._apply_hotkeys,
                          kind="primary").pack(side="left", padx=(0, 6))
        self.theme.button(btns, "恢复默认", self._reset_hotkeys).pack(side="left", padx=6)

        self.engine_status = tk.Label(sec, text="", bg=c["panel"], fg=c["subtext"],
                                      font=self.theme.font(9), anchor="w", justify="left")
        self.engine_status.pack(fill="x", pady=(10, 0))

    def _apply_hotkeys(self) -> None:
        bindings = {k: v.get().strip() for k, v in self._key_vars.items() if v.get().strip()}
        self.app.apply_hotkeys(bindings)

    def _reset_hotkeys(self) -> None:
        from core.hotkeys import DEFAULT_BINDINGS

        for action, combo in DEFAULT_BINDINGS.items():
            if action in self._key_vars:
                self._key_vars[action].set(combo)
        self._apply_hotkeys()

    # ---------------------------------------------------------------- 日志页

    def _build_log_tab(self) -> None:
        c = self.theme.colors
        sec = self._section(self.tab_log, "运行日志")
        wrap = tk.Frame(sec, bg=c["panel"])
        wrap.pack(fill="both", expand=True)

        self.log_text = tk.Text(wrap, height=16, wrap="word", bg=c["panel_alt"],
                                fg=c["text"], relief="flat", bd=0, padx=10, pady=8,
                                font=self.theme.mono_font(9), state="disabled",
                                highlightthickness=0)
        scroll = ttk.Scrollbar(wrap, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set,
                                selectbackground=c["primary_soft"])
        scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        self.log_text.tag_configure("WARN", foreground=c["warn"])
        self.log_text.tag_configure("ERROR", foreground=c["error"])
        self.log_text.tag_configure("INFO", foreground=c["text"])
        self.log_text.tag_configure("OK", foreground=c["success"])

        btns = tk.Frame(sec, bg=c["panel"])
        btns.pack(fill="x", pady=(8, 0))
        self.theme.button(btns, "清空", self.clear_log, kind="ghost").pack(side="left")
        self.theme.button(btns, "打开日志目录", self.app.open_log_dir,
                          kind="ghost").pack(side="left", padx=6)

    # ================================================================ 交互

    def _push(self, key: str, value: Any) -> None:
        """把控件变化推给控制器（构建期与回填期静默）。"""
        if self._building or self._suppress:
            return
        self.app.apply_setting(key, value)

    def _browse_bank(self) -> None:
        initial = self.cfg.get("last_import_dir") or ""
        path = filedialog.askopenfilename(
            title="选择题库文件",
            initialdir=initial or None,
            filetypes=[
                ("题库文件", "*.xlsx *.xlsm *.docx *.doc *.csv *.tsv *.json *.txt *.md"),
                ("Excel", "*.xlsx *.xlsm"),
                ("Word", "*.docx *.doc"),
                ("CSV", "*.csv *.tsv"),
                ("JSON", "*.json"),
                ("全部文件", "*.*"),
            ],
        )
        if path:
            self._vars["bank_path"].set(path)
            self.cfg.set("last_import_dir", os.path.dirname(path))
            self.app.import_bank(path)

    # ---------------------------------------------------------------- 回填

    def _load_from_config(self) -> None:
        """把 Config 的值灌入控件（不触发回调）。"""
        self._suppress = True
        cfg = self.cfg
        try:
            self._vars["bank_path"].set(cfg.get("bank_path", "") or "")
            for key in ("capture_interval_ms", "change_threshold", "similarity_threshold",
                        "top_k", "min_question_len", "preprocess.scale",
                        "float_window.alpha", "float_window.font_size",
                        "float_window.width", "float_window.height",
                        "capture_frame.alpha", "capture_frame.border",
                        "ocr_threads"):
                if key in self._vars:
                    val = cfg.get(key)
                    if val is not None:
                        self._vars[key].set(float(val))
                        if key in self.value_labels:
                            self._refresh_scale_label(key, float(val))

            for key in ("preprocess.grayscale", "preprocess.sharpen", "preprocess.binarize",
                        "float_window.topmost", "ocr_mkldnn", "capture_frame.show",
                        "main_window.minimize_on_run"):
                if key in self._vars:
                    self._vars[key].set(bool(cfg.get(key, False)))

            if "ocr_engine" in self._vars:
                self._vars["ocr_engine"].set(cfg.get("ocr_engine", "auto"))
            if "ocr_lang" in self._vars:
                self._vars["ocr_lang"].set(cfg.get("ocr_lang", "ch"))

            from core.hotkeys import DEFAULT_BINDINGS

            hotkeys = cfg.get("hotkeys") or {}
            for action, var in self._key_vars.items():
                var.set(hotkeys.get(action) or DEFAULT_BINDINGS.get(action, ""))

            self.set_region(cfg.get("region"))
        finally:
            self._suppress = False

    def _refresh_scale_label(self, key: str, value: float) -> None:
        fmt_map = {
            "capture_interval_ms": lambda v: f"{int(v)} ms",
            "change_threshold": lambda v: f"{v:.1f}",
            "similarity_threshold": lambda v: f"{v * 100:.0f}%",
            "top_k": lambda v: f"{int(v)} 条",
            "min_question_len": lambda v: f"{int(v)} 字",
            "preprocess.scale": lambda v: f"{v:.1f}×",
            "float_window.alpha": lambda v: f"{v * 100:.0f}%",
            "float_window.font_size": lambda v: f"{int(v)} pt",
            "float_window.width": lambda v: f"{int(v)} px",
            "float_window.height": lambda v: f"{int(v)} px",
            "capture_frame.alpha": lambda v: f"{v * 100:.0f}%",
            "capture_frame.border": lambda v: f"{int(v)} px",
            "ocr_threads": lambda v: "自动" if int(v) == 0 else f"{int(v)} 线程",
        }
        fmt = fmt_map.get(key)
        if fmt and key in self.value_labels:
            self.value_labels[key].configure(text=fmt(value))

    # ---------------------------------------------------------------- 外部接口

    def set_running(self, running: bool) -> None:
        self.run_btn.configure(text="⏸ 暂停识别" if running else "▶ 开始识别")

    def set_scale(self, key: str, value: float) -> None:
        """外部改值后回填滑杆（静默，不触发 apply_setting）。"""
        var = self._vars.get(key)
        if var is None:
            return
        self._suppress = True
        try:
            var.set(float(value))
            if key in self.value_labels:
                self._refresh_scale_label(key, float(value))
        except Exception:
            pass
        finally:
            self._suppress = False

    def set_region(self, region: Optional[list]) -> None:
        if region:
            self.region_label.configure(
                text=f"x={region[0]}  y={region[1]}  宽={region[2]}  高={region[3]}",
                fg=self.theme.colors["text"])
        else:
            self.region_label.configure(text="未设置（按 F1 或点「划定区域」）",
                                        fg=self.theme.colors["warn"])

    def set_bank_stats(self, stats: Dict[str, Any], path: str = "") -> None:
        if not stats or not stats.get("total"):
            self.bank_stats.configure(text="尚未导入题库")
            return
        types = stats.get("types") or {}
        type_text = "  ".join(f"{k}:{v}" for k, v in sorted(types.items(),
                                                          key=lambda x: -x[1])[:6])
        name = os.path.basename(path or stats.get("source") or "")
        text = (f"文件      {name}\n"
                f"总题数    {stats.get('total', 0)}\n"
                f"含答案    {stats.get('with_answer', 0)}\n"
                f"含解析    {stats.get('with_analysis', 0)}\n"
                f"题型分布  {type_text or '—'}")
        self.bank_stats.configure(text=text)

    def set_status(self, text: str) -> None:
        try:
            self.status_label.configure(text=text)
        except Exception:
            pass

    def set_perf(self, text: str) -> None:
        try:
            self.perf_label.configure(text=text)
        except Exception:
            pass

    def log(self, level: str, message: str) -> None:
        try:
            tag = level if level in ("INFO", "WARN", "ERROR", "OK") else "INFO"
            self.log_text.configure(state="normal")
            self.log_text.insert("end", message.rstrip() + "\n", tag)
            # 只保留最近 800 行，避免长时间运行内存膨胀
            lines = int(self.log_text.index("end-1c").split(".")[0])
            if lines > 800:
                self.log_text.delete("1.0", f"{lines - 800}.0")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        except Exception:
            pass

    def clear_log(self) -> None:
        try:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")
        except Exception:
            pass

    def set_engine_status(self, text: str, ok: bool = True) -> None:
        try:
            self.engine_status.configure(
                text=text, fg=self.theme.colors["success" if ok else "warn"])
        except Exception:
            pass

    def show(self) -> None:
        try:
            self.root.deiconify()
            self.root.lift()
        except Exception:
            pass

    def hide(self) -> None:
        try:
            self.root.withdraw()
        except Exception:
            pass

    def alert(self, title: str, message: str) -> None:
        try:
            self.root.after(0, lambda: messagebox.showwarning(title, message,
                                                              parent=self.root))
        except Exception:
            pass


def open_in_explorer(path: str) -> None:
    """在文件管理器中打开目录。"""
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


__all__ = ["ControlPanel", "open_in_explorer", "qb", "ENGINE_LABELS", "_LANG_LABELS"]
