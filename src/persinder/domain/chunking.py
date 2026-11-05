"""Нарезка текста на чанки — чистая функция ядра.

Используется и при приёме (``ingest``), и при подсчёте эмбеддингов (``embed``),
поэтому живёт в ядре, а не в одном из сервисов. Диапазоны считаются
детерминированно: при тех же параметрах воркер воспроизводит те же чанки.
"""

from __future__ import annotations


def chunk_text(text: str, size: int, overlap: int) -> list[tuple[int, int]]:
    """Нарезать текст на диапазоны ``(start, end)`` с перекрытием."""
    if size <= 0:
        raise ValueError("размер чанка должен быть положительным")
    if overlap < 0 or overlap >= size:
        raise ValueError("перекрытие должно быть в диапазоне [0, размер чанка)")
    length = len(text)
    spans: list[tuple[int, int]] = []
    start = 0
    while start < length:
        end = min(start + size, length)
        spans.append((start, end))
        if end >= length:
            break
        start = end - overlap
    return spans
