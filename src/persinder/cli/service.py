"""Сборка сервисов приёма/поиска/эмбеддингов из настроек и конфига областей."""

from __future__ import annotations

import os

from ..api import IndexLog
from ..config import Settings
from ..embed import (
    PLACEHOLDER,
    EmbedWorker,
    ModelState,
    build_embedder,
    load,
)
from ..ingest import IngestJournal, IngestService
from ..morphology import default_stemmer
from ..search import SearchService
from ..sources import AreaConfig, ExtractorRegistry, default_registry, queue_models_for
from ..store import SqliteStore
from ..textstore import FsTextStore
from ..vectorindex import ModelScope, VectorSearch


def area_prefix(area: AreaConfig) -> str:
    """Префикс ``source_path`` области: каталог + разделитель (без соседей)."""
    return str(area.path) + os.sep


def match_area(areas: tuple[AreaConfig, ...], source_path: str) -> AreaConfig | None:
    """Область источника (самый длинный подходящий префикс); ``None`` — вне."""
    if not source_path:
        return None
    best: tuple[int, AreaConfig] | None = None
    for area in areas:
        prefix = area_prefix(area)
        if source_path.startswith(prefix) and (best is None or len(prefix) > best[0]):
            best = (len(prefix), area)
    return best[1] if best is not None else None


def models_for_source(
    areas: tuple[AreaConfig, ...],
    source_path: str,
    models: ModelState,
    *,
    semantic: bool = True,
) -> tuple[str, ...]:
    """Модели источника при старте: та же логика, что у конвейера индексации."""
    if not semantic:
        return ()
    area = match_area(areas, source_path)
    if area is None:
        return models.queue_models()  # виртуальный документ или путь вне областей
    resolved = queue_models_for(area, semantic_enabled=semantic)
    return models.queue_models() if resolved is None else resolved


def search_scopes(
    areas: tuple[AreaConfig, ...], models: ModelState
) -> dict[str, ModelScope]:
    """Скоупы поиска: каждая активная модель → только её области.

    Активная модель — текущая глобальная (``models.search``) плюс модели,
    указанные в конфиге папок, даже если папка ещё не проиндексирована.
    Папка со своей моделью исключается из скоупа глобальной (даже если от неё
    остались старые эмбеддинги), ``semantic: false`` — из всех моделей.
    """
    global_paths: list[str] = []
    area_paths: dict[str, list[str]] = {}
    for area in areas:
        if not area.semantic:
            continue
        if area.embedding_model:
            area_paths.setdefault(area.embedding_model, []).append(area_prefix(area))
        else:
            global_paths.append(area_prefix(area))
    # Глобальная модель активна всегда; виртуальные документы (web/вставка)
    # принадлежат профилю, а не папке.
    scopes: dict[str, ModelScope] = {
        models.search: ModelScope(paths=tuple(global_paths), allow_virtual=True)
    }
    for model, paths in area_paths.items():
        existing = scopes.get(model)
        if existing is not None:
            # та же модель, что глобальная: области складываются
            scopes[model] = ModelScope(
                paths=existing.paths + tuple(paths), allow_virtual=True
            )
        else:
            scopes[model] = ModelScope(paths=tuple(paths))
    return scopes


def build_service(
    settings: Settings,
    *,
    areas: tuple[AreaConfig, ...] | None = None,
    semantic: bool = True,
    min_score: float | None = None,
) -> tuple[IngestService, SearchService, SqliteStore]:
    """Собрать сервисы приёма и поиска из настроек.

    ``areas`` — области из конфига: по ним строится скоуп каждой модели
    (модель ищет только там, где ею индексировали). ``None`` — конфига нет
    (HTTP-only): поиск глобальной моделью без скоупа, как раньше.
    ``semantic=False`` — семантика выключена полностью: эмбеддинги не ставятся
    в очередь, векторного слоя поиска нет. ``min_score`` — порог косинуса
    семантического слоя по умолчанию (из блока ``search`` конфига).
    """
    settings.ensure()
    store = SqliteStore(settings.db_path)
    textstore = FsTextStore(settings.text_dir)
    stemmer = default_stemmer()
    models = load(settings.models_state_path)
    ingest = IngestService(
        store=store,
        textstore=textstore,
        stemmer=stemmer,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        journal=IngestJournal(settings.home / "ingest.jsonl"),
        virtual_dir=settings.virtual_dir,
        embedding_models=models.queue_models() if semantic else (),
    )
    vector_search: VectorSearch | None = None
    if semantic:
        if areas is None:
            search_model: str | tuple[str, ...] = models.search
            scopes = None
        else:
            scopes = search_scopes(areas, models)
            search_model = tuple(scopes)
        vector_search = VectorSearch(
            store=store,
            embedder_factory=lambda key: build_embedder(settings.models_dir, key),
            search_model=search_model,
            ann_dir=settings.ann_dir,
            scopes=scopes,
        )
    search = SearchService(
        store=store,
        stemmer=stemmer,
        vector_search=vector_search,
        default_min_score=min_score,
    )
    return ingest, search, store


def build_embed_worker(
    settings: Settings,
    log: IndexLog | None = None,
    registry: ExtractorRegistry | None = None,
) -> tuple[EmbedWorker, SqliteStore, str]:
    """Собрать воркер эмбеддингов; вернуть (воркер, сторе, подпись моделей).

    Эмбеддер подбирается фабрикой по ключу задачи из очереди: в одном проходе
    могут встретиться задачи разных моделей (миграция, D24) — у каждой свой
    сайдкар. Заглушка placeholder-v1 встроена; реальная e5-small берётся из
    ``models_dir/<ключ>/``, если установлена.

    ``registry`` нужен фолбэку: если текста нет в кэше, воркер перечитывает
    оригинал и сверяет хэш (D26). По умолчанию — стандартный набор экстракторов.
    """
    store = SqliteStore(settings.db_path)
    textstore = FsTextStore(settings.text_dir)
    worker = EmbedWorker(
        store=store,
        textstore=textstore,
        embedder_factory=lambda key: build_embedder(settings.models_dir, key),
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        virtual_dir=settings.virtual_dir,
        ann_dir=settings.ann_dir,
        log=log,
        registry=registry if registry is not None else default_registry(),
    )
    return worker, store, describe_models(settings)


def describe_models(settings: Settings) -> str:
    """Активные модели профиля для отчётов (D24: embedding ≠ search — миграция)."""
    models = load(settings.models_state_path)
    if models.embedding == PLACEHOLDER:
        embedding = "заглушка (placeholder-v1)"
        hint = "; установи реальную: persinder model install e5-small-int8-v1"
    else:
        embedding = models.embedding
        hint = ""
    if models.search == models.embedding:
        return f"embedding = search = {embedding}{hint}"
    return f"embedding = {embedding}, search = {models.search} (миграция){hint}"
