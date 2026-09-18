# -*- coding: utf-8 -*-
"""模糊匹配与相似度评分。

评分策略（多信号加权融合），单一指标在 OCR 场景下都不可靠：

    ratio          Levenshtein 归一化相似度 —— 对整体长度敏感，抗错字
    partial_ratio  最佳子串相似度 —— 抗 OCR 截断/多余行，但对长度差虚高，做长度惩罚
    token_set      字符二元组集合相似度 —— 中文无词边界，二元组抗中间抖动最强

另外加入「包含即高置信」与「查询过短降权」两条经验规则。

规模适配：题库 > 1500 条时启用字符二元组倒排索引做候选预筛，
把每次比对量从 N 降到 ~400，保证单次匹配停留在毫秒级。
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

try:
    from rapidfuzz import fuzz

    _HAS_RAPIDFUZZ = True
except Exception:  # 纯 Python 兜底，慢约 30x，但保证可运行
    import difflib

    _HAS_RAPIDFUZZ = False

    class _FuzzShim:  # pragma: no cover
        @staticmethod
        def ratio(a: str, b: str) -> float:
            return difflib.SequenceMatcher(None, a, b).ratio() * 100

        @staticmethod
        def partial_ratio(a: str, b: str) -> float:
            if not a or not b:
                return 0.0
            if len(a) > len(b):
                a, b = b, a
            best = 0.0
            step = max(1, len(b) // 200)
            for i in range(0, len(b) - len(a) + 1, step):
                best = max(best, difflib.SequenceMatcher(None, a, b[i:i + len(a)]).ratio())
            return best * 100

        @staticmethod
        def token_set_ratio(a: str, b: str) -> float:
            sa, sb = set(a.split()), set(b.split())
            if not sa or not sb:
                return 0.0
            inter = len(sa & sb)
            return 200.0 * inter / (len(sa) + len(sb))

    fuzz = _FuzzShim()  # type: ignore

from . import text_utils as tu
from .question_bank import Question, QuestionBank

log = logging.getLogger("answerassist.matcher")

# 匹配时忽略的行（屏幕上如果已经打印了答案，不应干扰题干匹配）
_SKIP_LINE_MARKERS = ("解析", "分析", "答案", "参考答案", "正确答案", "得分", "评卷")

_MAX_QUERY_LEN = 600
_MIN_CONFIDENT_LEN = 8


@dataclass
class Match:
    score: float                 # 0.0 ~ 1.0
    question: Question
    matched_on: str = "question"  # question | full
    query: str = ""               # 触发本次匹配的 OCR 片段

    @property
    def percent(self) -> float:
        return self.score * 100.0


class Matcher:
    """题库模糊检索器（线程安全）。"""

    def __init__(self, bank: QuestionBank, threshold: float = 0.58, top_k: int = 5,
                 candidate_limit: int = 400, index_threshold: int = 1500):
        self.threshold = float(threshold)
        self.top_k = int(top_k)
        self.candidate_limit = int(candidate_limit)
        self.index_threshold = int(index_threshold)

        self._lock = threading.RLock()
        self._bank = bank
        self._norm_q: List[str] = []
        self._norm_full: List[str] = []
        self._index: Dict[str, List[int]] = {}
        self._cache_key: str = ""
        self._cache_val: List[Match] = []
        self.build()

    # ---------------------------------------------------------------- 索引

    def set_bank(self, bank: QuestionBank) -> None:
        with self._lock:
            self._bank = bank
            self.build()

    @property
    def bank(self) -> QuestionBank:
        return self._bank

    def build(self) -> None:
        """构建归一化文本与倒排索引。题库变更后必须调用。"""
        with self._lock:
            self._norm_q = [tu.normalize(q.question) for q in self._bank.questions]
            self._norm_full = [tu.normalize(q.search_text) for q in self._bank.questions]
            self._index = {}
            if len(self._bank.questions) >= self.index_threshold:
                for idx, text in enumerate(self._norm_q):
                    for gram in {text[i:i + 2] for i in range(max(0, len(text) - 1))}:
                        self._index.setdefault(gram, []).append(idx)
                log.info("已建立检索索引：%d 题 / %d 二元组", len(self._norm_q), len(self._index))
            self._cache_key = ""
            self._cache_val = []

    def set_threshold(self, value: float) -> None:
        self.threshold = float(value)

    def set_top_k(self, value: int) -> None:
        self.top_k = int(value)

    # ---------------------------------------------------------------- 评分

    @staticmethod
    def _score_pair(a: str, b: str) -> float:
        """核心评分函数，输入为两个归一化串，返回 0~1。"""
        if not a or not b:
            return 0.0
        la, lb = len(a), len(b)
        # 长度过于悬殊直接快速淘汰（相差 4 倍以上几乎不可能是同一题）
        if max(la, lb) > 4 * min(la, lb) + 12 and min(la, lb) < 20:
            return 0.0

        s_ratio = fuzz.ratio(a, b) / 100.0
        s_partial = fuzz.partial_ratio(a, b) / 100.0
        length_ratio = min(la, lb) / max(la, lb)
        s_partial_adj = s_partial * (0.55 + 0.45 * length_ratio)
        s_bigram = fuzz.token_set_ratio(tu.bigrams(a), tu.bigrams(b)) / 100.0

        score = 0.42 * s_ratio + 0.26 * s_partial_adj + 0.32 * s_bigram

        # 一方完全包含另一方：高置信（如题干尾部被截断）
        if la >= 8 and (a in b or b in a):
            score = max(score, 0.88 * min(1.0, length_ratio + 0.3))
        return min(1.0, score)

    def _candidates(self, query: str) -> Sequence[int]:
        n = len(self._norm_q)
        if not self._index or n < self.index_threshold:
            return range(n)
        grams = {query[i:i + 2] for i in range(max(0, len(query) - 1))}
        if not grams:
            return range(n)
        counter: Counter = Counter()
        for gram in grams:
            for idx in self._index.get(gram, ()):  # type: ignore[arg-type]
                counter[idx] += 1
        if not counter:
            return range(n)
        return [idx for idx, _ in counter.most_common(self.candidate_limit)]

    # ---------------------------------------------------------------- 检索

    def search(self, text: str, top_k: Optional[int] = None,
               threshold: Optional[float] = None) -> List[Match]:
        """对 OCR 文本做模糊检索，返回按相似度降序的匹配列表。"""
        queries = build_queries(text)
        if not queries:
            return []

        cache_key = "||".join(queries)
        with self._lock:
            if cache_key == self._cache_key:
                return list(self._cache_val)

        top_k = self.top_k if top_k is None else int(top_k)
        threshold = self.threshold if threshold is None else float(threshold)

        best: Dict[int, Match] = {}
        for query in queries:
            norm = tu.normalize(query)
            if len(norm) < 3:
                continue
            for idx in self._candidates(norm):
                q = self._bank.questions[idx]
                s_q = self._score_pair(norm, self._norm_q[idx])
                s_f = self._score_pair(norm, self._norm_full[idx]) if self._norm_full[idx] else 0.0
                if s_q >= s_f:
                    score, on = s_q, "question"
                else:
                    score, on = s_f, "full"

                # 查询过短 -> 降权，避免"下列说法正确的是"这类通用句误命中
                if len(norm) < _MIN_CONFIDENT_LEN:
                    score *= 0.6 + 0.05 * len(norm)

                if score < threshold:
                    continue
                prev = best.get(idx)
                if prev is None or score > prev.score:
                    best[idx] = Match(score=score, question=q, matched_on=on,
                                      query=query[:120])

        results = sorted(best.values(), key=lambda m: m.score, reverse=True)[:max(1, top_k)]
        with self._lock:
            self._cache_key = cache_key
            self._cache_val = list(results)
        return results

    def best(self, text: str,
             threshold: Optional[float] = None) -> Optional[Match]:
        """只要最优匹配，无命中返回 None。"""
        hits = self.search(text, top_k=1, threshold=threshold)
        return hits[0] if hits else None

    # ---------------------------------------------------------------- 诊断

    def score_text(self, text: str) -> List[Tuple[str, float]]:
        """调试用：列出与查询最接近的若干题干及其分数（不受阈值限制）。"""
        norm = tu.normalize(text)
        if not norm:
            return []
        scored = [(self._bank.questions[i].question, self._score_pair(norm, self._norm_q[i]))
                  for i in self._candidates(norm)]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:10]


# ---------------------------------------------------------------- 查询构造

def _is_skip_line(line: str) -> bool:
    stripped = tu.to_halfwidth(line).strip()
    if not stripped:
        return True
    for marker in _SKIP_LINE_MARKERS:
        if stripped.startswith(marker) or stripped.startswith(marker + "：") \
                or stripped.startswith(marker + ":"):
            return True
    # 纯"答案：A"这类
    return tu.parse_answer_line(stripped) is not None


def build_queries(text: str, max_blocks: int = 4) -> List[str]:
    """把 OCR 文本切成若干待检索片段。

    屏上往往会同时框到多道题，或框到已打印的「答案/解析」行。这里：
      1. 丢弃答案/解析行，避免污染题干；
      2. 以带题号的行作为分块起点；
      3. 极短碎片与相邻块合并，最后限制块数上限以保证响应速度。
    """
    if not text:
        return []

    lines = [ln.strip() for ln in text.splitlines() if ln and ln.strip()]
    if not lines:
        return []

    content_lines = [ln for ln in lines if not _is_skip_line(ln)]
    if not content_lines:
        return []

    blocks: List[List[str]] = []
    current: List[str] = []
    for line in content_lines:
        if tu._NUM_PREFIX_RE.match(line) and current:
            blocks.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append(current)

    # 合并过短的块（多数是题干换行碎片）
    merged: List[List[str]] = []
    for blk in blocks:
        if merged and sum(len(x) for x in merged[-1]) < 10:
            merged[-1].extend(blk)
        else:
            merged.append(blk)
    blocks = merged or [content_lines]

    queries: List[str] = []
    for blk in blocks[:max_blocks]:
        q = "\n".join(blk)[:_MAX_QUERY_LEN].strip()
        if len(tu.normalize(q)) >= 3:
            queries.append(q)

    if not queries:
        q = "\n".join(content_lines)[:_MAX_QUERY_LEN].strip()
        if len(tu.normalize(q)) >= 3:
            queries.append(q)
    return queries


def ocr_quality_hint(text: str) -> str:
    """给 UI 的一句质量提示，帮助用户调整选区。"""
    norm = tu.normalize(text)
    n = len(norm)
    if n == 0:
        return "未识别到文字"
    if n < 8:
        return "识别内容过短，建议扩大选区"
    if n > 500:
        return "选区过大，可能框入多题"
    return ""
