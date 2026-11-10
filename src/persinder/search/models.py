"""Внутренние модели сервиса поиска."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SearchQuery:
    """Запрос поиска.

    ``min_score`` — порог косинуса для семантического слоя (``None`` — без
    порога, только ``limit``). Абсолютное значение зависит от модели: его
    подбирают под конкретную модель/корпус, а не считают вероятностью.
    """

    text: str
    limit: int = 20
    min_score: float | None = None


@dataclass(frozen=True)
class SearchHit:
    """Одно попадание поиска.

    ``context`` — текст вокруг чанка (строки хита плюс несколько строк до и
    после, ``None`` — если сырой текст недоступен). ``area``, ``source_path``
    и ``filename`` — источник документа: область, полный путь на диске и имя
    файла (``source_path`` пуст у виртуального документа — web/вставка).
    """

    document_id: str
    chunk_index: int
    score: float
    context: str | None = None
    area: str = ""
    source_path: str = ""
    filename: str = ""


@dataclass(frozen=True)
class SearchResult:
    """Результат поиска.

    ``hits`` — морфологический слой (FTS5); ``semantic_hits`` — векторный слой
    (модуль 3). Пока это отдельные группы; объединение в единый рейтинг
    (D16: точные → семантика → морфология → тэги) — следующий шаг.
    """

    query: str
    hits: tuple[SearchHit, ...] = ()
    semantic_hits: tuple[SearchHit, ...] = ()
