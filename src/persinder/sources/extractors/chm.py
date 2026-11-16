"""CHM (Compiled HTML Help) — stdlib-распаковщик ITSF/ITSP + LZX."""

from __future__ import annotations

from pathlib import Path

from ..chm import ChmArchive
from ..classify import PUB_FORMATS
from ..encodings import decode_text
from ._base import BaseExtractor, Extracted
from ._common import dedup_tags, extract_html


class ChmExtractor(BaseExtractor):
    """CHM (Compiled HTML Help) — stdlib-распаковщик ITSF/ITSP + LZX.

    Страницы (``.htm``/``.html``/``.xhtml``) прогоняются через общий
    ``extract_html`` и склеиваются в порядке оглавления. Если страниц нет,
    берутся индексные файлы ``.hhc``/``.hhk`` (в них — оглавление и указатель).
    Сбой одной страницы не валит документ: он пропускается, ошибка видна,
    только если текста не набралось совсем.
    """

    name = "chm"
    format_class = PUB_FORMATS
    subtypes = frozenset({"chm"})

    _PAGE_SUFFIXES = (".htm", ".html", ".xhtml")
    _INDEX_SUFFIXES = (".hhc", ".hhk")

    def suffixes(self) -> frozenset[str]:
        return frozenset({".chm"})

    def extract(self, path: Path) -> Extracted:
        try:
            archive = ChmArchive(path)
        except Exception as exc:  # noqa: BLE001 — битый или не-CHM файл
            return Extracted(text="", error=f"chm: {exc}", extractor=self.name)
        names = archive.names()
        pages = [n for n in names if n.lower().endswith(self._PAGE_SUFFIXES)]
        if not pages:
            pages = [n for n in names if n.lower().endswith(self._INDEX_SUFFIXES)]

        parts: list[str] = []
        tags: list[str] = []
        errors: list[str] = []
        for name in pages:
            try:
                raw = archive.read(name)
            except Exception as exc:  # noqa: BLE001 — повреждённая запись
                errors.append(f"{name}: {exc}")
                continue
            result = extract_html(decode_text(raw))
            if result.text:
                parts.append(result.text)
                tags.extend(result.tags)
        if not parts:
            detail = "; ".join(errors[:3]) or "нет текстовых страниц"
            return Extracted(text="", error=f"chm: {detail}", extractor=self.name)
        return Extracted(
            text="\n\n".join(parts),
            tags=dedup_tags(tags),
            pages=len(parts),
            extractor=self.name,
        )
