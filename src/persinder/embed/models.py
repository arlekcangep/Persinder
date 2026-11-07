"""Модели модуля ``embed``: отпечаток модели, параметры чанкинга, итоги воркера."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EmbedFingerprint:
    """Отпечаток модели эмбеддингов — обязательная часть сайдкара (§7).

    По нему сайдкар опознаётся как пригодный: сменилась модель или версия —
    отпечаток другой, значит векторы пересчитываются.
    """

    model: str  # id + версия, например "placeholder-v1"
    dim: int
    quant: str  # "f16" | "f32"


@dataclass(frozen=True)
class ChunkParams:
    """Параметры чанкинга, при которых считались векторы."""

    size: int
    overlap: int


@dataclass
class EmbedStats:
    """Итоги одного прохода воркера."""

    docs: int = 0
    chunks: int = 0
    sidecars: int = 0
    skipped: int = 0  # документ без текста — очередь отброшена
    errors: int = 0
    messages: list[str] = field(default_factory=list[str])
