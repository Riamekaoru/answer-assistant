# -*- coding: utf-8 -*-
"""截屏工作进程。

职责单一：按固定节奏抓取指定屏幕区域，做帧差检测，只把「有变化」的帧
投递给搜索进程。这样 OCR 不会对着静止画面反复空转。

节流策略：
    - frame_q 容量为 2，满时丢弃最旧帧（OCR 永远只处理最新画面）
    - 帧差低于阈值则跳过，除非收到 force 信号
    - 空闲时降频轮询，避免空转吃满一个核
"""

from __future__ import annotations

import time
from typing import Any, Dict

from workers.ipc import (
    MSG_STATUS,
    put_latest,
    put_status,
    setup_worker_logging,
)

IDLE_POLL = 0.08          # 无任务时的轮询间隔（秒）
STAT_INTERVAL = 2.0       # 状态上报间隔（秒）


def capture_main(cfg: Dict[str, Any], ipc: Dict[str, Any]) -> None:
    """截屏进程入口。"""
    log = setup_worker_logging(cfg.get("log_dir"), "capture")
    log.info("截屏进程启动 pid=%s", __import__("os").getpid())

    from core.screen import ScreenCapture, clamp_region, frame_diff

    try:
        screen = ScreenCapture()
    except Exception as exc:
        put_status(ipc, "ERROR", f"截屏初始化失败：{exc}")
        log.exception("截屏初始化失败")
        return

    region_arr = ipc["region"]
    interval_ms = ipc["interval_ms"]
    change_thresh = ipc["change_thresh"]
    running = ipc["running"]
    paused = ipc["paused"]
    force = ipc["force"]
    stop = ipc["stop"]
    frame_q = ipc["frame_q"]

    prev = None
    seq = 0
    emitted = 0
    last_stat = time.time()
    last_bounds_check = 0.0
    bounds = screen.virtual_bounds()
    waiting_notified = False

    put_status(ipc, "INFO", "截屏进程就绪")

    while not stop.is_set():
        try:
            if not running.is_set() or paused.is_set():
                if not waiting_notified:
                    put_status(ipc, "INFO", "截屏已暂停")
                    waiting_notified = True
                time.sleep(IDLE_POLL)
                continue
            waiting_notified = False

            now = time.time()
            if now - last_bounds_check > 5.0:
                bounds = screen.virtual_bounds()
                last_bounds_check = now

            region = tuple(int(v) for v in region_arr[:])
            target = clamp_region(region, bounds)
            if target is None:
                put_status(ipc, "WARN", "扫描区域无效或已移出屏幕，请重新划定")
                time.sleep(0.5)
                continue

            frame = screen.grab(target)
            diff = frame_diff(prev, frame)
            prev = frame

            forced = force.is_set()
            if forced:
                force.clear()

            threshold = float(change_thresh.value)
            if not forced and seq > 0 and diff < threshold:
                time.sleep(max(0.01, interval_ms.value / 1000.0))
                continue

            seq += 1
            emitted += 1
            put_latest(frame_q, {
                "type": "frame",
                "seq": seq,
                "ts": now,
                "region": target,
                "diff": diff,
                "frame": frame,
                "forced": forced,
            })

            if now - last_stat >= STAT_INTERVAL:
                last_stat = now
                put_status(ipc, "STAT", "截屏统计",
                           seq=seq, emitted=emitted, diff=round(diff, 2),
                           region=list(target))

            # 有变化时按设定间隔；强制/首帧立即进入下一次采样
            delay = 0.02 if forced else max(0.05, interval_ms.value / 1000.0)
            deadline = time.time() + delay
            while time.time() < deadline and not stop.is_set():
                time.sleep(min(0.05, max(0.005, deadline - time.time())))

        except Exception as exc:  # 单帧异常不应终止进程
            log.exception("截屏循环异常")
            put_status(ipc, "ERROR", f"截屏异常：{exc}")
            time.sleep(0.3)

    try:
        screen.close()
    except Exception:
        pass
    log.info("截屏进程退出，共投递 %d 帧", emitted)


__all__ = ["capture_main", "MSG_STATUS", "put_status"]
