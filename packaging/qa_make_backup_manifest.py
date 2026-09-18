# -*- coding: utf-8 -*-
"""生成备份清单（相对路径 + 大小）。

用法：
    python packaging/qa_make_backup_manifest.py [备份目录] [输出文件]

备份目录默认在 `<仓库同级>/_backup/` 下自动找最新的 `answer-assistant_v*`。
**不写死绝对路径** —— 把打包机的用户名和目录结构带进仓库没有意义。
"""
import datetime
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _latest_backup() -> Path:
    base = ROOT.parent / "_backup"
    cands = sorted(p for p in base.glob("answer-assistant_v*") if p.is_dir())
    return cands[-1] if cands else base / "answer-assistant_v1"


BAK = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else _latest_backup()
OUT = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else BAK.parent / "MANIFEST-v1.txt"

rows = []
for dirpath, dirnames, filenames in os.walk(BAK):
    for fn in filenames:
        p = Path(dirpath) / fn
        try:
            st = p.stat()
        except OSError:
            continue
        rel = str(p.relative_to(BAK)).replace("\\", "/")
        rows.append((rel, st.st_size))
rows.sort()

total = sum(s for _, s in rows)
with open(OUT, "w", encoding="utf-8", newline="\n") as f:
    f.write("# answer-assistant 备份清单 v1\n")
    f.write("# 备份时间: %s\n" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    f.write("# 来源   : %s\n" % ROOT)
    f.write("# 文件数 : %d\n" % len(rows))
    f.write("# 总字节 : %d (%.2f GB)\n" % (total, total / 1024 ** 3))
    f.write("# 格式   : 大小(字节) <TAB> 相对路径\n")
    f.write("#\n")
    for rel, size in rows:
        f.write("%d\t%s\n" % (size, rel))

print("清单已写出:", OUT)
print("条目数:", len(rows), " 总字节:", total)
