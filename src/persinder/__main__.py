"""Запуск HTTP-сервиса Persinder, watcher'а и эмбеддингов: ``python -m persinder``."""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import cast

from .api import serve
from .config import Settings
from .embed import (
    CATALOG,
    PLACEHOLDER,
    EmbedStats,
    EmbedWorker,
    ModelState,
    build_embedder,
    delete_model_sidecars,
    file_sidecar_path,
    install,
    installed_keys,
    is_installed,
    load,
    remove_weights,
    save,
    virtual_sidecar_path,
)
from .ingest import IngestJournal, IngestService, RecoveryStats
from .morphology import default_stemmer
from .search import SearchHit, SearchQuery, SearchResult, SearchService
from .sources import (
    JOURNAL_RETENTION_DAYS,
    AreaConfig,
    ConfigError,
    IndexingPipeline,
    Journal,
    ScanResult,
    Watcher,
    default_registry,
    load_config,
    queue_models_for,
)
from .store import SqliteStore
from .textstore import FsTextStore
from .vectorindex import ModelScope, VectorSearch, rebuild_index


def _area_prefix(area: AreaConfig) -> str:
    """Префикс ``source_path`` области: каталог + разделитель (без соседей)."""
    return str(area.path) + os.sep


def _match_area(areas: tuple[AreaConfig, ...], source_path: str) -> AreaConfig | None:
    """Область источника (самый длинный подходящий префикс); ``None`` — вне."""
    if not source_path:
        return None
    best: tuple[int, AreaConfig] | None = None
    for area in areas:
        prefix = _area_prefix(area)
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
    area = _match_area(areas, source_path)
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
            area_paths.setdefault(area.embedding_model, []).append(_area_prefix(area))
        else:
            global_paths.append(_area_prefix(area))
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


def _known_models(settings: Settings) -> frozenset[str]:
    """Установленные модели профиля + встроенная заглушка (для валидации)."""
    return frozenset(installed_keys(settings.models_dir)) | {PLACEHOLDER}


def _candidate_config(settings: Settings) -> Path | None:
    """Первый существующий конфиг из стандартных мест (``--config`` не задан)."""
    for candidate in _config_candidates(settings):
        if candidate.is_file():
            return candidate
    return None


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
        textstore=textstore,
        default_min_score=min_score,
    )
    return ingest, search, store


def run_watch(config_path: Path, home: Path | None) -> int:
    """Запустить watcher: скан областей и индексация найденного."""
    registry = default_registry()
    settings = Settings.default()
    if home:
        settings = replace(settings, home=home)
    areas, watch = load_config(
        config_path,
        known_formats=registry.known_names(),
        known_models=_known_models(settings),
    )
    ingest, _search, store = build_service(
        settings, areas=areas, semantic=watch.semantic
    )
    journal = Journal(settings.home / "journal.jsonl")
    dropped = journal.compact()
    if dropped:
        print(
            f"Журнал: обрезано старых записей: {dropped} "
            f"(старше {JOURNAL_RETENTION_DAYS} дней)"
        )
    recovery = ingest.recover()
    if recovery.removed:
        print(format_recovery(recovery))
    pipeline = IndexingPipeline(
        ingest, registry, journal=journal, semantic=watch.semantic
    )

    print(f"Областей: {len(areas)}, интервал скана: {watch.poll_interval} с")
    watcher = Watcher(
        areas,
        watch,
        lambda result: print_stats(pipeline.process(result)),
        journal=journal,
    )
    watcher.start()
    try:
        import signal

        signal.pause() if hasattr(signal, "pause") else _wait_forever()
    except KeyboardInterrupt:
        pass
    finally:
        watcher.stop()
        store.close()
    return 0


def run_serve(config_path: Path, settings: Settings, reindex: bool = False) -> int:
    """Полный сервис: HTTP + watcher + эмбеддинги + восстановление после сбоев.

    Это единая точка запуска всех фоновых активностей:

    * журнал ``compact`` — убрать старую историю, сохранив последние состояния;
    * ``ingest.recover`` — убрать брошенные тексты прерванных вызовов;
    * watcher — первый проход сразу доиндексирует всё новое и недоделанное
      (журнал считает «устроенными» только завершённые цепочки);
    * поток эмбеддингов — разбирает очередь в векторы/сайдкары, пока сервис жив;
    * HTTP-сервер блокирует главный поток до Ctrl+C.

    При ``reindex=True`` журнал сбрасывается полностью (все файлы считаются
    новыми): сервис пересканирует все области, ingest сверяет хэши содержимого
    с базой (файл мог измениться, пока сервис был выключен) и под активную
    модель ставится пересчёт всех источников — полная переиндексация.
    """
    settings.ensure()
    print(f"Home: {settings.home}", flush=True)
    registry = default_registry()
    areas, watch = load_config(
        config_path,
        known_formats=registry.known_names(),
        known_models=_known_models(settings),
    )

    ingest, search, store = build_service(
        settings, areas=areas, semantic=watch.semantic, min_score=watch.min_score
    )
    embed_worker: EmbedWorker | None = None
    embed_store: SqliteStore | None = None
    if watch.semantic:
        embed_worker, embed_store, embed_label = build_embed_worker(settings)
    else:
        embed_label = (
            "Semantic: выключено конфигом (semantic: false) — эмбеддинги не считаются"
        )

    models_state = load(settings.models_state_path)
    if (
        watch.semantic
        and models_state.embedding != PLACEHOLDER
        and not is_installed(settings.models_dir, models_state.embedding)
    ):
        print(
            f"ВНИМАНИЕ: активная модель {models_state.embedding} не установлена "
            f"в {settings.models_dir} — сайдкары не будут считаться.\n"
            f"Установи её: persinder model install {models_state.embedding}",
            flush=True,
        )

    journal = Journal(settings.home / "journal.jsonl")
    if reindex:
        journal_path = settings.home / "journal.jsonl"
        if journal_path.exists():
            journal_path.unlink()
        print(
            "Полная переиндексация: журнал сброшен, пересканируются все области. "
            "Пересчёт эмбеддингов поставлен в очередь.",
            flush=True,
        )
        journal = Journal(settings.home / "journal.jsonl")  # пустой, заново
    # Под активную модель в очередь идут только источники без сайдкара:
    # уже посчитанное заново не ставится — рестарт не перезаполняет очередь
    # и не трогает файлы с сайдкарами. Проверка на каждом старте: после
    # переключения модели или оборванного прогона досчёт случается и без
    # полного ``--reindex``.
    models = load(settings.models_state_path)

    def _has_sidecar(model: str, doc_id: str, source_path: str) -> bool:
        if source_path:
            return file_sidecar_path(source_path, model).exists()
        return virtual_sidecar_path(settings.virtual_dir, doc_id, model).exists()

    store.enqueue_missing(
        list(models.queue_models()) if watch.semantic else [],
        _has_sidecar,
        models_for=lambda _area, source_path: models_for_source(
            areas, source_path, models, semantic=watch.semantic
        ),
    )
    dropped = journal.compact()
    if dropped:
        print(
            f"Журнал: обрезано старых записей: {dropped} "
            f"(старше {JOURNAL_RETENTION_DAYS} дней)",
            flush=True,
        )
    recovery = ingest.recover()
    if recovery.removed:
        print(format_recovery(recovery), flush=True)

    pipeline = IndexingPipeline(
        ingest, registry, journal=journal, semantic=watch.semantic
    )
    stop = threading.Event()
    kick = threading.Event()

    def on_events(result: ScanResult) -> None:
        stats = pipeline.process(result)
        if (
            stats.indexed
            or stats.deleted
            or stats.duplicates
            or stats.rejected
            or stats.skipped
            or stats.errors
        ):
            print_stats(stats)
        if stats.indexed:
            kick.set()  # новые документы в очереди — будим эмбеддинги сразу

    watcher = Watcher(areas, watch, on_events, journal=journal)
    embed_thread: threading.Thread | None = None
    if embed_worker is not None:
        embed_thread = threading.Thread(
            target=_embed_loop,
            args=(
                embed_worker,
                stop,
                kick,
                5.0,
                settings.home / "embed_errors.log",
            ),
            daemon=True,
            name="embed",
        )

    print(
        f"Областей: {len(areas)}, интервал скана: {watch.poll_interval} с", flush=True
    )
    print(embed_label, flush=True)
    print(
        f"Очередь эмбеддингов при старте: {store.counts()['pending_embeddings']} задач",
        flush=True,
    )
    print(
        f"Ошибки эмбеддингов пишутся в {settings.home / 'embed_errors.log'}",
        flush=True,
    )
    # Файл лога создаётся при каждом запуске — его наличие само по себе
    # говорит, что сервис дошёл до фонового цикла. Если файла нет, запуск
    # упал раньше (конфиг, импорт, OOM) — тогда смотри вывод run.sh.
    _append_embed_error(
        settings.home / "embed_errors.log",
        "запуск: " + embed_label,
    )
    watcher.start()
    if embed_thread is not None:
        embed_thread.start()
    print(f"Persinder слушает http://{settings.host}:{settings.port}", flush=True)
    try:
        serve(ingest, search, settings.host, settings.port)
    except KeyboardInterrupt:
        pass
    finally:
        print("Останавливаюсь — завершаю watcher и эмбеддинги...", flush=True)
        stop.set()
        kick.set()
        watcher.stop()
        if embed_thread is not None:
            embed_thread.join(10.0)
            if not embed_thread.is_alive() and embed_store is not None:
                embed_store.close()
        store.close()
        print("Persinder остановлен.", flush=True)
    return 0


def print_stats(stats: object) -> None:
    """Печать итогов обработки."""
    print(stats, flush=True)


def format_recovery(stats: RecoveryStats) -> str:
    """Человекочитаемый отчёт восстановления (оформление — на слое CLI)."""
    return (
        f"Ingest: проверено незавершённых вызовов {stats.checked}, "
        f"убрано брошенных текстов {stats.removed}"
    )


def format_embed(stats: EmbedStats) -> str:
    """Человекочитаемый отчёт прохода воркера эмбеддингов."""
    if stats.docs == 0 and stats.skipped == 0 and stats.errors == 0:
        return "Embed: очередь пуста"
    return (
        f"Embed: документов {stats.docs}, чанков {stats.chunks}, "
        f"сайдкаров {stats.sidecars}, пропущено {stats.skipped}, "
        f"ошибок {stats.errors}"
    )


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


def build_embed_worker(
    settings: Settings,
) -> tuple[EmbedWorker, SqliteStore, str]:
    """Собрать воркер эмбеддингов; вернуть (воркер, сторе, подпись моделей).

    Эмбеддер подбирается фабрикой по ключу задачи из очереди: в одном проходе
    могут встретиться задачи разных моделей (миграция, D24) — у каждой свой
    сайдкар. Заглушка placeholder-v1 встроена; реальная e5-small берётся из
    ``models_dir/<ключ>/``, если установлена.
    """
    store = SqliteStore(settings.db_path)
    textstore = FsTextStore(settings.text_dir)
    label = describe_models(settings)
    worker = EmbedWorker(
        store=store,
        textstore=textstore,
        embedder_factory=lambda key: build_embedder(settings.models_dir, key),
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        virtual_dir=settings.virtual_dir,
        ann_dir=settings.ann_dir,
    )
    return worker, store, label


def _drain(worker: EmbedWorker, limit: int) -> EmbedStats:
    """Разобрать очередь до пустой (миграция, ``embed --background``)."""
    total = EmbedStats()
    while True:
        stats = worker.run_once(limit)
        total.docs += stats.docs
        total.chunks += stats.chunks
        total.sidecars += stats.sidecars
        total.skipped += stats.skipped
        total.errors += stats.errors
        total.messages.extend(stats.messages)
        if stats.docs == 0 and stats.skipped == 0 and stats.errors == 0:
            break
        time.sleep(0.2)
    return total


def _append_embed_error(log_path: Path | None, message: str) -> None:
    """Дописать строку ошибки в ``<home>/embed_errors.log`` (best-effort).

    Лог нужен, чтобы сбой был виден после факта: пользователь не сидит у
    консоли, а отсутствие папки/сайдкара замечает позже.
    """
    if log_path is None:
        return
    try:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except OSError:
        pass  # лог не должен ронять сервис


def _embed_loop(
    worker: EmbedWorker,
    stop: threading.Event,
    kick: threading.Event,
    interval: float = 5.0,
    log_path: Path | None = None,
) -> None:
    """Фоновый разбор очереди эмбеддингов, пока сервис жив.

    Один тик — порция задач (``run_once``); пока очередь не опустела, тики идут
    без паузы, на пустой очереди — пауза ``interval``. ``kick`` будит цикл сразу
    после индексации (иначе новые документы ждали бы тика). Ошибка одного
    прохода не валит сервис: задачи остаются в очереди и уходят в следующий тик.
    Ошибки дублируются в ``log_path`` (``<home>/embed_errors.log``).
    """
    while not stop.is_set():
        try:
            stats = worker.run_once(100)
        except Exception as exc:  # noqa: BLE001 — сервис не должен падать
            message = f"ошибка прохода: {exc}"
            print(f"Embed: {message}", flush=True)
            _append_embed_error(log_path, message)
            kick.wait(interval)
            kick.clear()
            continue
        if stats.docs or stats.skipped or stats.errors:
            print(format_embed(stats), flush=True)
            for message in stats.messages[:10]:
                print(f"Embed: {message}", flush=True)
                _append_embed_error(log_path, message)
        if stats.docs == 0:
            kick.wait(interval)
            kick.clear()


def run_embed(
    home: Path | None, limit: int, all_: bool, model: str | None, background: bool
) -> int:
    """Эмбеддинги: очередь → векторы → сайдкары (§7)."""
    settings = Settings.default()
    if home:
        settings = replace(settings, home=home)
    settings.ensure()
    if all_:
        key = model or load(settings.models_state_path).embedding
        store = SqliteStore(settings.db_path)
        try:
            store.enqueue_all([key])
        finally:
            store.close()
        print(f"поставлен пересчёт всех источников под {key}")
    worker, store, label = build_embed_worker(settings)
    try:
        stats = _drain(worker, limit) if background else worker.run_once(limit)
    finally:
        store.close()
    print(label)
    print(format_embed(stats))
    return 0


# --- модель-менеджмент (D24): явные ручки, миграция стартует при install ---


def run_model_list(settings: Settings) -> int:
    """Установленные модели, активные хендлы и каталог известных."""
    root = settings.models_dir
    models = load(settings.models_state_path)
    print(
        f"активные (профиль): embedding = {models.embedding}, search = {models.search}"
    )
    print("установлено:")
    installed = installed_keys(root)
    for key in installed:
        mark = "  * активна" if key in (models.embedding, models.search) else ""
        print(f"  {key}{mark}")
    if not installed:
        print("  (нет — работает встроенная заглушка placeholder-v1)")
    print("каталог (доступно для install):")
    for key in sorted(CATALOG):
        print(f"  {key} — {CATALOG[key].title}")
    return 0


def run_model_install(settings: Settings, key: str) -> int:
    """Скачать модель, переключить embedding и поставить миграцию в очередь.

    Поиск при этом не трогается — он остаётся на старой модели, пока по новой
    не пересчитаны все существующие файлы (разбираются обычными ``embed``).
    """
    if key == PLACEHOLDER:
        print("заглушка placeholder-v1 встроена, устанавливать её не нужно.")
        return 1
    root = settings.models_dir
    root.mkdir(parents=True, exist_ok=True)
    if not is_installed(root, key):
        print(f"качаю {key} -> {root / key} ...")
        install(root, key)
        print("скачано.")
    else:
        print(f"{key} уже установлена.")

    models = load(settings.models_state_path)
    if models.embedding == key:
        print(f"embedding уже {key}.")
    else:
        save(
            ModelState(embedding=key, search=models.search),
            settings.models_state_path,
        )
        store = SqliteStore(settings.db_path)
        try:
            store.enqueue_all([key])
        finally:
            store.close()
        print(
            f"embedding переключён на {key} — пересчёт существующих файлов в очереди."
        )
    print(
        "поиск остаётся на старой модели. Когда очередь разобрана "
        "(persinder embed --background), переключи поиск: persinder model use ..."
    )
    return 0


def run_model_use(settings: Settings, key: str) -> int:
    """Переключить поиск (и embedding) на модель — она должна быть установлена.

    Именно здесь поиск переходит на новую модель: к этому моменту по ней уже
    должны быть пересчитаны существующие файлы (миграция стартовала при install).
    """
    root = settings.models_dir
    if key != PLACEHOLDER and not is_installed(root, key):
        print(f"модель не установлена: {key} (сначала persinder model install {key}).")
        return 1
    models = load(settings.models_state_path)
    if models.embedding == key and models.search == key:
        print(f"{key} уже активна (embedding и search).")
        return 0
    save(ModelState(embedding=key, search=key), settings.models_state_path)
    print(f"модель {key} активна: embedding = search = {key}.")
    return 0


def run_model_remove(settings: Settings, key: str) -> int:
    """Удалить модель вместе с её сайдкарами (не раньше, D24).

    Пока модель активна (embedding или search) — отказ: другие устройства могут
    ещё работать со старой моделью, решать, когда её убрать, должен юзер.
    """
    if key == PLACEHOLDER:
        print("встроенную заглушку placeholder-v1 удалить нельзя.")
        return 1
    models = load(settings.models_state_path)
    if key in (models.embedding, models.search):
        print(f"модель {key} активна — сначала переключи: persinder model use ...")
        return 1
    # Модель может быть активной не только в models.json, но и для папки в
    # конфиге: убирать её тогда нельзя, иначе конфиг станет невалидным.
    config_path = _candidate_config(settings)
    if config_path is not None:
        try:
            areas, _watch = load_config(
                config_path, known_formats=default_registry().known_names()
            )
        except ConfigError:
            areas = ()
        if any(area.embedding_model == key for area in areas):
            print(
                f"модель {key} выбрана для папки в {config_path} — "
                "сначала убери embedding_model из конфига."
            )
            return 1
    root = settings.models_dir
    if not is_installed(root, key):
        print(f"модель не установлена: {key}.")
        return 1
    store = SqliteStore(settings.db_path)
    try:
        sources = store.all_sources()
        store.drop_queue_model(key)
        store.drop_embeddings_model(key)
    finally:
        store.close()
    ann_path = settings.ann_dir / f"{key}.usearch"
    if ann_path.exists():
        ann_path.unlink()
    delete_model_sidecars(settings.virtual_dir, key, sources)
    remove_weights(root, key)
    print(f"удалена {key}: веса, сайдкары, векторы и ANN-индекс.")
    return 0


SEARCH_HTTP_TIMEOUT = 10.0  # секунд; локальный сервер отвечает или отказывает сразу


def _json_str(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("string expected")
    return value


def _json_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("int expected")
    return value


def _json_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("number expected")
    return float(value)


def _json_opt_str(value: object) -> str:
    return "" if value is None else _json_str(value)


def _hits_from_json(raw: object) -> tuple[SearchHit, ...]:
    """Разобрать список хитов ответа ``GET /search``; ошибка — ``ValueError``."""
    if not isinstance(raw, list):
        raise ValueError("hits list expected")
    hits: list[SearchHit] = []
    for item in cast("list[object]", raw):
        if not isinstance(item, dict):
            raise ValueError("hit object expected")
        data = cast("dict[str, object]", item)
        context = data.get("context")
        hits.append(
            SearchHit(
                document_id=_json_str(data["document_id"]),
                chunk_index=_json_int(data["chunk_index"]),
                score=_json_float(data["score"]),
                context=None if context is None else _json_str(context),
                area=_json_opt_str(data.get("area")),
                source_path=_json_opt_str(data.get("source_path")),
                filename=_json_opt_str(data.get("filename")),
            )
        )
    return tuple(hits)


def search_over_http(
    settings: Settings,
    text: str,
    limit: int,
    min_score: float | None = None,
    morph_limit: int | None = None,
    semantic_limit: int | None = None,
) -> SearchResult | None:
    """Поиск через запущенный ``serve``; ``None`` — сервер недоступен.

    Скоупы и модели берутся у сервера, поэтому CLI видит то же, что HTTP.
    Любая ошибка транспорта или формы ответа — повод для локального fallback.
    """
    params = {"q": text, "limit": str(limit)}
    if min_score is not None:
        params["min_score"] = str(min_score)
    if morph_limit is not None:
        params["morph_limit"] = str(morph_limit)
    if semantic_limit is not None:
        params["semantic_limit"] = str(semantic_limit)
    query = urllib.parse.urlencode(params)
    url = f"http://{settings.host}:{settings.port}/search?{query}"
    try:
        with urllib.request.urlopen(url, timeout=SEARCH_HTTP_TIMEOUT) as resp:
            raw: object = json.loads(resp.read().decode("utf-8"))
        if not isinstance(raw, dict):
            return None
        data = cast("dict[str, object]", raw)
        echo = data.get("query")
        return SearchResult(
            query=text if echo is None else _json_str(echo),
            hits=_hits_from_json(data.get("hits")),
            semantic_hits=_hits_from_json(data.get("semantic_hits")),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _local_search(
    settings: Settings,
    text: str,
    limit: int,
    config_path: Path | None = None,
    min_score: float | None = None,
    morph_limit: int | None = None,
    semantic_limit: int | None = None,
) -> SearchResult:
    """Локальный поиск в БД профиля (сервера нет или запрошен ``--local``).

    Если найден конфиг областей, семантика ищет по всем активным моделям и
    только в тех папках, где этими моделями индексировали; глобальное
    ``semantic: false`` выключает векторный слой совсем.
    """
    settings.ensure()
    areas: tuple[AreaConfig, ...] | None = None
    semantic = True
    config_min_score: float | None = None
    config_path = config_path or _candidate_config(settings)
    if config_path is not None:
        registry = default_registry()
        areas, watch = load_config(
            config_path,
            known_formats=registry.known_names(),
            known_models=_known_models(settings),
        )
        semantic = watch.semantic
        config_min_score = watch.min_score
    _ingest, search, store = build_service(
        settings,
        areas=areas,
        semantic=semantic,
        min_score=config_min_score,
    )
    try:
        return search.search(
            SearchQuery(
                text=text,
                limit=limit,
                min_score=min_score,
                morph_limit=morph_limit,
                semantic_limit=semantic_limit,
            )
        )
    finally:
        store.close()


def _hit_source(hit: SearchHit) -> str:
    """Источник для строки результата: путь (или имя) и область."""
    label = hit.source_path or hit.filename or hit.document_id
    return f"{label} ({hit.area})" if hit.area else label


def _print_search_result(result: SearchResult) -> None:
    print(f"морфология: {len(result.hits)}, семантика: {len(result.semantic_hits)}")
    for kind, hits in (
        ("морфология", result.hits),
        ("семантика", result.semantic_hits),
    ):
        for hit in hits:
            print(
                f"  [{kind}] {_hit_source(hit)} "
                f"чанк {hit.chunk_index} = {hit.score:.4f}"
            )


def run_search(
    home: Path | None,
    text: str,
    limit: int,
    config_path: Path | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
    local: bool = False,
    min_score: float | None = None,
    morph_limit: int | None = None,
    semantic_limit: int | None = None,
) -> int:
    """Поиск из консоли: сначала через запущенный сервер, затем локально.

    HTTP-first: сервер — источник истины по моделям и скоупам, поэтому CLI
    видит те же результаты, что ``GET /search``. Локальный поиск — fallback,
    когда сервер не отвечает, или принудительно (``--local``). Явный
    ``--config`` тоже включает локальный режим: он задаёт конфиг CLI, а не
    сервера. ``min_score`` — порог косинуса семантического слоя;
    ``morph_limit``/``semantic_limit`` — отдельные лимиты слоёв.
    """
    settings = Settings.default()
    if home:
        settings = replace(settings, home=home)
    if host:
        settings = replace(settings, host=host)
    if port is not None:
        settings = replace(settings, port=port)

    if not (local or config_path is not None):
        result = search_over_http(
            settings,
            text,
            limit,
            min_score,
            morph_limit,
            semantic_limit,
        )
        if result is not None:
            _print_search_result(result)
            return 0
        print(
            f"ВНИМАНИЕ: сервер http://{settings.host}:{settings.port} не отвечает — "
            "локальный поиск; скоупы и модели могут отличаться от сервера.",
            flush=True,
        )

    result = _local_search(
        settings,
        text,
        limit,
        config_path,
        min_score,
        morph_limit,
        semantic_limit,
    )
    _print_search_result(result)
    return 0


def run_ann_rebuild(home: Path | None, model: str | None) -> int:
    """Пересобрать ANN-индексы из таблицы ``embeddings`` (§8).

    Нужно после холодного старта устройства (таблица есть, индекса нет) и при
    рассинхронизации. По умолчанию — все модели, у которых есть векторы.
    """
    settings = Settings.default()
    if home:
        settings = replace(settings, home=home)
    settings.ensure()
    store = SqliteStore(settings.db_path)
    try:
        keys = [model] if model else store.embedding_models()
        for key in keys:
            count = rebuild_index(
                store,
                lambda k: build_embedder(settings.models_dir, k),
                settings.ann_dir,
                key,
            )
            print(f"{key}: ANN-индекс пересобран, ключей {count}.")
        return 0
    finally:
        store.close()


def _wait_forever() -> None:
    import time as _time

    while True:
        _time.sleep(3600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="persinder", description="Персональный индексатор Persinder"
    )
    sub = parser.add_subparsers(dest="command")
    # Дефолт --home — тот же каталог приложения, что Settings.default(): ~/.persinder.
    default_home = Settings.default().home
    home_help = f"каталог приложения (по умолчанию {default_home})"

    serve_p = sub.add_parser("serve", help="HTTP-сервис + watcher + эмбеддинги")
    serve_p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="YAML-конфиг областей (по умолчанию <home>/persinder.yaml|yml)",
    )
    serve_p.add_argument("--home", type=Path, default=None, help=home_help)
    serve_p.add_argument("--host", default=None)
    serve_p.add_argument("--port", type=int, default=None)
    serve_p.add_argument(
        "--reindex",
        action="store_true",
        help="полная переиндексация: сбросить журнал, пересканировать все области "
        "и пересчитать все эмбеддинги при старте",
    )

    watch_p = sub.add_parser("watch", help="следить за областями и индексировать")
    watch_p.add_argument("--config", type=Path, required=True, help="YAML-конфиг")
    watch_p.add_argument("--home", type=Path, default=None, help=home_help)

    embed_p = sub.add_parser("embed", help="эмбеддинги: очередь → векторы → сайдкары")
    embed_p.add_argument("--home", type=Path, default=None, help=home_help)
    embed_p.add_argument("--limit", type=int, default=100, help="задач за проход")
    embed_p.add_argument(
        "--all",
        action="store_true",
        help="поставить пересчёт всех источников под моделью, затем проход",
    )
    embed_p.add_argument("--model", default=None, help="модель для --all")
    embed_p.add_argument(
        "--background",
        action="store_true",
        help="разбирать очередь до пустой (миграция)",
    )

    model_p = sub.add_parser("model", help="управление моделями эмбеддингов (D24)")
    model_common = argparse.ArgumentParser(add_help=False)
    model_common.add_argument("--home", type=Path, default=None, help=home_help)
    model_sub = model_p.add_subparsers(dest="model_command")

    model_sub.add_parser(
        "list", parents=[model_common], help="установленные + активные + каталог"
    )
    install_p = model_sub.add_parser(
        "install", parents=[model_common], help="скачать модель и начать миграцию"
    )
    install_p.add_argument("key")
    use_p = model_sub.add_parser(
        "use", parents=[model_common], help="переключить поиск/embedding на модель"
    )
    use_p.add_argument("key")
    remove_p = model_sub.add_parser(
        "remove", parents=[model_common], help="удалить модель и её сайдкары"
    )
    remove_p.add_argument("key")

    search_p = sub.add_parser(
        "search", help="поиск из консоли (морфология + семантика)"
    )
    search_p.add_argument("--home", type=Path, default=None, help=home_help)
    search_p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="YAML-конфиг областей для локального поиска (включает --local; "
        "по умолчанию ищется в cwd и <home>)",
    )
    search_p.add_argument(
        "--host", default=None, help="хост сервера (по умолчанию 127.0.0.1)"
    )
    search_p.add_argument(
        "--port", type=int, default=None, help="порт сервера (по умолчанию 8756)"
    )
    search_p.add_argument(
        "--local",
        action="store_true",
        help="не обращаться к серверу, искать в БД профиля",
    )
    search_p.add_argument(
        "--min-score",
        type=float,
        default=None,
        help="порог косинуса семантического слоя (например 0.85); "
        "абсолютное значение зависит от модели",
    )
    search_p.add_argument(
        "--morph-limit",
        type=int,
        default=None,
        help="лимит морфологического слоя (0 — выключить; по умолчанию --limit)",
    )
    search_p.add_argument(
        "--semantic-limit",
        type=int,
        default=None,
        help="лимит семантического слоя (0 — выключить; по умолчанию --limit)",
    )
    search_p.add_argument("--limit", type=int, default=20)
    search_p.add_argument("text")

    ann_p = sub.add_parser("ann", help="ANN-индекс (USearch): пересборка из таблицы")
    ann_p.add_argument("--home", type=Path, default=None, help=home_help)
    ann_p.add_argument(
        "--model", default=None, help="только эта модель (по умолчанию все)"
    )
    ann_p.add_argument(
        "subcommand",
        nargs="?",
        default="rebuild",
        choices=["rebuild"],
        help="операция (сейчас только rebuild)",
    )

    # Совместимость: без подкоманды — serve с флагами. ``dest`` отдельный:
    # значение, переданное до подкоманды, не должно затираться её дефолтом.
    parser.add_argument(
        "--home", dest="home_top", type=Path, default=None, help=argparse.SUPPRESS
    )
    parser.add_argument("--host", dest="host_top", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--port", dest="port_top", type=int, default=None, help=argparse.SUPPRESS
    )

    args = parser.parse_args(argv)
    # ``--home`` понимается и до подкоманды, и после: у подкоманд свой флаг.
    # Без этого ``persinder --home X search`` молча искал бы в ~/.persinder.
    args.home = getattr(args, "home", None) or args.home_top or default_home
    if getattr(args, "host", None) is None:
        args.host = getattr(args, "host_top", None)
    if getattr(args, "port", None) is None:
        args.port = getattr(args, "port_top", None)
    if args.command == "watch":
        return run_watch(args.config, args.home)
    if args.command == "embed":
        return run_embed(args.home, args.limit, args.all, args.model, args.background)
    if args.command == "search":
        return run_search(
            args.home,
            args.text,
            args.limit,
            args.config,
            host=args.host,
            port=args.port,
            local=args.local,
            min_score=args.min_score,
            morph_limit=args.morph_limit,
            semantic_limit=args.semantic_limit,
        )
    if args.command == "ann":
        return run_ann_rebuild(args.home, args.model)
    if args.command == "model":
        return _model_main(parser, args)
    if args.command == "serve" or args.command is None:
        return _serve_main(parser, args)
    parser.print_help()
    return 1


def _model_main(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    settings = Settings.default()
    if getattr(args, "home", None):
        settings = replace(settings, home=args.home)
    if args.model_command is None:
        parser.parse_args([args.command, "--help"])
        return 1
    if args.model_command == "list":
        return run_model_list(settings)
    if args.model_command == "install":
        return run_model_install(settings, args.key)
    if args.model_command == "use":
        return run_model_use(settings, args.key)
    if args.model_command == "remove":
        return run_model_remove(settings, args.key)
    return 1


def _config_candidates(settings: Settings) -> tuple[Path, ...]:
    """Где искать конфиг, если ``--config`` не задан: папка запуска, затем home."""
    names = ("persinder.yaml", "persinder.yml")
    return tuple(Path.cwd() / name for name in names) + tuple(
        settings.home / name for name in names
    )


def _serve_main(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    settings = Settings.default()
    if getattr(args, "home", None):
        settings = replace(settings, home=args.home)
    if getattr(args, "host", None):
        settings = replace(settings, host=args.host)
    if getattr(args, "port", None):
        settings = replace(settings, port=args.port)
    # Home и подкаталоги существуют всегда — даже если конфига нет (HTTP-only).
    settings = settings.ensure()

    config_path: Path | None = getattr(args, "config", None)
    if config_path is None:
        for candidate in _config_candidates(settings):
            if candidate.is_file():
                config_path = candidate
                print(f"Конфиг не задан — использую {config_path}", flush=True)
                break
    if config_path is not None:
        return run_serve(config_path, settings, getattr(args, "reindex", False))

    # Конфига нет ни явного, ни в папке запуска/home — деградация до HTTP-only:
    # watcher/эмбеддинги не знают областей.
    print(
        "ВНИМАНИЕ: конфиг не найден — работает только HTTP (приём + поиск), "
        "без watcher и эмбеддингов.\n"
        f"Искал: persinder.{{yaml,yml}} в текущей папке и в {settings.home}\n"
        "Полный сервис: persinder serve --config persinder.yaml",
        flush=True,
    )
    ingest, search, store = build_service(settings)
    recovery = ingest.recover()
    if recovery.removed:
        print(format_recovery(recovery))
    print(f"Persinder слушает http://{settings.host}:{settings.port}", flush=True)
    try:
        serve(ingest, search, settings.host, settings.port)
    except KeyboardInterrupt:
        pass
    finally:
        print("Останавливаюсь (Ctrl+C)...", flush=True)
        store.close()
        print("Persinder остановлен.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
