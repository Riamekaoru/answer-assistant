# -*- coding: utf-8 -*-
"""题库导入与持久化。

支持格式：
    .json / .jsonl   结构化题库（题目/答案/选项/解析）
    .csv / .tsv      表头自动识别（题目、答案、选项A-D、解析、题型 ...）
    .xlsx / .xlsm    同上，走 openpyxl；兼容常见考试模板的列名
    .xls             需本机安装 xlrd
    .docx / .doc     Word 题库。**按文件头分流**：OOXML 走 python-docx，
                     OLE2 走 core.legacy_doc（Word 97-2003 Piece Table）。
                     两者扩展名经常对不上（.doc 改名成 .docx 很常见），
                     所以由 from_word 嗅探决定，不看后缀。
    .txt / .md       与 Word 相同的块解析器

段落式题库可识别的标记写法（docx / doc / txt / md 通用）：
    题号      `1、` `1.` `第1题` `(1)`
    选项      `A、xx` `A. xx` `（A）xx` `A：xx`
    答案      `答案：A` `参考答案 ABC` `【答案】B` `【答案】正确`
    解析      `解析：xx` `【解析】xx` `【答案解析】xx`，也支持与答案同一行
    题型      `题型：多选题` `【题型】判断题`（分组标题，作用于其后各题）

统一输出 Question 对象，题库对象负责去重与统计。
"""

from __future__ import annotations

import csv
import json
import logging
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from . import legacy_doc
from . import text_utils as tu

log = logging.getLogger("answerassist.bank")

# ---------------------------------------------------------------- 常量

# 严格题号识别：必须带明确分隔符，避免把 "2020年" 误判为新题
_STRICT_NUM_RE = re.compile(
    r"^\s*(?:第\s*)?(\d{1,4})\s*(?:题|小题)\s*[.、．,，)）:：]?\s*"      # 第1题 / 1题
    r"|^\s*[（(\[【]\s*(\d{1,4})\s*[）)\]】]\s*"                          # (1)
    r"|^\s*(\d{1,4})\s*[.、．)）]\s*"                                     # 1. / 1、
)

# 题干里出现这些词通常意味着答案紧随其后
_INLINE_ANSWER_RE = re.compile(r"答\s*案\s*[是为:：]\s*([^\s，。；]+)")

# 文档标题 / 说明性段落的关键词。
# 这类段落既没有选项也没有答案，如果不过滤就会被解析成一道空题。
# 例："××× 练习题库"、"答案与解析"、"考试说明"
_TITLE_HINT_RE = re.compile(
    r"(题库|试题|试卷|练习|习题|考题|考试|测验|模拟|复习|大纲|提纲|"
    r"目录|说明|注意事项|评分标准|分值分布|答案\s*与?\s*解析)"
)
_QUESTION_NUM_START_RE = re.compile(r"^\s*(第\s*)?\d{1,4}\s*(题|小题)\s*[.、．,，)）:：]")

_Q_KEYS = ("题目", "问题", "题干", "题目内容", "question", "q", "title", "content",
           "question_text", "题目文本")
_A_KEYS = ("答案", "正确答案", "参考答案", "answer", "a", "correct", "correct_answer",
           "标准答案")
_ANALYSIS_KEYS = ("解析", "答案解析", "分析", "说明", "analysis", "explain", "explanation",
                  "解析内容")
_TYPE_KEYS = ("题型", "类型", "题目类型", "type", "qtype", "question_type")
_OPTION_KEYS = ("选项", "选项内容", "options", "option", "choices")
_OPTION_COL_ONLY = ("答案乱序", "阅卷人", "阅卷类型", "难度设置", "分值", "序号", "编号")

_ENC_CANDIDATES = ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5", "latin-1")

# "A. xxx" / "A、xxx" / "(A) xxx" 拆选项用
_OPT_SPLIT_RE = re.compile(r"(?:^|(?<=[\s|;；\n]))\s*[（(\[【]?\s*([A-Ha-h])\s*[）)\]】]?"
                           r"\s*[.、．,，:：)）]\s*")


# ---------------------------------------------------------------- 数据模型

@dataclass
class Question:
    qid: str = ""
    question: str = ""
    answer: str = ""
    answer_letters: str = ""
    options: List[str] = field(default_factory=list)
    qtype: str = ""
    analysis: str = ""
    source: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("extra", None)
        return d

    @property
    def answer_display(self) -> str:
        """把答案渲染成可读文本；选择题尽量带上选项内容。"""
        letters = self.answer_letters
        if letters and self.options:
            picked: List[str] = []
            for ch in letters:
                for opt in self.options:
                    if opt[:1].upper() == ch:
                        picked.append(opt.strip())
                        break
                else:
                    picked.append(ch)
            return "\n".join(picked) if picked else (self.answer or letters)
        return self.answer or letters

    @property
    def search_text(self) -> str:
        """用于匹配的完整文本：题干 + 选项。"""
        parts = [self.question]
        if self.options:
            parts.extend(self.options)
        return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------- 解析辅助

def _pick(row: Dict[str, Any], keys: Sequence[str], loose: bool = True) -> str:
    """从一行数据里按候选列名取值（大小写与空白不敏感）。

    loose=False 时只做精确列名匹配。单字母列探测（A / B / C / D）必须用它：
    否则宽松包含匹配会让 "a" 命中 "answer"、"analysis" 之类的列，
    把答案或解析内容当成选项正文。
    """
    norm = {str(k).strip().lower().replace(" ", ""): v for k, v in row.items() if k is not None}
    for key in keys:
        k = key.strip().lower().replace(" ", "")
        if k in norm:
            v = norm[k]
            if v is None:
                return ""
            if isinstance(v, float) and v == int(v):
                return str(int(v))
            return str(v).strip()
    if not loose:
        return ""
    # 再做一次宽松包含匹配
    for key in keys:
        k = key.strip().lower()
        for rk, rv in norm.items():
            if k and k in rk:
                if rv is None:
                    return ""
                if isinstance(rv, float) and rv == int(rv):
                    return str(int(rv))
                return str(rv).strip()
    return ""


def _split_options(raw: str, row: Optional[Dict[str, Any]] = None) -> List[str]:
    """从「选项」列文本还原选项列表。"""
    opts: List[str] = []

    # 优先使用独立的 A/B/C/D 列（必须精确匹配列名，避免误取到 answer 等列）
    if row:
        for letter in "ABCDEFGH":
            val = _pick(row, (letter, f"选项{letter}", f"option_{letter.lower()}"), loose=False)
            if val:
                opts.append(f"{letter}. {val}")

    if opts:
        return opts
    if not raw:
        return []

    text = str(raw).replace("\r", "\n")
    # 用 "A." / "(A)" 这类标记切分。
    # 注意：_OPT_SPLIT_RE 里带捕获组，re.split 会把捕获到的字母一并插进结果，
    # 必须用 finditer 手工切段，不能拿 split 的结果去 zip。
    marks = list(_OPT_SPLIT_RE.finditer(text))
    if len(marks) >= 2:
        for idx, m in enumerate(marks):
            end = marks[idx + 1].start() if idx + 1 < len(marks) else len(text)
            body = text[m.end():end].strip(" \n\t|;；")
            if body:
                opts.append(f"{m.group(1).upper()}. {body}")
        if opts:
            return opts

    # 退化：按换行 / 竖线 / 分号切
    chunks = [c.strip() for c in re.split(r"[\n|;；]+", text) if c.strip()]
    for idx, chunk in enumerate(chunks):
        if idx >= 8:
            break
        m = tu.parse_option_line(chunk)
        if m:
            opts.append(f"{m[0]}. {m[1]}")
        else:
            opts.append(f"{'ABCDEFGH'[idx]}. {chunk}")
    return opts


def _build_question(question: str, answer: str, options: List[str], qtype: str,
                    analysis: str, source: str, extra: Optional[Dict[str, Any]] = None,
                    qid: str = "") -> Optional[Question]:
    question = (question or "").strip()
    if len(question) < 2:
        return None
    letters = tu.extract_answer_letters(answer)
    qtype = tu.normalize_qtype(qtype)
    if not qtype:
        qtype = tu.guess_type(question, answer, options)
    return Question(
        qid=qid or f"q{abs(hash(tu.similarity_key(question))) % (10 ** 10):010d}",
        question=question,
        answer=(answer or "").strip(),
        answer_letters=letters,
        options=options or [],
        qtype=qtype,
        analysis=(analysis or "").strip(),
        source=source,
        extra=extra or {},
    )


# ---------------------------------------------------------------- 块解析器

def _looks_like_heading(text: str, leading: bool) -> bool:
    """判断一个「没有选项、也没有答案/解析」的块是文档标题还是真题目。

    过严会漏掉真正无答案的题，过松会多出空题。这里用一组保守信号：
    真题目通常带问号或以题号开头，而标题通常命中 _TITLE_HINT_RE，
    或者是文档的第一个单行段落（练习册的标题行）。
    """
    t = text.strip()
    if not t or len(t) > 40:
        return False
    if re.search(r"[？?]", t):                          # 真题目基本都带问号
        return False
    if _QUESTION_NUM_START_RE.match(t):                 # 明显是"第1题/1题"开头
        return False
    if _TITLE_HINT_RE.search(t):
        return True
    return bool(leading and "\n" not in t)


def parse_blocks(lines: Iterable[str], source: str = "") -> List[Question]:
    """段落式题库解析器（docx / txt / md 共用）。

    识别规则：
        遇到带明确题号的行 -> 开启新题
        "A. xx" 形式        -> 追加选项
        "答案：xx"          -> 记录答案
        "解析：xx"          -> 记录解析
        其余                 -> 追加到题干

    没有选项也没有答案/解析、且看起来像标题的段落会被丢弃
    （否则练习册的标题行会变成一道空题）。
    """
    questions: List[Question] = []
    q_lines: List[str] = []
    options: List[str] = []
    answer = ""
    analysis = ""
    qtype = ""

    def flush() -> None:
        nonlocal q_lines, options, answer, analysis, qtype
        text = "\n".join(q_lines).strip()
        if text and not (options or answer or analysis) \
                and _looks_like_heading(text, not questions):
            log.debug("跳过疑似标题段落: %s", text[:40])
            text = ""
        if text:
            q = _build_question(text, answer, options, qtype,
                                analysis, source)
            if q:
                questions.append(q)
        q_lines, options, answer, analysis, qtype = [], [], "", "", ""

    pending_number_only = False

    for raw_line in lines:
        line = (raw_line or "").strip()
        if not line:
            continue

        m_ans = tu.parse_answer_line(line)
        m_ana = tu.parse_analysis_line(line)
        m_typ = tu.parse_type_line(line)
        m_opt = tu.parse_option_line(line)

        if m_typ and len(m_typ) <= 12:
            # 题型标记有两种排版约定，靠「当前题是否已经收齐答案/解析」区分：
            #   A) 写在题内：题干 / 选项 / 题型：单选 / 答案：A / 解析：xx
            #      -> 此时当前题还没有答案，标记就是当前题的题型
            #   B) 写成分组标题：… / 【答案】B / 【题型】多选题 / 16、下一题…
            #      -> 此时当前题已经有答案，标记属于下一题，必须先把当前题收尾，
            #         否则上一题会被贴上下一组的题型
            if answer or analysis:
                flush()
            qtype = m_typ
            continue
        if m_ana is not None:
            analysis = (analysis + " " + m_ana).strip() if analysis else m_ana
            continue
        if m_ans is not None:
            # "答案：A 解析：xxx" / "【答案】B【解析】xxx" 这类同行写法
            body, inline_ana = tu.split_inline_analysis(m_ans)
            if inline_ana:
                analysis = (analysis + " " + inline_ana).strip() if analysis else inline_ana
            answer = body
            continue

        # 新题起始判定
        if _STRICT_NUM_RE.match(line) and (q_lines or options or answer):
            flush()
            pending_number_only = True
            stripped = _STRICT_NUM_RE.sub("", line, count=1).strip()
            if stripped:
                q_lines.append(stripped)
            continue
        if _STRICT_NUM_RE.match(line) and not q_lines:
            stripped = _STRICT_NUM_RE.sub("", line, count=1).strip()
            pending_number_only = bool(not stripped)
            if stripped:
                q_lines.append(stripped)
            continue

        if m_opt is not None and (q_lines or options):
            options.append(f"{m_opt[0]}. {m_opt[1]}")
            continue

        inline = _INLINE_ANSWER_RE.search(line)
        if inline and not q_lines:
            continue

        q_lines.append(line)
        pending_number_only = False

    flush()
    _ = pending_number_only
    return questions


def _parse_table_rows(rows: Sequence[Dict[str, Any]], source: str) -> List[Question]:
    """表格型（csv/xlsx）解析：按表头映射列。"""
    out: List[Question] = []
    for row in rows:
        question = _pick(row, _Q_KEYS)
        answer = _pick(row, _A_KEYS)
        options = _split_options(_pick(row, _OPTION_KEYS), row)
        analysis = _pick(row, _ANALYSIS_KEYS)
        qtype = _pick(row, _TYPE_KEYS)
        if not question:
            # 有些题库把题干放在序号列之后，尝试拼接非空单元格
            vals = [str(v) for k, v in row.items()
                    if v is not None and str(v).strip() and k not in _OPTION_COL_ONLY]
            question = " ".join(vals[:1])
        extra = {k: v for k, v in row.items()
                 if k and str(k).strip() in _OPTION_COL_ONLY and v is not None}
        q = _build_question(question, answer, options, qtype, analysis, source, extra)
        if q:
            out.append(q)
    return out


# ---------------------------------------------------------------- 各格式读取

def _read_text_with_fallback(path: Path) -> str:
    last_exc: Optional[Exception] = None
    for enc in _ENC_CANDIDATES:
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError as exc:
            last_exc = exc
        except Exception as exc:
            last_exc = exc
            break
    if last_exc:
        raise last_exc
    return ""


def from_json(path: Path) -> List[Question]:
    text = _read_text_with_fallback(path)
    text = text.lstrip("\ufeff").strip()
    if not text:
        return []

    records: List[Any] = []
    if text[0] in "[{":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, list):
            records = data
        elif isinstance(data, dict):
            for key in ("questions", "data", "list", "items", "题库", "题目"):
                if isinstance(data.get(key), list):
                    records = data[key]
                    break
            else:
                # { "题目": "答案" } 形式
                records = [{"question": k, "answer": v} for k, v in data.items()]
    else:
        # jsonl
        for line in text.splitlines():
            line = line.strip().rstrip(",")
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    out: List[Question] = []
    for rec in records:
        if isinstance(rec, str):
            out.extend(parse_blocks(rec.splitlines(), path.name))
            continue
        if not isinstance(rec, dict):
            continue
        question = _pick(rec, _Q_KEYS)
        answer = _pick(rec, _A_KEYS)
        options = rec.get("options") or rec.get("选项") or []
        if isinstance(options, str):
            options = _split_options(options, rec)
        elif isinstance(options, list):
            norm_opts: List[str] = []
            for i, opt in enumerate(options):
                s = str(opt).strip()
                if not s:
                    continue
                if tu.parse_option_line(s):
                    norm_opts.append(s)
                else:
                    norm_opts.append(f"{'ABCDEFGH'[i]}. {s}")
            options = norm_opts
        else:
            options = []
        if not question:
            continue
        q = _build_question(question, answer, options, _pick(rec, _TYPE_KEYS),
                            _pick(rec, _ANALYSIS_KEYS), path.name)
        if q:
            out.append(q)
    return out


def from_csv(path: Path) -> List[Question]:
    text = _read_text_with_fallback(path)
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        delim = dialect.delimiter
    except Exception:
        delim = "\t" if path.suffix.lower() == ".tsv" else ","

    reader = csv.DictReader(text.splitlines(), delimiter=delim)
    rows = [dict(r) for r in reader]
    return _parse_table_rows(rows, path.name)


def from_excel(path: Path) -> List[Question]:
    from openpyxl import load_workbook

    wb = load_workbook(filename=str(path), read_only=True, data_only=True)
    out: List[Question] = []
    try:
        for ws in wb.worksheets:
            rows_iter = ws.iter_rows(values_only=True)
            try:
                header = next(rows_iter)
            except StopIteration:
                continue
            header = ["" if h is None else str(h).strip() for h in header]
            if not any(header):
                continue
            for values in rows_iter:
                if values is None or all(v is None or str(v).strip() == "" for v in values):
                    continue
                row = {header[i] if i < len(header) and header[i] else f"col{i}": values[i]
                       for i in range(len(values))}
                out.extend(_parse_table_rows([row], path.name))
    finally:
        try:
            wb.close()
        except Exception:
            pass
    return out


def from_docx(path: Path) -> List[Question]:
    # 中文办公环境里「.doc 另存后扩展名还写着 .docx」非常常见。
    # python-docx 对这种文件只会抛 ValueError，看不出所以然，所以要按文件头分流。
    if legacy_doc.is_ole2(path):
        log.info("%s 扩展名是 .docx，但文件头是 OLE2 复合文档，按 Word 97-2003 解析",
                 path.name)
        return from_doc(path)

    from docx import Document

    doc = Document(str(path))
    out: List[Question] = []

    # 1) 表格优先：两列以上且含"题目/答案"表头
    for table in doc.tables:
        try:
            rows = [[(c.text or "").strip() for c in r.cells] for r in table.rows]
        except Exception:
            continue
        if len(rows) < 2:
            continue
        header = [h.lower() for h in rows[0]]
        joined = "".join(header)
        if any(k in joined for k in ("题目", "题干", "问题", "question")):
            dicts = [{rows[0][i] or f"col{i}": row[i] for i in range(len(row))}
                     for row in rows[1:]]
            out.extend(_parse_table_rows(dicts, path.name))
        elif len(rows[0]) >= 2:
            dicts = [{"题目": row[0], "答案": row[1],
                      "选项": row[2] if len(row) > 2 else ""} for row in rows[1:]]
            out.extend(_parse_table_rows(dicts, path.name))

    # 2) 正文段落
    lines: List[str] = []
    for para in doc.paragraphs:
        t = (para.text or "").strip()
        if t:
            lines.append(t)
    out.extend(parse_blocks(lines, path.name))
    return out


def from_word(path: Path) -> List[Question]:
    """Word 文档统一入口：按**文件头**判断，不信任扩展名。

    OLE2(D0CF11E0...) -> Word 97-2003（.doc）
    ZIP(PK...)        -> OOXML（.docx）

    为什么必须看内容：用户拿到的题库经常是从别人那里拷来的，
    扩展名和真实格式对不上（改过后缀名、另存为时选错类型等）。
    """
    if legacy_doc.is_ole2(path):
        return from_doc(path)
    return from_docx(path)


def from_doc(path: Path) -> List[Question]:
    """Word 97-2003（.doc）段落式题库。

    纯 Python 解析（``core.legacy_doc``，走 Word 的 Piece Table）；
    解析失败且本机装了 Word / WPS 时，退回 COM 转换。
    """
    lines: Optional[List[str]] = None
    error: Optional[Exception] = None

    try:
        lines = legacy_doc.extract_paragraphs(path)
    except Exception as exc:                     # 结构异常 / 缺 olefile
        error = exc
        log.warning("纯 Python 解析 %s 失败：%s，尝试 COM 兜底", path.name, exc)

    if not lines:
        text = legacy_doc.read_doc_via_word_com(path)
        if text:
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    if not lines:
        raise ValueError(
            f"无法读取 .doc 文件 {path.name}：{error or '未解析出任何正文'}。"
            "请在 Word / WPS 中另存为 .docx 后重试。"
        )

    return parse_blocks(lines, path.name)


def from_text(path: Path) -> List[Question]:
    text = _read_text_with_fallback(path)
    return parse_blocks(text.splitlines(), path.name)


_LOADERS = {
    ".json": from_json, ".jsonl": from_json, ".js": from_json,
    ".csv": from_csv, ".tsv": from_csv,
    ".xlsx": from_excel, ".xlsm": from_excel,
    # .docx / .doc 都走 from_word：真实格式靠文件头判定，不信任扩展名
    ".docx": from_word, ".doc": from_word,
    ".txt": from_text, ".md": from_text, ".markdown": from_text,
}

SUPPORTED_SUFFIXES = tuple(sorted(_LOADERS.keys()))


# ---------------------------------------------------------------- 题库

class QuestionBank:
    """内存题库：去重、统计、持久化。"""

    def __init__(self) -> None:
        self.questions: List[Question] = []
        self._keys: Dict[str, int] = {}
        self.source_file: str = ""

    # -- 构建 ---------------------------------------------------------

    def add(self, q: Question) -> bool:
        key = tu.similarity_key(q.question)
        if len(key) < 4:
            return False
        if key in self._keys:
            # 题干重复：补全缺失字段
            existing = self.questions[self._keys[key]]
            for attr in ("answer", "analysis", "qtype"):
                if not getattr(existing, attr) and getattr(q, attr):
                    setattr(existing, attr, getattr(q, attr))
            if not existing.options and q.options:
                existing.options = q.options
            if not existing.answer_letters and q.answer_letters:
                existing.answer_letters = q.answer_letters
            return False
        self._keys[key] = len(self.questions)
        self.questions.append(q)
        return True

    def extend(self, items: Iterable[Question]) -> int:
        added = 0
        for q in items:
            if self.add(q):
                added += 1
        return added

    def clear(self) -> None:
        self.questions.clear()
        self._keys.clear()
        self.source_file = ""

    def __len__(self) -> int:
        return len(self.questions)

    # -- 导入 ---------------------------------------------------------

    def load_file(self, path: str | Path, merge: bool = True) -> int:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"题库文件不存在: {p}")
        suffix = p.suffix.lower()
        loader = _LOADERS.get(suffix)
        if loader is None:
            raise ValueError(
                f"不支持的题库格式 {suffix or '(无扩展名)'}；"
                f"支持: {', '.join(SUPPORTED_SUFFIXES)}"
            )
        if suffix == ".xls":
            raise ValueError("旧版 .xls 需先另存为 .xlsx")

        if not merge:
            self.clear()
        items = loader(p)
        added = self.extend(items)
        self.source_file = str(p)
        log.info("题库导入: %s -> 解析 %d 条, 新增 %d 条, 当前共 %d 条",
                 p.name, len(items), added, len(self))
        return added

    # -- 持久化 -------------------------------------------------------

    def save_json(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "count": len(self),
            "source": self.source_file,
            "questions": [q.to_dict() for q in self.questions],
        }
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def stats(self) -> Dict[str, Any]:
        types: Dict[str, int] = {}
        with_answer = 0
        with_analysis = 0
        for q in self.questions:
            types[q.qtype or "未分类"] = types.get(q.qtype or "未分类", 0) + 1
            if q.answer or q.answer_letters:
                with_answer += 1
            if q.analysis:
                with_analysis += 1
        return {
            "total": len(self),
            "with_answer": with_answer,
            "with_analysis": with_analysis,
            "types": types,
            "source": self.source_file,
        }


def load_bank(path: str | Path) -> QuestionBank:
    """便捷函数：新建题库并导入。"""
    bank = QuestionBank()
    bank.load_file(path)
    return bank
