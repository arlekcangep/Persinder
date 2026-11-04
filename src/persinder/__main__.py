"""Запуск HTTP-сервиса приёма: ``python -m persinder``."""

from __future__ import annotations

import argparse
from pathlib import Path

from .api import serve
from .config import Settings
from .ingest import IngestService
from .morphology import default_stemmer
from .store import SqliteStore
from .textstore import FsTextStore


def build_service(settings: Settings) -> tuple[IngestService, SqliteStore]:
    """Собрать сервис приёма из настроек."""
    settings.ensure()
    store = SqliteStore(settings.db_path)
    textstore = FsTextStore(settings.text_dir)
    service = IngestService(
        store=store,
        textstore=textstore,
        stemmer=default_stemmer(),
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
    )
    return service, store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="persinder", description="Сервис приёма Persinder")
    parser.add_argument("--data-dir", type=Path, default=None, help="каталог данных")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    settings = Settings.default()
    if args.data_dir:
        settings = Settings(
            data_dir=args.data_dir,
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
            host=settings.host,
            port=settings.port,
        )
    if args.host:
        settings = Settings(**{**settings.__dict__, "host": args.host})
    if args.port:
        settings = Settings(**{**settings.__dict__, "port": args.port})

    service, store = build_service(settings)
    print(f"Persinder слушает http://{settings.host}:{settings.port}")
    try:
        serve(service, settings.host, settings.port)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
