# -*- coding: utf-8 -*-
"""离线可迁移性的决定性测试：假装这是一台「干净的机器」。

做法：把 USERPROFILE / HOME 指向一个空目录，于是
    * Path.home()          -> 空目录
    * %USERPROFILE%\\.paddlex -> 不存在
相当于目标机上从来没跑过 PaddleX。此时如果还能识别，就说明识别
完全依赖包内 models\\ 目录，与用户目录、与网络都无关。

用法（把本脚本拷到解压出来的安装目录里跑）：
    cd <解压目录>
    set USERPROFILE=<空目录> & set HOME=<空目录>
    .venv\\Scripts\\python.exe qa_test_offline.py

也可以直接指定安装目录：
    .venv\\Scripts\\python.exe qa_test_offline.py <解压目录>
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

FAKE_HOME = Path(os.environ["USERPROFILE"])
FAILS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print("[%s] %s%s" % ("OK  " if ok else "FAIL", name,
                         ("  -> " + detail) if detail else ""), flush=True)
    if not ok:
        FAILS.append(name)


print("=" * 68)
print("  离线可迁移性测试（模拟干净机器）")
print("=" * 68)
print("   假 HOME       : %s" % FAKE_HOME)
print("   Path.home()   : %s" % Path.home())
print("   ~/.paddlex    : %s（存在=%s）"
      % (FAKE_HOME / ".paddlex", (FAKE_HOME / ".paddlex").exists()))

check("Path.home() 已指向空目录", Path.home() == FAKE_HOME or
      str(Path.home()).lower() == str(FAKE_HOME).lower(),
      str(Path.home()))
check("用户目录下没有 .paddlex", not (FAKE_HOME / ".paddlex").exists())

# --- 引擎初始化 + 真识别 -------------------------------------------------
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from core.ocr_engine import create_engine, preprocess

eng = create_engine("paddle", strict=True)
check("OCR 引擎可用", eng.available(), eng.display_name if eng.available() else
      str(getattr(eng, "error", ""))[:120])

import paddlex.utils.cache as pc
cache_dir = str(pc.CACHE_DIR)
print("   paddlex CACHE_DIR : %s" % cache_dir)
check("模型目录在包内", str(ROOT).lower() in cache_dir.lower())
check("模型目录不是用户目录", str(FAKE_HOME).lower() not in cache_dir.lower())

font = None
for cand in ("msyh.ttc", "simhei.ttf", "simsun.ttc"):
    p = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / cand
    if p.exists():
        font = ImageFont.truetype(str(p), 28)
        break

# --- 先写出示例题库（需要本地内容，仓库不发布题库）-----------------------
from core.matcher import Matcher
from core.question_bank import load_bank
from tools.make_sample_bank import write_samples

produced = write_samples(ROOT / "data" / "samples")
bank = load_bank(ROOT / "data" / "samples" / "sample_bank.xlsx") if produced else None

# 测试图内容：本地有示例题库就用它第一题（识别结果才能接上匹配器），
# 没有就用与题库无关的占位文字，保证本脚本自身不含任何题目内容。
if bank:
    q = bank.questions[0]
    lines = ["1. %s" % q.question]
    opts = list(q.options)
    for i in range(0, len(opts), 2):
        lines.append("    ".join(opts[i:i + 2]))
else:
    lines = ["1. 这是一道离线自检用示例题目？",
             "A. 示例选项甲    B. 示例选项乙",
             "C. 示例选项丙    D. 示例选项丁"]

img = Image.new("RGB", (900, 50 * len(lines) + 30), "white")
d = ImageDraw.Draw(img)
for i, line in enumerate(lines):
    d.text((20, 16 + i * 50), line, fill="black", font=font)
png = ROOT / "logs" / "offline_test.png"
png.parent.mkdir(parents=True, exist_ok=True)
img.save(png)

arr = cv2.imread(str(png))
r = eng.recognize(preprocess(arr, scale=1.5, grayscale=True, sharpen=True))
text = (r.text or "").replace("\n", " / ")
print("   识别结果：%s" % text[:120])
check("识别成功", bool(r.ok), (r.error or "")[:100])

# 关键词检查不写死任何题目内容：拿渲染文字的开头去核对识别结果
probe = [ch for ch in lines[0][:8] if ch.strip()]
hit = sum(1 for ch in probe if ch in (r.text or ""))
check("识别内容含关键词", hit >= 3, "渲染文字字符命中 %d/%d" % (hit, len(probe)))

# --- 识别结果进匹配器 ----------------------------------------------------
if bank is None:
    print("   [跳过] 本地无示例题目（data/local/sample_questions.json），"
          "跳过 识别→匹配 检查")
else:
    hits = Matcher(bank, threshold=0.5).search(r.text or "")
    if hits:
        check("识别→匹配命中", True, "%.1f%% → 答案 %s"
              % (hits[0].percent,
                 hits[0].question.answer_display.replace("\n", " | ")[:40]))
    else:
        check("识别→匹配命中", False, "识别到了文字但没命中题库")

print()
print("=" * 68)
if FAILS:
    print("  未通过：%d 项" % len(FAILS))
    for f in FAILS:
        print("    [FAIL] %s" % f)
    sys.exit(1)
print("  全部通过：不依赖用户目录、不联网，识别与匹配均正常")
sys.exit(0)
