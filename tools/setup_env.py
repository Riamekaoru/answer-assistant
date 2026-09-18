# -*- coding: utf-8 -*-
"""离线安装：把随包预装的虚拟环境接到本目录下的 Python 运行时上。

调用方式（注意用的是 runtime 里的 python，不是 .venv 里的）：

    runtime\\python\\python.exe tools\\setup_env.py

为什么必须这么绕：虚拟环境的 pyvenv.cfg 把基解释器路径**绝对写死**了，
安装包换机器、换目录之后那些路径就是错的，.venv\\Scripts\\python.exe 起不来，
也就没法用 .venv 自己的 python 来修自己。而 runtime 里的解释器是按自身位置
定位的，跟 .venv 无关，所以只能用它来修。

做四件事：
    1. 重写 .venv/pyvenv.cfg，指向本目录的 runtime/python
    2. 补齐可写目录 data / logs / assets
    3. 校验 tkinter（GUI 必需）与虚拟环境可启动
    4. 汇报依赖与模型是否齐备

刻意不做的事：
    - 不联网、不安装任何包（依赖已随包预装，装完即可用）
    - 不往 %USERPROFILE% 拷模型（模型留在包内，由 core.config.ensure_bundled_models()
      在导入 paddle 之前把 PADDLE_PDX_CACHE_HOME 指过来）
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "runtime" / "python"
VENV = ROOT / ".venv"
CFG = VENV / "pyvenv.cfg"
VPY = VENV / "Scripts" / "python.exe"


def log(msg: str) -> None:
    print("      " + msg, flush=True)


def fail(msg: str) -> int:
    print()
    print("  [ERROR] " + msg)
    print()
    return 1


# ---------------------------------------------------------------- 1. pyvenv.cfg

def write_pyvenv_cfg() -> int:
    py = RUNTIME / "python.exe"
    if not py.exists():
        return fail("找不到附带运行时：%s" % py)
    if not VENV.is_dir():
        return fail("找不到虚拟环境目录：%s" % VENV)

    text = (
        "home = %s\n"
        "include-system-site-packages = false\n"
        "version = %d.%d.%d\n"
        "executable = %s\n"
        "command = %s -m venv %s\n"
    ) % (RUNTIME, sys.version_info.major, sys.version_info.minor,
         sys.version_info.micro, py, py, VENV)

    # CPython 启动阶段是按本地代码页解析 pyvenv.cfg 的，路径含中文时必须
    # 跟着用 mbcs 写，否则 home 会被解成乱码、虚拟环境起不来。
    data = None
    try:
        data = text.encode("mbcs")
    except (LookupError, UnicodeEncodeError):
        data = text.encode("utf-8")
    CFG.write_bytes(data)
    log("已重写 %s" % CFG.relative_to(ROOT))
    log("  home = %s" % RUNTIME)
    ascii_ok = all(ord(c) < 128 for c in str(ROOT))
    if not ascii_ok:
        log("  [提示] 安装路径含非英文字符，若启动失败请改到纯英文路径")
    return 0


# ---------------------------------------------------------------- 2. 可写目录

def make_dirs() -> None:
    for name in ("data", "logs", "assets"):
        d = ROOT / name
        d.mkdir(parents=True, exist_ok=True)
    log("已确认可写目录：data / logs / assets")


# ---------------------------------------------------------------- 3. 校验

def check_tkinter() -> int:
    r = subprocess.run([str(RUNTIME / "python.exe"), "-c", "import tkinter"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return fail("附带运行时缺少 tkinter，GUI 无法启动。\n"
                    "        请确认 runtime 目录完整解压（不要只解出一部分）。")
    log("tkinter 可用")
    return 0


def check_venv() -> int:
    if not VPY.exists():
        return fail("找不到虚拟环境解释器：%s" % VPY)
    r = subprocess.run(
        [str(VPY), "-c",
         "import sys;print(sys.version.split()[0]);"
         "import numpy,cv2,openpyxl,docx,mss,keyboard,rapidfuzz;print('deps ok')"],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    if r.returncode != 0:
        print(r.stdout[-1000:])
        print(r.stderr[-1500:])
        return fail("虚拟环境无法启动或核心依赖缺失（见上方输出）。")
    for line in (r.stdout or "").strip().splitlines():
        log(line)
    return 0


def check_models() -> None:
    md = ROOT / "models" / "paddlex" / "official_models"
    if not md.is_dir():
        log("[提示] 未随包附带 OCR 模型；首次识别时会尝试联网下载")
        return
    names = sorted(p.name for p in md.iterdir() if p.is_dir())
    size = sum(f.stat().st_size for f in md.rglob("*") if f.is_file())
    log("已附带 OCR 模型 %d 个（%.0f MB）：%s"
        % (len(names), size / 1048576.0, ", ".join(names)))
    log("识别时由 core.config.ensure_bundled_models() 指向包内目录，无需联网")


# ---------------------------------------------------------------- main

def main() -> int:
    print()
    print("  安装路径：%s" % ROOT)
    print("  运行时  ：Python %d.%d.%d" % (sys.version_info[:3]))
    print()

    print("  [1/4] 重写虚拟环境配置 ...")
    rc = write_pyvenv_cfg()
    if rc:
        return rc

    print("  [2/4] 准备可写目录 ...")
    make_dirs()

    print("  [3/4] 校验运行时与虚拟环境 ...")
    rc = check_tkinter()
    if rc:
        return rc
    rc = check_venv()
    if rc:
        return rc

    print("  [4/4] 检查 OCR 模型 ...")
    check_models()

    print()
    print("  安装完成。用 run.bat 启动（无控制台窗口），")
    print("  或用 run_debug.bat 启动（带日志窗口，排错用）。")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
