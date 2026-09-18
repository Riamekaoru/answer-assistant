# -*- coding: utf-8 -*-
"""打包：把「程序代码 + 便携 Python + 预装依赖 + OCR 模型」打成一个可离线分发的 zip。

用法（项目根目录下执行）：

    .venv\\Scripts\\python.exe tools\\pack.py --report          看看会打进去什么、多大
    .venv\\Scripts\\python.exe tools\\pack.py                   打包到 dist_package\\

产物：
    dist_package\\AnswerAssistant-<版本>-win64.zip
    解压后目录结构：
        AnswerAssistant-<版本>-win64\\
            install.bat          离线一键安装（重写环境路径 + 自检）
            run.bat              正常启动
            run_debug.bat        带日志窗口启动
            安装说明.md           适用范围 / 使用前提 / 排错
            main.py core\\ ui\\ workers\\ tools\\
            runtime\\python\\     便携 Python（含 tkinter）
            .venv\\               预装全部依赖的虚拟环境
            models\\paddlex\\     OCR 模型（离线识别）

设计要点：
    - 直接流式写入 zip，不落一份 750MB 的暂存副本
    - 白名单式收集，开发中间产物（build/dist/logs/data/__pycache__）不会进包
    - 字节码缓存（*.pyc）不打进去：能省下压缩后约 50MB，代价是首次启动要重建缓存
    - 打完包必须做一次「换目录解压 + 安装 + 自检」，验证可迁移性。
      更完整的验收流程与三个 qa 脚本见 packaging\README-打包流程.md
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

VERSION = "1.0.0"
PKG_NAME = "AnswerAssistant-%s-win64" % VERSION

# 打包时间只取一次：zip 内的「安装说明.md」和 zip 旁边的渲染稿是同一份文档，
# 各取一次 strftime 会让两边差出一分钟（曾经差 2 分钟），看起来像两份东西。
BUILD_TIME = time.strftime("%Y-%m-%d %H:%M")

# 随包的具体文件（源路径 -> 包内相对路径）
FILES = [
    ("main.py", "main.py"),
    ("README.md", "README.md"),
    ("requirements.txt", "requirements.txt"),
    ("requirements-ocr.txt", "requirements-ocr.txt"),
    ("run.bat", "run.bat"),
    ("run_debug.bat", "run_debug.bat"),
    ("tools/setup_env.py", "tools/setup_env.py"),
    ("packaging/install.bat", "install.bat"),
    ("packaging/安装说明.md", "安装说明.md"),
]

# 随包的整棵目录
DIRS = ["core", "ui", "workers", "tools"]

# 整棵带上的运行时大树：(包内路径, 会不会进包由 --no-runtime 之类开关控制)
TREES = ["runtime/python", ".venv"]

# 模型：源目录默认在用户目录，包内固定放 models/paddlex/official_models/...
MODELS_ARC = "models/paddlex"

EXCLUDE_DIR_NAMES = {"__pycache__", ".wheels_tmp", ".git", ".pytest_cache",
                     ".mypy_cache", ".idea", ".vscode"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log"}

# 只对开发机有意义的脚本，不进包：
#   pack.py / slim.py 依赖 packaging\ 目录，而目标机上的包只保留 install.bat
#   和安装说明，把它们带进去只会让人误跑出一个残缺的包。
EXCLUDE_REL = {"tools/pack.py", "tools/slim.py"}

# 这几个文件在包内要用真实信息替换占位符
TEMPLATED = {"安装说明.md"}


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%d B" % n if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f GB" % n


def iter_tree(src: Path, arc_base: str, src_rel: str = ""):
    """遍历目录，产出 (文件路径, 包内路径)，自动跳过缓存与中间产物。

    src_rel 是该目录在项目根下的相对路径（如 "tools"），
    用于和 EXCLUDE_REL 里的"项目根相对路径"对齐。
    """
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIR_NAMES]
        rel_dir = os.path.relpath(dirpath, src)
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in EXCLUDE_SUFFIXES:
                continue
            if any(fnmatch.fnmatch(fn, p) for p in ("*.pdb", "*.lib", "*.exp", "*.pyi")):
                continue
            p = Path(dirpath) / fn
            rel = fn if rel_dir == "." else os.path.join(rel_dir, fn)
            full_rel = ("%s/%s" % (src_rel, rel) if src_rel else rel).replace("\\", "/")
            if full_rel in EXCLUDE_REL:
                continue
            yield p, "%s/%s" % (arc_base, rel.replace("\\", "/"))


def collect(models_src: Path | None):
    """汇总所有要打包的条目，返回 [(类型, 源路径, 包内路径)]。

    按包内路径去重：FILES 里显式列出的文件（如 tools/setup_env.py）
    同时也在 DIRS 的整目录扫描范围内，不去重会写进 zip 两遍。
    """
    items = []
    seen = set()

    def add(kind: str, src: Path, arc: str) -> None:
        if arc in seen:
            return
        seen.add(arc)
        items.append((kind, src, arc))

    for src_rel, arc_rel in FILES:
        p = ROOT / src_rel
        if not p.exists():
            print("  [警告] 缺少 %s，跳过" % src_rel)
            continue
        add("file", p, "%s/%s" % (PKG_NAME, arc_rel))

    for d in DIRS:
        p = ROOT / d
        if not p.is_dir():
            print("  [警告] 缺少目录 %s，跳过" % d)
            continue
        for f, arc in iter_tree(p, "%s/%s" % (PKG_NAME, d), d):
            add("file", f, arc)

    for t in TREES:
        p = ROOT / t
        if not p.is_dir():
            print("  [警告] 缺少运行时目录 %s，跳过" % t)
            continue
        for f, arc in iter_tree(p, "%s/%s" % (PKG_NAME, t), t):
            add("file", f, arc)

    if models_src and models_src.is_dir():
        for f, arc in iter_tree(models_src, "%s/%s" % (PKG_NAME, MODELS_ARC)):
            add("file", f, arc)
    elif models_src:
        print("  [警告] 模型目录不存在：%s" % models_src)

    return items


def read_templated(src: Path) -> bytes:
    text = src.read_text(encoding="utf-8")
    text = (text.replace("__PKG_NAME__", PKG_NAME)
                .replace("__VERSION__", VERSION)
                .replace("__BUILD_TIME__", BUILD_TIME))
    return text.encode("utf-8")


def report(items) -> None:
    total = 0
    per_top = {}
    for kind, src, arc in items:
        try:
            size = src.stat().st_size
        except OSError:
            size = 0
        total += size
        top = arc.split("/")[1] if arc.count("/") > 1 else "(根)"
        per_top[top] = per_top.get(top, 0) + size
    print()
    print("  将打包 %d 个文件，未压缩合计 %s" % (len(items), human(total)))
    print("  " + "-" * 56)
    for name, size in sorted(per_top.items(), key=lambda kv: kv[1], reverse=True):
        print("    %10s  %s" % (human(size), name))
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="答题助手打包工具")
    ap.add_argument("--out", default=str(ROOT / "dist_package"), help="输出目录")
    ap.add_argument("--models-src", default=os.path.join(
        os.path.expanduser("~"), ".paddlex"), help="OCR 模型源目录")
    ap.add_argument("--no-models", action="store_true", help="不带 OCR 模型")
    ap.add_argument("--report", action="store_true", help="只报告不打包")
    args = ap.parse_args()

    models_src = None if args.no_models else Path(args.models_src)
    items = collect(models_src)

    print("=" * 62)
    print("  打包 %s" % PKG_NAME)
    print("=" * 62)
    report(items)
    if args.report:
        return 0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / ("%s.zip" % PKG_NAME)

    templated_arcs = {"%s/%s" % (PKG_NAME, k) for k in TEMPLATED}

    t0 = time.time()
    done = 0
    raw = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for kind, src, arc in items:
            try:
                if arc in templated_arcs:
                    data = read_templated(src)
                    zf.writestr(arc, data)
                    raw += len(data)
                else:
                    zf.write(src, arc)
                    raw += src.stat().st_size
            except OSError as exc:
                print("   跳过 %s：%s" % (src, exc))
                continue
            done += 1
            if done % 500 == 0:
                print("   已写入 %5d 个文件 ... %s" % (done, human(zip_path.stat().st_size)),
                      flush=True)

    size = zip_path.stat().st_size
    print()
    print("  完成：%s" % zip_path)
    print("   文件数：%d" % done)
    print("   原始  ：%s" % human(raw))
    print("   压缩后：%s（压缩率 %.0f%%）" % (human(size), size * 100.0 / max(raw, 1)))
    print("   耗时  ：%.1f 秒" % (time.time() - t0))

    # 顺手把渲染后的安装说明放到 zip 旁边，方便不打开压缩包直接看
    doc_src = ROOT / "packaging" / "安装说明.md"
    if doc_src.exists():
        doc_out = out_dir / ("安装说明-%s.md" % PKG_NAME)
        doc_out.write_bytes(read_templated(doc_src))
        print("   说明  ：%s" % doc_out)

    print()
    print("  下一步：换一个目录解压，双击 install.bat 验证可迁移性。")
    print("          完整验收流程见 packaging\\README-打包流程.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
