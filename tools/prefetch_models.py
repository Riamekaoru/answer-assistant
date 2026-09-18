# -*- coding: utf-8 -*-
"""OCR 模型预下载。

首次识别时 PaddleOCR 会自动联网拉取 PP-OCR 模型（约 20MB），
RapidOCR 的 ONNX 模型随包分发不需下载。

如果目标电脑完全离线，请在**有网**的机器上执行本脚本，
把输出目录整体拷贝过去，或在离线机上设置
`PADDLE_PDX_MODEL_SOURCE` / 手动放置到对应缓存目录。

    python tools/prefetch_models.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _report_dir(label: str, path: Path) -> None:
    if path.exists():
        total = 0
        count = 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                    count += 1
                except OSError:
                    pass
        print(f"  {label:<10} {path}")
        print(f"              {count} 个文件，{total / 1024 / 1024:.1f} MB")
    else:
        print(f"  {label:<10} {path}  (不存在)")


def prefetch() -> int:
    print("=" * 62)
    print(" 答题助手 · OCR 模型预下载")
    print("=" * 62)

    home = Path.home()

    print("\n[1/2] 初始化 PaddleOCR（自动下载 PP-OCR 模型）…")
    try:
        from core.ocr_engine import create_engine
        import numpy as np

        t0 = time.perf_counter()
        engine = create_engine("paddle", strict=True)
        if engine.available():
            dummy = np.full((40, 200, 3), 255, dtype=np.uint8)
            res = engine.recognize(dummy)
            print(f"  OK  {engine.display_name}，耗时 {time.perf_counter() - t0:.1f}s")
            if not res.ok:
                print(f"  !  推理返回错误：{res.error}")
        else:
            print("  !  PaddleOCR 不可用，跳过")
    except Exception as exc:
        print(f"  !  PaddleOCR 预下载失败：{exc}")

    print("\n[2/2] 初始化 RapidOCR（模型随包分发）…")
    try:
        from core.ocr_engine import create_engine
        import numpy as np

        engine = create_engine("rapid", strict=True)
        if engine.available():
            dummy = np.full((40, 200, 3), 255, dtype=np.uint8)
            res = engine.recognize(dummy)
            print(f"  OK  {engine.display_name}")
            if not res.ok:
                print(f"  !  推理返回错误：{res.error}")
        else:
            print("  !  RapidOCR 不可用，跳过")
    except Exception as exc:
        print(f"  !  RapidOCR 预下载失败：{exc}")

    print("\n模型缓存位置：")
    _report_dir("PaddleX", home / ".paddlex")
    _report_dir("PaddleOCR", home / ".paddleocr")
    candidates = [
        home / ".cache" / "rapidocr",
        home / "AppData" / "Local" / "rapidocr",
    ]
    for c in candidates:
        if c.exists():
            _report_dir("RapidOCR", c)

    print("\n离线部署方法：把上面列出的缓存目录整体拷贝到目标机器的相同位置即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(prefetch())
