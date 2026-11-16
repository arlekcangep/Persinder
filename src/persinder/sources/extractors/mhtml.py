"""MHTML (``.mht``) — MIME-посылка, стандартная библиотека."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from ..classify import STRUCTURE_TEXT
from ..encodings import decode_text
from ._base import BaseExtractor, Extracted
from ._common import extract_html


class MhtmlExtractor(BaseExtractor):
    """MHTML (``.mht``) — MIME-посылка, стандартная библиотека."""

    name = "mhtml"
    format_class = STRUCTURE_TEXT
    subtypes = frozenset({"mhtml"})

    def suffixes(self) -> frozenset[str]:
        return frozenset({".mht", ".mhtml"})

    def extract(self, path: Path) -> Extracted:
        import email
        from email import policy

        try:
            msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
            html: str | None = None
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_content()
                    html = payload if isinstance(payload, str) else None
                    break
        except Exception as exc:  # noqa: BLE001 — повреждённая MIME-посылка
            return Extracted(text="", error=f"mhtml: {exc}", extractor=self.name)
        if html is None:
            # Не multipart: иногда это просто HTML с MIME-заголовками.
            raw = decode_text(path.read_bytes())
            html = raw
        return dataclasses.replace(extract_html(html), extractor=self.name)
