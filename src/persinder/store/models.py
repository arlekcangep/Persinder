"""Модели слоя персистентности.

Строки/записи и результаты запросов SQLite. Наружу за пределы адаптера не
выносятся, кроме как через порты (см. ``interfaces.MetaStore``).
"""

from __future__ import annotations

from typing import TypedDict


class MorphHit(TypedDict):
    """Строка результата морфологического поиска."""

    doc_id: str
    chunk_index: int
    score: float


class EmbeddingQueueItem(TypedDict):
    """Строка очереди на эмбеддинги."""

    id: int
    doc_id: str
    chunk_index: int
    area: str
    source_path: str  # пусто — виртуальный элемент (нет файла на диске)
    model: str  # модель, чей вектор считается (сайдкар на модель)


class SourceRow(TypedDict):
    """Источник: для удаления сайдкаров модели (D24)."""

    doc_id: str
    area: str
    filename: str
    source_path: str


class DocumentSource(TypedDict):
    """Документ + его главный источник (для ``/context``, вариант C).

    ``extractor`` — имя типа экстрактора (``"markdown"``, ``"pdf"`` …),
    по которому перечитывается оригинал при отсутствии текста в textstore.
    ``container_path`` и ``container_entries`` — разбор пути для части архива:
    какой файл-контейнер открыть и какие записи в нём пройти. У обычного
    файла ``container_path == ""``, ``container_entries == []``.
    """

    doc_id: str
    content_hash: str
    extractor: str
    source_path: str
    container_path: str
    container_entries: list[str]


class EmbeddingRow(TypedDict):
    """Строка таблицы эмбеддингов (локальное производное, §8).

    Векторы чанков под моделью, материализованные воркером ``embed``
    (сайдкар — синхронизируемый артефакт, эта таблица — локальный кэш для
    векторного поиска, модуль 3). ``vector`` — f16-блоб (см. sidecar).
    ``id`` — стабильный ключ строки: по нему ANN-индекс (USearch) ссылается
    на чанк, upsert вектора не меняет id.
    """

    id: int
    doc_id: str
    chunk_index: int
    vector: bytes


class StoreCounts(TypedDict):
    """Счётчики содержимого метастора."""

    documents: int
    sources: int
    chunks: int
    vectors: int
    pending_embeddings: int
    pending_documents: int
