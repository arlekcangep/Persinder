"""Текст вокруг хита.

Два режима построения контекста:

* ``context_around`` — по границам *чанка*: строки, покрывающие чанк целиком,
  плюс ``lines`` строк до и после. Используется для семантического слоя, где
  «найденного слова» нет (векторное сходство — по всему чанку).

* ``sentence_context`` / ``hit_context`` — по *найденному слову*: ищется позиция
  слова морфологически (стем токенов чанка сверяется со стемами запроса) и
  выводятся ``sentences`` предложений до и после него — а не весь чанк.
  Используется для морфологического слоя (FTS5), где известно, какое слово
  совпало.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from typing import TYPE_CHECKING

from ..morphology import token_positions

if TYPE_CHECKING:
    from ..interfaces import Stemmer

DEFAULT_CONTEXT_LINES = 3
DEFAULT_CONTEXT_SENTENCES = 3

# Конец предложения: знаки . ! ? … или перенос строки (строки/абзацы близки к
# предложениям и в plain-text, и в markdown), за которыми идёт пробел/конец.
_SENTENCE_RE = re.compile(r"[.!?…\n]+")


def _line_starts(text: str) -> list[int]:
    """Позиции начал строк (включая возможную пустую строку после ``\\n``)."""
    return [0, *(index + 1 for index, char in enumerate(text) if char == "\n")]


def _line_end(text: str, starts: list[int], line: int) -> int:
    """Конец строки без завершающего ``\\n``."""
    if line + 1 < len(starts):
        return starts[line + 1] - 1
    return len(text)


def context_around(
    text: str,
    start: int,
    end: int,
    lines: int = DEFAULT_CONTEXT_LINES,
) -> str:
    """Вернуть строки хита плюс ``lines`` строк до и после него.

    ``start``/``end`` — границы чанка в ``text`` (``end`` не включается).
    Границы расширяются до целых строк, поэтому сам хит попадает в контекст
    целиком, даже если чанк начинается или заканчивается посреди строки.
    """
    if lines < 0:
        raise ValueError("число строк контекста не может быть отрицательным")

    length = len(text)
    start = max(0, min(start, length))
    end = max(start, min(end, length))

    starts = _line_starts(text)
    first = bisect_right(starts, start) - 1
    last_pos = end - 1 if end > start else start
    last = bisect_right(starts, last_pos) - 1

    low = max(0, first - lines)
    high = min(len(starts) - 1, last + lines)
    return text[starts[low] : _line_end(text, starts, high)]


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Диапазоны предложений ``(start, end)`` без ведущих пробелов у следующих."""
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _SENTENCE_RE.finditer(text):
        end = match.end()
        if end > start:  # непустое предложение
            spans.append((start, end))
        start = end
        while start < len(text) and text[start].isspace():
            start += 1
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def sentence_context(
    text: str,
    position: int,
    sentences: int = DEFAULT_CONTEXT_SENTENCES,
) -> str:
    """Вернуть ``sentences`` предложений до и после позиции ``position``.

    Само предложение с найденным словом всегда входит в контекст. Отсчёт от
    ``position`` по символам текста; границы — по ``_SENTENCE_RE``.
    """
    if sentences < 0:
        raise ValueError("число предложений контекста не может быть отрицательным")
    if not text:
        return text

    pos = max(0, min(position, len(text) - 1))
    spans = _sentence_spans(text)
    if not spans:
        return text

    # Предложение, которому принадлежит позиция: первое с ``start <= pos < end``.
    # Позиция на стыке (pos == конец предыдущего предложения) относится
    # к следующему — в него и попадает найденное слово.
    index = len(spans) - 1
    for i, (start, end) in enumerate(spans):
        if start <= pos < end:
            index = i
            break
    low = max(0, index - sentences)
    high = min(len(spans) - 1, index + sentences)
    return text[spans[low][0] : spans[high][1]].rstrip()


def match_start(
    text: str,
    span: tuple[int, int] | None,
    stems: frozenset[str],
    stemmer: Stemmer,
) -> int | None:
    """Позиция первого слова чанка, чей стем есть в ``stems``; ``None`` — нет.

    ``stems`` — уже стеммированные формы слов запроса (как их готовит
    ``analyze_to_string`` для ``search_morph``): стем каждого токена в границах
    ``span`` сверяется с этим набором. Возвращает позицию начала найденного
    слова в ``text``, либо ``None``, если совпадений нет.
    """
    if span is None:
        return None
    lo, hi = span
    lo = max(0, min(lo, len(text)))
    hi = max(lo, min(hi, len(text)))
    base = lo
    for start, end in token_positions(text[lo:hi]):
        token = text[lo + start : lo + end]
        if stemmer.stem([token])[0] in stems:
            return base + start
    return None


def hit_context(
    text: str,
    span: tuple[int, int] | None,
    stems: frozenset[str],
    stemmer: Stemmer,
    sentences: int = DEFAULT_CONTEXT_SENTENCES,
) -> str:
    """Контекст от найденного слова: ``sentences`` предложений до и после.

    Если слово найти не удалось (маловероятно при корректном FTS) — fallback
    на ``context_around`` по границам чанка, чтобы хит не остался без контекста.
    """
    position = match_start(text, span, stems, stemmer)
    if position is None:
        return context_around(text, span[0], span[1]) if span is not None else ""
    return sentence_context(text, position, sentences)
