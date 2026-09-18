# -*- coding: utf-8 -*-
"""自检脚本：装配完成后跑一遍，确认本机具备完整能力。

    python tools/selfcheck.py            全部检查
    python tools/selfcheck.py --quick    跳过 OCR 与进程测试（秒级）

检查项：
    1. 运行环境（Python 版本 / 屏幕缩放 / 目录可写）
    2. 核心模块导入
    3. 依赖检查
    4. 题库导入（json / csv / xlsx / docx / doc / txt 全格式，含格式自动判定）
    5. 模糊匹配（用 OCR 常见噪声变体验证命中率）
    6. 屏幕采集（抓一帧存盘）
    7. 常驻识别框（尺寸夹紧 / 透明度 / 显隐 / 区域回调）
    8. OCR 识别（合成中文图片端到端）
    9. 三进程启动冒烟测试
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
_results: List[Tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = "") -> None:
    _results.append((name, status, detail))
    mark = {PASS: "[ OK ]", FAIL: "[FAIL]", WARN: "[WARN]"}[status]
    line = f"{mark} {name}"
    if detail:
        line += f"\n       {detail}"
    print(line, flush=True)


# ---------------------------------------------------------------- 检查项

def check_env() -> None:
    print("\n=== 1. 运行环境 ===")
    record("Python 版本", PASS, f"{sys.version.split()[0]} @ {sys.executable}")

    try:
        from core.screen import enable_dpi_awareness, primary_dpi_scale

        enable_dpi_awareness()
        record("DPI 感知", PASS, f"主屏缩放 {primary_dpi_scale():.2f}x")
    except Exception as exc:
        record("DPI 感知", FAIL, str(exc))

    try:
        from core.config import logs_dir, user_data_dir

        for d in (logs_dir(), user_data_dir()):
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        record("目录可写", PASS, f"{user_data_dir()}")
    except Exception as exc:
        record("目录可写", FAIL, str(exc))


def check_imports() -> None:
    print("\n=== 2. 核心模块导入 ===")
    modules = [
        "core.config", "core.logger", "core.text_utils", "core.screen",
        "core.ocr_engine", "core.question_bank", "core.legacy_doc",
        "core.matcher", "core.hotkeys",
        "workers.ipc", "workers.capture_worker", "workers.search_worker",
        "ui.theme", "ui.region_selector", "ui.capture_frame", "ui.float_window",
        "ui.settings_window",
    ]
    import importlib

    bad = []
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as exc:
            bad.append(f"{name}: {type(exc).__name__}: {exc}")
    if bad:
        record("模块导入", FAIL, "\n       ".join(bad))
    else:
        record("模块导入", PASS, f"{len(modules)} 个模块全部可用")


def check_optional_deps() -> None:
    print("\n=== 3. 依赖检查 ===")
    required = {
        "numpy": "数值计算", "cv2": "图像预处理", "PIL": "图像处理",
        "mss": "屏幕采集", "rapidfuzz": "模糊匹配",
    }
    optional = {
        "docx": "Word 题库导入", "olefile": "Word 97 (.doc) 题库导入",
        "openpyxl": "Excel 题库导入",
        "keyboard": "全局快捷键", "pandas": "表格处理（可选）",
        "paddleocr": "PaddleOCR 引擎", "paddle": "PaddlePaddle 后端",
        "rapidocr": "RapidOCR 引擎（备用）",
    }
    import importlib.util

    for mod, label in required.items():
        ok = importlib.util.find_spec(mod) is not None
        record(f"{label} ({mod})", PASS if ok else FAIL,
               "" if ok else "缺失，请运行 install.bat")

    missing_opt = []
    for mod, label in optional.items():
        if importlib.util.find_spec(mod) is None:
            missing_opt.append(label)
    if missing_opt:
        record("可选依赖", WARN, "未安装：" + "、".join(missing_opt))
    else:
        record("可选依赖", PASS, "全部已安装")


def _check_bank_templates(out_dir: Path) -> None:
    """题库模板导入：仓库自带 templates/，克隆后不依赖任何题库内容即可验证各导入器。"""
    from core.question_bank import load_bank

    try:
        from tools.make_bank_template import write_templates

        tpl_dir = ROOT / "templates"
        tpl_files = [p for p in sorted(tpl_dir.glob("bank_template.*"))
                     if p.suffix.lower() in (".json", ".csv", ".xlsx",
                                             ".docx", ".doc", ".txt")]
        origin = "templates/"
        if not tpl_files:
            tpl_files = write_templates(out_dir)
            origin = f"现场生成 -> {out_dir}"
        if not tpl_files:
            record("题库模板导入", FAIL, "既没有 templates/ 也没能生成")
            return

        bad = []
        for path in tpl_files:
            try:
                n = len(load_bank(path))
                if n != 1:
                    bad.append(f"{path.name}: {n} 题（应为 1 行占位示例）")
            except Exception as exc:
                bad.append(f"{path.name}: {type(exc).__name__}: {exc}")
        if bad:
            record("题库模板导入", FAIL, "；".join(bad))
        else:
            record("题库模板导入", PASS,
                   f"{len(tpl_files)} 种格式各 1 行占位示例（来自 {origin}）")
    except Exception as exc:
        record("题库模板导入", FAIL, f"{type(exc).__name__}: {exc}")


def _check_word_format_sniffing(work_dir: Path) -> None:
    """Word 真实格式判定：中文办公环境里「另存为 .doc 但扩展名还是 .docx」极常见，
    这种文件早期会让导入直接抛 python-docx 的 ValueError。
    现在按文件头分流，扩展名不参与判断。

    优先用示例题库（一对 .doc/.docx），没有就用仓库自带的 templates/。
    """
    from core import legacy_doc
    from core.question_bank import _LOADERS, load_bank

    try:
        doc_src = work_dir / "sample_bank.doc"
        ooxml_src = work_dir / "sample_bank.docx"
        if not (doc_src.exists() and ooxml_src.exists()):
            doc_src = ROOT / "templates" / "bank_template.doc"
            ooxml_src = ROOT / "templates" / "bank_template.docx"
        assert doc_src.exists() and ooxml_src.exists(), "找不到可用的 .doc/.docx 测试文件"

        assert doc_src.read_bytes()[:8] == legacy_doc.OLE2_MAGIC, ".doc 文件头不是 OLE2"
        assert legacy_doc.looks_like_word_doc(doc_src), ".doc 里没有 WordDocument 流"

        paras = legacy_doc.extract_paragraphs(doc_src)
        assert any(p.strip() for p in paras), "OLE2 Piece Table 未还原出正文"

        expect = len(load_bank(doc_src))

        # 历史情形：OLE2 内容配 .docx 扩展名，必须照样读出来
        fake = work_dir / "misnamed_extension.docx"
        fake.write_bytes(doc_src.read_bytes())
        n1 = len(load_bank(fake))
        assert n1 == expect, f"OLE2 内容配 .docx 扩展名解析异常（{n1} != {expect}）"

        # 反过来：OOXML 内容配 .doc 扩展名
        fake2 = work_dir / "misnamed_extension.doc"
        fake2.write_bytes(ooxml_src.read_bytes())
        n2 = len(load_bank(fake2))
        assert n2 == expect, f"OOXML 内容配 .doc 扩展名解析异常（{n2} != {expect}）"

        assert ".doc" in _LOADERS and ".docx" in _LOADERS
        record("Word 格式自动判定", PASS,
               f"OLE2 与 OOXML 双向错配扩展名均可导入（{expect} 题），"
               f"OLE2 段落 {len(paras)} 段")
    except Exception as exc:
        record("Word 格式自动判定", FAIL, f"{type(exc).__name__}: {exc}")


def _check_bank_samples(out_dir: Path, files: List[Path]) -> None:
    """本地有示例题目时，跑全部内容相关检查。"""
    from core.question_bank import load_bank

    baseline = 0
    for path in files:
        try:
            bank = load_bank(path)
            count = len(bank)
            if path.suffix == ".json":
                baseline = count
            detail = f"{count} 题"
            if baseline and path.suffix in (".csv", ".xlsx", ".docx", ".doc", ".txt"):
                # 允许少量差异（选项写法的歧义），但不应低于 85%
                if count < baseline * 0.85:
                    record(f"导入 {path.name}", WARN,
                           f"{detail}；JSON 基准为 {baseline} 题，解析可能不完整")
                    continue
            record(f"导入 {path.name}", PASS if count > 0 else FAIL, detail)
        except Exception as exc:
            record(f"导入 {path.name}", FAIL, f"{type(exc).__name__}: {exc}")

    # 校验字段完整性
    try:
        bank = load_bank(out_dir / "sample_bank.json")
        sample = bank.questions[0]
        assert sample.question and sample.answer, "题干或答案为空"
        assert sample.options, "选项未解析"
        with_answer = sum(1 for q in bank.questions if q.answer_letters or q.answer)
        record("字段解析", PASS,
               f"含答案 {with_answer}/{len(bank)}，示例题型={sample.qtype}，"
               f"答案={sample.answer_letters}")
    except Exception as exc:
        record("字段解析", FAIL, str(exc))

    # 各格式的选项正文完整性（CSV/XLSX/DOC/DOCX 都走同一拆分逻辑）
    try:
        worst = None
        for path in files:
            if path.suffix not in (".csv", ".xlsx", ".docx", ".doc", ".txt"):
                continue
            b = load_bank(path)
            for q in b.questions:
                for opt in q.options:
                    body = opt.split(".", 1)[-1].strip()
                    if len(body) <= 1 and (worst is None or len(body) < len(worst)):
                        worst = body
        if worst is None:
            record("各格式选项完整性", PASS, "CSV/XLSX/DOC/DOCX/TXT 选项正文无退化")
        else:
            record("各格式选项完整性", FAIL, f"出现退化选项正文: {worst!r}")
    except Exception as exc:
        record("各格式选项完整性", FAIL, f"{type(exc).__name__}: {exc}")

    # 各格式题目数必须完全一致：同一份样本用 6 种格式序列化，
    # 解析结果不同就说明某个格式的解析器在"多塞"或"漏读"。
    try:
        counts = {}
        for path in files:
            if path.suffix in (".json", ".csv", ".xlsx", ".docx", ".doc", ".txt"):
                counts[path.suffix] = len(load_bank(path))
        if len(counts) >= 4 and len(set(counts.values())) == 1:
            record("各格式题目数一致", PASS,
                   "、".join(f"{k}={v}" for k, v in sorted(counts.items())))
        else:
            record("各格式题目数一致", FAIL, str(counts))
    except Exception as exc:
        record("各格式题目数一致", FAIL, f"{type(exc).__name__}: {exc}")

    # 各格式的答案与解析也不能丢：只比题目数会漏掉「题在、答案没了」这种故障 ——
    # 题型标记的两种排版约定（题内 / 分组标题）如果在解析器里处理反了，
    # 就是这个症状。样例题库每一题都有答案和解析，所以数量必须完全相等。
    try:
        expect = len(load_bank(out_dir / "sample_bank.json"))
        bad = []
        for path in files:
            if path.suffix not in (".json", ".csv", ".xlsx", ".docx", ".doc", ".txt"):
                continue
            b = load_bank(path)
            n_ans = sum(1 for q in b.questions if q.answer or q.answer_letters)
            n_ana = sum(1 for q in b.questions if q.analysis)
            if n_ans != expect or n_ana != expect:
                bad.append(f"{path.suffix}: 答案 {n_ans} 解析 {n_ana}（应各 {expect}）")
        if bad:
            record("各格式答案/解析完整", FAIL, "；".join(bad))
        else:
            record("各格式答案/解析完整", PASS, f"六种格式均为 {expect} 题带答案+解析")
    except Exception as exc:
        record("各格式答案/解析完整", FAIL, f"{type(exc).__name__}: {exc}")

    _check_word_format_sniffing(out_dir)


def _check_option_split() -> None:
    """选项拆分回归：正则带捕获组，re.split 会把字母一起返回，
    历史上导致 "A.A / B.<选项A正文> / C.B" 这种错位（答案渲染成 "C. B"）。
    """
    try:
        from core.question_bank import _split_options

        raw = ("A. 这是第一个示例选项\nB. 这是第二个示例选项\n"
               "C. 这是第三个示例选项\nD. 这是第四个示例选项")
        got = _split_options(raw)
        expect = ["A. 这是第一个示例选项", "B. 这是第二个示例选项",
                  "C. 这是第三个示例选项", "D. 这是第四个示例选项"]
        assert got == expect, f"多行选项拆分错误: {got}"

        # 正文不能退化成单个字母
        for opt in got:
            body = opt.split(".", 1)[-1].strip()
            assert len(body) > 1, f"选项正文退化: {opt}"

        for name, case, want in (
            ("单行空格", "A. 甲 B. 乙 C. 丙", 3),
            ("竖线分隔", "A. 甲|B. 乙|C. 丙", 3),
            ("括号", "(A) 甲 (B) 乙 (C) 丙 (D) 丁", 4),
        ):
            r = _split_options(case)
            assert len(r) == want, f"{name}拆分错误({len(r)}): {r}"
        record("选项拆分", PASS, "多行 / 单行 / 竖线 / 括号 均正确")
    except Exception as exc:
        record("选项拆分", FAIL, f"{type(exc).__name__}: {exc}")


def _check_bracket_markers() -> None:
    """中文括号标记写法 + 题型标记的两种排版约定。

    全部是自造的占位文本，测的是识别层，不依赖任何真实题库文件。
    """
    try:
        from core.question_bank import parse_blocks

        # 约定 B：题型写成分组标题（前一题答案之后）
        grouped = parse_blocks([
            "【题型】单选题",
            "1、示例单选题的题干（  ）。",
            "A、甲选项", "B、乙选项", "C、丙选项", "D、丁选项",
            "【答案】B",
            "【题型】多选题",
            "16、示例多选题的题干（ ）",
            "A、第一条", "B、第二条", "C、第三条", "D、第四条",
            "【答案】ABC",
            "【题型】判断题",
            "21、示例判断题的题干。（）",
            "【答案】错误",
        ], "t")
        assert len(grouped) == 3, f"中文括号标记解析数量异常：{len(grouped)}"
        types = [q.qtype for q in grouped]
        assert types == ["单选题", "多选题", "判断题"], f"题型分组错乱：{types}"
        assert grouped[0].answer_letters == "B", f"答案被当成解析？{grouped[0].answer!r}"
        assert grouped[1].answer_letters == "ABC"
        assert grouped[2].answer_letters == "F", f"判断题答案异常：{grouped[2].answer!r}"
        assert len(grouped[1].options) == 4, "顿号选项未拆开"

        # 约定 A：题型写在题内（选项之后、答案之前），必须仍然生效
        inline = parse_blocks([
            "题型：多选题",
            "1. 示例题：下列哪些属于示例选项？",
            "A. 甲", "B. 乙", "C. 丙", "D. 丁",
            "答案：ABC",
            "解析：甲乙丙三项都符合条件。",
        ], "t")
        assert len(inline) == 1, f"题内题型写法解析数量异常：{len(inline)}"
        assert inline[0].qtype == "多选题", f"题内题型丢失：{inline[0].qtype!r}"
        assert inline[0].answer_letters == "ABC", "题内写法的答案丢失"
        assert inline[0].analysis, "题内写法的解析丢失"

        # 答案与解析同一行
        same_line = parse_blocks([
            "1、示例题：以下说法正确的是（ ）", "A、甲", "B、乙",
            "【答案】B【解析】因为甲不满足条件。",
        ], "t")
        assert len(same_line) == 1
        assert same_line[0].answer_letters == "B", \
            f"同行写法答案错误：{same_line[0].answer!r}"
        assert "甲不满足" in same_line[0].analysis, \
            f"同行写法解析未分离：{same_line[0].analysis!r}"

        record("中文括号标记解析", PASS,
               "【题型】分组标题 / 题内题型 / 顿号选项 / 【答案】 / 同行解析 均正确")
    except Exception as exc:
        record("中文括号标记解析", FAIL, f"{type(exc).__name__}: {exc}")


def _check_title_filter() -> None:
    """文档标题不能变成题目：练习册首行的标题段既无选项也无答案，
    早期版本会把它解析成一道空的简答题。
    """
    try:
        from core.question_bank import parse_blocks

        dropped = parse_blocks(
            ["示例练习题库 · 专题一",
             "1. 这是一道带选项和答案的示例题目？", "A. 甲", "B. 乙", "答案：A"], "t")
        assert len(dropped) == 1 and "示例题目" in dropped[0].question, \
            f"首行标题未被过滤: {[q.question[:20] for q in dropped]}"

        mid = parse_blocks(
            ["1. 这是第一道示例题目？", "A. 甲", "答案：A",
             "答案与解析",
             "2. 这是第二道示例题目？", "A. 乙", "答案：A"], "t")
        assert len(mid) == 2, f"中间标题未被过滤: {[q.question[:20] for q in mid]}"

        # 反向用例：真正无答案、但带问号的题目必须保留
        kept = parse_blocks(
            ["1. 这是一道没有选项和答案的示例题目？",
             "2. 这是第二道示例题目？", "A. 甲", "答案：A"], "t")
        assert len(kept) == 2, f"误删了无答案题目: {[q.question[:20] for q in kept]}"

        record("文档标题不算题目", PASS, "首行标题 / 中间标题已过滤，无答案题目保留")
    except Exception as exc:
        record("文档标题不算题目", FAIL, f"{type(exc).__name__}: {exc}")


def check_bank():
    print("\n=== 4. 题库导入 ===")
    from core.config import user_data_dir
    from tools.make_sample_bank import write_samples

    out_dir = user_data_dir() / "samples"

    # 模板：仓库自带，克隆后不依赖任何题库内容即可验证各导入器
    _check_bank_templates(out_dir)

    # 示例题库：题目内容不进仓库，本地有才跑内容类检查
    files = write_samples(out_dir)
    if files:
        record("示例题库生成", PASS, f"{len(files)} 个文件 -> {out_dir}")
        _check_bank_samples(out_dir, files)
    else:
        record("示例题库", WARN,
               "本地没有示例题目（data/local/sample_questions.json）；"
               "仓库不发布题库内容，依赖示例题的检查已跳过")
        _check_word_format_sniffing(out_dir)

    # 以下为纯逻辑回归，任何时候都跑
    _check_option_split()
    _check_bracket_markers()
    _check_title_filter()
    return out_dir


def check_matcher(out_dir: Path) -> None:
    print("\n=== 5. 模糊匹配 ===")
    from core.matcher import Matcher, build_queries
    from core.question_bank import load_bank

    # 匹配阈值验证需要示例题库（本地内容，不进仓库）
    if not (out_dir / "sample_bank.json").exists():
        record("匹配测试", WARN,
               "本地没有示例题目（data/local/sample_questions.json），"
               "无法验证匹配阈值；已跳过")
    else:
        bank = load_bank(out_dir / "sample_bank.json")
        matcher = Matcher(bank, threshold=0.5, top_k=3)

        # 用题库里最长的那道题现场构造 OCR 常见失真形态（题号残留、多余空格、
        # 中间错字、只截到半句、混入选项）。**代码里不写死任何题目内容**，
        # 阈值取跨题实测的下限再留余量。
        q0 = max(bank.questions, key=lambda q: len(q.question or ""))
        stem = (q0.question or "").strip().rstrip("？?")
        mid = max(1, len(stem) // 2)
        typo = stem[:mid] + ("错" if stem[mid] != "错" else "误") + stem[mid + 1:]

        cases = [
            ("干净题干", f"{stem}？", 0.90),
            ("带题号", f"1. {stem}？", 0.85),
            ("含错字", f"{typo}？", 0.80),
            ("混入选项", f"{stem}？\n" + "\n".join(list(q0.options)[:3]), 0.80),
            ("半句截断", stem[: max(6, len(stem) // 2)], 0.60),
            ("多余空白", " ".join(stem) + "？", 0.85),
        ]

        for label, query, expect in cases:
            try:
                hits = matcher.search(query)
                if not hits:
                    record(f"匹配 · {label}", FAIL, "无命中")
                    continue
                top = hits[0]
                status = PASS if top.score >= expect else WARN
                record(f"匹配 · {label}", status,
                       f"{top.score * 100:.1f}%（期望≥{expect * 100:.0f}%）"
                       f" -> {top.question.answer_letters or top.question.answer}")
            except Exception as exc:
                record(f"匹配 · {label}", FAIL, f"{type(exc).__name__}: {exc}")

    # 分块逻辑是纯逻辑，任何时候都跑
    try:
        multi = ("1. 示例题干一？\nA. 选项甲\nB. 选项乙\n"
                 "2. 示例题干二？\nA. 选项丙")
        blocks = build_queries(multi)
        record("OCR 分块", PASS if len(blocks) >= 2 else WARN,
               f"切成 {len(blocks)} 块")
    except Exception as exc:
        record("OCR 分块", FAIL, str(exc))


def check_capture() -> None:
    print("\n=== 6. 屏幕采集 ===")
    try:
        from core.config import logs_dir
        from core.screen import ScreenCapture, frame_diff
        import numpy as np

        sc = ScreenCapture()
        monitors = sc.monitor_count()
        frame = sc.grab((0, 0, 320, 180))
        assert frame.shape == (180, 320, 3), f"尺寸异常 {frame.shape}"
        out = logs_dir() / "selfcheck_capture.png"
        try:
            import cv2

            cv2.imwrite(str(out), frame)
            detail = f"{monitors} 个显示器，抓取 {frame.shape[1]}x{frame.shape[0]}，样本 {out.name}"
        except Exception:
            detail = f"{monitors} 个显示器，抓取 {frame.shape[1]}x{frame.shape[0]}"
        record("截屏", PASS, detail)

        diff_same = frame_diff(frame, frame)
        record("帧差检测", PASS if diff_same == 0 else WARN,
               f"同帧差异={diff_same}")
        sc.close()
    except Exception as exc:
        record("截屏", FAIL, f"{type(exc).__name__}: {exc}")


def check_capture_frame() -> None:
    """常驻识别框：尺寸夹紧、透明度/边框范围、显隐状态、区域回调。"""
    print("\n=== 7. 常驻识别框 ===")
    try:
        import tkinter as tk

        from core.screen import enable_dpi_awareness
        from ui.capture_frame import CaptureFrame
        from ui.theme import Theme

        enable_dpi_awareness()
        root = tk.Tk()
        root.withdraw()
        theme = Theme(root, mode="light", scale=1.0)
        theme.apply()

        events: List[Tuple[Tuple[int, int, int, int], bool]] = []
        closed: List[bool] = []
        frame = CaptureFrame(
            root, theme,
            {"alpha": 0.75, "border": 10, "region": (200, 200, 520, 180)},
            on_region_change=lambda region, final: events.append((tuple(region), final)),
            on_close=lambda: closed.append(True))

        frame.set_region((0, 0, 10, 5))
        x, y, w, h = frame.region
        assert w >= CaptureFrame.MIN_W and h >= CaptureFrame.MIN_H, \
            f"最小尺寸未生效：{frame.region}"
        assert x >= 0 and y >= 0, f"未夹在屏幕内：{frame.region}"
        record("尺寸约束", PASS, f"10x5 -> {w}x{h} @({x},{y})")

        assert abs(frame.set_alpha(0.5) - 0.5) < 1e-6
        assert abs(frame.set_alpha(9.0) - 1.0) < 1e-6
        assert abs(frame.set_alpha(-1.0) - 0.15) < 1e-6
        frame.set_alpha(0.75)
        record("透明度范围", PASS, "越界自动夹紧到 0.15 ~ 1.0")

        assert frame.set_border(999) == 30 and frame.set_border(1) == 4
        frame.set_border(10)
        record("边框粗细", PASS, "越界自动夹紧到 4 ~ 30 px")

        frame.show()
        assert frame.visible, "show() 后不可见"
        for state in ("running", "paused", "idle"):
            frame.set_state(state)
        frame.hide()
        assert not frame.visible, "hide() 后仍可见"
        record("显隐与状态", PASS, "idle / running / paused 三态配色切换正常")

        frame.set_region((300, 300, 400, 120), notify=True, final=True)
        assert events and events[-1] == ((300, 300, 400, 120), True), f"回调异常 {events}"
        record("区域回调", PASS, f"收到 {len(events)} 次事件，末次 final=True 可触发立即识别")

        # 顶栏 ✕：有 on_close 时交给主进程（此处不自行隐藏），没有时退化为隐藏
        frame.show()
        frame._on_close_press()
        assert closed == [True], f"✕ 未回调 {closed}"
        assert frame.visible, "✕ 交给了主进程，不应自行隐藏"
        frame._on_close = None
        frame._on_close_press()
        assert not frame.visible, "无 on_close 时 ✕ 应退化为隐藏"
        record("顶栏关闭按钮", PASS, "有回调时交主进程处理，无回调时退化为隐藏")

        frame.destroy()
        try:
            root.destroy()
        except Exception:
            pass
    except Exception as exc:
        record("常驻识别框", FAIL, f"{type(exc).__name__}: {exc}")


def _make_test_image() -> Tuple[Path, List[str]]:
    """合成一张中文题目图片，作为 OCR 端到端测试输入。

    本地有示例题库时优先渲染题库里的题目（识别结果才能接着送进匹配器），
    没有就用与题库无关的占位文本 —— **仓库里不出现任何题目内容**。
    返回 (图片路径, 期望能被识别到的字符列表)。
    """
    from PIL import Image, ImageDraw, ImageFont

    from core.config import logs_dir, user_data_dir
    from core.question_bank import load_bank

    lines: List[str] = []
    sample = user_data_dir() / "samples" / "sample_bank.json"
    if sample.exists():
        try:
            bank = load_bank(sample)
            q = max(bank.questions, key=lambda x: len(x.question or ""))
            lines = [f"1. {q.question.strip()}"]
            opts = list(q.options)
            for i in range(0, len(opts), 2):
                lines.append("    ".join(opts[i:i + 2]))
        except Exception:
            lines = []
    if not lines:
        lines = [
            "1. 这是一道自检用示例题目？",
            "A. 示例选项甲    B. 示例选项乙",
            "C. 示例选项丙    D. 示例选项丁",
        ]

    font = None
    for candidate in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc"):
        path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / candidate
        if path.exists():
            try:
                font = ImageFont.truetype(str(path), 26)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()

    width = max(760, max((len(line) for line in lines), default=20) * 20)
    img = Image.new("RGB", (width, 46 * len(lines) + 24), "white")
    draw = ImageDraw.Draw(img)
    y = 18
    for line in lines:
        draw.text((20, y), line, fill="black", font=font)
        y += 46

    out = logs_dir() / "selfcheck_ocr_input.png"
    img.save(out)

    # 期望关键词不写死题目内容：取渲染文本开头的实义字符
    probe = [ch for ch in lines[0] if not ch.isspace()][:10]
    return out, probe


def check_ocr() -> None:
    print("\n=== 8. OCR 端到端 ===")
    try:
        from core.ocr_engine import create_engine, preprocess, detect_available

        available = detect_available()
        record("引擎探测", PASS if available else FAIL,
               "可用：" + "、".join(available) if available else "未检测到任何 OCR 引擎")

        img_path, expect_chars = _make_test_image()
        import cv2

        img = cv2.imread(str(img_path))

        # 逐个引擎单独验证，便于定位到底是谁挂了
        for name in ("paddle", "rapid"):
            eng = create_engine(name, strict=True)
            if not eng.available():
                detail = getattr(eng, "error", "") or "初始化失败"
                record(f"引擎 {name}", WARN if name == "rapid" else FAIL, str(detail)[:120])
                continue
            r = eng.recognize(preprocess(img, scale=1.5, grayscale=True, sharpen=True))
            if r.ok:
                record(f"引擎 {name}", PASS,
                       f"{eng.display_name} | {r.text.replace(chr(10), ' / ')[:90]}")
            else:
                record(f"引擎 {name}", FAIL, f"{eng.display_name} | {r.error[:110]}")

        # 自动选择的引擎跑完整链路
        engine = create_engine("auto")
        if not engine.available():
            record("自动选择引擎", FAIL,
                   "没有引擎通过预热推理。PaddleOCR 若报 oneDNN 相关错误，"
                   "可在控制面板关闭「启用 oneDNN」，或改用 RapidOCR")
            return

        result = engine.recognize(preprocess(img, scale=1.5, grayscale=True, sharpen=True))
        if not result.ok:
            record("自动选择引擎", FAIL, result.error[:150])
            return

        keys = expect_chars
        hit = sum(1 for k in keys if k in result.text)
        need = max(3, len(keys) // 2)
        record("自动选择引擎", PASS if hit >= need else WARN,
               f"{result.engine} | 字符命中 {hit}/{len(keys)}")

        # 识别结果直接进匹配器，验证整条链路（需要本地示例题库）
        from core.config import user_data_dir
        from core.matcher import Matcher
        from core.question_bank import load_bank

        sample = user_data_dir() / "samples" / "sample_bank.json"
        if not sample.exists():
            record("链路 识别→匹配", WARN,
                   "识别成功；本地没有示例题目，跳过匹配环节")
        else:
            bank = load_bank(sample)
            hits = Matcher(bank, threshold=0.5).search(result.text)
            if hits:
                record("链路 识别→匹配", PASS,
                       f"{hits[0].score * 100:.1f}% → 答案 "
                       f"{hits[0].question.answer_letters}")
            else:
                record("链路 识别→匹配", WARN, "识别成功但未命中题库")
    except Exception as exc:
        record("OCR 识别", FAIL, f"{type(exc).__name__}: {exc}")


def check_processes() -> None:
    print("\n=== 9. 三进程冒烟测试 ===")
    import multiprocessing as mp

    try:
        from core.config import logs_dir, user_data_dir
        from workers.capture_worker import capture_main
        from workers.search_worker import search_main
        from workers.ipc import build_ipc

        ctx = mp.get_context("spawn")
        ipc = build_ipc(ctx)

        # 冒烟测试只需要一个能加载成功的题库路径：优先示例题库，
        # 仓库克隆（无本地题目）时退回自带模板。
        sample = user_data_dir() / "samples" / "sample_bank.json"
        if not sample.exists():
            sample = ROOT / "templates" / "bank_template.json"

        worker_cfg = {
            "log_dir": str(logs_dir()),
            "bank_path": str(sample),
            "ocr_engine": "none",          # 冒烟测试不加载模型，避免拖慢
            "ocr_lang": "ch",
            "ocr_use_gpu": False,
            "preprocess": {"scale": 1.0},
            "min_question_len": 6,
        }
        # 在虚拟桌面左上角取一块有效区域
        from core.screen import ScreenCapture

        sc = ScreenCapture()
        left, top, w, h = sc.virtual_bounds()
        sc.close()
        for i, v in enumerate([left + 20, top + 20, 300, 160]):
            ipc["region"][i] = v

        procs = [
            ctx.Process(target=capture_main, args=(worker_cfg, ipc), name="cap", daemon=True),
            ctx.Process(target=search_main, args=(worker_cfg, ipc), name="search", daemon=True),
        ]
        for p in procs:
            p.start()
        record("进程启动", PASS, "capture / search 已拉起，pid=" +
               ",".join(str(p.pid) for p in procs))

        ipc["running"].set()
        ipc["force"].set()
        time.sleep(6.0)

        alive = [p.name for p in procs if p.is_alive()]
        if len(alive) == len(procs):
            record("进程存活", PASS, "两个工作进程稳定运行中")
        else:
            record("进程存活", FAIL, f"存活 {alive}，异常退出")

        # 检查是否真的产出了帧
        stats = 0
        try:
            while True:
                ipc["stat_q"].get_nowait()
                stats += 1
        except Exception:
            pass
        record("进程通信", PASS if stats else WARN,
               f"收到 {stats} 条状态消息（截屏进程已开始工作）" if stats else "未收到状态消息")

        ipc["stop"].set()
        for p in procs:
            p.join(timeout=3)
            if p.is_alive():
                p.terminate()
        record("进程退出", PASS, "已正常回收")
    except Exception as exc:
        record("三进程测试", FAIL, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- 入口

def main() -> int:
    parser = argparse.ArgumentParser(description="答题助手自检")
    parser.add_argument("--quick", action="store_true", help="跳过 OCR 与进程测试")
    args = parser.parse_args()

    print("=" * 66)
    print(" 答题助手 · 自检")
    print("=" * 66)

    check_env()
    check_imports()
    check_optional_deps()
    out_dir = check_bank() or Path(".")
    check_matcher(out_dir)
    check_capture()
    check_capture_frame()
    if not args.quick:
        check_ocr()
        check_processes()

    fails = [r for r in _results if r[1] == FAIL]
    warns = [r for r in _results if r[1] == WARN]
    print("\n" + "=" * 66)
    print(f" 结果：{len(_results) - len(fails) - len(warns)} 通过 / "
          f"{len(warns)} 警告 / {len(fails)} 失败")
    for name, status, detail in fails + warns:
        print(f"   · [{status}] {name}: {detail.splitlines()[0] if detail else ''}")
    print("=" * 66)

    if fails:
        print("\n存在失败项。若失败集中在 OCR 相关，请先执行：")
        print("    .venv\\Scripts\\python.exe -m pip install paddlepaddle==3.3.1 paddleocr")
        print("  首次运行需要联网下载约 20MB 的 PP-OCR 模型。")
    else:
        print("\n全部关键检查通过，可以运行 run.bat 启动程序。")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
