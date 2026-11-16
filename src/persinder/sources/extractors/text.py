"""Простой текст (без frontmatter-логики), класс ``text``."""

from __future__ import annotations

from pathlib import Path

from ..classify import TEXT
from ..encodings import decode_text
from ._base import BaseExtractor, Extracted


class TextExtractor(BaseExtractor):
    """Простой текст (без frontmatter-логики), класс ``text``."""

    name = "text"
    format_class = TEXT
    subtypes = frozenset({"text"})

    def suffixes(self) -> frozenset[str]:
        return frozenset({".txt"})

    def extract(self, path: Path) -> Extracted:
        return Extracted(
            text=decode_text(path.read_bytes()),
            extractor=self.name,
        )
