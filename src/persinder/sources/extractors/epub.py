"""EPUB — zip с OPF-навигацией (stdlib), класс ``pub-formats``."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

from ..classify import PUB_FORMATS
from ..encodings import decode_text
from ._base import BaseExtractor, Extracted
from ._common import extract_html


class EpubExtractor(BaseExtractor):
    """EPUB — zip с OPF-навигацией (stdlib), класс ``pub-formats``.

    Через ``container.xml`` находим манифест (OPF), порядок чтения — spine;
    HTML-части прогоняются через общий ``extract_html`` и склеиваются.
    """

    name = "epub"
    format_class = PUB_FORMATS
    subtypes = frozenset({"epub"})

    def suffixes(self) -> frozenset[str]:
        return frozenset({".epub"})

    def extract(self, path: Path) -> Extracted:
        try:
            with zipfile.ZipFile(path) as archive:
                return self._extract_zip(archive)
        except Exception as exc:  # noqa: BLE001 — битый epub/zip
            return Extracted(text="", error=f"epub: {exc}", extractor=self.name)

    def _extract_zip(self, archive: Any) -> Extracted:
        names = set(archive.namelist())
        opf_path = _epub_opf_path(archive)
        spine = (
            _epub_spine_hrefs(archive, opf_path)
            if opf_path and opf_path in names
            else []
        )
        if not spine:
            spine = [
                n for n in names if n.lower().endswith((".xhtml", ".html", ".htm"))
            ]
        parts: list[str] = []
        for href in spine:
            target = _resolve_opf_path(opf_path, href) if opf_path else href
            if target not in names:
                continue
            raw_html = archive.read(target)
            text = decode_text(raw_html)
            result = extract_html(text)
            if result.text:
                parts.append(result.text)
        if not parts:
            return Extracted(
                text="", error="epub: нет текстовых частей", extractor=self.name
            )
        return Extracted(text="\n\n".join(parts), extractor=self.name)


def _epub_opf_path(archive: Any) -> str | None:
    import xml.etree.ElementTree as ET

    try:
        names = set(archive.namelist())
        container_name = next(n for n in names if n.lower() == "meta-inf/container.xml")
        container = ET.fromstring(archive.read(container_name))
    except (StopIteration, KeyError, ET.ParseError):
        return None
    for rootfile in container.iter():
        if rootfile.tag.endswith("}rootfile"):
            return rootfile.attrib.get("full-path") or None
    return None


def _epub_spine_hrefs(archive: Any, opf_path: str) -> list[str]:
    import xml.etree.ElementTree as ET

    try:
        opf = ET.fromstring(archive.read(opf_path))
    except (KeyError, ET.ParseError):
        return []
    manifest: dict[str, str] = {}
    for item in opf.iter():
        if item.tag.endswith("}item") and "id" in item.attrib and "href" in item.attrib:
            manifest[item.attrib["id"]] = item.attrib["href"]
    spine: list[str] = []
    for ref in opf.iter():
        if ref.tag.endswith("}itemref") and "idref" in ref.attrib:
            href = manifest.get(ref.attrib["idref"])
            if href:
                spine.append(href)
    return spine


def _resolve_opf_path(opf_path: str, href: str) -> str:
    from urllib.parse import unquote, urlparse

    href = unquote(urlparse(href).path)
    base = Path(opf_path).parent
    if base.as_posix() in ("", "."):
        return href
    return (base / href).as_posix()


# Пределы распаковки архивов — защита от архивов-бомб (много записей / огромные).
