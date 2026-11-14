"""PDF через pypdf (extra ``pdf``), класс ``pub-formats``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..classify import PUB_FORMATS
from ._base import BaseExtractor, Extracted


class PdfExtractor(BaseExtractor):
    """PDF через pypdf (extra ``pdf``), класс ``pub-formats``."""

    name = "pdf"
    format_class = PUB_FORMATS
    subtypes = frozenset({"pdf"})

    def __init__(self) -> None:
        try:
            import pypdf

            del pypdf  # доступность extra 'pdf' проверена
        except ImportError as exc:
            raise RuntimeError(
                "PDF: не установлен пакет pypdf (extra 'pdf') — "
                'pip install -e ".[pdf]"'
            ) from exc

    def suffixes(self) -> frozenset[str]:
        return frozenset({".pdf"})

    def extract(self, path: Path) -> Extracted:
        try:
            from pypdf import PdfReader

            reader: Any = PdfReader(str(path))  # pyright: ignore[reportUnknownVariableType, reportMissingTypeStubs]
            raw_pages: list[Any] = list(reader.pages)
            pages: list[str] = []
            for page in raw_pages:
                text: object = page.extract_text()
                pages.append((str(text) if text else "").strip())
        except Exception as exc:  # noqa: BLE001 — любое повреждение PDF
            return Extracted(text="", tags=(), error=f"pdf: {exc}", extractor=self.name)
        result = Extracted(
            text="\n\n".join(p for p in pages if p),
            pages=len(pages),
            extractor=self.name,
        )
        return result
