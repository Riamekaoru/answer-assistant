# -*- coding: utf-8 -*-
"""备份完整性校验：逐文件比对 相对路径 + 大小 + 源码哈希。

用法：
    python packaging/qa_verify_backup.py [源目录] [备份目录]

两个路径都可省略：源目录默认取本仓库根目录，备份目录默认在
`<仓库同级>/_backup/` 下自动找最新的 `answer-assistant_v*`。
**不写死绝对路径** —— 把打包机的用户名和目录结构带进仓库没有意义。
"""
import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _latest_backup() -> Path:
    base = ROOT.parent / "_backup"
    cands = sorted(p for p in base.glob("answer-assistant_v*") if p.is_dir())
    return cands[-1] if cands else base / "answer-assistant_v1"


SRC = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT
BAK = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else _latest_backup()

TEXT_EXT = {".py", ".bat", ".md", ".txt", ".json", ".csv", ".gitignore", ".cfg", ".toml", ""}


def walk(root: Path):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for fn in filenames:
            p = Path(dirpath) / fn
            try:
                out[str(p.relative_to(root)).replace("\\", "/")] = p.stat().st_size
            except OSError:
                out[str(p.relative_to(root)).replace("\\", "/")] = -1
    return out


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


a = walk(SRC)
b = walk(BAK)
print("源文件数:", len(a), " 备份文件数:", len(b))

only_src = sorted(set(a) - set(b))
only_bak = sorted(set(b) - set(a))
size_diff = sorted(k for k in (set(a) & set(b)) if a[k] != b[k])

print("仅源有:", len(only_src), only_src[:10])
print("仅备份有:", len(only_bak), only_bak[:10])
print("大小不一致:", len(size_diff), size_diff[:10])

# 源码/文本文件逐字节校验
checked = bad = 0
for rel in sorted(a):
    if Path(rel).suffix.lower() not in TEXT_EXT:
        continue
    if rel not in b:
        bad += 1
        continue
    if sha(SRC / rel) != sha(BAK / rel):
        bad += 1
        print("  哈希不一致:", rel)
    checked += 1
print("逐字节校验文本文件:", checked, "个, 不一致:", bad)

total = sum(a.values())
print("源总字节: %.2f GB" % (total / 1024 ** 3))
print("备份总字节: %.2f GB" % (sum(b.values()) / 1024 ** 3))
ok = not only_src and not size_diff and bad == 0
print()
print("=== 备份完整性:", "通过" if ok else "不通过", "===")
sys.exit(0 if ok else 1)
