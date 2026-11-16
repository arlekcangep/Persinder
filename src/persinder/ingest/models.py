"""Внутренние модели сервиса приёма.

Вход и выход сценария ``ingest``: не зависят от HTTP и от БД.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain import Area


@dataclass(frozen=True)
class IngestRequest:
    """Вход сервиса приёма: текст и метаданные."""

    filename: str
    area: Area
    text: str
    title: str | None = None
    source_path: str | None = None
    """Абсолютный путь источника на диске.

    Есть — файл реальный, сайдкар пишется рядом с ним (``.persinder/``).
    Нет — данных на диске нет (web, вставленный текст): их эмбеддинги и сам
    текст живут в отдельной папке (``Settings.virtual_dir``).
    """
    extractor: str | None = None
    """Имя экстрактора, который извлёк этот текст (для атрибуции в БД)."""
    container_path: str = ""
    """Путь файла-контейнера на диске (``""`` — обычный файл).

    Для части архива ``source_path`` — человекочитаемая строка вида
    ``/area/a.zip/b.zip/x.md``, а ``container_path`` + ``container_entries`` —
    точный разбор: какой файл открыть и какие записи в нём пройти.
    """
    container_entries: tuple[str, ...] = ()
    """Цепочка записей внутри ``container_path`` (пусто — обычный файл)."""
    cache_text: bool = True
    """Копировать ли текст в кэш (``text_cache``, D27).

    ``False`` — экстрактор считает текст воспроизводимым из оригинала
    (``is_cachable()``): на диске он не хранится, ``/context`` и воркер
    перечитывают файл. Виртуальные документы и части контейнеров — всегда
    ``True`` (другого места для их текста нет).
    """
    embedding_models: tuple[str, ...] | None = None
    """Модели для этой записи; ``None`` — глобальные модели сервиса.

    Пустой кортеж — семантика выключена: эмбеддинги не ставятся в очередь.
    Так конфиг области выбирает модель для конкретной папки (или отключает
    её), не меняя глобальные настройки профиля.
    """


@dataclass(frozen=True)
class IngestResult:
    """Результат приёма документа."""

    INDEXED = "indexed"
    ALREADY = "already_indexed"
    REJECTED = "rejected"

    status: str
    document_id: str | None = None
    chunks: int = 0
    message: str | None = None


@dataclass(frozen=True)
class RemoveResult:
    """Итог удаления источника: был ли он и вычищен ли документ целиком."""

    found: bool
    document_id: str | None = None


@dataclass(frozen=True)
class RecoveryStats:
    """Итог восстановления по журналу: сколько брошенных текстов убрано."""

    checked: int = 0
    removed: int = 0
