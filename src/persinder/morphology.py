"""Морфологический слой: нормализация, токенизация, стемминг.

Первый и самый быстрый слой индексации. Стеммер подключается через интерфейс
(см. ``interfaces.Stemmer``): по умолчанию — Snowball (русский + английский),
если пакет ``snowballstemmer`` доступен, иначе — заглушка без стемминга.
"""

from __future__ import annotations

import re
from typing import Iterable

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
    """Стеммер Snowball для русского и английского (опциональная зависимость)."""

    def __init__(self) -> None:
        import snowballstemmer

        self._ru = snowballstemmer.stemmer("russian")
        self._en = snowballstemmer.stemmer("english")
        self.name = "snowball:ru+en"

    def stem(self, tokens: Iterable[str]) -> list[str]:
        out: list[str] = []
        for token in tokens:
            if _CYRILLIC_RE.search(token):
                out.append(self._ru.stemWord(token))
            else:
                out.append(self._en.stemWord(token))
        return out


def default_stemmer() -> object:
    """Snowball, если доступен, иначе заглушка."""
    try:
        return SnowballStemmer()
    except ImportError:
        return NoopStemmer()


def analyze(text: str, stemmer: object) -> list[str]:
    """Нормализовать текст: токенизация + стемминг."""
    return stemmer.stem(tokenize(text))


def analyze_to_string(text: str, stemmer: object) -> str:
    """Стеммированный текст для FTS5 (токены через пробел)."""
    return " ".join(analyze(text, stemmer))
