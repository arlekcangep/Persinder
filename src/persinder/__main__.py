"""Запуск HTTP-сервиса Persinder и watcher'а: ``python -m persinder``."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from .api import serve
from .config import Settings
from .ingest import IngestService
from .morphology import default_stemmer
from .search import SearchService
from .sources import (
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
    pipeline = IndexingPipeline(ingest, registry, metastore=store, journal=journal)

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

    # Совместимость: без подкоманды — serve с флагами.
    parser.add_argument("--data-dir", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--host", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=None, help=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    if args.command == "watch":
        return run_watch(args.config, args.data_dir)
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
    print(f"Persinder слушает http://{settings.host}:{settings.port}")
    try:
        serve(ingest, search, settings.host, settings.port)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
