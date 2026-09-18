# -*- coding: utf-8 -*-
"""构造最小可用的 OLE2 / Word 97-2003（.doc）文件。

用途
----
``core.legacy_doc`` 解析 .doc 依赖三样东西：OLE2 容器 → ``WordDocument`` 流里的
FIB → ``1Table`` 流里的 Piece Table。要让这段逻辑有回归测试，就需要一个**真实
的二进制 .doc**：纯文本 fixture 测不到这一层。

本机常常没有 Word / WPS / LibreOffice，没法把 docx 转成 doc，所以这里直接按
格式规范把文件拼出来。

**注意**：这是"结构合法的最小文件"，不是 Word 生成的成品。我们的解析器读它没
问题，但拿 Word 打开可能会抱怨内容不规范 —— 它只服务于回归测试。
真·Word 产物的兼容性由用户的实际题库文件验证（见 packaging/README-打包流程.md）。

OLE2（CFBF）布局
---------------
    偏移 0      头 512 字节（签名 / 扇区大小 / FAT 与目录扇区号 / 109 项 DIFAT）
    扇区 0..N-1 每个 512 字节；扇区 k 的文件偏移 = 512 + k*512
    目录      每项 128 字节（名称 / 类型 / 颜色 / 左右兄弟 / 子项 / 起始扇区 / 长度）

本实现刻意不做 ministream：把小于 4096 字节的流补零到 4096，就落进常规 FAT 链，
省掉 miniFAT 与 ministream 两套结构。对解析器完全透明。

Word 97 的 FIB（File Information Block）要点
-------------------------------------------
    0x0A  flags，bit9 = fWhichTblStm（1 -> 用 "1Table" 流）
    0x18  fcMin / 0x1C fcMac        正文在 WordDocument 里的起止偏移
    0x20  csw=14 / 0x3E cslw=22     决定 rgFcLcb97 的起始位置
    0x1A2 fcClx / 0x1A6 lcbClx      CLX（Piece Table）在 Table 流中的位置
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

SECTOR = 512
MINI_CUTOFF = 4096

FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD

OBJ_STORAGE = 1
OBJ_STREAM = 2
OBJ_ROOT = 5

COLOR_RED = 0
COLOR_BLACK = 1

# Word 正文起点。放在 0x400 是为了给 FIB 留足空间（rgFcLcb97 结束在 0x1AA 之后）。
FC_MIN = 0x400


# ---------------------------------------------------------------- OLE2 写出

def _pad(data: bytes, size: int) -> bytes:
    return data + b"\x00" * (size - len(data))


def _dir_entry(name: str, obj_type: int, color: int, left: int = FREESECT,
               right: int = FREESECT, child: int = FREESECT,
               start: int = ENDOFCHAIN, size: int = 0) -> bytes:
    raw_name = (name + "\x00").encode("utf-16-le")
    if len(raw_name) > 64:
        raise ValueError(f"OLE2 目录项名称过长: {name}")
    entry = bytearray(128)
    entry[0:len(raw_name)] = raw_name
    struct.pack_into("<H", entry, 0x40, len(raw_name))
    entry[0x42] = obj_type
    entry[0x43] = color
    struct.pack_into("<I", entry, 0x44, left)
    struct.pack_into("<I", entry, 0x48, right)
    struct.pack_into("<I", entry, 0x4C, child)
    struct.pack_into("<I", entry, 0x74, start)
    struct.pack_into("<Q", entry, 0x78, size)
    return bytes(entry)


def build_ole2(streams: Sequence[Tuple[str, bytes]]) -> bytes:
    """把若干命名流打成 OLE2 容器。

    目录树固定成「根节点 -> 长子链」：根 child 指向第 1 个流，
    第 i 个流的 right 指向第 i+1 个流。两点三流足够，不需要着色平衡。
    """
    # 1) 每个流的扇区占用
    chunks: List[bytes] = []
    chains: List[List[int]] = []
    sizes: List[int] = []
    next_sector = 0

    fat_sector_count = 2                     # 512*2/4 = 256 个 FAT 项，够用
    next_sector += fat_sector_count          # 扇区 0..1 留给 FAT
    dir_sector = next_sector
    next_sector += 1                         # 目录固定占 1 个扇区

    for _name, data in streams:
        logical = len(data)
        if logical < MINI_CUTOFF:
            # 补零推过 ministream 阈值，就能放进常规 FAT 链
            data = _pad(data, MINI_CUTOFF)
        elif logical % SECTOR:
            data = _pad(data, (logical // SECTOR + 1) * SECTOR)
        count = len(data) // SECTOR
        chain = list(range(next_sector, next_sector + count))
        next_sector += count
        chunks.append(data)
        chains.append(chain)
        sizes.append(logical)

    # 2) FAT
    fat = [FREESECT] * (fat_sector_count * SECTOR // 4)
    for k in range(fat_sector_count):
        fat[k] = FATSECT
    fat[dir_sector] = ENDOFCHAIN
    for chain in chains:
        for i, sec in enumerate(chain):
            fat[sec] = chain[i + 1] if i + 1 < len(chain) else ENDOFCHAIN

    # 3) 目录项
    root = _dir_entry("\x05Root Entry", OBJ_ROOT, COLOR_BLACK,
                      child=1 if streams else FREESECT)
    entries = [root]
    for i, (name, _data) in enumerate(streams):
        entries.append(_dir_entry(
            name, OBJ_STREAM, COLOR_BLACK,
            right=(i + 2) if i + 1 < len(streams) else FREESECT,
            start=chains[i][0], size=sizes[i]))
    directory = _pad(b"".join(entries), (len(entries) + 3) // 4 * SECTOR)
    if len(directory) > SECTOR:
        raise ValueError("目录区超过 1 个扇区，需要扩展本实现的布局")

    # 4) 头
    header = bytearray(SECTOR)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<H", header, 0x18, 0x003E)     # minor version
    struct.pack_into("<H", header, 0x1A, 0x0003)     # major version (512B 扇区)
    struct.pack_into("<H", header, 0x1C, 0xFFFE)     # byte order
    struct.pack_into("<H", header, 0x1E, 9)          # sector shift -> 512
    struct.pack_into("<H", header, 0x20, 6)          # mini sector shift -> 64
    struct.pack_into("<I", header, 0x28, 0)          # 目录扇区数（v3 恒为 0）
    struct.pack_into("<I", header, 0x2C, fat_sector_count)
    struct.pack_into("<I", header, 0x30, dir_sector)
    struct.pack_into("<I", header, 0x34, 0)          # transaction signature
    struct.pack_into("<I", header, 0x38, MINI_CUTOFF)
    struct.pack_into("<I", header, 0x3C, ENDOFCHAIN)  # 无 miniFAT
    struct.pack_into("<I", header, 0x40, 0)
    struct.pack_into("<I", header, 0x44, ENDOFCHAIN)  # 无 DIFAT 链
    struct.pack_into("<I", header, 0x48, 0)
    for k in range(109):
        struct.pack_into("<I", header, 0x4C + 4 * k,
                         k if k < fat_sector_count else FREESECT)

    # 5) 拼文件
    body = bytearray(b"\x00" * (next_sector * SECTOR))
    for k in range(fat_sector_count):
        body[k * SECTOR:(k + 1) * SECTOR] = struct.pack(
            f"<{len(fat) // fat_sector_count}I",
            *fat[k * len(fat) // fat_sector_count:
                 (k + 1) * len(fat) // fat_sector_count])
    body[dir_sector * SECTOR:dir_sector * SECTOR + len(directory)] = directory
    for chain, data in zip(chains, chunks):
        offset = chain[0] * SECTOR
        body[offset:offset + len(data)] = data

    return bytes(header) + bytes(body)


# ---------------------------------------------------------------- Word 97

def _piece_table(text_utf16: bytes) -> bytes:
    """构造只有一个 piece 的 CLX（整个正文连续存放、UTF-16LE）。"""
    cp_end = len(text_utf16) // 2

    pcd = bytearray(8)
    struct.pack_into("<H", pcd, 0, 0)          # flags：无特殊标记
    struct.pack_into("<I", pcd, 2, FC_MIN)     # FC：最高位为 0 -> UTF-16LE
    struct.pack_into("<H", pcd, 6, 0)          # prm
    plc = struct.pack("<II", 0, cp_end) + bytes(pcd)

    clx = bytearray()
    clx.append(0x02)                           # Pcdt
    clx += struct.pack("<I", len(plc))
    clx += plc
    return bytes(clx)


def build_word97_doc(text: str) -> bytes:
    """把纯文本包成 Word 97-2003 结构（段落用 ``\\r`` 分隔）。"""
    body = text.replace("\n", "\r").encode("utf-16-le")
    clx = _piece_table(body)

    word = bytearray(max(MINI_CUTOFF, FC_MIN + len(body)))
    struct.pack_into("<H", word, 0x00, 0xA5EC)          # wIdent
    struct.pack_into("<H", word, 0x02, 0x00C1)           # nFib (Word 97)
    struct.pack_into("<H", word, 0x06, 0x0804)           # lid = 简体中文
    struct.pack_into("<H", word, 0x0A, 0x0204)           # fComplex + fWhichTblStm
    struct.pack_into("<H", word, 0x0C, 0x00BF)           # nFibBack
    struct.pack_into("<I", word, 0x18, FC_MIN)           # fcMin
    struct.pack_into("<I", word, 0x1C, FC_MIN + len(body))  # fcMac
    struct.pack_into("<H", word, 0x20, 14)               # csw
    struct.pack_into("<H", word, 0x3E, 22)               # cslw
    struct.pack_into("<H", word, 0x98, 93)               # cbRgFcLcb
    struct.pack_into("<I", word, 0x1A2, 0)               # fcClx -> 1Table 开头
    struct.pack_into("<I", word, 0x1A6, len(clx))        # lcbClx
    word[FC_MIN:FC_MIN + len(body)] = body

    return build_ole2([("WordDocument", bytes(word)), ("1Table", clx)])


def write_word97_doc(path: str | Path, text: str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(build_word97_doc(text))
    return p
