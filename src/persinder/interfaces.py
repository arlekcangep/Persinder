"""Протоколы (интерфейсы) модулей Persinder.

Модули общаются через эти протоколы, а не через конкретные реализации.
Порты импортируют модели только для аннотаций (``TYPE_CHECKING``), чтобы не
создавать циклических зависимостей между слоями.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .domain import Chunk, Document, EmbeddingJob
    from .ingest.models import IngestRequest, IngestResult
    from .search.models import SearchQuery, SearchResult
    from .store.models import EmbeddingQueueItem, MorphHit, StoreCounts


@runtime_checkable
class Stemmer(Protocol):
    """Стеммер: нормализует токены."""

    name: str

    def stem(self, tokens: Iterable[str]) -> list[str]: ...


@runtime_checkable
class TextStore(Protocol):
    """Хранилище сырого текста."""

    def put(self, key: str, text: str) -> str:
        """Сохранить текст, вернуть путь."""
        ...

    def read(self, key: str) -> str: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...


@runtime_checkable
class MetaStore(Protocol):
    """Метастор: документы, чанки, FTS5, очередь."""

    def find_document_by_hash(self, content_hash: str) -> str | None: ...

    def add_source(self, doc_id: str, area: str, filename: str) -> None: ...

    def has_source(self, area: str, filename: str) -> bool: ...

    def remove_source(self, area: str, filename: str) -> str | None: ...

    def delete_document(self, doc_id: str) -> None: ...

    def save_document(
        self,
        doc: Document,
        area: str,
        filename: str,
        chunks: list[Chunk],
        jobs: list[EmbeddingJob],
    ) -> None: ...

    def search_morph(self, stemmed_query: str, limit: int = 20) -> list[MorphHit]: ...

    def pending_embeddings(self, limit: int = 100) -> list[EmbeddingQueueItem]: ...

    def counts(self) -> StoreCounts: ...

    def close(self) -> None: ...


@runtime_checkable
class Ingest(Protocol):
    """Сервис приёма текста и метаданных."""

    def ingest(self, request: IngestRequest) -> IngestResult: ...


@runtime_checkable
class Search(Protocol):
    """Сервис поиска."""

    def search(self, query: SearchQuery) -> SearchResult: ...
