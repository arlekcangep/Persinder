"""Запуск HTTP-сервиса Persinder, watcher'а и эмбеддингов: ``python -m persinder``."""

from __future__ import annotations

import argparse
import threading
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
    ScanResult,
    Watcher,
    default_registry,
    load_config,
)
from .store import SqliteStore
from .textstore import FsTextStore
from .vectorindex import VectorSearch, rebuild_index


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
        journal=IngestJournal(settings.home / "ingest.jsonl"),
        virtual_dir=settings.virtual_dir,
        embedding_models=models.queue_models(),
    )
    vector_search = VectorSearch(
        store=store,
        embedder_factory=lambda key: build_embedder(settings.models_dir, key),
        search_model=models.search,
        ann_dir=settings.ann_dir,
    )
    search = SearchService(store=store, stemmer=stemmer, vector_search=vector_search)
    return ingest, search, store


def run_watch(config_path: Path, home: Path | None) -> int:
    """Запустить watcher: скан областей и индексация найденного."""
    registry = default_registry()
    areas, watch = load_config(config_path, known_formats=registry.known_names())
    settings = Settings.default()
    if home:
        settings = replace(settings, home=home)
    ingest, _search, store = build_service(settings)
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


def run_serve(config_path: Path, settings: Settings) -> int:
    """Полный сервис: HTTP + watcher + эмбеддинги + восстановление после сбоев.

    Это единая точка запуска всех фоновых активностей:

    * журнал ``compact`` — убрать старую историю, сохранив последние состояния;
    * ``ingest.recover`` — убрать брошенные тексты прерванных вызовов;
    * watcher — первый проход сразу доиндексирует всё новое и недоделанное
      (журнал считает «устроенными» только завершённые цепочки);
    * поток эмбеддингов — разбирает очередь в векторы/сайдкары, пока сервис жив;
    * HTTP-сервер блокирует главный поток до Ctrl+C.
    """
    settings.ensure()
    registry = default_registry()
    areas, watch = load_config(config_path, known_formats=registry.known_names())

    ingest, search, store = build_service(settings)
    embed_worker, embed_store, embed_label = build_embed_worker(settings)

    journal = Journal(settings.home / "journal.jsonl")
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

    pipeline = IndexingPipeline(ingest, registry, journal=journal)
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
    embed_thread = threading.Thread(
        target=_embed_loop, args=(embed_worker, stop, kick), daemon=True, name="embed"
    )

    print(
        f"Областей: {len(areas)}, интервал скана: {watch.poll_interval} с", flush=True
    )
    print(embed_label, flush=True)
    watcher.start()
    embed_thread.start()
    print(f"Persinder слушает http://{settings.host}:{settings.port}", flush=True)
    try:
        serve(ingest, search, settings.host, settings.port)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        kick.set()
        watcher.stop()
        embed_thread.join(10.0)
        if not embed_thread.is_alive():
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


def _embed_loop(
    worker: EmbedWorker,
    stop: threading.Event,
    kick: threading.Event,
    interval: float = 5.0,
) -> None:
    """Фоновый разбор очереди эмбеддингов, пока сервис жив.

    Один тик — порция задач (``run_once``); пока очередь не опустела, тики идут
    без паузы, на пустой очереди — пауза ``interval``. ``kick`` будит цикл сразу
    после индексации (иначе новые документы ждали бы тика). Ошибка одного
    прохода не валит сервис: задачи остаются в очереди и уходят в следующий тик.
    """
    while not stop.is_set():
        try:
            stats = worker.run_once(100)
        except Exception as exc:  # noqa: BLE001 — сервис не должен падать
            print(f"Embed: ошибка прохода: {exc}", flush=True)
            kick.wait(interval)
            kick.clear()
            continue
        if stats.docs or stats.skipped or stats.errors:
            print(format_embed(stats), flush=True)
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


def run_search(home: Path | None, text: str, limit: int) -> int:
    """Поиск из консоли: морфология + семантика (модуль 3)."""
    settings = Settings.default()
    if home:
        settings = replace(settings, home=home)
    settings.ensure()
    store = SqliteStore(settings.db_path)
    try:
        stemmer = default_stemmer()
        models = load(settings.models_state_path)
        vector_search = VectorSearch(
            store=store,
            embedder_factory=lambda key: build_embedder(settings.models_dir, key),
            search_model=models.search,
            ann_dir=settings.ann_dir,
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
    serve_p.add_argument("--home", type=Path, default=default_home, help=home_help)
    serve_p.add_argument("--host", default=None)
    serve_p.add_argument("--port", type=int, default=None)

    watch_p = sub.add_parser("watch", help="следить за областями и индексировать")
    watch_p.add_argument("--config", type=Path, required=True, help="YAML-конфиг")
    watch_p.add_argument("--home", type=Path, default=default_home, help=home_help)

    embed_p = sub.add_parser("embed", help="эмбеддинги: очередь → векторы → сайдкары")
    embed_p.add_argument("--home", type=Path, default=default_home, help=home_help)
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
    model_common.add_argument("--home", type=Path, default=default_home, help=home_help)
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
    search_p.add_argument("--home", type=Path, default=default_home, help=home_help)
    search_p.add_argument("--limit", type=int, default=20)
    search_p.add_argument("text")

    ann_p = sub.add_parser("ann", help="ANN-индекс (USearch): пересборка из таблицы")
    ann_p.add_argument("--home", type=Path, default=default_home, help=home_help)
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

    # Совместимость: без подкоманды — serve с флагами.
    parser.add_argument(
        "--home", type=Path, default=default_home, help=argparse.SUPPRESS
    )
    parser.add_argument("--host", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=None, help=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    if args.command == "watch":
        return run_watch(args.config, args.home)
    if args.command == "embed":
        return run_embed(args.home, args.limit, args.all, args.model, args.background)
    if args.command == "search":
        return run_search(args.home, args.text, args.limit)
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


def _serve_main(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    settings = Settings.default()
    if getattr(args, "home", None):
        settings = replace(settings, home=args.home)
    if getattr(args, "host", None):
        settings = replace(settings, host=args.host)
    if getattr(args, "port", None):
        settings = replace(settings, port=args.port)

    config_path: Path | None = getattr(args, "config", None)
    if config_path is None:
        # --config не задан: ищем конфиг в каталоге приложения.
        for name in ("persinder.yaml", "persinder.yml"):
            candidate = settings.home / name
            if candidate.is_file():
                config_path = candidate
                print(f"Конфиг не задан — использую {config_path}", flush=True)
                break
    if config_path is not None:
        return run_serve(config_path, settings)

    # Конфига нет ни явного, ни в home — деградация до HTTP-only:
    # watcher/эмбеддинги не знают областей.
    print(
        "ВНИМАНИЕ: конфиг не найден — работает только HTTP (приём + поиск), "
        "без watcher и эмбеддингов.\n"
        f"Искал: {settings.home}/persinder.{{yaml,yml}}\n"
        "Полный сервис: persinder serve --config persinder.yaml",
        flush=True,
    )
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
