"""Модели транспортного слоя HTTP (DTO).

Форма запроса и ответа конкретного эндпоинта ``/ingest`` и правила перехода
между DTO и внутренними моделями сервиса. Не зависят от БД.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from ..domain import Area
from ..ingest import IngestRequest, IngestResult


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
