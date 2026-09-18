# -*- coding: utf-8 -*-
"""进程间通信原语与共用工具。

所有共享对象必须在主进程中创建（绑定到具体的 multiprocessing 上下文），
再作为参数传给子进程，这样才能在 Windows 的 spawn 启动方式下正确传递句柄。
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

# 保证子进程即使以脚本方式启动也能 import core.*
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

MSG_RESULT = "result"
MSG_STATUS = "status"
MSG_BANK = "bank"
MSG_ERROR = "error"
MSG_READY = "ready"


def build_ipc(ctx) -> Dict[str, Any]:
    """创建一组共享对象。ctx 由 multiprocessing.get_context('spawn') 得到。"""
    return {
        # 控制量
        "region": ctx.Array("i", [0, 0, 0, 0], lock=True),
        "interval_ms": ctx.Value("i", 700, lock=True),
        "change_thresh": ctx.Value("d", 1.2, lock=True),
        "sim_thresh": ctx.Value("d", 0.58, lock=True),
        "top_k": ctx.Value("i", 5, lock=True),
        "min_len": ctx.Value("i", 6, lock=True),
        # 预处理参数： [放大倍数, 灰度, 锐化, 二值化]
        "prep": ctx.Array("d", [1.0, 0.0, 0.0, 0.0], lock=True),
        # 事件
        "running": ctx.Event(),
        "paused": ctx.Event(),
        "force": ctx.Event(),
        "stop": ctx.Event(),
        "ocr_ready": ctx.Event(),
        # 队列
        "frame_q": ctx.Queue(maxsize=2),      # 截屏 -> 搜索（满则丢最旧）
        "result_q": ctx.Queue(maxsize=16),    # 搜索 -> 主进程
        "stat_q": ctx.Queue(maxsize=128),     # 任意 -> 主进程（状态/日志）
        "ctrl_q": ctx.Queue(maxsize=32),      # 主进程 -> 搜索（命令）
    }


def put_latest(q, item: Any) -> None:
    """向队列写入并丢弃过期数据（生产者永不阻塞）。"""
    try:
        q.put_nowait(item)
        return
    except queue.Full:
        pass
    for _ in range(3):
        try:
            q.get_nowait()
        except queue.Empty:
            break
    try:
        q.put_nowait(item)
    except queue.Full:
        pass


def put_status(ipc: Dict[str, Any], level: str, text: str, **extra: Any) -> None:
    payload = {"type": MSG_STATUS, "level": level, "text": text, "ts": time.time()}
    payload.update(extra)
    put_latest(ipc["stat_q"], payload)


def put_error(ipc: Dict[str, Any], text: str, **extra: Any) -> None:
    payload = {"type": MSG_ERROR, "level": "ERROR", "text": text, "ts": time.time()}
    payload.update(extra)
    put_latest(ipc["stat_q"], payload)


def setup_worker_logging(log_dir: Optional[str], name: str) -> logging.Logger:
    """子进程日志只写文件，避免向不可见的 stderr 写入。"""
    from core import logger as logmod

    if log_dir:
        logmod.setup(Path(log_dir), console=False)
    else:
        logmod.setup(None, console=False)
    return logmod.get(name)
