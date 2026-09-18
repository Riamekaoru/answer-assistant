# -*- coding: utf-8 -*-
"""把已有的解压目录同步成与安装包 zip 逐字节一致，并给出比对证据。

目的：避免再花半小时重新解压 1.5 万个文件，同时得到比"重新解压"更强的结论——
      同步完成时，目标目录的每一个文件的 CRC32 都与 zip 内的记录相同，
      且没有多余文件。之后在这个目录里跑 install.bat + 端到端验证，
      等价于（且严格于）"重新解压一遍再验证"。

用法（在开发机上跑，用任意 Python）：
    python packaging\\qa_sync_extracted.py <zip> <解压目录> <zip 内顶层目录名>

例：
    python packaging\\qa_sync_extracted.py ^
        answer-assistant\\dist_package\\AnswerAssistant-1.0.0-win64.zip ^
        _install_test\\AnswerAssistant-1.0.0-win64 ^
        AnswerAssistant-1.0.0-win64
"""
from __future__ import annotations

import os
import shutil
import sys
import time
import zipfile
import zlib
from pathlib import Path

ZIP = Path(sys.argv[1])
DEST = Path(sys.argv[2])          # 解压出来的顶层目录（含 PKG_NAME 那一层里面）
TOP = sys.argv[3]                 # zip 内顶层目录名，如 AnswerAssistant-1.0.0-win64


def crc_of(path: Path) -> int:
    crc = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            crc = zlib.crc32(chunk, crc)
    return crc & 0xFFFFFFFF


t0 = time.time()
zf = zipfile.ZipFile(ZIP)
prefix = TOP + "/"
entries = {}
for info in zf.infolist():
    if info.is_dir():
        continue
    if not info.filename.startswith(prefix):
        continue
    entries[info.filename[len(prefix):]] = info

print("zip 内文件数：%d" % len(entries))

# 本地现有文件
local = {}
for dirpath, _dn, fns in os.walk(DEST):
    for fn in fns:
        p = Path(dirpath) / fn
        local[p.relative_to(DEST).as_posix()] = p

print("目标目录现有文件数：%d" % len(local))
print()

# 1) 补 / 覆盖
fixed, checked = [], 0
for rel, info in entries.items():
    p = DEST / rel
    ok = False
    if p.is_file() and p.stat().st_size == info.file_size:
        if crc_of(p) == info.CRC:
            ok = True
            checked += 1
    if ok:
        continue
    p.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(info) as src, p.open("wb") as dst:
        shutil.copyfileobj(src, dst)
    fixed.append(rel)

# 2) 删掉 zip 里没有的
extra = [rel for rel in local if rel not in entries]
deleted = []
for rel in extra:
    p = DEST / rel
    try:
        p.unlink()
        deleted.append(rel)
    except OSError as exc:
        print("  删除失败 %s: %s" % (rel, exc))

print("未变更（CRC 已一致）：%d 个文件" % checked)
print("补齐/覆盖：%d 个文件" % len(fixed))
for rel in sorted(fixed)[:40]:
    print("   + %s" % rel)
if len(fixed) > 40:
    print("   ... 其余 %d 个" % (len(fixed) - 40))
print("删除多余文件：%d 个" % len(deleted))
for rel in sorted(deleted)[:40]:
    print("   - %s" % rel)
if len(deleted) > 40:
    print("   ... 其余 %d 个" % (len(deleted) - 40))

# 3) 终检：逐字节核对
print()
print("开始终检：逐文件核对 CRC32 ...", flush=True)
now = {}
for dirpath, _dn, fns in os.walk(DEST):
    for fn in fns:
        p = Path(dirpath) / fn
        now[p.relative_to(DEST).as_posix()] = p

problems = []
if set(now) != set(entries):
    problems.append("文件集合不一致：只在本地的 %s；只在包里的 %s"
                    % (sorted(set(now) - set(entries))[:5],
                       sorted(set(entries) - set(now))[:5]))
else:
    for i, (rel, info) in enumerate(entries.items(), 1):
        p = now[rel]
        if p.stat().st_size != info.file_size or crc_of(p) != info.CRC:
            problems.append(rel)
        if i % 3000 == 0:
            print("   已核对 %5d / %d ..." % (i, len(entries)), flush=True)

total = sum(p.stat().st_size for p in now.values())
print()
if problems:
    print("!! 终检未通过，%d 处不一致：" % len(problems))
    for x in problems[:10]:
        print("   ", x)
    sys.exit(1)

print("终检通过：%d 个文件，合计 %.1f MB，与 zip 内记录逐字节一致"
      % (len(now), total / 1048576))
print("耗时 %.0f 秒" % (time.time() - t0))
