"""Модели транспортного слоя HTTP (DTO).

Форма запроса и ответа конкретного эндпоинта ``/ingest`` и правила перехода
между DTO и внутренними моделями сервиса. Не зависят от БД.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from ..domain import Area
from ..ingest import IngestRequest, IngestResult
from ..search import SearchHit, SearchResult


class PayloadError(ValueError):
    """Некорректное тело запроса ``POST /ingest``."""


@dataclass(frozen=True)
class IngestPayload:
    """Разобранное тело запроса ``POST /ingest`` (DTO запроса)."""

    filename: str
    area: Area
    text: str
    title: str | None = None

    @classmethod
    def from_json(cls, raw: object) -> IngestPayload:
        """Разобрать произвольный JSON в DTO; при ошибке — ``PayloadError``."""
        if not isinstance(raw, dict):
            raise PayloadError("json object expected")
        data = cast("dict[str, object]", raw)

        try:
            area = Area.parse(data.get("area", ""))
        except ValueError as exc:
            raise PayloadError(str(exc)) from exc

        title_raw = data.get("title")
        return cls(
            filename=str(data.get("filename", "")),
            area=area,
            text=str(data.get("text", "")),
            title=str(title_raw) if title_raw is not None else None,
        )

    def to_request(self) -> IngestRequest:
        """Перевести DTO во вход сценария ``ingest``."""
        return IngestRequest(
            filename=self.filename,
            area=self.area,
            text=self.text,
            title=self.title,
        )


@dataclass(frozen=True)
class IngestResponse:
    """Тело ответа ``POST /ingest`` (DTO ответа)."""

    status: str
    document_id: str | None = None
    chunks: int = 0
    message: str | None = None

    @classmethod
    def from_result(cls, result: IngestResult) -> IngestResponse:
        """Собрать DTO из результата сценария ``ingest``."""
        return cls(
            status=result.status,
            document_id=result.document_id,
            chunks=result.chunks,
            message=result.message,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "document_id": self.document_id,
            "chunks": self.chunks,
            "message": self.message,
        }


@dataclass(frozen=True)
class SearchResponse:
    """Тело ответа ``GET /search`` (DTO ответа).

    ``hits`` — морфологический слой, ``semantic_hits`` — векторный (модуль 3).
    """

    query: str
    hits: tuple[SearchHit, ...] = ()
    semantic_hits: tuple[SearchHit, ...] = ()

    @classmethod
    def from_result(cls, result: SearchResult) -> SearchResponse:
        """Собрать DTO из результата сервиса поиска."""
        return cls(
            query=result.query,
            hits=result.hits,
            semantic_hits=result.semantic_hits,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "hits": self._hits_to_dict(self.hits),
            "semantic_hits": self._hits_to_dict(self.semantic_hits),
        }

    @staticmethod
    def _hits_to_dict(hits: tuple[SearchHit, ...]) -> list[dict[str, object]]:
        return [
            {
                "document_id": hit.document_id,
                "chunk_index": hit.chunk_index,
                "score": hit.score,
                "context": hit.context,
                "area": hit.area,
                "source_path": hit.source_path,
                "filename": hit.filename,
            }
            for hit in hits
        ]
