"""FB2 (FictionBook XML) — стандартная библиотека, класс ``pub-formats``."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..classify import PUB_FORMATS
from ..encodings import decode_text
from ._base import BaseExtractor, Extracted


class Fb2Extractor(BaseExtractor):
    """FB2 (FictionBook XML) — стандартная библиотека, класс ``pub-formats``.

    Текст берётся из тела документа (``body``), границы абзацев — переносы;
    тэги извлекаются из ``<tag>`` внутри текста.
    """

    name = "fb2"
    format_class = PUB_FORMATS
    subtypes = frozenset({"fb2"})

    def suffixes(self) -> frozenset[str]:
        return frozenset({".fb2"})

    def extract(self, path: Path) -> Extracted:
        return self._extract_xml(path.read_bytes())

    def _extract_xml(self, data: bytes) -> Extracted:
        import xml.etree.ElementTree as ET

        # Декларация может называть windows-1251/koi8-r, которые ElementTree
        # декодировать не умеет (бросает ValueError). Декодируем сами по
        # детектору (BOM → UTF-8 → cp1251 → cp1252 → latin-1) и парсим строку:
        # для str декларация encoding игнорируется, а текст уже в юникоде.
        try:
            root = ET.fromstring(decode_text(data))
        except Exception as exc:  # noqa: BLE001 — повреждённый XML
            return Extracted(text="", error=f"fb2: {exc}", extractor=self.name)
        paragraphs: list[str] = []
        for body_tag in root.iter():
            if not body_tag.tag.endswith("}body"):
                continue
            for para in body_tag.iter():
                if para.tag.endswith(("}p", "}poem", "}subtitle", "}title")):
                    piece: list[str] = []
                    _collect_text(para, piece)
                    joined = "".join(piece).strip()
                    if joined:
                        paragraphs.append(joined)
        tags = tuple(dict.fromkeys(t.strip() for t in text_tag_iter(root)))
        text = "\n\n".join(paragraphs)
        if not text:
            return Extracted(
                text="", error="fb2: не найдено тело документа", extractor=self.name
            )
        return Extracted(text=text, tags=tags, extractor=self.name)


def _collect_text(elem: Any, out: list[str]) -> None:
    """Рекурсивно собрать текст элемента и его потомков (с хвостами)."""
    if elem.text:
        out.append(elem.text)
    for child in elem:
        _collect_text(child, out)
        if child.tail:
            out.append(child.tail)


def text_tag_iter(elem: Any) -> Iterable[str]:
    """Все тексты внутренних ``<tag>`` (не итератор-тегов ``tag``)."""
    for node in elem.iter():
        if node.tag.endswith("}tag") and node.text:
            yield node.text
