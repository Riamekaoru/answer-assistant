# -*- coding: utf-8 -*-
"""文本归一化与清洗工具。

答题助手面对的最大噪声来自 OCR：全角半角混用、多余空白、标点丢失/错认、
行内换行、题号前缀残留、选项标记格式不一。这里集中处理这些问题。
"""

from __future__ import annotations

import re
import unicodedata
from typing import List, Tuple

# ---------------------------------------------------------------- 常量

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# 题号前缀： "1." "1、" "第3题" "（2）" "12)"
_NUM_PREFIX_RE = re.compile(
    r"^\s*(?:第\s*)?(\d{1,4})\s*(?:题|小题)?\s*[.、．,，)）:：>\-—]?\s*"
)

# 选项行： "A. xxx" "A、xxx" "（A）xxx" "A) xxx" "A：xxx"
_OPTION_LINE_RE = re.compile(
    r"^\s*[（(\[【]?\s*([A-Ha-h])\s*[）)\]】]?\s*[.、．,，:：)）]\s*(.*)$"
)

# 标记两侧可能带的括号：【答案】 / [答案] / （答案） / 〔题型〕 ...
_LB = r"[【\[（(〔]?"
_RB = r"[】\]）)〕]?"

# 答案行： "答案：A" "正确答案 ABC" "参考答案：A、C" "【答案】B"
_ANSWER_LINE_RE = re.compile(
    rf"^\s*{_LB}\s*(?:正确|参考|标准)?\s*答\s*案\s*{_RB}\s*[是为:：]?\s*(.+)$"
)

# 解析行： "解析：..." "分析：..." "【解析】..." "【答案解析】..."
#
# 注意："答案" 不能单独作为候选词。正则里的候选是"前缀锚定"的，
# 一旦列了裸 "答案"，"【答案】B" 会先被 _ANALYSIS_LINE_RE 命中
# （捕获组拿到 "B"），于是题目只留下解析、答案为空。
# 带"答案"的写法必须整体是"答案解析 / 答案及解析"这类复合词。
_ANALYSIS_LINE_RE = re.compile(
    rf"^\s*{_LB}\s*(?:答案\s*解析|答案及解析|解析|分析|说明)\s*{_RB}\s*[是为:：]?\s*(.*)$"
)

# 行内的解析标记（用于 "【答案】B【解析】xxx" 这种把答案和解析写在一行的写法）
_ANALYSIS_INLINE_RE = re.compile(
    rf"{_LB}\s*(?:答案\s*解析|答案及解析|解析|分析|说明)\s*{_RB}\s*[是为:：]?\s*"
)

# 题型别名 -> 标准名。顺序敏感："多选" 必须排在 "单选" 之前。
_TYPE_ALIASES = (
    ("多选", "多选题"), ("不定项", "多选题"),
    ("单选", "单选题"),
    ("判断", "判断题"),
    ("填空", "填空题"),
    ("简答", "简答题"), ("问答", "简答题"), ("论述", "简答题"),
)

# 题型行："题型：单选" "【题型】单选题" "类型 多选题"
_TYPE_LINE_RE = re.compile(
    rf"^\s*{_LB}\s*(?:题\s*型|类型)\s*{_RB}\s*[是为:：]?\s*(.+)$"
)

_ANSWER_TOKEN_RE = re.compile(r"[A-Ha-h]")

# 匹配用的标点/符号集合（归一化时一律剔除）
_PUNCT_TABLE = str.maketrans({
    "，": ",", "。": ".", "、": ",", "；": ";", "：": ":",
    "？": "?", "！": "!", "（": "(", "）": ")", "【": "[", "】": "]",
    "“": '"', "”": '"', "‘": "'", "’": "'", "《": "<", "》": ">",
    "\u3000": " ", "\u00a0": " ", "\u200b": "",
})


# ---------------------------------------------------------------- 归一化

def to_halfwidth(text: str) -> str:
    """全角转半角（NFKC），并统一常见中文标点。"""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    return t.translate(_PUNCT_TABLE)


def normalize(text: str, keep_punct: bool = False) -> str:
    """归一化为「比对串」。

    keep_punct=False（默认）：仅保留字母、数字、汉字，用于相似度计算。
    keep_punct=True：保留可读标点，用于日志与去重键。
    """
    if not text:
        return ""
    t = to_halfwidth(text).lower()
    t = re.sub(r"\s+", "", t)
    if keep_punct:
        return t
    # \w 在 Python3 下含 Unicode 字母与数字，故汉字会被保留
    t = re.sub(r"[^0-9a-z\u3400-\u4dbf\u4e00-\u9fff]+", "", t)
    return t


def strip_question_prefix(text: str) -> str:
    """去掉行首题号，如 '1.' / '第 3 题' / '(2)'。"""
    if not text:
        return ""
    return _NUM_PREFIX_RE.sub("", text, count=1).strip()


def bigrams(text: str) -> str:
    """把字符串切成字符二元组并以空格连接，用于 token_set 相似度。

    中文没有词边界，二元组能显著提升 OCR 抖动下的鲁棒性。
    """
    if not text:
        return ""
    if len(text) < 3:
        return text
    return " ".join(text[i:i + 2] for i in range(len(text) - 1))


# ---------------------------------------------------------------- 结构解析

def split_lines(text: str) -> List[str]:
    """按行切分并去除空白行。"""
    if not text:
        return []
    raw = re.split(r"[\r\n]+", text)
    return [ln.strip() for ln in raw if ln and ln.strip()]


def parse_option_line(line: str) -> Tuple[str, str] | None:
    """尝试把一行解析为 (选项字母, 选项内容)。失败返回 None。"""
    m = _OPTION_LINE_RE.match(line)
    if not m:
        return None
    letter, body = m.group(1).upper(), m.group(2).strip()
    # 避免把 "A 是正确答案" 这类正文误判为选项：选项内容通常非空
    if not body:
        return None
    return letter, body


def parse_answer_line(line: str) -> str | None:
    """尝试从一行中提取答案内容的原始串。"""
    m = _ANSWER_LINE_RE.match(to_halfwidth(line))
    if not m:
        return None
    return m.group(1).strip()


def extract_answer_letters(text: str) -> str:
    """从任意答案文本中抽取选项字母，如 '答案：A、C' -> 'AC'。"""
    if not text:
        return ""
    t = to_halfwidth(text)
    # 判断题常见写法
    for kw, letter in (("正确", "T"), ("对", "T"), ("错误", "F"), ("错", "F")):
        if kw in t and not _ANSWER_TOKEN_RE.search(t):
            return letter
    letters = _ANSWER_TOKEN_RE.findall(t)
    seen, out = set(), []
    for ch in letters:
        u = ch.upper()
        if u not in seen:
            seen.add(u)
            out.append(u)
    return "".join(out)


def parse_analysis_line(line: str) -> str | None:
    """从一行中提取解析内容。"""
    m = _ANALYSIS_LINE_RE.match(to_halfwidth(line))
    if m is None:
        return None
    return m.group(1).strip()


def split_inline_analysis(text: str) -> Tuple[str, str]:
    """把答案与解析写在同一行的写法拆开。

    例：``"B【解析】因为 x"`` -> ``("B", "因为 x")``
        ``"AC 解析：半数以上"`` -> ``("AC", "半数以上")``

    Returns:
        (答案部分, 解析部分)；行内没有解析标记时解析部分为空串。
    """
    if not text:
        return "", ""
    t = to_halfwidth(text)
    m = _ANALYSIS_INLINE_RE.search(t)
    if m is None:
        return t.strip(), ""
    head = t[:m.start()].strip(" \t,;:.-、，；：")
    tail = t[m.end():].strip()
    return head, tail


def parse_type_line(line: str) -> str | None:
    """从一行中提取题型。"""
    m = _TYPE_LINE_RE.match(to_halfwidth(line))
    if m is None:
        return None
    return m.group(1).strip()


def normalize_qtype(text: str) -> str:
    """把题型写法归一到「单选题 / 多选题 / 判断题 / 填空题 / 简答题」。

    认不出来时原样返回（题库里可能有"翻译题"之类自定义题型，
    不能强行塞进固定的几类）。
    """
    if not text:
        return ""
    t = to_halfwidth(str(text)).strip()
    if not t:
        return ""
    for key, canon in _TYPE_ALIASES:
        if key in t:
            return canon
    return t


def guess_type(question: str, answer: str, options: List[str]) -> str:
    """根据题目内容猜测题型。"""
    letters = extract_answer_letters(answer)
    if options:
        if len(letters) > 1:
            return "多选题"
        return "单选题"
    if letters in ("T", "F") or answer.strip() in ("正确", "错误", "对", "错", "√", "×"):
        return "判断题"
    if "___" in question or "____" in question or "（ ）" in question or "( )" in question:
        return "填空题"
    return "简答题"


def similarity_key(text: str) -> str:
    """生成用于结果去重的稳定键。"""
    return normalize(text)[:180]


def truncate(text: str, limit: int) -> str:
    """按字符数截断并加省略号。"""
    if text is None:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"
