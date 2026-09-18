# -*- coding: utf-8 -*-
"""诊断 PaddleOCR 初始化失败的完整堆栈。用法：python diag_ocr.py <ROOT>"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.setdefault("OMP_NUM_THREADS", "4")
os.environ.setdefault("MKL_NUM_THREADS", "4")

print("ROOT =", ROOT)
print("sys.executable  =", sys.executable)
print("sys.base_prefix =", sys.base_prefix)

try:
    from core.config import bundled_models_dir, ensure_bundled_models
    print("bundled_models_dir =", bundled_models_dir())
    ensure_bundled_models()
    print("PADDLE_PDX_CACHE_HOME =", os.environ.get("PADDLE_PDX_CACHE_HOME"))
except Exception:
    traceback.print_exc()

try:
    import paddle
    print("paddle", paddle.__version__, "from", os.path.dirname(paddle.__file__))
except Exception:
    traceback.print_exc()
    raise SystemExit(1)

try:
    import paddlex
    print("paddlex", getattr(paddlex, "__version__", "?"),
          "from", os.path.dirname(paddlex.__file__))
except Exception:
    traceback.print_exc()
    raise SystemExit(1)

from paddleocr import PaddleOCR

kwargs = dict(lang="ch", use_doc_orientation_classify=False,
              use_doc_unwarping=False, use_textline_orientation=False,
              device="cpu", enable_mkldnn=False, cpu_threads=4)
print("\n--> PaddleOCR(%s)" % ", ".join("%s=%r" % kv for kv in kwargs.items()))
try:
    ocr = PaddleOCR(**kwargs)
    print("INIT OK ->", type(ocr).__name__)
except Exception:
    print("INIT FAILED")
    traceback.print_exc()
    # 逐层挖掘真实原因
    exc = sys.exc_info()[1]
    cause = exc
    depth = 0
    while cause is not None and depth < 8:
        print("\n[cause %d] %s: %s" % (depth, type(cause).__name__, cause))
        cause = getattr(cause, "__cause__", None) or getattr(cause, "__context__", None)
        depth += 1
