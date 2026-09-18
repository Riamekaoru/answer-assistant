# -*- coding: utf-8 -*-
"""答题助手 · 瘦身工具。

清理运行时用不到的冗余文件，减小分发体积。所有清理项都是「可再生成」或
「只在构建期需要」的东西，删掉不影响程序运行。

用法（项目根目录下执行）：
    .venv\\Scripts\\python.exe tools\\slim.py --report        只报告体积构成，不删
    .venv\\Scripts\\python.exe tools\\slim.py --dry-run       预览将删除的内容
    .venv\\Scripts\\python.exe tools\\slim.py                 执行清理
    .venv\\Scripts\\python.exe tools\\slim.py --deps 包名 …    额外卸载指定依赖

清理项：
    1. __pycache__ / *.pyc      字节码缓存，首次运行会自动重建（注意：跑一次程序就会重新生成）
    2. *.pdb                    调试符号，运行完全不需要（单项最大，约 85MB）
    3. *.lib / *.exp            静态导入库，只在编译链接时需要
    4. *.pyi                    类型存根，只在静态检查时需要
    5. paddle/include           只在编译自定义算子时需要
    6. build/ dist/ .wheels_tmp/ 构建中间产物与依赖 wheel 暂存目录
    7. logs/*.log logs/*.png    运行日志与自检截图
    8. runtime 里的 idlelib / turtledemo / Tix / Tk demos 等从不加载的模块
    9. cv2 的 FFmpeg 视频编解码 DLL（约 29MB，本程序只截屏不处理视频）

不要在 `import paddle` 之前删除 paddle/utils/cpp_extension，原因见下方常量注释。
建议在打包前最后一步执行本工具，否则跑过程序之后 __pycache__ 又会回来。

实现上刻意只做「一次目录遍历」，否则 3 万多个文件的项目会慢到不可用。
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 相对项目根目录、整目录删除的项
DIR_TARGETS = [
    "build",
    "dist",
    ".wheels_tmp",                       # 打包/装依赖时下载的 wheel 暂存目录（构建期产物）
    "runtime/python/Lib/idlelib",        # IDLE 编辑器，本程序不用
    "runtime/python/Lib/turtledemo",     # 海龟绘图示例
    "runtime/python/tcl/tix8.4.3",       # Tix 控件集，tkinter 默认不加载
    "runtime/python/tcl/tk8.6/demos",    # Tk 示例程序
]

# 相对 .venv/Lib/site-packages、整目录删除的项
#
# 注意：`paddle/utils/cpp_extension` 看起来像"编译期才需要"，实际
# paddle/utils/__init__.py 在 import 阶段就 `from . import cpp_extension`，
# 删掉会让 `import paddle` 直接失败（PaddleOCR 随之不可用）。
# 这是踩过的坑，不要加回来。
SP_DIR_TARGETS = [
    "paddle/include",               # C++ 头文件，实测删掉后 paddle 导入与 OCR 均正常
]

# 另一个踩过的坑：不要用 opencv-python-headless 替换 opencv-contrib-python。
#
# paddlex/utils/deps.py 的 is_dep_available() 是按 **发行包名** 查
# importlib.metadata.version()，而 PaddleOCR 3.x 创建 OCR 流水线时会执行
#     pipeline_requires_extra("ocr", alt="ocr-core")
# "ocr-core" 这一组的清单里就有 opencv-contrib-python。装了 headless 版本
# 只能提供 cv2 模块，dist 名字对不上（opencv-python-headless != 
# opencv-contrib-python），检查直接失败：
#     DependencyError: `OCR` requires additional dependencies.
#     RuntimeError: A dependency error occurred during pipeline creation.
# 结果是 OCR 引擎完全起不来，而报错信息完全不提 opencv，极难定位。
#
# 结论：opencv 只保留 opencv-contrib-python 一个（它与当前 cv2 API 版本对应），
# 体积上只多出 cv2.pyd 的一部分，不值得为此赌上 OCR。真正该省的是它旁边那个
# 用不到的视频编解码库 —— 见 BASENAME_GLOBS 里的 opencv_videoio_ffmpeg*.dll。


EXT_TARGETS = {".pyc", ".pyo", ".pdb", ".lib", ".exp", ".pyi"}
DIRNAME_TARGETS = {"__pycache__"}
LOG_SUFFIXES = {".log", ".png", ".txt"}

# 按文件名通配删除（只放确定用不到的东西，逐条注明理由）
BASENAME_GLOBS = [
    "opencv_videoio_ffmpeg*.dll",   # cv2 的视频编解码 DLL（约 29MB）；本程序只截屏，不处理视频
    "_test*.pyd",                   # CPython 自带的测试扩展模块
    "*_test.pyd",
    "_test*.exe",
]


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%d B" % n if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f GB" % n


def scan(root: Path):
    """单次遍历，返回 (待删文件[(path,size)], 待删目录[path], 目录总字节, 各级体积, 文件总数)。

    属于「待删目录」内部的文件会被跳过（随目录一起删除，避免重复计数）。
    """
    extra_dirs = []
    for t in DIR_TARGETS:
        p = root / t
        if p.is_dir():
            extra_dirs.append(str(p))
    sp = root / ".venv" / "Lib" / "site-packages"
    for t in SP_DIR_TARGETS:
        p = sp / t
        if p.is_dir():
            extra_dirs.append(str(p))

    logs = root / "logs"
    files = []
    dirs = list(extra_dirs)
    marked = set(os.path.normcase(d) for d in extra_dirs)
    total = 0
    n_all = 0
    sizes = {}

    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        # 标记 __pycache__
        for d in list(dirnames):
            if d in DIRNAME_TARGETS:
                p = os.path.join(dirpath, d)
                dirs.append(p)
                marked.add(os.path.normcase(p))

        inside_marked = False
        cur = dirpath
        while cur and cur != root:
            if os.path.normcase(cur) in marked:
                inside_marked = True
                break
            nxt = os.path.dirname(cur)
            if nxt == cur:
                break
            cur = nxt

        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            total += size
            if inside_marked:
                continue
            n_all += 1
            fl = fn.lower()
            if os.path.splitext(fl)[1].lower() in EXT_TARGETS \
                    or any(fnmatch.fnmatch(fl, g) for g in BASENAME_GLOBS):
                files.append((p, size))
            elif dirpath == str(logs) and os.path.splitext(fl)[1].lower() in LOG_SUFFIXES:
                files.append((p, size))

        # 记录一级子目录体积（用于报告）
        rel = os.path.relpath(dirpath, root)
        top = rel.split(os.sep)[0]
        if rel != "." and top:
            sizes[top] = sizes.get(top, 0) + sum(
                os.path.getsize(os.path.join(dirpath, f)) for f in filenames
                if os.path.exists(os.path.join(dirpath, f))
            )
    return files, dirs, total, sizes, n_all


def report() -> None:
    print("=" * 66)
    print("  体积构成报告：", ROOT)
    print("=" * 66)
    files, dirs, total, sizes, n_all = scan(ROOT)
    rows = sorted(sizes.items(), key=lambda kv: kv[1], reverse=True)
    for name, size in rows[:22]:
        print("  %10s  %5.1f%%  %s" % (human(size), size * 100.0 / max(total, 1), name))
    print("  " + "-" * 60)
    print("  %10s  合计（文件 %d 个）" % (human(total), n_all))

    print()
    print("  可清理项：")
    fs = sum(s for _, s in files)
    print("    按后缀 %s ：%s（%d 个文件）" % (
        "/".join(sorted(EXT_TARGETS)), human(fs), len(files)))
    ds = 0
    for d in dirs:
        for dirpath, _dn, fns in os.walk(d, onerror=lambda e: None):
            for fn in fns:
                try:
                    ds += os.path.getsize(os.path.join(dirpath, fn))
                except OSError:
                    pass
    print("    整目录删除（__pycache__ / build / dist / paddle 头文件）：%s（%d 个目录）"
          % (human(ds), len(dirs)))
    print("    合计可释放约：%s" % human(fs + ds))
    print()


def clean(dry_run: bool, deps) -> None:
    print("=" * 66)
    print("  开始瘦身：", ROOT)
    print("=" * 66)

    files, dirs, total, _, _ = scan(ROOT)
    print("  待删：文件 %d 个、目录 %d 个" % (len(files), len(dirs)))

    freed = 0
    n_files = n_dirs = 0
    for p, size in files:
        try:
            if not dry_run:
                os.remove(p)
            freed += size
            n_files += 1
        except OSError as exc:
            print("    跳过 %s：%s" % (p, exc))

    for d in dirs:
        try:
            dsize = 0
            for dirpath, _dn, fns in os.walk(d, onerror=lambda e: None):
                for fn in fns:
                    try:
                        dsize += os.path.getsize(os.path.join(dirpath, fn))
                    except OSError:
                        pass
            if not dry_run:
                shutil.rmtree(d, ignore_errors=True)
            freed += dsize
            n_dirs += 1
        except OSError as exc:
            print("    跳过 %s：%s" % (d, exc))
        print("    %s目录 %s" % ("将清理" if dry_run else "已清理", d), flush=True)

    if deps:
        uninstall(deps)

    print()
    print("  删除文件 %d 个、目录 %d 个，释放 %s%s" % (
        n_files, n_dirs, human(freed),
        "（dry-run 预览，未实际删除）" if dry_run else ""))
    if not dry_run:
        print("  瘦身前：%s" % human(total))
        print("  瘦身后：%s" % human(total - freed))
        print("  减少  ：%s（%.1f%%）" % (
            human(freed), freed * 100.0 / max(total, 1)))


def uninstall(names) -> None:
    vpy = ROOT / ".venv" / "Scripts" / "python.exe"
    if not vpy.exists():
        print("  找不到 .venv，跳过依赖卸载")
        return
    for name in names:
        print("  卸载 %s ..." % name, flush=True)
        r = subprocess.run(
            [str(vpy), "-m", "pip", "uninstall", "-y", name],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        tail = [l for l in (r.stdout or "").strip().splitlines() if l.strip()]
        print("     " + (tail[-1] if tail else "无输出"))


def main() -> int:
    ap = argparse.ArgumentParser(description="答题助手瘦身工具")
    ap.add_argument("--report", action="store_true", help="只报告体积构成")
    ap.add_argument("--dry-run", action="store_true", help="预览，不实际删除")
    ap.add_argument("--deps", nargs="*", default=None, help="额外卸载这些 pip 包")
    args = ap.parse_args()

    if args.report:
        report()
        return 0
    clean(args.dry_run, args.deps)
    return 0


if __name__ == "__main__":
    sys.exit(main())
