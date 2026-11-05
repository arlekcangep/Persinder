"""Запуск HTTP-сервиса Persinder и watcher'а: ``python -m persinder``."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from .api import serve
from .config import Settings
from .embed import (
    EmbedStats,
    EmbedWorker,
    OnnxEmbedder,
    PlaceholderEmbedder,
)
from .ingest import IngestJournal, IngestService, RecoveryStats
from .interfaces import Embedder
from .morphology import default_stemmer
from .search import SearchService
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


def build_service(
    settings: Settings,
) -> tuple[IngestService, SearchService, SqliteStore]:
    """Собрать сервисы приёма и поиска из настроек."""
    settings.ensure()
    store = SqliteStore(settings.db_path)
    textstore = FsTextStore(settings.text_dir)
    stemmer = default_stemmer()
    ingest = IngestService(
        store=store,
        textstore=textstore,
        stemmer=stemmer,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        journal=IngestJournal(settings.data_dir / "ingest.jsonl"),
        virtual_dir=settings.virtual_dir,
    )
    search = SearchService(store=store, stemmer=stemmer)
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


def build_embed_worker(
    settings: Settings,
) -> tuple[EmbedWorker, SqliteStore, str]:
    """Собрать воркер эмбеддингов; вернуть (воркер, сторе, подпись модели).

    Реальная модель e5-small (ONNX) — если она скачана в ``Settings.model_dir``;
    иначе детерминированная заглушка ``PlaceholderEmbedder`` (тот же порт
    ``Embedder``). Отпечаток модели попадает в сайдкары, поэтому смена
    эмбеддера пересчитает старые векторы.
    """
    store = SqliteStore(settings.db_path)
    textstore = FsTextStore(settings.text_dir)
    embedder: Embedder
    if OnnxEmbedder.available(settings.model_dir):
        embedder = OnnxEmbedder(settings.model_dir)
        label = f"модель: {embedder.name}"
    else:
        embedder = PlaceholderEmbedder()
        label = (
            f"модель: {embedder.name} (заглушка; скачай e5-small: "
            "python scripts/download_model.py)"
        )
    worker = EmbedWorker(
        store=store,
        textstore=textstore,
        embedder=embedder,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        virtual_dir=settings.virtual_dir,
        fingerprint_path=settings.data_dir / "embed_fingerprint.json",
    )
    return worker, store, label


def run_embed(data_dir: Path | None, limit: int) -> int:
    """Эмбеддинги по требованию: очередь → векторы → сайдкары (§7)."""
    settings = Settings.default()
    if data_dir:
        settings = replace(settings, data_dir=data_dir)
    settings.ensure()
    worker, store, label = build_embed_worker(settings)
    try:
        stats = worker.run_once(limit)
    finally:
        store.close()
    print(label)
    print(format_embed(stats))
    return 0


def _wait_forever() -> None:
    import time

    while True:
        time.sleep(3600)


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

    # Совместимость: без подкоманды — serve с флагами.
    parser.add_argument("--data-dir", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--host", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=None, help=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    if args.command == "watch":
        return run_watch(args.config, args.data_dir)
    if args.command == "embed":
        return run_embed(args.data_dir, args.limit)
    if args.command == "serve" or args.command is None:
        return _serve_main(parser, args)
    parser.print_help()
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
