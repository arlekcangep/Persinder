"""Текст вокруг хита: строки до и после чанка.

Контекст считается по сырому тексту документа (``TextStore``) и границам
чанка (``MetaStore.chunk_span``). Границы расширяются до целых строк, чтобы
в ответ не попадали обрывки предложений.
"""

from __future__ import annotations

from bisect import bisect_right

DEFAULT_CONTEXT_LINES = 3


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
