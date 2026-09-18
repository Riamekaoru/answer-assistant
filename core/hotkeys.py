# -*- coding: utf-8 -*-
"""全局快捷键。

使用 keyboard 库注册系统级热键，回调只做一件事：把动作名塞进队列，
由主进程的 Tk 事件循环消费 —— 绝不能在钩子线程里直接操作 UI。

若 keyboard 库缺失或被安全策略拦截（部分环境需要管理员权限），
自动降级为「仅窗口焦点内生效」，由 Tk 的 bind_all 接管。
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, Dict, Optional

log = logging.getLogger("answerassist.hotkey")

DEFAULT_BINDINGS: Dict[str, str] = {
    "toggle_run": "f2",
    "select_region": "f1",
    "toggle_float": "f3",
    "capture_once": "f4",
    "toggle_main": "f5",
    "clear": "f6",
    "cycle_alpha": "f7",
    "toggle_frame": "f9",
    "quit": "f8",
}

ACTION_LABELS: Dict[str, str] = {
    "toggle_run": "开始 / 暂停持续识别",
    "select_region": "重新划定扫描区域",
    "toggle_float": "显示 / 隐藏答案浮窗",
    "capture_once": "立即识别一次",
    "toggle_main": "显示 / 隐藏控制面板",
    "clear": "清空当前结果",
    "cycle_alpha": "循环切换浮窗透明度",
    "toggle_frame": "进入 / 退出识别态（识别框 + 面板最小化）",
    "quit": "退出程序",
}

_TK_KEYSYM = {
    "f1": "<F1>", "f2": "<F2>", "f3": "<F3>", "f4": "<F4>",
    "f5": "<F5>", "f6": "<F6>", "f7": "<F7>", "f8": "<F8>",
    "f9": "<F9>", "f10": "<F10>", "f11": "<F11>", "f12": "<F12>",
}


def normalize_combo(combo: str) -> str:
    """把用户输入统一为 keyboard 库可识别的写法，如 'Ctrl+Alt+Q'。"""
    if not combo:
        return ""
    parts = [p.strip().lower() for p in str(combo).split("+") if p.strip()]
    aliases = {
        "control": "ctrl", "ctl": "ctrl", "win": "windows", "super": "windows",
        "meta": "windows", "esc": "escape", "return": "enter", "del": "delete",
        "space": "space", "pgup": "page up", "pgdn": "page down",
    }
    fixed = [aliases.get(p, p) for p in parts]
    # 修饰键在前，主键在后
    mods = [k for k in fixed if k in ("ctrl", "alt", "shift", "windows")]
    keys = [k for k in fixed if k not in ("ctrl", "alt", "shift", "windows")]
    ordered = mods + keys
    return "+".join(ordered)


def to_tk_sequence(combo: str) -> Optional[str]:
    """把组合键转成 Tk 事件序列（仅支持无修饰的单功能键与常见修饰组合）。"""
    norm = normalize_combo(combo)
    if not norm:
        return None
    if norm in _TK_KEYSYM:
        return _TK_KEYSYM[norm]
    parts = norm.split("+")
    mods = []
    key = ""
    for p in parts:
        if p in ("ctrl", "alt", "shift"):
            mods.append({"ctrl": "Control", "alt": "Alt", "shift": "Shift"}[p])
        elif p == "windows":
            return None
        else:
            key = p
    if not key:
        return None
    keyname = key if len(key) == 1 else key.capitalize()
    if not mods:
        return f"<{keyname}>"
    return "<" + "-".join(mods + [keyname]) + ">"


class HotkeyManager:
    """系统级热键管理器。"""

    def __init__(self, bindings: Optional[Dict[str, str]] = None,
                 event_queue: Optional[queue.Queue] = None):
        self.bindings: Dict[str, str] = dict(DEFAULT_BINDINGS)
        if bindings:
            for action, combo in bindings.items():
                self.bindings[action] = normalize_combo(combo) or self.bindings.get(action, "")
        self.queue: queue.Queue = event_queue or queue.Queue()
        self.available = False
        self.error = ""
        self._keyboard = None
        self._registered: Dict[str, str] = {}
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 生命周期

    def _emit(self, action: str) -> None:
        try:
            self.queue.put_nowait(("hotkey", action))
        except Exception:
            pass

    def start(self) -> bool:
        """注册全部热键。返回是否成功启用系统级热键。"""
        try:
            import keyboard  # type: ignore
        except Exception as exc:
            self.error = f"未安装 keyboard 库: {exc}"
            log.warning("系统级热键不可用：%s（将仅支持窗口内快捷键）", self.error)
            return False

        self._keyboard = keyboard
        with self._lock:
            for action, combo in self.bindings.items():
                if not combo:
                    continue
                try:
                    keyboard.add_hotkey(combo, self._make_cb(action), suppress=False,
                                        trigger_on_release=True)
                    self._registered[action] = combo
                except Exception as exc:
                    log.warning("热键注册失败 %s(%s): %s", action, combo, exc)
            self.available = bool(self._registered)
        if self.available:
            log.info("系统级热键已启用：%s",
                     ", ".join(f"{v}→{ACTION_LABELS.get(k, k)}" for k, v in self._registered.items()))
        return self.available

    def _make_cb(self, action: str) -> Callable[[], None]:
        def _cb() -> None:
            self._emit(action)
        return _cb

    def stop(self) -> None:
        kb = self._keyboard
        if kb is None:
            return
        try:
            for action, combo in list(self._registered.items()):
                try:
                    kb.remove_hotkey(combo)
                except Exception:
                    pass
        finally:
            self._registered.clear()
            self.available = False

    def rebind(self, bindings: Dict[str, str]) -> bool:
        """重新绑定热键（先全部注销再注册）。"""
        self.stop()
        for action, combo in (bindings or {}).items():
            norm = normalize_combo(combo)
            if norm:
                self.bindings[action] = norm
        return self.start()

    # ---------------------------------------------------------------- 查询

    def describe(self) -> str:
        rows = []
        for action, label in ACTION_LABELS.items():
            rows.append(f"{self.bindings.get(action, '-').upper() or '-':<14}{label}")
        return "\n".join(rows)

    def combo_for(self, action: str) -> str:
        return self.bindings.get(action, "")

    def conflicts(self) -> Dict[str, list]:
        """检查重复绑定。"""
        seen: Dict[str, list] = {}
        for action, combo in self.bindings.items():
            if combo:
                seen.setdefault(combo, []).append(action)
        return {c: a for c, a in seen.items() if len(a) > 1}

    def tk_bindings(self):
        """产出 (Tk事件序列, action) 对，用于窗口内兜底。"""
        out = []
        for action, combo in self.bindings.items():
            seq = to_tk_sequence(combo)
            if seq:
                out.append((seq, action))
        return out
