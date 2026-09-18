# -*- coding: utf-8 -*-
"""OCR 引擎适配层。

对外只暴露一个统一接口，内部兼容三种实现：

    1. PaddleOCR 3.x  ->  PaddleOCR(lang=...) + .predict() 返回 dict 风格结果
    2. PaddleOCR 2.x  ->  PaddleOCR(...) + .ocr() 返回 [box, (text, score)] 嵌套列表
    3. RapidOCR       ->  基于 PaddleOCR 的 PP-OCR 模型转 ONNX，轻量、纯离线

设计原则：绝不因为某个引擎不可用就崩掉整个程序。任何初始化失败都会被捕获，
并自动降级到下一个可用引擎；全部不可用时返回 NullOcrEngine（识别结果为空）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

import numpy as np

from .config import ensure_bundled_models
from .text_utils import parse_answer_line

# 若安装包里带了 OCR 模型，先把 PaddleX 的缓存目录指过去。
# 必须在这里做：本模块之后才会 import paddle/paddlex，而 PaddleX 的缓存路径
# 是导入期固化的，晚了就无效。源码运行时该函数是空操作。
ensure_bundled_models()

log = logging.getLogger("answerassist.ocr")

BBox = List[List[float]]


def _warn_if_bundled_models_unused() -> None:
    """随包模型存在、却没有被真正用上时，喊一声。

    PaddleX 在 import 阶段就把 CACHE_DIR 固化（paddlex/utils/cache.py），
    所以只要有任何模块抢先 import 了 paddle/paddlex，
    PADDLE_PDX_CACHE_HOME 就来不及生效，识别会**悄悄**退回用户目录
    （%USERPROFILE%\\.paddlex）。开发机上那个目录恰好有模型，所以看不出问题；
    到了没网的机器上就是「找不到可用 OCR 引擎」。
    这种失败极难排查，所以这里主动检查并写明原因。
    """
    try:
        from .config import app_root, bundled_models_dir

        if bundled_models_dir() is None:      # 源码方式运行，允许用用户目录
            return
        import paddlex.utils.cache as _cache  # type: ignore

        cache_dir = str(getattr(_cache, "CACHE_DIR", "") or "")
    except Exception:
        return

    root = str(app_root()).lower()
    if cache_dir and root not in cache_dir.lower():
        log.warning(
            "随包 OCR 模型未被使用：paddlex 缓存目录是 %s，期望在 %s 之下。"
            "通常是某个模块在 core 包初始化之前就 import 了 paddle/paddlex，"
            "导致 PADDLE_PDX_CACHE_HOME 失效；离线机器上会因此找不到模型。",
            cache_dir, app_root())


# ---------------------------------------------------------------- 数据结构

@dataclass
class OcrLine:
    text: str
    score: float = 1.0
    box: Optional[BBox] = None


@dataclass
class OcrResult:
    lines: List[OcrLine] = field(default_factory=list)
    engine: str = "none"
    elapsed: float = 0.0
    ok: bool = False
    error: str = ""

    @property
    def text(self) -> str:
        return "\n".join(ln.text for ln in self.lines)

    @property
    def compact(self) -> str:
        return " ".join(ln.text for ln in self.lines)

    @property
    def empty(self) -> bool:
        return not self.lines

    def mean_score(self) -> float:
        if not self.lines:
            return 0.0
        return sum(ln.score for ln in self.lines) / len(self.lines)


# ---------------------------------------------------------------- 前处理

def preprocess(img: np.ndarray, *, scale: float = 1.0, grayscale: bool = False,
               sharpen: bool = False, binarize: bool = False) -> np.ndarray:
    """在送 OCR 之前做轻量增强。经验值：小字号 + 深色主题下 scale=1.5~2.0 收益最大。"""
    import cv2

    out = img
    if scale and abs(scale - 1.0) > 0.01:
        h, w = out.shape[:2]
        interp = cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA
        out = cv2.resize(out, (max(1, int(w * scale)), max(1, int(h * scale))),
                         interpolation=interp)

    if grayscale or binarize:
        if out.ndim == 3:
            out = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)

    if sharpen and out.ndim == 2:
        # 非锐化掩模，比固定卷积核更稳定
        blur = cv2.GaussianBlur(out, (0, 0), 1.0)
        out = cv2.addWeighted(out, 1.6, blur, -0.6, 0)
    elif sharpen and out.ndim == 3:
        blur = cv2.GaussianBlur(out, (0, 0), 1.0)
        out = cv2.addWeighted(out, 1.6, blur, -0.6, 0)

    if binarize:
        if out.ndim == 3:
            out = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)
        out = cv2.adaptiveThreshold(out, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                    cv2.THRESH_BINARY, 31, 11)
        out = cv2.cvtColor(out, cv2.COLOR_GRAY2BGR)

    if out.ndim == 2:
        out = cv2.cvtColor(out, cv2.COLOR_GRAY2BGR)
    return out


def _poly_to_box(poly: Any) -> Optional[BBox]:
    try:
        arr = np.asarray(poly, dtype=float)
        if arr.ndim == 2 and arr.shape[1] >= 2:
            return arr[:, :2].tolist()
    except Exception:
        pass
    return None


def _dedup_lines(lines: List[OcrLine], min_score: float = 0.35) -> List[OcrLine]:
    """过滤低置信度与重复行。"""
    out: List[OcrLine] = []
    seen = set()
    for ln in lines:
        t = (ln.text or "").strip()
        if not t or ln.score < min_score:
            continue
        key = t.replace(" ", "")
        if key in seen:
            continue
        seen.add(key)
        out.append(OcrLine(text=t, score=float(ln.score), box=ln.box))
    return out


# ---------------------------------------------------------------- 基类

class BaseOcrEngine:
    name = "base"
    display_name = "未加载"

    def available(self) -> bool:
        return False

    def warmup(self) -> bool:
        return self.available()

    def recognize(self, img_bgr: np.ndarray) -> OcrResult:
        raise NotImplementedError


# ---------------------------------------------------------------- PaddleOCR

class PaddleOcrEngine(BaseOcrEngine):
    """PaddleOCR。优先使用，识别精度最高。

    ⚠ 已知坑：PaddlePaddle 3.3.x 在部分 CPU 上开启 oneDNN（mkldnn）后，
    推理会抛 `ConvertPirAttribute2RuntimeAttribute not support ...`
    且该错误发生在**运行时**而非初始化阶段，所以只能在预热时发现。
    因此这里默认关闭 mkldnn，并在预热失败时自动尝试「取反」重建一次。
    """

    name = "paddle"

    def __init__(self, lang: str = "ch", use_gpu: bool = False,
                 det_limit_side_len: int = 1280,
                 enable_mkldnn: Optional[bool] = False,
                 cpu_threads: int = 0):
        self._ocr = None
        self._mode = ""            # "v3" | "v2"
        self._version = ""
        self.display_name = "PaddleOCR"
        self._lang = lang
        self._use_gpu = use_gpu
        self._det_limit = det_limit_side_len
        self._mkldnn = enable_mkldnn
        self._threads = max(0, int(cpu_threads or 0))
        self._init()

    # -- 初始化 -------------------------------------------------------

    def _init(self) -> None:
        try:
            import paddleocr
            self._version = str(getattr(paddleocr, "__version__", "") or "")
        except Exception as exc:
            log.warning("PaddleOCR 不可用: %s", exc)
            return

        # PaddleOCR / PaddleX 的日志非常吵，压制到 WARNING
        for noisy in ("ppocr", "paddlex", "paddle"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

        if self._build(enable_mkldnn=self._mkldnn):
            return
        self._try_init_v2()

    def _build(self, enable_mkldnn: Optional[bool]) -> bool:
        """构造 3.x 实例。成功返回 True。"""
        major = 0
        try:
            major = int(self._version.split(".")[0])
        except Exception:
            pass
        try:
            from paddleocr import PaddleOCR  # type: ignore
        except Exception:
            return False

        base = dict(lang=self._lang)
        # 3.x 关闭文档方向/矫正，既提速也避免下载额外模型
        for key in ("use_doc_orientation_classify", "use_doc_unwarping",
                    "use_textline_orientation"):
            base[key] = False
        base["device"] = "gpu:0" if self._use_gpu else "cpu"
        if enable_mkldnn is not None:
            base["enable_mkldnn"] = bool(enable_mkldnn)

        # 依次尝试：带线程上限 -> 不带（老版本不认 cpu_threads） -> 最简参数
        attempts = []
        if self._threads > 0 and not self._use_gpu:
            attempts.append(dict(base, cpu_threads=self._threads))
        attempts.append(dict(base))
        attempts.append(dict(lang=self._lang))
        last = len(attempts) - 1

        for index, kwargs in enumerate(attempts):
            try:
                self._ocr = PaddleOCR(**kwargs)
                self._mode = "v3"
                suffix = ""
                if not self._use_gpu:
                    suffix = " (mkldnn 开)" if base.get("enable_mkldnn") else " (mkldnn 关)"
                    if kwargs.get("cpu_threads"):
                        suffix += f" / {kwargs['cpu_threads']} 线程"
                self.display_name = f"PaddleOCR {self._version or '3.x'}{suffix}"
                log.info("OCR 引擎就绪: %s", self.display_name)
                _warn_if_bundled_models_unused()
                return True
            except Exception as exc:
                if index == last and major >= 3:
                    log.warning("PaddleOCR 3.x 初始化失败: %s", exc)
        self._ocr = None
        return False

    def _try_init_v2(self) -> bool:
        try:
            from paddleocr import PaddleOCR  # type: ignore

            self._ocr = PaddleOCR(use_angle_cls=True, lang=self._lang,
                                  show_log=False, use_gpu=self._use_gpu)
            self._mode = "v2"
            self.display_name = f"PaddleOCR {self._version or '2.x'}"
            log.info("OCR 引擎就绪: %s", self.display_name)
            return True
        except Exception as exc:
            log.warning("PaddleOCR 2.x 初始化失败: %s", exc)
            self._ocr = None
            return False

    def available(self) -> bool:
        return self._ocr is not None

    # -- 预热（同时探测 oneDNN 兼容性）--------------------------------

    def warmup(self) -> bool:
        if self._ocr is None:
            return False
        dummy = np.full((48, 260, 3), 255, dtype=np.uint8)
        probe = self.recognize(dummy)
        if probe.ok:
            return True

        err = probe.error or ""
        log.warning("PaddleOCR 预热失败：%s", err[:200])

        # oneDNN 兼容性问题（或其它运行时异常）：换 mkldnn 开关重建一次
        if not self._use_gpu:
            flipped = not bool(self._mkldnn)
            log.info("尝试以 enable_mkldnn=%s 重建 PaddleOCR", flipped)
            self._ocr = None
            if self._build(enable_mkldnn=flipped):
                probe2 = self.recognize(dummy)
                if probe2.ok:
                    self.display_name += " (mkldnn 自动切换)"
                    log.info("PaddleOCR 已通过 mkldnn=%s 恢复正常", flipped)
                    return True
                log.warning("重建后仍然失败：%s", (probe2.error or "")[:200])
        return False

    # -- 识别 ---------------------------------------------------------

    def recognize(self, img_bgr: np.ndarray) -> OcrResult:
        if self._ocr is None:
            return OcrResult(engine=self.name, ok=False, error="PaddleOCR 未初始化")

        t0 = time.perf_counter()
        try:
            if self._mode == "v3":
                lines = self._run_v3(img_bgr)
            else:
                lines = self._run_v2(img_bgr)
        except Exception as exc:
            log.debug("PaddleOCR 识别异常，尝试另一种 API: %s", exc)
            try:
                lines = self._run_v2(img_bgr) if self._mode == "v3" else self._run_v3(img_bgr)
            except Exception as exc2:
                return OcrResult(engine=self.name, ok=False,
                                 elapsed=time.perf_counter() - t0,
                                 error=f"{type(exc2).__name__}: {exc2}")

        return OcrResult(lines=_dedup_lines(lines), engine=self.display_name,
                         elapsed=time.perf_counter() - t0, ok=True)

    def _run_v3(self, img: np.ndarray) -> List[OcrLine]:
        try:
            raw = self._ocr.predict(img)
        except TypeError:
            raw = self._ocr.predict(input=img)  # type: ignore[call-arg]
        if raw is None:
            return []
        if not isinstance(raw, (list, tuple)):
            raw = [raw]

        lines: List[OcrLine] = []
        for item in raw:
            lines.extend(self._parse_v3_item(item))
        return lines

    @staticmethod
    def _parse_v3_item(item: Any) -> List[OcrLine]:
        """3.x 的返回是 OCRResult（dict 子类）或含 .json 属性的对象。"""
        data: Any = item
        if not isinstance(item, dict):
            for attr in ("json", "res", "to_dict"):
                val = getattr(item, attr, None)
                if val is None:
                    continue
                try:
                    data = val() if callable(val) else val
                except Exception:
                    continue
                if isinstance(data, dict):
                    break
        if isinstance(data, dict) and "res" in data and isinstance(data["res"], dict):
            data = data["res"]
        if not isinstance(data, dict):
            return []

        texts = data.get("rec_texts") or data.get("texts") or []
        scores = data.get("rec_scores") or data.get("scores") or []
        polys = (data.get("rec_polys") or data.get("dt_polys")
                 or data.get("rec_boxes") or [])

        out: List[OcrLine] = []
        for i, text in enumerate(texts):
            score = float(scores[i]) if i < len(scores) else 1.0
            box = _poly_to_box(polys[i]) if i < len(polys) else None
            out.append(OcrLine(text=str(text), score=score, box=box))
        return out

    def _run_v2(self, img: np.ndarray) -> List[OcrLine]:
        try:
            raw = self._ocr.ocr(img, cls=True)
        except TypeError:
            raw = self._ocr.ocr(img)
        if not raw:
            return []

        out: List[OcrLine] = []
        # 2.x 结构: [ page ][ line ][ box, (text, score) ]；单页时也可能少一层
        pages = raw if isinstance(raw[0], list) and raw and isinstance(raw[0][0], list) \
            and len(raw[0]) and isinstance(raw[0][0], list) else [raw]
        for page in pages:
            if not page:
                continue
            for entry in page:
                try:
                    if entry is None or len(entry) < 2:
                        continue
                    box = _poly_to_box(entry[0])
                    payload = entry[1]
                    if isinstance(payload, (list, tuple)) and len(payload) >= 2:
                        text, score = str(payload[0]), float(payload[1])
                    else:
                        text, score = str(payload), 1.0
                    out.append(OcrLine(text=text, score=score, box=box))
                except Exception:
                    continue
        return out


# ---------------------------------------------------------------- RapidOCR

class RapidOcrEngine(BaseOcrEngine):
    """RapidOCR —— PaddleOCR 的 PP-OCR 模型转 ONNX，纯离线、安装体积小得多。

    注意：rapidocr 3.x 自身只是框架，ONNX 后端由独立包提供
    （`rapidocr-onnxruntime`）。该包声明 Python <3.13，所以在 3.13 上
    ONNX 后端不可用；此时可安装 `rapidocr-openvino` 作为替代后端。
    """

    name = "rapid"

    def __init__(self, **_ignored: Any):
        self._engine = None
        self._flavor = ""
        self.display_name = "RapidOCR"
        self.error = ""
        self._init()

    def _init(self) -> None:
        # 新版包名 rapidocr（>=2.0）
        try:
            from rapidocr import RapidOCR  # type: ignore

            self._engine = RapidOCR()
            self._flavor = "new"
            self.display_name = "RapidOCR (ONNX)"
            log.info("OCR 引擎就绪: %s", self.display_name)
            return
        except Exception as exc:
            self.error = str(exc)
            log.debug("rapidocr 不可用: %s", exc)

        # 旧版包名 rapidocr_onnxruntime（1.x）
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore

            self._engine = RapidOCR()
            self._flavor = "old"
            self.display_name = "RapidOCR-ONNXRuntime"
            log.info("OCR 引擎就绪: %s", self.display_name)
            return
        except Exception as exc:
            self.error = str(exc)

        hint = ""
        if "rapidocr_onnxruntime" in self.error:
            hint = ("；该后端不支持当前 Python 版本（需 <3.13），"
                    "可改用 pip install rapidocr-openvino，或使用 PaddleOCR")
        log.warning("RapidOCR 不可用: %s%s", self.error, hint)

    def available(self) -> bool:
        return self._engine is not None

    def warmup(self) -> bool:
        if self._engine is None:
            return False
        dummy = np.full((48, 260, 3), 255, dtype=np.uint8)
        return self.recognize(dummy).ok

    def recognize(self, img_bgr: np.ndarray) -> OcrResult:
        if self._engine is None:
            return OcrResult(engine=self.name, ok=False, error="RapidOCR 未初始化")

        t0 = time.perf_counter()
        try:
            raw = self._engine(img_bgr)
        except Exception as exc:
            return OcrResult(engine=self.name, ok=False,
                             elapsed=time.perf_counter() - t0,
                             error=f"{type(exc).__name__}: {exc}")

        lines = self._parse(raw)
        return OcrResult(lines=_dedup_lines(lines), engine=self.display_name,
                         elapsed=time.perf_counter() - t0, ok=True)

    def _parse(self, raw: Any) -> List[OcrLine]:
        out: List[OcrLine] = []

        # 新版：RapidOCROutput 对象，带 .boxes/.txts/.scores
        txts = getattr(raw, "txts", None)
        if txts is not None:
            scores = getattr(raw, "scores", None) or []
            boxes = getattr(raw, "boxes", None)
            for i, t in enumerate(txts):
                out.append(OcrLine(
                    text=str(t),
                    score=float(scores[i]) if i < len(scores) else 1.0,
                    box=_poly_to_box(boxes[i]) if boxes is not None and i < len(boxes) else None,
                ))
            return out

        # 旧版： (result, elapse)，result = [[box, text, score], ...]
        result = raw[0] if isinstance(raw, tuple) and raw else raw
        if not result:
            return []
        for entry in result:
            try:
                if isinstance(entry, (list, tuple)) and len(entry) >= 3:
                    out.append(OcrLine(text=str(entry[1]), score=float(entry[2]),
                                       box=_poly_to_box(entry[0])))
                elif isinstance(entry, (list, tuple)) and len(entry) == 2:
                    out.append(OcrLine(text=str(entry[0]), score=float(entry[1])))
            except Exception:
                continue
        return out


# ---------------------------------------------------------------- Null

class NullOcrEngine(BaseOcrEngine):
    name = "none"
    display_name = "未启用"

    def recognize(self, img_bgr: np.ndarray) -> OcrResult:
        return OcrResult(engine=self.name, ok=False, error="OCR 引擎不可用")


# ---------------------------------------------------------------- 工厂

_ALIASES = {
    "": "auto", "auto": "auto", "paddle": "paddle", "paddleocr": "paddle",
    "rapid": "rapid", "rapidocr": "rapid", "onnx": "rapid",
    "none": "none", "off": "none",
}

ENGINE_LABELS = {
    "auto": "自动选择（优先 PaddleOCR）",
    "paddle": "PaddleOCR",
    "rapid": "RapidOCR（ONNX，轻量）",
    "none": "关闭 OCR",
}


def detect_available() -> List[str]:
    """探测本机可用的引擎名（不含 auto/none）。

    用 find_spec 而不是 import —— 主进程（UI）里绝不能真的把
    PaddleOCR 拉进来，否则模型加载会拖慢界面并破坏进程隔离。
    """
    import importlib.util

    found: List[str] = []

    def _has(mod: str) -> bool:
        try:
            return importlib.util.find_spec(mod) is not None
        except Exception:
            return False

    if _has("paddleocr") and _has("paddle"):
        found.append("paddle")
    elif _has("paddleocr"):
        found.append("paddle")
    if _has("rapidocr") or _has("rapidocr_onnxruntime"):
        found.append("rapid")
    return found


def create_engine(prefer: str = "auto", lang: str = "ch", use_gpu: bool = False,
                  strict: bool = False, enable_mkldnn: Optional[bool] = False,
                  probe: bool = True, cpu_threads: int = 0) -> BaseOcrEngine:
    """按偏好创建引擎，失败自动降级。

    与「只看初始化是否成功」不同，这里默认会跑一次预热推理（probe=True）。
    PaddleOCR 的 oneDNN 不兼容只在推理阶段暴露，必须真跑一次才能确认可用，
    否则会出现「引擎初始化成功但每次识别都报错」的假可用状态。

    strict=True 时只创建指定引擎，不降级（用于自检工具）。
    """
    prefer = _ALIASES.get((prefer or "auto").strip().lower(), "auto")
    if prefer == "none":
        return NullOcrEngine()

    order: Sequence[str]
    if prefer == "auto":
        order = ("paddle", "rapid")
    elif strict:
        order = (prefer,)
    else:
        order = (prefer, "paddle" if prefer != "paddle" else "rapid")

    last_error = ""
    for name in order:
        try:
            if name == "paddle":
                engine: BaseOcrEngine = PaddleOcrEngine(
                    lang=lang, use_gpu=use_gpu, enable_mkldnn=enable_mkldnn,
                    cpu_threads=cpu_threads)
            else:
                engine = RapidOcrEngine()
        except Exception as exc:
            last_error = f"{name}: {exc}"
            log.warning("创建 OCR 引擎 %s 失败: %s", name, exc)
            continue

        if not engine.available():
            last_error = f"{name} 初始化后不可用"
            continue

        if probe:
            try:
                if not engine.warmup():
                    last_error = f"{name} 预热推理失败"
                    log.warning("OCR 引擎 %s 预热失败，尝试下一个", name)
                    continue
            except Exception as exc:
                last_error = f"{name} 预热异常: {exc}"
                log.warning("OCR 引擎 %s 预热异常: %s", name, exc)
                continue

        return engine

    log.error("没有可用的 OCR 引擎（%s）。程序将以「无 OCR」模式运行。", last_error)
    return NullOcrEngine()


def looks_like_answer_text(text: str) -> bool:
    """辅助判断：OCR 文本里是否含「答案/解析」等标记。"""
    return bool(parse_answer_line(text)) or "解析" in text or "分析" in text
