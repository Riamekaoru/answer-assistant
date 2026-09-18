# -*- coding: utf-8 -*-
"""配置读写与运行时路径解析。

路径策略：
    源码运行  -> 项目根目录下的 data/ 与 logs/
    打包运行  -> %LOCALAPPDATA%\\AnswerAssistant\\data 与 logs
这样打包成 exe 后安装在 Program Files 等只读目录时也不会写失败。
"""

from __future__ import annotations

import copy
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

APP_NAME = "AnswerAssistant"

DEFAULT_CONFIG: Dict[str, Any] = {
    "version": "1.0.0",
    "bank_path": "",
    "last_import_dir": "",
    "region": None,                       # [x, y, w, h]
    "capture_interval_ms": 120,           # 截屏间隔（越小越跟手；帧差检测会挡住静止画面的重复 OCR）
    "change_threshold": 1.2,              # 帧差异阈值（0-255 灰度均差），越小越灵敏
    "similarity_threshold": 0.58,         # 匹配相似度门槛 0-1
    "top_k": 5,
    "min_question_len": 6,                # OCR 文本短于此长度不触发匹配
    "ocr_engine": "auto",                 # auto | paddle | rapid | none
    "ocr_lang": "ch",
    "ocr_use_gpu": False,
    "ocr_threads": 4,                     # OCR 推理线程数；0=交给引擎自动（可能吃满所有核心）
    "ocr_mkldnn": False,                  # PaddlePaddle oneDNN 加速；部分 CPU 上会崩溃，默认关闭
    "preprocess": {
        "scale": 1.6,                     # 放大倍数，小字识别关键
        "grayscale": True,
        "sharpen": True,
        "binarize": False,
    },
    "float_window": {
        "alpha": 0.95,
        "font_size": 11,
        "width": 470,
        "height": 400,
        "topmost": True,
        "x": None,
        "y": None,
        "auto_show": True,
    },
    # 常驻识别框（半透明边框窗）。区域本身存在 region 里，这里只放外观。
    "capture_frame": {
        "show": True,                     # 开始识别时是否自动显示识别框
        "alpha": 0.75,                    # 识别框不透明度 0.15~1.0
        "border": 10,                     # 边框粗细（像素），画在扫描区之外
        "header_height": 24,              # 顶栏高度（像素）
        "color": "",                      # 空 -> 用主题主色
    },
    "main_window": {
        "x": None,
        "y": None,
        "minimize_on_run": True,          # 开始识别时把主面板最小化，退出识别框时再唤回
    },
    "hotkeys": {
        "toggle_run": "f2",
        "select_region": "f1",
        "toggle_float": "f3",
        "capture_once": "f4",
        "toggle_main": "f5",
        "clear": "f6",
        "cycle_alpha": "f7",
        "toggle_frame": "f9",
        "quit": "f8",
    },
    "log_level": "INFO",
    "autostart": False,
}


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """程序所在目录（用于定位 assets、示例题库）。"""
    if _frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def user_data_dir() -> Path:
    """可写的用户数据目录。"""
    if _frozen():
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / APP_NAME
    return app_root() / "data"


def logs_dir() -> Path:
    if _frozen():
        return user_data_dir() / "logs"
    return app_root() / "logs"


def assets_dir() -> Path:
    return app_root() / "assets"


def bundled_models_dir() -> Optional[Path]:
    """随安装包附带的 OCR 模型目录（用于完全离线运行）；没有则返回 None。"""
    p = app_root() / "models" / "paddlex"
    return p if p.is_dir() else None


def ensure_bundled_models() -> Optional[Path]:
    """把 PaddleX 的模型缓存目录指到随包目录，实现不联网也能识别。

    必须在 import paddle / paddlex **之前**调用：paddlex.utils.cache 在导入时
    就把 CACHE_DIR 固化下来了，之后再设环境变量已经无效。

    没有随包模型（源码方式运行）时什么都不做，沿用 %USERPROFILE%\\.paddlex。
    """
    if os.environ.get("PADDLE_PDX_CACHE_HOME"):
        return None
    d = bundled_models_dir()
    if d is not None:
        os.environ["PADDLE_PDX_CACHE_HOME"] = str(d)
    return d


def config_path() -> Path:
    return user_data_dir() / "config.json"


# ---------------------------------------------------------------- Config

class Config:
    """线程安全的配置容器（点分键访问）。"""

    def __init__(self, path: Optional[Path] = None, data: Optional[dict] = None):
        self.path = Path(path) if path else config_path()
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
        if data:
            self._merge(self._data, data)

    # -- 合并 / 读写 --------------------------------------------------

    @staticmethod
    def _merge(base: Dict[str, Any], incoming: Dict[str, Any]) -> None:
        for key, value in (incoming or {}).items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                Config._merge(base[key], value)
            else:
                base[key] = value

    def load(self) -> "Config":
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    with self._lock:
                        self._merge(self._data, raw)
        except Exception:
            # 配置损坏时保留默认值，不阻断启动
            pass
        return self

    def save(self) -> bool:
        with self._lock:
            payload = copy.deepcopy(self._data)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
            return True
        except Exception:
            return False

    # -- 访问 ---------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            node: Any = self._data
            for part in key.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return copy.deepcopy(node) if isinstance(node, (dict, list)) else node

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            parts = key.split(".")
            node = self._data
            for part in parts[:-1]:
                if not isinstance(node.get(part), dict):
                    node[part] = {}
                node = node[part]
            node[parts[-1]] = value

    def update(self, mapping: Dict[str, Any]) -> None:
        for k, v in mapping.items():
            self.set(k, v)

    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def reset(self) -> None:
        with self._lock:
            self._data = copy.deepcopy(DEFAULT_CONFIG)

    # -- 便捷属性 -----------------------------------------------------

    @property
    def data(self) -> Dict[str, Any]:
        return self._data

    def ensure_dirs(self) -> None:
        user_data_dir().mkdir(parents=True, exist_ok=True)
        logs_dir().mkdir(parents=True, exist_ok=True)
