# -*- coding: utf-8 -*-
"""答题助手 —— 主程序入口。

用法：
    python main.py              正常启动
    python main.py --debug      输出调试日志到控制台

进程模型（三进程隔离）：
    主进程   UI + 编排 + 全局热键 + 配置
    截屏进程 mss 抓屏 + 帧差检测
    搜索进程 OCR + 题库模糊匹配

主进程与工作进程之间只通过 multiprocessing 的 Queue / Event / Value 通信，
不共享 Python 对象，任何一个环节异常都不会拖垮界面。
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import queue
import sys
import time
import tkinter as tk
from pathlib import Path
from typing import Any, Dict, List, Optional

# 保证以脚本 / 打包方式启动时都能 import 到包内模块
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import logger as logmod
from core.config import Config, assets_dir, logs_dir, user_data_dir
from core.hotkeys import HotkeyManager
from core.ocr_engine import ENGINE_LABELS
from core.screen import clamp_region, enable_dpi_awareness, primary_dpi_scale
from ui.capture_frame import CaptureFrame
from ui.float_window import FloatWindow
from ui.region_selector import RegionSelector
from ui.settings_window import ControlPanel, open_in_explorer
from ui.theme import Theme
from workers.capture_worker import capture_main
from workers.ipc import (
    MSG_BANK,
    MSG_ERROR,
    MSG_READY,
    MSG_RESULT,
    MSG_STATUS,
    build_ipc,
)
from workers.search_worker import search_main

POLL_MS = 60
STARTUP_BANNER = (
    "答题助手已启动 —— 按 F2 开始识别：识别框随即出现、主面板自动收进任务栏；"
    "调好位置后点识别框的 ✕（或按 F9）退出识别并唤回面板。"
)


class App:
    """主进程控制器。"""

    # ================================================================ 生命周期

    def __init__(self, root: tk.Tk, cfg: Config, theme: Theme):
        self.root = root
        self.cfg = cfg
        self.theme = theme
        self.log = logmod.get("app")

        self.ctx = mp.get_context("spawn")
        self.ipc: Dict[str, Any] = build_ipc(self.ctx)
        self.hotkey_q: "queue.Queue" = queue.Queue()

        self.panel = ControlPanel(root, theme, cfg, self)
        self.float_win = FloatWindow(root, theme, cfg.get("float_window") or {},
                                     on_geometry=self._on_float_geometry)
        self.capture_frame = CaptureFrame(
            root, theme, self._frame_cfg(),
            on_region_change=self._on_frame_region,
            on_close=self._on_frame_close)
        self.hotkeys = HotkeyManager(cfg.get("hotkeys"), self.hotkey_q)
        self.selector = RegionSelector(root, theme)

        self.processes: Dict[str, mp.process.BaseProcess] = {}
        self._dirty = False
        self._last_save = time.time()
        self._last_result_ts = 0.0
        self._restart_count: Dict[str, int] = {"capture": 0, "search": 0}
        self._dead_reported: set = set()
        self._minimized_by_focus = False     # 主面板是否被「识别态」收进了任务栏
        self._stat: Dict[str, str] = {}      # 面板统计栏的分段文本
        self._quitting = False

        self._sync_ipc_from_config()
        logmod.set_ui_sink(self._on_log)

    # ================================================================ 启动

    def start(self) -> None:
        self._bind_tk_hotkeys()
        self._start_workers()

        if self.hotkeys.start():
            self.panel.set_engine_status(
                "全局热键已启用（系统级，任意窗口有效）：\n" + self.hotkeys.describe())
        else:
            self.panel.set_engine_status(
                "全局热键未启用（keyboard 库缺失或权限不足），已降级为窗口内快捷键；\n"
                "把焦点切到本面板或浮窗时依然可用。\n"
                f"详细信息：{self.hotkeys.error}")
            self.panel.log("WARN", "全局热键不可用，已降级为窗口内快捷键")

        conflicts = self.hotkeys.conflicts()
        if conflicts:
            detail = "；".join(f"{c} 被 {len(a)} 个动作占用" for c, a in conflicts.items())
            self.panel.log("WARN", f"快捷键冲突：{detail}")

        self.float_win.clear()
        if self.cfg.get("float_window.auto_show", True):
            self.float_win.show()
        self.float_win.set_indicator("idle")

        region = self.cfg.get("region")
        self.panel.set_region(region)
        # 识别框只在「开始识别」之后出现，启动时不提前显示
        self.panel.set_status(STARTUP_BANNER)
        self.panel.log("INFO", STARTUP_BANNER)
        self.panel.log("INFO", "日志目录：" + str(logs_dir()))
        self.panel.log("INFO", "数据目录：" + str(user_data_dir()))

        bank = self.cfg.get("bank_path")
        if not bank:
            self.panel.log("WARN", "尚未导入题库，可点「生成示例题库」先跑通流程")

        self._check_overlap()
        self.root.after(POLL_MS, self._poll)

    def _worker_cfg(self) -> Dict[str, Any]:
        """工作进程启动参数。首次启动与崩溃重启共用同一份，避免两处漂移。"""
        return {
            "log_dir": str(logs_dir()),
            "bank_path": self.cfg.get("bank_path") or "",
            "ocr_engine": self.cfg.get("ocr_engine", "auto"),
            "ocr_lang": self.cfg.get("ocr_lang", "ch"),
            "ocr_use_gpu": bool(self.cfg.get("ocr_use_gpu", False)),
            "ocr_threads": int(self.cfg.get("ocr_threads", 4) or 0),
            "preprocess": self.cfg.get("preprocess") or {},
            "min_question_len": int(self.cfg.get("min_question_len", 6)),
        }

    def _start_workers(self) -> None:
        worker_cfg = self._worker_cfg()

        self.processes["capture"] = self.ctx.Process(
            target=capture_main, args=(worker_cfg, self.ipc),
            name="capture", daemon=True)
        self.processes["search"] = self.ctx.Process(
            target=search_main, args=(worker_cfg, self.ipc),
            name="search", daemon=True)

        for name, proc in self.processes.items():
            proc.start()
            self.log.info("已启动 %s 进程 pid=%s", name, proc.pid)
        self.panel.log("INFO", "截屏进程与搜索进程已启动，正在加载 OCR 引擎…")

    # ================================================================ 消息轮询

    def _poll(self) -> None:
        if self._quitting:
            return
        try:
            self._drain_hotkeys()
            self._drain_stats()
            self._drain_results()
            self._check_processes()
            if self._dirty and time.time() - self._last_save > 5.0:
                self._save_config()
        except Exception:
            self.log.exception("轮询异常")
        finally:
            if not self._quitting:
                self.root.after(POLL_MS, self._poll)

    def _drain_hotkeys(self) -> None:
        for _ in range(20):
            try:
                kind, action = self.hotkey_q.get_nowait()
            except queue.Empty:
                return
            if kind == "hotkey":
                self._do_action(action)

    def _drain_stats(self) -> None:
        for _ in range(60):
            try:
                msg = self.ipc["stat_q"].get_nowait()
            except queue.Empty:
                return
            except Exception:
                return
            if not isinstance(msg, dict):
                continue
            mtype = msg.get("type")
            if mtype == MSG_ERROR:
                level = "ERROR"
            elif mtype == MSG_STATUS:
                level = msg.get("level", "INFO")
            else:
                continue

            text = str(msg.get("text") or "")
            if level == "STAT":
                self._apply_stat(msg)
                continue
            if level == "READY":
                self.float_win.set_indicator("running" if self.ipc["running"].is_set() else "idle")
                self.panel.set_engine_status(text, ok=True)
                self.panel.log("OK", text)
                self.panel.set_status(text)
                continue
            if level == "ERROR":
                self.float_win.set_indicator("error")
            self.panel.log(level, text)
            if level in ("WARN", "ERROR"):
                self.panel.set_status(text)

    def _apply_stat(self, msg: Dict[str, Any]) -> None:
        """把截屏与 OCR 两侧的统计合并显示，避免后到的那个把前一个刷掉。

        OCR 单次耗时是用户体感延迟的主要来源，必须一直可见。
        """
        name = msg.get("text") or ""
        if name.startswith("识别统计"):
            parts = []
            if msg.get("avg_ocr_ms") is not None:
                parts.append(f"OCR {msg['avg_ocr_ms']}ms/次")
            if msg.get("frames") is not None:
                parts.append(f"识别 {msg['frames']} 次")
            if msg.get("hits") is not None:
                parts.append(f"命中 {msg['hits']}")
            self._stat["ocr"] = "  ·  ".join(parts)
        elif name.startswith("截屏统计"):
            self._stat["cap"] = (f"截屏 {msg.get('emitted', 0)}/{msg.get('seq', 0)} 帧"
                                 f"  ·  变化 {msg.get('diff', 0)}")
        else:
            return
        merged = [v for v in (self._stat.get("cap"), self._stat.get("ocr")) if v]
        self.panel.set_perf("  ·  ".join(merged))

    def _drain_results(self) -> None:
        for _ in range(20):
            try:
                msg = self.ipc["result_q"].get_nowait()
            except queue.Empty:
                return
            except Exception:
                return
            if not isinstance(msg, dict):
                continue
            mtype = msg.get("type")
            if mtype == MSG_RESULT:
                self._on_match_result(msg)
            elif mtype == MSG_BANK:
                stats = msg.get("stats") or {}
                self.panel.set_bank_stats(stats, msg.get("path", ""))
                self.panel.log("OK", f"题库已就绪：{stats.get('total', 0)} 题"
                                     f"（{stats.get('elapsed', 0)}s）")
            elif mtype == MSG_READY:
                self.log.info("搜索进程就绪")

    def _on_match_result(self, msg: Dict[str, Any]) -> None:
        self._last_result_ts = time.time()
        self.float_win.set_result(msg)

        matches = msg.get("matches") or []
        if matches:
            top = matches[0]
            self.panel.set_status(
                f"匹配成功 {top.get('score', 0) * 100:.1f}%  ·  "
                f"{len(matches)} 个候选  ·  OCR {msg.get('ocr_ms', 0)}ms")
            self.float_win.set_indicator("running")
            if self.cfg.get("float_window.auto_show", True) and not self.float_win.visible:
                self.float_win.show()
            self.panel.log("OK", f"命中：{(top.get('question') or {}).get('question', '')[:40]}"
                                 f"（{(top.get('answer_display') or '').strip()[:20]}）")
        else:
            note = msg.get("quality") or "未匹配到题目"
            self.panel.set_status(note)

    def _check_processes(self) -> None:
        """工作进程异常退出时报告并尝试重启一次。"""
        for name, proc in list(self.processes.items()):
            if proc.is_alive():
                continue
            code = proc.exitcode
            if self._restart_count.get(name, 0) > 1 or name in self._dead_reported:
                # 已经报告过且不再重启，避免每 60ms 刷一次日志
                continue
            self._dead_reported.add(name)
            self.log.warning("%s 进程已退出 exitcode=%s", name, code)
            if self._quitting:
                return
            if self._restart_count.get(name, 0) >= 1:
                self.panel.log("ERROR", f"{name} 进程反复退出（code={code}），"
                                        f"请查看日志后重启程序")
                self._restart_count[name] = 2
                continue
            self.panel.log("WARN", f"{name} 进程异常退出（code={code}），正在重启…")
            self._restart_worker(name)

    def _restart_worker(self, name: str) -> None:
        target = capture_main if name == "capture" else search_main
        try:
            proc = self.ctx.Process(target=target, args=(self._worker_cfg(), self.ipc),
                                    name=name, daemon=True)
            proc.start()
        except Exception:
            self.log.exception("重启 %s 进程失败", name)
            self.panel.log("ERROR", f"{name} 进程重启失败，请查看日志")
            self._restart_count[name] = 2
            return
        self.processes[name] = proc
        self._restart_count[name] = 1
        self._dead_reported.discard(name)
        self.ipc["ocr_ready"].clear()

    # ================================================================ 动作分发

    def _do_action(self, action: str) -> None:
        table = {
            "toggle_run": self.toggle_run,
            "select_region": self.select_region,
            "toggle_float": self.toggle_float,
            "capture_once": self.capture_once,
            "toggle_main": self.toggle_panel,
            "clear": self.clear_result,
            "cycle_alpha": self.cycle_alpha,
            "toggle_frame": self.toggle_frame,
            "quit": self.quit,
        }
        fn = table.get(action)
        if fn is None:
            return
        try:
            fn()
        except Exception:
            self.log.exception("执行动作失败：%s", action)

    def toggle_run(self) -> None:
        if not self.cfg.get("region"):
            self.panel.set_status("请先划定扫描区域（F1），划完自动开始识别")
            self.panel.log("WARN", "尚未划区，先划区再自动开始识别")
            self.select_region(auto_start=True)
            return

        running = self.ipc["running"]
        if running.is_set():
            running.clear()
            self.float_win.set_indicator("paused")
            self.panel.set_running(False)
            self.capture_frame.show()
            self.capture_frame.set_state("paused")
            self.panel.set_status("已暂停（识别框仍在；点 ✕ 或按 F9 退出识别并唤回面板）")
            self.panel.log("INFO", "已暂停识别")
        else:
            running.set()
            self.ipc["paused"].clear()
            self.float_win.set_indicator("running")
            self.panel.set_running(True)
            self.enter_focus_mode()
            self.panel.set_status("识别中…（拖动识别框微调；点 ✕ 或按 F9 退出识别并唤回面板）")
            self.panel.log("INFO", "开始持续识别：识别框已显示，主面板已收进任务栏")
            self.ipc["force"].set()

    def capture_once(self) -> None:
        if not self.cfg.get("region"):
            self.panel.set_status("请先划定扫描区域（F1）")
            self.select_region(auto_start=True)
            return
        if not self.ipc["running"].is_set():
            self.ipc["running"].set()
            self.panel.set_running(True)
        self.ipc["force"].set()
        self.float_win.set_indicator("running")
        self.enter_focus_mode()
        self.panel.set_status("已触发单次识别…")

    def select_region(self, auto_start: bool = False) -> None:
        was_running = self.ipc["running"].is_set()
        was_float = self.float_win.visible
        was_frame = self.capture_frame.visible
        self.ipc["paused"].set()
        if was_float:
            self.float_win.hide()
        if was_frame:
            # 全屏选区工具不能盖在识别框下面，先收起来
            self.capture_frame.hide()
        try:
            self.root.update_idletasks()
            region = self.selector.select()
        finally:
            if was_float:
                self.float_win.show()
            self.ipc["paused"].clear()

        if not region:
            if was_frame:
                self.capture_frame.show()
            self.panel.set_status("已取消划区")
            return

        try:
            import mss

            with mss.mss() as sct:
                mon = sct.monitors[0]
                bounds = (int(mon["left"]), int(mon["top"]),
                          int(mon["width"]), int(mon["height"]))
            fixed = clamp_region(region, bounds)
        except Exception:
            fixed = region

        if not fixed:
            self.panel.log("ERROR", f"选区无效：{region}")
            self.panel.set_status("选区无效，请重新划定")
            return

        self.cfg.set("region", list(fixed))
        self._write_region(fixed)
        self.panel.set_region(list(fixed))
        self.capture_frame.set_region(fixed)
        self._save_config()
        self.float_win.set_indicator("running" if was_running else "idle")
        if was_frame:
            # 本来就在识别态里，重划完把框放回来；否则不去主动显示
            self.show_frame("running" if was_running else "idle")
        self.panel.log("INFO", f"扫描区域已设置：x={fixed[0]} y={fixed[1]} "
                               f"{fixed[2]}×{fixed[3]}")

        if auto_start and not self.ipc["running"].is_set():
            # 从「开始识别」进来的：划完区直接进识别态，省掉再点一次
            self.ipc["running"].set()
            self.ipc["paused"].clear()
            self.panel.set_running(True)
            self.float_win.set_indicator("running")
            self.enter_focus_mode()
            self.panel.log("INFO", "划区完成，已自动开始识别：识别框已显示，主面板已收进任务栏")
            self.ipc["force"].set()
            # 重叠警告优先，别被「识别中」盖掉
            if not self._check_overlap():
                self.panel.set_status("识别中…（点 ✕ 或按 F9 退出识别并唤回面板）")
            return

        self.panel.set_status("区域已保存，按 F2 开始识别，之后可拖动识别框微调")
        self._check_overlap()
        self.ipc["force"].set()

    def clear_region(self) -> None:
        self.cfg.set("region", None)
        self._write_region((0, 0, 0, 0))
        self.panel.set_region(None)
        self.capture_frame.hide()
        if self.ipc["running"].is_set():
            self.ipc["running"].clear()
            self.panel.set_running(False)
        self._save_config()
        self.panel.set_status("已清除扫描区域")
        self.panel.log("INFO", "扫描区域已清除")

    def toggle_float(self) -> None:
        self.float_win.toggle()
        self.panel.set_status("浮窗已显示" if self.float_win.visible else "浮窗已隐藏")
        self._check_overlap()

    def toggle_panel(self) -> None:
        try:
            if self.root.state() == "withdrawn":
                self.hide_panel_off = False
                self.panel.show()
            else:
                self.panel.hide()
        except Exception:
            self.panel.show()

    def hide_panel(self) -> None:
        self.panel.hide()
        self.panel.log("INFO", "控制面板已隐藏，按 F5 可重新唤起")

    def clear_result(self) -> None:
        self.float_win.clear()
        self.panel.set_status("已清空结果")

    def cycle_alpha(self) -> None:
        value = self.float_win.cycle_alpha()
        self.cfg.set("float_window.alpha", round(value, 2))
        self._dirty = True

    # ================================================================ 题库

    def import_bank(self, path: str) -> None:
        path = (path or "").strip().strip('"')
        if not path:
            self.panel.set_status("请先选择题库文件")
            return
        if not os.path.exists(path):
            self.panel.log("ERROR", f"题库文件不存在：{path}")
            self.panel.set_status("题库文件不存在")
            return

        suffix = Path(path).suffix.lower()
        if suffix == ".xls":
            self.panel.alert("格式不支持", "旧版 .xls 请先用 Excel 另存为 .xlsx 再导入。")
            return
        if suffix not in (".xlsx", ".xlsm", ".docx", ".csv", ".tsv", ".json",
                          ".jsonl", ".txt", ".md"):
            self.panel.alert("格式不支持",
                             f"暂不支持 {suffix or '该'} 格式。\n"
                             "支持：docx / xlsx / xlsm / csv / tsv / json / txt / md")
            return

        self.cfg.set("bank_path", path)
        self.panel.log("INFO", f"正在导入题库：{os.path.basename(path)} …")
        self.panel.set_status("正在导入题库…")
        try:
            self.ipc["ctrl_q"].put_nowait({"cmd": "reload_bank", "path": path})
        except Exception:
            self.panel.log("ERROR", "无法与搜索进程通信")
        self._dirty = True

    def make_sample_bank(self) -> None:
        """生成示例题库；本地没有示例题目时改为生成题库模板。

        题目内容不进仓库（放在被忽略的 data/local/sample_questions.json），
        所以别人克隆本项目后点这个按钮，拿到的是空模板而不是示例题。
        """
        try:
            from tools.make_sample_bank import write_samples

            out_dir = user_data_dir() / "samples"
            files = write_samples(out_dir)
            if files:
                self.panel.log("OK", "示例题库已生成：" + "，".join(f.name for f in files))
                target = next((f for f in files if f.suffix == ".json"),
                              files[0] if files else None)
                if target:
                    self.panel._vars["bank_path"].set(str(target))
                    self.import_bank(str(target))
                return

            from tools.make_bank_template import write_templates

            created = write_templates(out_dir)
            self.panel.log(
                "OK",
                "本地没有示例题目（data/local/sample_questions.json），"
                "已改为生成题库模板：" + "，".join(f.name for f in created)
                + "。填入自己的题目后导入即可。")
        except Exception as exc:
            self.log.exception("生成示例题库失败")
            self.panel.alert("生成失败", f"生成示例题库时出错：{exc}")

    def export_bank_json(self) -> None:
        path = (self.cfg.get("bank_path") or "").strip()
        if not path or not os.path.exists(path):
            self.panel.alert("无题库", "请先导入题库再导出。")
            return
        from tkinter import filedialog

        target = filedialog.asksaveasfilename(
            title="导出为 JSON 题库", defaultextension=".json",
            initialfile=Path(path).stem + "_normalized.json",
            filetypes=[("JSON", "*.json")])
        if not target:
            return
        try:
            self.panel.set_status("正在导出…")
            from core.question_bank import load_bank

            bank = load_bank(path)
            bank.save_json(target)
            self.panel.log("OK", f"已导出 {len(bank)} 题到 {target}")
            self.panel.set_status("导出完成")
        except Exception as exc:
            self.log.exception("导出失败")
            self.panel.alert("导出失败", str(exc))

    # ================================================================ 设置

    def apply_setting(self, key: str, value: Any) -> None:
        self.cfg.set(key, value)
        try:
            if key == "capture_interval_ms":
                self.ipc["interval_ms"].value = int(value)
            elif key == "change_threshold":
                self.ipc["change_thresh"].value = float(value)
            elif key == "similarity_threshold":
                self.ipc["sim_thresh"].value = float(value)
            elif key == "top_k":
                self.ipc["top_k"].value = int(value)
            elif key == "min_question_len":
                self.ipc["min_len"].value = int(value)
            elif key.startswith("preprocess."):
                self._sync_prep()
            elif key == "float_window.alpha":
                self.float_win.set_alpha(float(value))
            elif key == "float_window.font_size":
                self.float_win.set_font_size(int(value))
            elif key == "float_window.width":
                self.float_win.set_size(int(value), int(self.cfg.get("float_window.height", 400)))
            elif key == "float_window.height":
                self.float_win.set_size(int(self.cfg.get("float_window.width", 470)), int(value))
            elif key == "float_window.topmost":
                self.float_win.set_topmost(bool(value))
            elif key == "capture_frame.alpha":
                self.capture_frame.set_alpha(float(value))
            elif key == "capture_frame.border":
                self.capture_frame.set_border(int(value))
            elif key == "capture_frame.show":
                if bool(value):
                    self.show_frame()
                else:
                    self.capture_frame.hide()
            elif key == "main_window.minimize_on_run":
                if not bool(value) and self._minimized_by_focus:
                    self._restore_main()
        except Exception:
            self.log.exception("应用设置失败 %s", key)
        self._dirty = True

    def apply_hotkeys(self, bindings: Dict[str, str]) -> None:
        self.cfg.set("hotkeys", dict(bindings))
        ok = self.hotkeys.rebind(bindings)
        self._bind_tk_hotkeys()
        if ok:
            self.panel.set_engine_status("全局热键已重新绑定：\n" + self.hotkeys.describe())
            self.panel.log("OK", "快捷键已更新")
        else:
            self.panel.set_engine_status(
                "全局热键注册失败，已降级为窗口内快捷键。\n" + self.hotkeys.describe(), ok=False)
            self.panel.log("WARN", "全局热键注册失败")
        self._dirty = True

    def reset_float_position(self) -> None:
        self.float_win.move_to_corner()
        self.panel.set_status("浮窗已复位到右上角")
        self._check_overlap()

    def reset_frame_position(self) -> None:
        """把识别框重置到屏幕中央并恢复默认尺寸。"""
        try:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
        except Exception:
            sw, sh = 1920, 1080
        w, h = 560, 200
        x = max(0, (sw - w) // 2)
        y = max(0, int(sh * 0.45) - h // 2)
        self.capture_frame.set_region((x, y, w, h), notify=True, final=True)
        self.show_frame(force=True)
        self.panel.set_status(f"识别框已重置到屏幕中央（{w}×{h}）")
        self.panel.log("INFO", "识别框已重置到屏幕中央")

    def open_log_dir(self) -> None:
        open_in_explorer(str(logs_dir()))

    # ================================================================ 内部同步

    def _sync_ipc_from_config(self) -> None:
        self.ipc["interval_ms"].value = int(self.cfg.get("capture_interval_ms", 120))
        self.ipc["change_thresh"].value = float(self.cfg.get("change_threshold", 0.9))
        self.ipc["sim_thresh"].value = float(self.cfg.get("similarity_threshold", 0.58))
        self.ipc["top_k"].value = int(self.cfg.get("top_k", 5))
        self.ipc["min_len"].value = int(self.cfg.get("min_question_len", 6))
        self._sync_prep()
        self._write_region(self.cfg.get("region") or (0, 0, 0, 0))

    def _sync_prep(self) -> None:
        prep = self.cfg.get("preprocess") or {}
        values = [
            float(prep.get("scale", 1.0) or 1.0),
            1.0 if prep.get("grayscale") else 0.0,
            1.0 if prep.get("sharpen") else 0.0,
            1.0 if prep.get("binarize") else 0.0,
        ]
        target = self.ipc["prep"]
        for i, v in enumerate(values):
            target[i] = v

    def _write_region(self, region) -> None:
        try:
            values = [int(v) for v in region]
            if len(values) != 4:
                values = [0, 0, 0, 0]
        except Exception:
            values = [0, 0, 0, 0]
        arr = self.ipc["region"]
        for i, v in enumerate(values):
            arr[i] = v

    # ================================================================ 识别框

    def _frame_cfg(self) -> Dict[str, Any]:
        """把 capture_frame 外观配置 + 当前区域打包给 CaptureFrame。"""
        frame_cfg = dict(self.cfg.get("capture_frame") or {})
        frame_cfg["region"] = tuple(self.cfg.get("region") or (220, 220, 520, 180))
        return frame_cfg

    def _on_frame_region(self, region, final: bool) -> None:
        """识别框被拖动 / 缩放 / 调透明度后回调。

        final=True 表示鼠标已松手，此时落盘配置并立刻触发一次识别。
        """
        try:
            values = tuple(int(v) for v in region)
        except Exception:
            return

        self.cfg.set("region", list(values))
        self._write_region(values)
        self.panel.set_region(list(values))
        self.cfg.set("capture_frame.alpha", round(self.capture_frame.alpha, 2))
        self._dirty = True

        if not final:
            return

        self.panel.set_scale("capture_frame.alpha", self.capture_frame.alpha)
        self._save_config()
        if self.ipc["running"].is_set():
            self.ipc["force"].set()
            self.panel.set_status(
                f"识别框已调整：x={values[0]} y={values[1]} {values[2]}×{values[3]}，正在重新识别…")
        else:
            self.panel.set_status(
                f"识别框已调整：{values[2]}×{values[3]}（按 F2 开始识别）")
        self._check_overlap()

    def show_frame(self, state: Optional[str] = None, force: bool = False) -> None:
        """显示常驻识别框，并按需同步状态配色。"""
        if not force and not self.cfg.get("capture_frame.show", True):
            return
        try:
            self.capture_frame.set_region(self.cfg.get("region") or self.capture_frame.region)
            self.capture_frame.show()
            self.capture_frame.set_state(state or ("running" if self.ipc["running"].is_set()
                                                  else "idle"))
            # 识别框是置顶窗，显示后把浮窗重新提到前面，别把答案盖住
            self.float_win.raise_up()
        except Exception:
            self.log.exception("显示识别框失败")

    def toggle_frame(self) -> None:
        if self.capture_frame.visible:
            self.exit_focus_mode()
        else:
            self.enter_focus_mode()
            self.panel.set_status("识别框已显示：拖动移动 · 拖边缘缩放 · 滚轮调透明度")
            self.panel.log("INFO", "识别框已显示，主面板已收进任务栏")

    # ---------------------------------------------------------- 识别态窗口编排

    def enter_focus_mode(self) -> None:
        """进入「识别态」：显示识别框，并把主面板收进任务栏。

        主面板挡住答题页面是最常见的干扰，所以开始识别就自动让位；
        识别框退出时再自动唤回（见 exit_focus_mode）。
        """
        self.show_frame("running")
        if self.cfg.get("main_window.minimize_on_run", True):
            # 延后一拍再最小化：先让识别框映射出来，避免看起来「什么都没发生」
            self.root.after(60, self._minimize_main)

    def exit_focus_mode(self) -> None:
        """退出「识别态」：停识别 + 收起识别框 + 唤回主面板。"""
        try:
            if self.ipc["running"].is_set():
                self.ipc["running"].clear()
                self.panel.set_running(False)
                self.float_win.set_indicator("paused")
            self.capture_frame.hide()
            self._restore_main()
            self.panel.set_status("识别框已退出，主面板已唤回")
            self.panel.log("INFO", "识别框已退出，主面板已唤回")
        except Exception:
            self.log.exception("退出识别框失败")

    def _minimize_main(self) -> None:
        """把主面板最小化到任务栏。"""
        try:
            state = self.root.state()
            if state == "withdrawn":
                # 用户此前用 F5 把面板藏起来了，保持隐藏，别硬拽出来
                return
            if state != "iconic":
                self.root.iconify()
            self._minimized_by_focus = True
        except Exception:
            self.log.exception("最小化主面板失败")

    def _restore_main(self) -> None:
        """唤回被识别态收走的主面板。"""
        try:
            if not self._minimized_by_focus:
                return
            self._minimized_by_focus = False
            self.root.deiconify()
            self.root.state("normal")
            self.root.lift()
            self.root.after(30, self._focus_main)
        except Exception:
            self.log.exception("唤回主面板失败")

    def _focus_main(self) -> None:
        try:
            self.root.focus_force()
        except Exception:
            pass

    def _on_frame_close(self) -> None:
        """识别框上的 ✕ 被点击。"""
        self.exit_focus_mode()

    def _bind_tk_hotkeys(self) -> None:
        for seq, action in self.hotkeys.tk_bindings():
            try:
                self.root.bind_all(seq, lambda _e, a=action: self._do_action(a), add="+")
            except Exception:
                pass
        self.root.bind_all("<Escape>", lambda _e: None, add="+")

    def _on_float_geometry(self, geometry) -> None:
        x, y, w, h = geometry
        self.cfg.set("float_window.x", int(x))
        self.cfg.set("float_window.y", int(y))
        if w > 100 and h > 100:
            self.cfg.set("float_window.width", int(w))
            self.cfg.set("float_window.height", int(h))
        self._dirty = True
        self._check_overlap()

    def _check_overlap(self) -> bool:
        """浮窗压住扫描区域会导致「识别到自己的输出」，必须提醒。

        返回 True 表示已经用警告覆盖了状态栏，调用方别再往上写正常提示。
        """
        region = self.cfg.get("region")
        if not region or not self.float_win.visible:
            return False
        try:
            fx, fy, fw, fh = self.float_win.geometry()
        except Exception:
            return False
        rx, ry, rw, rh = region
        if fx < rx + rw and rx < fx + fw and fy < ry + rh and ry < fy + fh:
            msg = "⚠ 浮窗与扫描区域重叠，可能造成循环识别，请把浮窗拖开"
            self.panel.set_status(msg)
            self.panel.log("WARN", msg)
            return True
        self.panel.set_status("区域与浮窗无重叠，状态正常")
        return False

    def _save_config(self) -> None:
        self._dirty = False
        self._last_save = time.time()
        if not self.cfg.save():
            self.log.warning("配置保存失败：%s", self.cfg.path)

    def _on_log(self, level: str, message: str) -> None:
        try:
            self.panel.log(level, message)
        except Exception:
            pass

    # ================================================================ 退出

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self.panel.set_status("正在退出…")
        self.log.info("准备退出")

        try:
            self._on_float_geometry(self.float_win.geometry())
        except Exception:
            pass
        try:
            self.cfg.set("region", list(self.capture_frame.region))
            self.cfg.set("capture_frame.alpha", round(self.capture_frame.alpha, 2))
        except Exception:
            pass
        # 最小化状态下 winfo_x/y 会返回 -32000 之类的位置，别拿它覆盖已保存的坐标
        try:
            if self.root.state() == "normal":
                self.cfg.set("main_window.x", self.root.winfo_x())
                self.cfg.set("main_window.y", self.root.winfo_y())
        except Exception:
            pass
        self._save_config()

        try:
            self.hotkeys.stop()
        except Exception:
            pass

        try:
            self.ipc["stop"].set()
            self.ipc["running"].clear()
        except Exception:
            pass

        for name, proc in self.processes.items():
            try:
                proc.join(timeout=2.0)
                if proc.is_alive():
                    proc.terminate()
                    proc.join(timeout=1.0)
                self.log.info("%s 进程已结束 exitcode=%s", name, proc.exitcode)
            except Exception:
                pass

        try:
            self.float_win.destroy()
        except Exception:
            pass
        try:
            self.capture_frame.destroy()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass


# ==================================================================== 入口

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="答题助手")
    parser.add_argument("--debug", action="store_true", help="输出调试日志")
    parser.add_argument("--bank", type=str, default="", help="启动时直接加载指定题库")
    parser.add_argument("--no-float", action="store_true", help="启动时不显示浮窗")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    if sys.platform == "win32":
        mp.freeze_support()

    args = parse_args(argv)

    # 必须在创建 Tk 之前完成 DPI 感知设置，否则高缩放屏下选区会偏移
    enable_dpi_awareness()

    cfg = Config().load()
    cfg.ensure_dirs()
    logmod.setup(logs_dir(), console=args.debug, level=10 if args.debug else 20)
    log = logmod.get("app")
    log.info("=" * 60)
    log.info("答题助手启动 | Python %s | 数据目录 %s", sys.version.split()[0], user_data_dir())
    scale = primary_dpi_scale()
    log.info("屏幕缩放比例 %.2f", scale)

    if args.bank:
        cfg.set("bank_path", args.bank)
    if args.no_float:
        cfg.set("float_window.auto_show", False)

    root = tk.Tk()
    theme = Theme(root, mode="light", scale=scale)
    theme.apply()

    window = cfg.get("main_window") or {}
    try:
        x = window.get("x")
        y = window.get("y")
        if isinstance(x, int) and isinstance(y, int):
            root.geometry(f"820x640+{max(0, x)}+{max(0, y)}")
        else:
            root.geometry("820x640")
    except Exception:
        root.geometry("820x640")

    app = App(root, cfg, theme)
    app.start()

    try:
        root.mainloop()
    except KeyboardInterrupt:
        app.quit()
    log.info("答题助手已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
