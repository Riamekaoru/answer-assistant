# -*- coding: utf-8 -*-
"""统一日志：文件（按大小轮转） + 控制台 + UI 回调。"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Callable, Optional

_LOGGER_NAME = "answerassist"
_configured = False
_ui_sink: Optional[Callable[[str, str], None]] = None


class _UiBridge(logging.Handler):
    """把日志转发给 UI（由主进程注册回调）。"""

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        if _ui_sink is None:
            return
        try:
            _ui_sink(record.levelname, self.format(record))
        except Exception:
            pass


def set_ui_sink(sink: Optional[Callable[[str, str], None]]) -> None:
    """注册 UI 日志回调（level_name, message）。"""
    global _ui_sink
    _ui_sink = sink


def setup(log_dir: Optional[Path] = None, level: int = logging.INFO,
          console: bool = True) -> logging.Logger:
    """初始化根日志器。可重复调用，仅首次生效。"""
    global _configured
    logger = logging.getLogger(_LOGGER_NAME)
    if _configured:
        return logger

    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(logging.NullHandler())

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(processName)-10s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                str(log_dir / "app.log"), maxBytes=2 * 1024 * 1024,
                backupCount=3, encoding="utf-8",
            )
            fh.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)-7s] %(processName)-10s %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            fh.setLevel(logging.DEBUG)
            logger.addHandler(fh)
        except Exception:
            pass

    if console:
        stream = sys.stderr
        if stream is not None:
            try:
                sh = logging.StreamHandler(stream)
                sh.setFormatter(fmt)
                sh.setLevel(level)
                logger.addHandler(sh)
            except Exception:
                pass

    bridge = _UiBridge()
    bridge.setFormatter(logging.Formatter("%(message)s"))
    bridge.setLevel(logging.INFO)
    logger.addHandler(bridge)

    logger.setLevel(level)
    _configured = True
    return logger


def get(name: str = "") -> logging.Logger:
    """获取子日志器。"""
    if name:
        return logging.getLogger(f"{_LOGGER_NAME}.{name}")
    return logging.getLogger(_LOGGER_NAME)


def set_level(level: int) -> None:
    """调整日志级别（同时影响文件与控制台 handler）。"""
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    for h in logger.handlers:
        if isinstance(h, _UiBridge):
            continue
        if h.level != logging.DEBUG or not isinstance(h, logging.handlers.RotatingFileHandler):
            h.setLevel(level)
