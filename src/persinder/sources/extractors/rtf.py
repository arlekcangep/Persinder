"""RTF — структурированный текст через ``striprtf`` (extra ``rtf``)."""

from __future__ import annotations

from pathlib import Path

from ..classify import PUB_FORMATS
from ..encodings import decode_text
from ._base import BaseExtractor, Extracted


class RtfExtractor(BaseExtractor):
    """RTF — структурированный текст; берём только текст без управляющих
    последовательностей (через ``striprtf``, extra ``rtf``), класс ``pub-formats``.
    """

    name = "rtf"
    format_class = PUB_FORMATS
    subtypes = frozenset({"rtf"})

    def __init__(self) -> None:
        try:
            import striprtf  # noqa: F401

            del striprtf  # доступность extra 'rtf' проверена
        except ImportError as exc:
            raise RuntimeError(
                "RTF: не установлен пакет striprtf (extra 'rtf') — "
                'pip install -e ".[rtf]"'
            ) from exc

    def suffixes(self) -> frozenset[str]:
        return frozenset({".rtf"})

    def extract(self, path: Path) -> Extracted:
        # striprtf — чистый Python, но без аннотаций типов (strict pyright).
        from striprtf.striprtf import rtf_to_text  # pyright: ignore[reportUnknownVariableType]

        try:
            raw = decode_text(path.read_bytes())
            text: str = rtf_to_text(raw)  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
        except Exception as exc:  # noqa: BLE001 — повреждённый RTF
            return Extracted(text="", error=f"rtf: {exc}", extractor=self.name)
        return Extracted(
            text=text.strip(),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            extractor=self.name,
        )
