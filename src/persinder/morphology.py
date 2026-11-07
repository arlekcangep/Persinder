"""Морфологический слой: нормализация, токенизация, стемминг.

Первый и самый быстрый слой индексации. Стеммер подключается через интерфейс
(см. ``interfaces.Stemmer``): по умолчанию — Snowball (русский + английский),
если пакет ``snowballstemmer`` доступен, иначе — заглушка без стемминга.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable
from typing import Any, cast

from .interfaces import Stemmer

_TOKEN_RE = re.compile(r"[0-9]+|[^\W\d_]+", re.UNICODE)
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")


def tokenize(text: str) -> list[str]:
    """Разбить текст на токены (слова и числа) в нижнем регистре."""
    return _TOKEN_RE.findall(text.lower())


class NoopStemmer:
    """Заглушка: не стеммит. Используется, если Snowball недоступен."""

    name = "noop"

    def stem(self, tokens: Iterable[str]) -> list[str]:
        return list(tokens)


class SnowballStemmer:
    """Стеммер Snowball для русского и английского (опциональная зависимость).

    Один экземпляр передаётся и в ``IngestService`` (watcher через ingest,
    HTTP ``/ingest``), и в ``SearchService`` (HTTP ``/search``) — вызовы идут
    из разных потоков. Объекты Snowball хранят состояние во время
    ``stemWord``, поэтому у каждого потока свой набор стеммеров
    (``threading.local``): блокировок нет, поиск никогда не ждёт ingest.
    Эмбеддеру стеммер не нужен — он берёт текст как есть.
    """

    def __init__(self) -> None:
        import snowballstemmer  # type: ignore[import-untyped]

        self._module: Any = snowballstemmer
        self._thread: Any = threading.local()
        self.name = "snowball:ru+en"

    def _pair(self) -> tuple[Any, Any]:
        """Поточно-локальная пара (russian, english) стеммеров."""
        pair = getattr(self._thread, "pair", None)
        if pair is None:
            module = self._module
            pair = (module.stemmer("russian"), module.stemmer("english"))
            self._thread.pair = pair
        return pair

    def stem(self, tokens: Iterable[str]) -> list[str]:
        ru, en = self._pair()
        out: list[str] = []
        for token in tokens:
            stem = ru.stemWord if _CYRILLIC_RE.search(token) else en.stemWord
            out.append(cast("str", stem(token)))
        return out


def default_stemmer() -> Stemmer:
    """Snowball, если доступен, иначе заглушка."""
    try:
        return SnowballStemmer()
    except ImportError:
        return NoopStemmer()


def analyze(text: str, stemmer: Stemmer) -> list[str]:
    """Нормализовать текст: токенизация + стемминг."""
    return stemmer.stem(tokenize(text))


def analyze_to_string(text: str, stemmer: Stemmer) -> str:
    """Стеммированный текст для FTS5 (токены через пробел)."""
    return " ".join(analyze(text, stemmer))
