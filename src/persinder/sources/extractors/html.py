"""HTML → текст (стандартная библиотека), класс ``structure-text``."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from ..classify import STRUCTURE_TEXT
from ..encodings import decode_text
from ._base import BaseExtractor, Extracted
from ._common import extract_html


class HtmlExtractor(BaseExtractor):
    """HTML → текст (стандартная библиотека), класс ``structure-text``."""

    name = "html"
    format_class = STRUCTURE_TEXT
    subtypes = frozenset({"html"})

    def suffixes(self) -> frozenset[str]:
        return frozenset({".html", ".htm"})

    def extract(self, path: Path) -> Extracted:
        raw = decode_text(path.read_bytes())
        return dataclasses.replace(extract_html(raw), extractor=self.name)
