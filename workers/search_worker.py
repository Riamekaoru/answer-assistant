# -*- coding: utf-8 -*-
"""搜索工作进程：OCR + 模糊匹配。

模型与题库都在本进程内加载，与 UI 完全隔离：
    - PaddleOCR 首次初始化要加载数秒到数十秒，且占用数百 MB 内存
    - 题库重建索引时会有短暂 CPU 峰值
这些都不应影响主进程的界面响应。

命令协议（主进程 -> 本进程，经 ctrl_q）：
    {"cmd": "reload_bank", "path": "..."}     重新导入题库
    {"cmd": "load_json_bank", "path": "..."}  直接读取已规范化的 json 题库
    {"cmd": "warmup"}                         预热 OCR
    {"cmd": "ping"}                           探活
"""

from __future__ import annotations

import os
import queue as _queue
import time
from typing import Any, Dict, Optional

from workers.ipc import (
    MSG_BANK,
    MSG_ERROR,
    MSG_READY,
    MSG_RESULT,
    MSG_STATUS,
    put_error,
    put_latest,
    put_status,
    setup_worker_logging,
)


def _drain_commands(ctrl_q, matcher, ipc, log) -> None:
    """非阻塞地处理所有待执行命令。"""
    while True:
        try:
            cmd = ctrl_q.get_nowait()
        except _queue.Empty:
            return
        except Exception:
            return
        if not isinstance(cmd, dict):
            continue
        name = cmd.get("cmd")
        try:
            if name in ("reload_bank", "load_json_bank"):
                path = cmd.get("path") or ""
                if not path or not os.path.exists(path):
                    put_error(ipc, f"题库文件不存在：{path}")
                    continue
                from core.question_bank import QuestionBank

                t0 = time.perf_counter()
                bank = QuestionBank()
                bank.load_file(path)
                matcher.set_bank(bank)
                stats = bank.stats()
                stats["elapsed"] = round(time.perf_counter() - t0, 3)
                put_latest(ipc["result_q"], {"type": MSG_BANK, "stats": stats,
                                            "path": path, "ts": time.time()})
                put_status(ipc, "INFO",
                           f"题库已加载：{stats['total']} 题"
                           f"（含答案 {stats['with_answer']}，含解析 {stats['with_analysis']}）")
                log.info("题库加载完成 %s", stats)
            elif name == "warmup":
                put_status(ipc, "INFO", "正在预热 OCR 引擎…")
                ok = matcher is not None
                _ = ok
                put_status(ipc, "INFO", "OCR 预热指令已接收")
            elif name == "ping":
                put_status(ipc, "INFO", "搜索进程存活")
            elif name == "set_engine":
                put_status(ipc, "WARN", "运行时切换 OCR 引擎需重启程序，已忽略")
        except Exception as exc:
            log.exception("命令执行失败 %s", name)
            put_error(ipc, f"命令 {name} 执行失败：{exc}")


def _pin_thread_env(threads: int) -> None:
    """把 BLAS / OpenMP 线程数钉住。

    Paddle 与 ONNXRuntime 默认按核心数铺线程，实测在 8 核机上能把整机吃干。
    识别间隔降到 300ms 后 OCR 调用变密，必须给个上限，否则用户会明显感到卡顿。
    必须在 import paddle / onnxruntime 之前设置才有效。

    threads <= 0 表示「交给引擎自动」，此时一个变量都不设。
    """
    threads = int(threads or 0)
    if threads <= 0:
        return
    value = str(threads)
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
        # 用户在系统里显式设过就不覆盖
        if not os.environ.get(key):
            os.environ[key] = value


def _load_engine(cfg: Dict[str, Any], ipc: Dict[str, Any], log):
    from core.ocr_engine import create_engine

    threads = int(cfg.get("ocr_threads", 0) or 0)
    if threads > 0:
        _pin_thread_env(threads)

    put_status(ipc, "INFO",
               f"正在加载 OCR 引擎（{cfg.get('ocr_engine', 'auto')}）…"
               "首次运行需下载模型，请稍候")
    t0 = time.perf_counter()
    # create_engine 内部会跑一次预热推理，能提前暴露 oneDNN 兼容性问题
    engine = create_engine(
        prefer=cfg.get("ocr_engine", "auto"),
        lang=cfg.get("ocr_lang", "ch"),
        use_gpu=bool(cfg.get("ocr_use_gpu", False)),
        enable_mkldnn=bool(cfg.get("ocr_mkldnn", False)),
        cpu_threads=threads,
    )
    cost = time.perf_counter() - t0
    if engine.available():
        put_status(ipc, "READY",
                   f"OCR 引擎就绪：{engine.display_name}（{cost:.1f}s）",
                   engine=engine.display_name)
        ipc["ocr_ready"].set()
    else:
        put_status(ipc, "ERROR",
                   "没有可用的 OCR 引擎，识别功能不可用。"
                   "请运行 install.bat，或查看 logs/app.log 了解具体原因。")
    return engine


def search_main(cfg: Dict[str, Any], ipc: Dict[str, Any]) -> None:
    """搜索进程入口。"""
    # 必须在任何数值库 import 之前钉住线程数，否则 native 库已经读走了默认值
    _pin_thread_env(int(cfg.get("ocr_threads", 0) or 0))

    log = setup_worker_logging(cfg.get("log_dir"), "search")
    log.info("搜索进程启动 pid=%d", os.getpid())

    region_arr = ipc["region"]
    sim_thresh = ipc["sim_thresh"]
    top_k = ipc["top_k"]
    force = ipc["force"]
    stop = ipc["stop"]
    frame_q = ipc["frame_q"]
    result_q = ipc["result_q"]
    ctrl_q = ipc["ctrl_q"]

    # ---- 题库 ------------------------------------------------------
    from core.matcher import Matcher, ocr_quality_hint, build_queries
    from core.ocr_engine import NullOcrEngine, preprocess
    from core.question_bank import QuestionBank

    bank = QuestionBank()
    bank_path = cfg.get("bank_path") or ""
    if bank_path and os.path.exists(bank_path):
        try:
            bank.load_file(bank_path)
            stats = bank.stats()
            put_latest(result_q, {"type": MSG_BANK, "stats": stats,
                                 "path": bank_path, "ts": time.time()})
            log.info("启动时加载题库 %s -> %d 题", bank_path, len(bank))
        except Exception as exc:
            log.exception("启动加载题库失败")
            put_error(ipc, f"题库加载失败：{exc}")
    else:
        put_status(ipc, "WARN", "尚未导入题库，匹配结果将为空")

    matcher = Matcher(bank, threshold=float(sim_thresh.value), top_k=int(top_k.value))

    # ---- OCR -------------------------------------------------------
    engine = _load_engine(cfg, ipc, log)
    if isinstance(engine, NullOcrEngine):
        put_status(ipc, "ERROR", "OCR 不可用：请执行 install.bat 安装识别引擎")

    prep = cfg.get("preprocess") or {}
    min_len = int(cfg.get("min_question_len", 6))
    idle_notified = False
    last_text_key = ""
    last_emit = 0.0
    stat_frames = 0
    stat_ocr_ms = 0.0
    stat_hits = 0
    put_latest(result_q, {"type": MSG_READY, "ts": time.time()})

    while not stop.is_set():
        try:
            _drain_commands(ctrl_q, matcher, ipc, log)

            # 阈值可运行时调整
            matcher.set_threshold(float(sim_thresh.value))
            matcher.set_top_k(int(top_k.value))

            try:
                payload = frame_q.get(timeout=0.15)
            except _queue.Empty:
                if not idle_notified:
                    put_status(ipc, "INFO", "等待画面变化…")
                    idle_notified = True
                continue
            idle_notified = False

            # 截屏通常比 OCR 快：队列里若已有更新的画面，旧帧就没必要再识别了。
            # 丢旧帧既省 CPU，也让结果对应的是「当前屏幕」而不是几百毫秒前的屏幕。
            stale = 0
            while True:
                try:
                    newer = frame_q.get_nowait()
                except Exception:
                    break
                if newer is not None:
                    payload = newer
                    stale += 1
            if stale:
                log.debug("跳过 %d 帧过期画面，只识别最新帧", stale)

            frame = payload.get("frame")
            if frame is None or getattr(frame, "size", 0) == 0:
                continue

            seq = int(payload.get("seq", 0))
            t0 = time.perf_counter()
            # 预处理参数支持运行时热更新（控制面板拖动即时生效）
            p_scale, p_gray, p_sharp, p_bin = (float(v) for v in ipc["prep"][:])
            img = preprocess(frame,
                             scale=p_scale or 1.0,
                             grayscale=p_gray > 0.5,
                             sharpen=p_sharp > 0.5,
                             binarize=p_bin > 0.5)
            ocr_result = engine.recognize(img)
            ocr_ms = (time.perf_counter() - t0) * 1000.0
            stat_frames += 1
            stat_ocr_ms += ocr_ms

            # 统计紧跟在 OCR 之后上报：下面有多条 continue 分支（识别过短、
            # 文本未变等），放到它们之后会出现「OCR 一直在跑但面板上从来看不到
            # 耗时」——而 OCR 正是这个程序真正的延迟瓶颈，必须让它可见。
            if stat_frames >= 5:
                put_status(ipc, "STAT", "识别统计",
                           avg_ocr_ms=round(stat_ocr_ms / stat_frames, 1),
                           frames=stat_frames, hits=stat_hits)
                stat_frames, stat_ocr_ms, stat_hits = 0, 0.0, 0

            if not ocr_result.ok:
                put_error(ipc, f"OCR 失败：{ocr_result.error}", seq=seq)
                continue

            text = ocr_result.text
            norm_len = len(text.replace(" ", "").replace("\n", ""))
            if norm_len < int(ipc["min_len"].value):
                put_latest(result_q, {
                    "type": MSG_RESULT, "seq": seq, "ts": time.time(),
                    "text": text, "matches": [], "ocr_ms": ocr_ms,
                    "engine": ocr_result.engine,
                    "quality": ocr_quality_hint(text) or "识别内容过短",
                    "region": list(payload.get("region") or region_arr[:]),
                })
                continue

            # OCR 文本未变时直接复用上次结果，省掉一次全量匹配
            text_key = text.replace(" ", "")
            if text_key == last_text_key and (time.time() - last_emit) < 3.0:
                continue

            t1 = time.perf_counter()
            matches = matcher.search(text)
            match_ms = (time.perf_counter() - t1) * 1000.0
            stat_hits += len(matches)

            last_text_key = text_key
            last_emit = time.time()

            put_latest(result_q, {
                "type": MSG_RESULT,
                "seq": seq,
                "ts": time.time(),
                "text": text,
                "blocks": build_queries(text),
                "matches": [{"score": m.score, "matched_on": m.matched_on,
                             "question": m.question.to_dict(),
                             "answer_display": m.question.answer_display,
                             "answer_letters": m.question.answer_letters}
                            for m in matches],
                "ocr_ms": round(ocr_ms, 1),
                "match_ms": round(match_ms, 1),
                "engine": ocr_result.engine,
                "quality": ocr_quality_hint(text),
                "diff": round(float(payload.get("diff", 0.0)), 2),
                "region": list(payload.get("region") or region_arr[:]),
            })

        except Exception as exc:
            log.exception("搜索循环异常")
            put_error(ipc, f"搜索异常：{exc}")
            time.sleep(0.2)

    log.info("搜索进程退出")
