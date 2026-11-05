"""Запуск HTTP-сервиса Persinder, watcher'а и эмбеддингов: ``python -m persinder``."""

from __future__ import annotations

import argparse
import time
from dataclasses import replace
from pathlib import Path

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
    install,
    installed_keys,
    is_installed,
    load,
    remove_weights,
    save,
)
from .ingest import IngestJournal, IngestService, RecoveryStats
from .morphology import default_stemmer
from .search import SearchQuery, SearchService
from .sources import (
    JOURNAL_RETENTION_DAYS,
    IndexingPipeline,
    Journal,
    Watcher,
    default_registry,
    load_config,
)
from .store import SqliteStore
from .textstore import FsTextStore
from .vectorindex import VectorSearch


def build_service(
    settings: Settings,
) -> tuple[IngestService, SearchService, SqliteStore]:
    """Собрать сервисы приёма и поиска из настроек."""
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
        journal=IngestJournal(settings.data_dir / "ingest.jsonl"),
        virtual_dir=settings.virtual_dir,
        embedding_models=models.queue_models(),
    )
    vector_search = VectorSearch(
        store=store,
        embedder_factory=lambda key: build_embedder(settings.models_dir, key),
        search_model=models.search,
    )
    search = SearchService(store=store, stemmer=stemmer, vector_search=vector_search)
    return ingest, search, store


def run_watch(config_path: Path, data_dir: Path | None) -> int:
    """Запустить watcher: скан областей и индексация найденного."""
    registry = default_registry()
    areas, watch = load_config(config_path, known_formats=registry.known_names())
    settings = Settings.default()
    if data_dir:
        settings = replace(settings, data_dir=data_dir)
    ingest, _search, store = build_service(settings)
    journal = Journal(settings.data_dir / "journal.jsonl")
    dropped = journal.compact()
    if dropped:
        print(
            f"Журнал: обрезано старых записей: {dropped} "
            f"(старше {JOURNAL_RETENTION_DAYS} дней)"
        )
    recovery = ingest.recover()
    if recovery.removed:
        print(format_recovery(recovery))
    pipeline = IndexingPipeline(ingest, registry, journal=journal)

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


def print_stats(stats: object) -> None:
    """Печать итогов обработки."""
    print(stats)


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


def run_embed(
    data_dir: Path | None, limit: int, all_: bool, model: str | None, background: bool
) -> int:
    """Эмбеддинги: очередь → векторы → сайдкары (§7)."""
    settings = Settings.default()
    if data_dir:
        settings = replace(settings, data_dir=data_dir)
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
    delete_model_sidecars(settings.virtual_dir, key, sources)
    remove_weights(root, key)
    print(f"удалена {key}: веса, сайдкары (файловые и виртуальные), задачи из очереди.")
    return 0


def run_search(data_dir: Path | None, text: str, limit: int) -> int:
    """Поиск из консоли: морфология + семантика (модуль 3)."""
    settings = Settings.default()
    if data_dir:
        settings = replace(settings, data_dir=data_dir)
    settings.ensure()
    store = SqliteStore(settings.db_path)
    try:
        stemmer = default_stemmer()
        models = load(settings.models_state_path)
        vector_search = VectorSearch(
            store=store,
            embedder_factory=lambda key: build_embedder(settings.models_dir, key),
            search_model=models.search,
        )
        search = SearchService(
            store=store, stemmer=stemmer, vector_search=vector_search
        )
        result = search.search(SearchQuery(text=text, limit=limit))
    finally:
        store.close()
    print(f"морфология: {len(result.hits)}, семантика: {len(result.semantic_hits)}")
    for kind, hits in (
        ("морфология", result.hits),
        ("семантика", result.semantic_hits),
    ):
        for hit in hits:
            print(
                f"  [{kind}] {hit.document_id} чанк {hit.chunk_index} = {hit.score:.4f}"
            )
    return 0


def _wait_forever() -> None:
    import time as _time

    while True:
        _time.sleep(3600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="persinder", description="Персональный индексатор Persinder"
    )
    sub = parser.add_subparsers(dest="command")

    serve_p = sub.add_parser("serve", help="HTTP-сервис (приём + поиск)")
    serve_p.add_argument("--data-dir", type=Path, default=None)
    serve_p.add_argument("--host", default=None)
    serve_p.add_argument("--port", type=int, default=None)

    watch_p = sub.add_parser("watch", help="следить за областями и индексировать")
    watch_p.add_argument("--config", type=Path, required=True, help="YAML-конфиг")
    watch_p.add_argument("--data-dir", type=Path, default=None)

    embed_p = sub.add_parser("embed", help="эмбеддинги: очередь → векторы → сайдкары")
    embed_p.add_argument("--data-dir", type=Path, default=None)
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
    model_common.add_argument("--data-dir", type=Path, default=None)
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
    search_p.add_argument("--data-dir", type=Path, default=None)
    search_p.add_argument("--limit", type=int, default=20)
    search_p.add_argument("text")

    # Совместимость: без подкоманды — serve с флагами.
    parser.add_argument("--data-dir", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--host", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=None, help=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    if args.command == "watch":
        return run_watch(args.config, args.data_dir)
    if args.command == "embed":
        return run_embed(
            args.data_dir, args.limit, args.all, args.model, args.background
        )
    if args.command == "search":
        return run_search(args.data_dir, args.text, args.limit)
    if args.command == "model":
        return _model_main(parser, args)
    if args.command == "serve" or args.command is None:
        return _serve_main(parser, args)
    parser.print_help()
    return 1


def _model_main(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    settings = Settings.default()
    if getattr(args, "data_dir", None):
        settings = replace(settings, data_dir=args.data_dir)
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


def _serve_main(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    settings = Settings.default()
    if getattr(args, "data_dir", None):
        settings = replace(settings, data_dir=args.data_dir)
    if getattr(args, "host", None):
        settings = replace(settings, host=args.host)
    if getattr(args, "port", None):
        settings = replace(settings, port=args.port)

    ingest, search, store = build_service(settings)
    recovery = ingest.recover()
    if recovery.removed:
        print(format_recovery(recovery))
    print(f"Persinder слушает http://{settings.host}:{settings.port}")
    try:
        serve(ingest, search, settings.host, settings.port)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
