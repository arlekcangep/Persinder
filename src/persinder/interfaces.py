"""Протоколы (интерфейсы) модулей Persinder.

Модули общаются через эти протоколы, а не через конкретные реализации.
Порты импортируют модели только для аннотаций (``TYPE_CHECKING``), чтобы не
создавать циклических зависимостей между слоями.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .domain import Area, Chunk, Document, EmbeddingJob
    from .ingest.models import IngestRequest, IngestResult, RemoveResult
    from .search.models import SearchQuery, SearchResult
    from .store.models import (
        EmbeddingQueueItem,
        EmbeddingRow,
        MorphHit,
        SourceRow,
        StoreCounts,
    )


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
class Embedder(Protocol):
    """Векторизатор: считает эмбеддинги для текстов.

    Порт модуля ``embed``: реальная модель (ONNX e5-small) и заглушка
    реализуют один интерфейс. ``name`` — отпечаток (id + версия), по нему
    сайдкар опознаётся как пригодный.
    """

    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Векторы пассажей (документы/чанки)."""
        ...

    def embed_query(self, texts: list[str]) -> list[list[float]]:
        """Векторы запросов (e5 требует префикс ``query:``, §12.1)."""
        ...


@runtime_checkable
class MetaStore(Protocol):
    """Метастор: документы, чанки, FTS5, очередь."""

    def find_document_by_hash(self, content_hash: str) -> str | None: ...

    def add_source(
        self, doc_id: str, area: str, filename: str, source_path: str = ""
    ) -> list[str]:
        """Закрепить источник; вернуть id вытесненных документов."""
        ...

    def has_source(self, area: str, filename: str) -> bool: ...

    def source_document(self, area: str, filename: str) -> str | None: ...

    def source_path(self, area: str, filename: str) -> str: ...

    def source_filenames_under(self, area: str, prefix: str) -> list[str]:
        """Имена источников области, начинающихся с ``prefix`` (части контейнера)."""
        ...

    def remove_source(self, area: str, filename: str) -> str | None: ...

    def delete_document(self, doc_id: str) -> None: ...

    def save_document(
        self,
        doc: Document,
        area: str,
        filename: str,
        chunks: list[Chunk],
        jobs: list[EmbeddingJob],
        source_path: str = "",
    ) -> list[str]:
        """Записать документ; вернуть id вытесненных документов."""
        ...

    def enqueue_document(
        self,
        doc_id: str,
        area: str,
        source_path: str = "",
        models: list[str] | None = None,
    ) -> None:
        """Поставить задачи на эмбеддинги для уже проиндексированного документа
        (дубликат с другим источником — сайдкар нужен и ему, под каждой моделью)."""
        ...

    def enqueue_all(self, models: list[str]) -> None:
        """Поставить задачи на эмбеддинги для всех источников под моделями
        (установка новой модели — миграция, D24)."""
        ...

    def drop_queue_model(self, model: str) -> None:
        """Убрать задачи модели из очереди (модель удалена)."""
        ...

    def all_sources(self) -> list[SourceRow]:
        """Все источники (для удаления сайдкаров модели)."""
        ...

    def put_embeddings(
        self, model: str, rows: list[tuple[str, int, bytes]]
    ) -> list[int]:
        """Записать/заменить векторы чанков документа под моделью (модуль 3).

        Возвращает стабильные ``id`` строк — по ним ANN-индекс ссылается
        на чанки (USearch).
        """
        ...

    def embeddings_for_model(self, model: str) -> list[EmbeddingRow]:
        """Все векторы чанков под моделью (для точного скана-фолбэка)."""
        ...

    def embeddings_ids_for_model(self, model: str) -> list[EmbeddingRow]:
        """Все строки модели (реконсиляция ANN-индекса с таблицей)."""
        ...

    def embeddings_for_ids(self, ids: list[int]) -> list[EmbeddingRow]:
        """Строки по ``id`` (ANN-хиты → doc_id/chunk_index; протухшие — отсекаются)."""
        ...

    def drop_embeddings_model(self, model: str) -> None:
        """Убрать векторы модели (``model remove`` — вместе с сайдкарами)."""
        ...

    def embedding_models(self) -> list[str]:
        """Модели, у которых есть векторы (для ``persinder ann rebuild``)."""
        ...

    def search_morph(self, stemmed_query: str, limit: int = 20) -> list[MorphHit]: ...

    def chunk_span(self, doc_id: str, chunk_index: int) -> tuple[int, int] | None:
        """Границы чанка в сыром тексте: ``(char_start, char_end)``."""
        ...

    def pending_embeddings(self, limit: int = 100) -> list[EmbeddingQueueItem]: ...

    def pending_for_docs(self, doc_ids: list[str]) -> list[EmbeddingQueueItem]: ...

    def drop_queue_rows(self, ids: list[int]) -> None: ...

    def counts(self) -> StoreCounts: ...

    def close(self) -> None: ...


@runtime_checkable
class Ingest(Protocol):
    """Сервис приёма текста и метаданных."""

    def ingest(self, request: IngestRequest) -> IngestResult: ...

    def remove_source(self, area: Area, filename: str) -> RemoveResult:
        """Убрать источник и его сырой текст, если документ вычищен целиком."""
        ...

    def remove_source_tree(self, area: Area, filename: str) -> RemoveResult:
        """Убрать источник и все его части (файлы контейнера: ``filename/...``)."""
        ...


@runtime_checkable
class Search(Protocol):
    """Сервис поиска."""

    def search(self, query: SearchQuery) -> SearchResult: ...
