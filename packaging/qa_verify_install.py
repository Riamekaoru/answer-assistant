# -*- coding: utf-8 -*-
"""在解压出来的安装目录里做端到端验证。

验证目标（对应「安装包可直接分发、开箱即用」这一承诺）：
    1. 解释器与虚拟环境指向本安装目录（不依赖开发机上的原路径）
    2. 核心依赖齐全
    3. OCR 模型走包内目录（不联网）
    4. 题库能生成/解析、匹配器可用
    5. 真·全链路：渲染题目图片 -> OCR 识别 -> 模糊匹配 -> 命中正确题目

用法（用解压目录里自带的 python 跑）：
    <解压目录>\\.venv\\Scripts\\python.exe qa_verify_install.py <解压目录>
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

os.environ.setdefault("OMP_NUM_THREADS", "4")
os.environ.setdefault("MKL_NUM_THREADS", "4")

# 关键：必须在 import paddle/paddlex 之前设置模型目录，
# 否则 paddlex 会把 CACHE_DIR 固化成用户目录，后面再设也没用。
try:
    from core.config import ensure_bundled_models as _ebm
    _ebm()
except Exception as _exc:
    print("   [提示] 预置模型目录失败：%s" % _exc)

FAILS = []
WARNS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print("[%s] %s%s" % ("OK  " if ok else "FAIL", name,
                         ("  -> " + detail) if detail else ""), flush=True)
    if not ok:
        FAILS.append(name)


def warn(name: str, detail: str = "") -> None:
    print("[WARN] %s%s" % (name, ("  -> " + detail) if detail else ""), flush=True)
    WARNS.append(name)


print("=" * 68)
print("  安装验证：%s" % ROOT)
print("=" * 68)
print("   本机 Python：%s" % sys.version.split()[0])

LROOT = str(ROOT).lower()

# ---------------------------------------------------------------- 0. 结构
print()
print("-- 0. 包内结构完整性 --")
for rel in ("main.py", "run.bat", "run_debug.bat", "install.bat",
            "runtime/python/python.exe", ".venv/Scripts/python.exe",
            "core", "ui", "workers", "tools"):
    check("存在 %s" % rel, (ROOT / rel).exists())
for junk in ("_backup", "dist_package", ".wheels_tmp", "build", "dist", "__pycache__"):
    check("无开发残留 %s" % junk, not (ROOT / junk).exists())

# ---------------------------------------------------------------- 1. 解释器
print()
print("-- 1. 解释器与虚拟环境 --")
print("   sys.prefix      : %s" % sys.prefix)
print("   sys.base_prefix : %s" % sys.base_prefix)
print("   sys.executable  : %s" % sys.executable)
check("基解释器指向本安装目录", LROOT in sys.base_prefix.lower(),
      sys.base_prefix)
check("虚拟环境指向本安装目录", LROOT in sys.prefix.lower(), sys.prefix)
check("解释器即包内 runtime", LROOT in sys.base_prefix.lower()
      and (ROOT / "runtime" / "python" / "python.exe").exists())

# ---------------------------------------------------------------- 2. 依赖
print()
print("-- 2. 核心依赖 --")
for mod, label in (("tkinter", "GUI(tkinter)"), ("numpy", "numpy"), ("cv2", "OpenCV"),
                   ("openpyxl", "Excel 题库"), ("docx", "Word 题库"),
                   ("olefile", "Word 97 (.doc) 题库"),
                   ("mss", "屏幕抓取"), ("keyboard", "全局热键"),
                   ("rapidfuzz", "模糊匹配"), ("PIL", "图像处理"),
                   ("paddle", "PaddlePaddle"), ("paddlex", "PaddleX")):
    try:
        m = __import__(mod)
        v = getattr(m, "__version__", "")
        check(label, True, v)
    except Exception as exc:
        check(label, False, "%s: %s" % (type(exc).__name__, exc))

# ---------------------------------------------------------------- 3. 模型
print()
print("-- 3. OCR 模型来源 --")
hub = ""
try:
    from core.config import bundled_models_dir, ensure_bundled_models
    bm = bundled_models_dir()
    print("   bundled_models_dir() : %s" % bm)
    check("包内模型目录存在", bool(bm) and Path(bm).is_dir())
    ensure_bundled_models()
    hub = os.environ.get("PADDLE_PDX_CACHE_HOME", "")
    print("   PADDLE_PDX_CACHE_HOME: %s" % (hub or "(未设置)"))
    check("模型缓存指向包内目录", bool(hub) and LROOT in hub.lower())
    om = Path(hub) / "official_models" if hub else None
    ok = bool(om and om.is_dir())
    check("official_models 存在", ok,
          ", ".join(sorted(p.name for p in om.iterdir())) if ok else "")
except Exception as exc:
    check("模型路径配置", False, "%s: %s" % (type(exc).__name__, exc))

# ---------------------------------------------------------------- 4. 题库
print()
print("-- 4. 题库生成 / 解析 / 匹配 --")
bank = None
target = None
try:
    from tools.make_sample_bank import write_samples
    from core.question_bank import load_bank
    from core.matcher import Matcher, build_queries

    samples_dir = ROOT / "data" / "samples"
    files = write_samples(samples_dir)
    print("   生成示例题库 %d 个：%s" % (len(files), ", ".join(f.suffix for f in files)))
    check("示例题库生成", len(files) >= 4)

    xlsx = samples_dir / "sample_bank.xlsx"
    bank = load_bank(xlsx)
    check("Excel 题库导入", len(bank) > 0, "%d 题" % len(bank))

    # 与其它格式交叉核对，确认解析口径一致
    counts = {}
    for f in files:
        counts[f.suffix] = len(load_bank(f))
    print("   各格式题目数：%s" % ", ".join("%s=%d" % (k, v) for k, v in sorted(counts.items())))
    check("各格式题目数一致", len(set(counts.values())) == 1, str(counts))

    # 选项完整性：不能退化成只有字母
    bad_opt = [q.question[:18] for q in bank.questions
               if q.options and max(len(o.split(".", 1)[-1].strip()) for o in q.options) <= 1]
    check("选项内容完整", not bad_opt, "异常题：%s" % bad_opt[:3])

    m = Matcher(bank)
    q0 = bank.questions[0]
    query = q0.question + " " + " ".join(q0.options)
    print("   查询块：%s" % " | ".join(build_queries(query))[:100])
    hits = m.search(query)
    check("匹配器命中", bool(hits), "命中 %d 条" % len(hits))
    if hits:
        check("命中题目正确", hits[0].question.question == q0.question,
              "相似度 %.1f%%" % hits[0].percent)

    # 挑一道适合渲染成图片的题（题干短、四个选项）
    for q in bank.questions:
        if len(q.options) == 4 and 8 <= len(q.question) <= 34:
            target = q
            break
    target = target or q0
except Exception as exc:
    check("题库解析与匹配", False, "%s: %s" % (type(exc).__name__, exc))

# ---------------------------------------------------------------- 5. 全链路
print()
print("-- 5. 全链路：渲染题目图 -> OCR -> 匹配 --")
try:
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    from core.ocr_engine import create_engine, preprocess

    font = None
    for cand in ("msyh.ttc", "simhei.ttf", "simsun.ttc", "msyhbd.ttc"):
        p = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / cand
        if p.exists():
            try:
                font = ImageFont.truetype(str(p), 28)
                break
            except Exception:
                continue
    check("找到中文字体", font is not None)

    lines = ["1. %s" % target.question]
    opts = target.options
    lines.append("%s    %s" % (opts[0], opts[1]) if len(opts) > 1 else opts[0])
    if len(opts) > 2:
        lines.append("%s    %s" % (opts[2], opts[3]) if len(opts) > 3 else opts[2])

    img = Image.new("RGB", (900, 50 * len(lines) + 30), "white")
    d = ImageDraw.Draw(img)
    y = 16
    for line in lines:
        d.text((20, y), line, fill="black", font=font)
        y += 50
    png = ROOT / "logs" / "verify_input.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    img.save(png)
    print("   测试图：%d 行 %s" % (len(lines), png))

    arr = cv2.imread(str(png))
    eng = create_engine("paddle", strict=True)
    if eng.available():
        check("OCR 引擎初始化", True, eng.display_name)
    else:
        check("OCR 引擎初始化", False, str(getattr(eng, "error", ""))[:140])
        raise SystemExit

    r = eng.recognize(preprocess(arr, scale=1.5, grayscale=True, sharpen=True))
    text = r.text or ""
    print("   识别文本：%s" % text.replace("\n", " / ")[:150])
    check("OCR 识别成功", bool(r.ok), (r.error or "")[:100])
    # 不写死任何题目内容：直接拿渲染用的题干与识别结果比对字符命中
    common = sum(1 for ch in set(target.question) if ch in text)
    check("识别内容含题干关键词", common >= 3, "题干字符命中 %d" % common)

    # 运行期真实使用的模型目录
    try:
        import paddlex.utils.cache as pc
        cache_dir = str(getattr(pc, "CACHE_DIR", ""))
        print("   paddlex CACHE_DIR    : %s" % cache_dir)
        check("运行期模型目录在包内", LROOT in cache_dir.lower())
    except Exception as exc:
        check("运行期模型目录在包内", False, str(exc))

    # 用 OCR 出来的文本去匹配，看能不能还原题目本身
    from core.matcher import Matcher
    m2 = Matcher(bank)
    hits = m2.search(text)
    if hits:
        got = hits[0].question.question
        print("   匹配结果：%s（%.1f%%）" % (got[:44], hits[0].percent))
        check("OCR -> 匹配命中目标题", got == target.question, "目标：%s" % target.question[:34])
        print("   答案：%s" % hits[0].question.answer_display.replace("\n", " | ")[:100])
    else:
        check("OCR -> 匹配命中目标题", False, "无命中，OCR 文本可能过短")
        print("   提示：以下为最接近的候选")
        for qtext, score in m2.score_text(text)[:3]:
            print("        %.3f  %s" % (score, qtext[:50]))
except SystemExit:
    pass
except Exception as exc:
    check("OCR 全链路", False, "%s: %s" % (type(exc).__name__, exc))

# ---------------------------------------------------------------- 结论
print()
print("=" * 68)
if FAILS:
    print("  验证未通过：失败 %d 项，告警 %d 项" % (len(FAILS), len(WARNS)))
    for f in FAILS:
        print("    [FAIL] %s" % f)
    sys.exit(1)
print("  验证全部通过（告警 %d 项）" % len(WARNS))
for w in WARNS:
    print("    [WARN] %s" % w)
sys.exit(0)
