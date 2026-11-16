"""Markdown: заметки Obsidian (frontmatter + inline-тэги).

Класс ``structure-text``: Markdown + frontmatter + тэги.
"""

from __future__ import annotations

from pathlib import Path

from ..classify import STRUCTURE_TEXT
from ..encodings import decode_text
from ._base import BaseExtractor, Extracted
from ._common import FRONTMATTER_RE, extract_tags, strip_code_inline


class MarkdownExtractor(BaseExtractor):
    """Заметки Obsidian: Markdown + frontmatter + inline-тэги (класс structure-text)."""

    name = "markdown"
    format_class = STRUCTURE_TEXT
    subtypes = frozenset({"markdown"})

    def suffixes(self) -> frozenset[str]:
        return frozenset({".md", ".markdown"})

    def extract(self, path: Path) -> Extracted:
        raw = decode_text(path.read_bytes())
        fm_match = FRONTMATTER_RE.match(raw)
        frontmatter: str | None = None
        body = raw
        if fm_match:
            frontmatter = fm_match.group(1)
            body = raw[fm_match.end() :]
        return Extracted(
            text=body,
            tags=extract_tags(strip_code_inline(body), frontmatter),
            extractor=self.name,
        )
