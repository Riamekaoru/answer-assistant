# -*- coding: utf-8 -*-
"""Word 97-2003（.doc）纯文本提取 —— 不依赖 Word / WPS / LibreOffice。

为什么需要它
------------
中文办公环境里「另存为 .doc」极其常见，更麻烦的是**扩展名常常还留着 .docx**。
这种文件其实是 OLE2 复合文档（头 8 字节 ``D0 CF 11 E0 A1 B1 1A E1``），
python-docx 打开只会抛 ``ValueError: file ... is not a Word file``，
于是整个题库导入直接失败。光看扩展名是分辨不出来的，必须查文件头。

实现思路（Word 97 及以后的标准做法）
-----------------------------------
    WordDocument 流的 FIB 里有 Piece Table（CLX）在 Table 流中的位置；
    CLX 里的 PlcPcd 把「字符位置 CP」映射到「文件偏移 FC」；
    每个 piece 按 FC 的最高位判断编码：
        置位 -> 单字节（cp1252），实际偏移 = (FC & 0x3FFFFFFF) / 2
        未置位 -> 双字节（UTF-16LE），实际偏移 = FC
    按 CP 区间取够字符数拼起来，就是正文。
    这个结构能正确处理「中英文混排」——中文 piece 走 UTF-16，英文 piece 走 cp1252。

依赖 ``olefile``（纯 Python、无第三方依赖）读取 OLE2 容器。
"""

from __future__ import annotations

import logging
import struct
from pathlib import Path
from typing import List, Optional

log = logging.getLogger("answerassist.legacydoc")

# OLE2 / CFBF 复合文档签名
OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# Word 正文里的控制字符
_CTRL_DROP = "\x00\x01\x02\x03\x04\x05\x06\x08\x13\x14\x15\x16\x17\x18\x19\x1a\x1b" \
             "\x1c\x1d"
_CTRL_PARA = "\r\x07\x0c\x0e"      # 段落 / 单元格 / 分页 / 分栏
_CTRL_BREAK = "\x0b\x0d"           # 手动换行


def is_ole2(path: str | Path) -> bool:
    """按文件头判断是不是 OLE2 复合文档（.doc/.xls/.ppt 都是这个容器）。"""
    try:
        with open(path, "rb") as fh:
            return fh.read(8) == OLE2_MAGIC
    except OSError:
        return False


def looks_like_word_doc(path: str | Path) -> bool:
    """OLE2 容器 + 含 WordDocument 流 = Word 97-2003 文档。"""
    if not is_ole2(path):
        return False
    try:
        import olefile
    except ImportError:
        return False
    try:
        with olefile.OleFileIO(str(path)) as ole:
            return ole.exists("WordDocument")
    except Exception:
        return False


# ---------------------------------------------------------------- 内部实现

def _u16(buf: bytes, off: int) -> int:
    return struct.unpack_from("<H", buf, off)[0]


def _u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _fc_clx_offset(word: bytes) -> int:
    """定位 rgFcLcb97 里 fcClx 的偏移。

    FIB 的布局随 csw / cslw 变化，所以按字段算而不是写死 0x01A2。
    Word 97 的标准值是 csw=14、cslw=22，此时结果正好是 0x01A2。
    """
    try:
        csw = _u16(word, 0x20)
        cslw_off = 0x22 + csw * 2
        cslw = _u16(word, cslw_off)
        cb_off = cslw_off + 2 + cslw * 4
        rg_off = cb_off + 2
        return rg_off + 33 * 8          # fcClx 是 rgFcLcb97 的第 34 对
    except struct.error:
        return 0x01A2


def _parse_clx(clx: bytes):
    """从 CLX 中取出 PlcPcd（跳过前面的 Prc 数组）。"""
    i = 0
    n = len(clx)
    while i < n:
        tag = clx[i]
        if tag == 0x01:                 # Prc：1 字节 tag + 2 字节 cbGrpprl + 数据
            if i + 3 > n:
                return None
            cb = struct.unpack_from("<h", clx, i + 1)[0]
            i += 3 + max(cb, 0)
        elif tag == 0x02:               # Pcdt：1 字节 tag + 4 字节 lcb
            if i + 5 > n:
                return None
            lcb = _u32(clx, i + 1)
            return clx[i + 5:i + 5 + lcb]
        else:
            return None
    return None


def _decode_piece(word: bytes, fc: int, cp_len: int) -> str:
    if fc & 0x40000000:                 # 单字节（cp1252）
        start = (fc & 0x3FFFFFFF) // 2
        raw = word[start:start + cp_len]
        return raw.decode("cp1252", errors="replace")
    raw = word[fc:fc + cp_len * 2]      # 双字节（UTF-16LE）
    if len(raw) % 2:
        raw = raw[:-1]
    return raw.decode("utf-16-le", errors="replace")


def _strip_controls(text: str) -> str:
    out = []
    for ch in text:
        if ch in _CTRL_PARA:
            out.append("\n")
        elif ch in _CTRL_BREAK:
            out.append("\n")
        elif ch in _CTRL_DROP:
            continue
        elif ch == "\x1e":
            out.append("-")
        elif ch == "\x1f":
            continue
        elif ch == "\xa0":
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def extract_text(path: str | Path) -> str:
    """提取 .doc 正文（段落之间以 ``\\n`` 分隔）。

    Raises:
        RuntimeError: 缺 olefile、不是 Word 文档、或 FIB 结构异常。
    """
    try:
        import olefile
    except ImportError as exc:          # pragma: no cover - 依赖缺失
        raise RuntimeError(
            "读取 .doc（Word 97-2003）需要 olefile，请执行 pip install olefile"
        ) from exc

    path = str(path)
    with olefile.OleFileIO(path) as ole:
        if not ole.exists("WordDocument"):
            raise RuntimeError("不是 Word 97-2003 文档（缺少 WordDocument 流）")

        word = ole.openstream("WordDocument").read()
        if len(word) < 0x200:
            raise RuntimeError("WordDocument 流过短，文件可能已损坏")

        table_name = "1Table" if (_u16(word, 0x0A) >> 9) & 1 else "0Table"
        if not ole.exists(table_name):
            table_name = "0Table" if ole.exists("0Table") else "1Table"
        table = ole.openstream(table_name).read() if ole.exists(table_name) else b""

    fc_clx_off = _fc_clx_offset(word)
    if fc_clx_off + 8 > len(word):
        raise RuntimeError("FIB 结构异常，找不到 Piece Table 位置")
    fc_clx = _u32(word, fc_clx_off)
    lcb_clx = _u32(word, fc_clx_off + 4)

    pieces: List[str] = []
    if table and lcb_clx and fc_clx + lcb_clx <= len(table):
        plc = _parse_clx(table[fc_clx:fc_clx + lcb_clx])
        if plc and len(plc) >= 12:
            count = (len(plc) - 4) // 12
            cps = [_u32(plc, 4 * k) for k in range(count + 1)]
            base = 4 * (count + 1)
            for k in range(count):
                fc = _u32(plc, base + 8 * k + 2)
                cp_len = cps[k + 1] - cps[k]
                if cp_len <= 0:
                    continue
                pieces.append(_decode_piece(word, fc, cp_len))

    if not pieces:                      # 退化路径：没有 Piece Table，按 fcMin/fcMac 直读
        fc_min = _u32(word, 0x18)
        fc_mac = _u32(word, 0x1C)
        if 0 < fc_min < fc_mac <= len(word):
            raw = word[fc_min:fc_mac]
            try:
                pieces.append(raw.decode("utf-16-le", errors="replace"))
            except Exception:
                pieces.append(raw.decode("cp1252", errors="replace"))

    if not pieces:
        raise RuntimeError("未能从 .doc 中解析出任何正文")

    return _strip_controls("".join(pieces))


def extract_paragraphs(path: str | Path) -> List[str]:
    """提取正文并按段落切分，过滤空段。"""
    text = extract_text(path)
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if line:
            out.append(line)
    return out


def read_doc_via_word_com(path: str | Path) -> Optional[str]:
    """兜底方案：本机装了 Word / WPS 时用 COM 转换。

    正常路径不走这里（COM 慢、有弹窗风险、还要装 pywin32），
    只在纯 Python 解析失败时作为最后手段。
    """
    try:
        import win32com.client  # type: ignore
    except ImportError:
        return None

    app = doc = None
    for prog_id in ("Word.Application", "KWPS.Application", "WPS.Application"):
        try:
            app = win32com.client.Dispatch(prog_id)
            app.Visible = False
            doc = app.Documents.Open(str(path), ReadOnly=True)
            text = doc.Content.Text
            return _strip_controls(text)
        except Exception as exc:
            log.debug("COM 方案 %s 失败: %s", prog_id, exc)
        finally:
            try:
                if doc is not None:
                    doc.Close(False)
                if app is not None:
                    app.Quit()
            except Exception:
                pass
    return None
