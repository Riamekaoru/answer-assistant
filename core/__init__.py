# -*- coding: utf-8 -*-
"""答题助手 —— 核心模块。

模块划分：
    config          配置读写与路径解析
    logger          统一日志
    text_utils      文本归一化 / 清洗
    screen          屏幕采集
    ocr_engine      OCR 引擎适配层（PaddleOCR / RapidOCR）
    question_bank   题库导入与持久化
    matcher         模糊匹配与相似度评分
    hotkeys         全局快捷键
"""

__version__ = "1.0.0"
__all__ = ["__version__"]


def _bootstrap_bundled_models() -> None:
    """尽早把 PaddleX 的模型缓存目录指向随包目录。

    放在包初始化里，是为了保证「任何 core.* 模块被导入」时环境变量就已经设好：
    paddlex 在 import 阶段就把 CACHE_DIR 固化下来，之后再设无效。
    万一某个模块抢先 import 了 paddle，识别会悄悄退回用户目录
    （%USERPROFILE%\\.paddlex），在没网的机器上就是直接失败。
    """
    try:
        from .config import ensure_bundled_models

        ensure_bundled_models()
    except Exception:  # 配置模块自身出错不应拖垮整个包
        pass


_bootstrap_bundled_models()
