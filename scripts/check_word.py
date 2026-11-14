"""Почему слово не находится: проверка по шагам, только чтение.

Печатает три вещи для одного запроса и сравнивает их:

1. есть ли слово в FTS-индексе базы (``chunks_fts``) — и в каких документах;
2. что отвечает **локальный** поиск по этой базе (оба слоя);
3. что отвечает **запущенный сервер** (``GET /search``), если он доступен.

Расхождение между (2) и (3) значит, что сервер работает с другим состоянием
(старый процесс, другой home), а не что поиск не умеет искать. Расхождение
(1) и (2) — что слово потерялось на слое поиска, а не в индексе.

    python scripts/check_word.py волчок
    python scripts/check_word.py волчок --home C:\\Users\\root\\.persinder \\
        --config D:\\...\\test-content\\persinder.yaml

Скрипт ничего не пишет в базу и не считает эмбеддинги.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from persinder.__main__ import _candidate_config, build_service, search_over_http
from persinder.config import Settings
from persinder.morphology import analyze_to_string, default_stemmer
from persinder.search import SearchQuery, SearchResult
from persinder.sources import load_config

TOP = 5  # сколько хитов печатать в каждом слое


def _fts_hits(db: Path, word: str, limit: int) -> list[sqlite3.Row] | None:
    """Совпадения в FTS-индексе (запрос стеммируется так же, как при поиске)."""
    stemmed = analyze_to_string(word, default_stemmer())
    print(f"\n1. FTS-индекс (морфология): «{word}» → стем «{stemmed}»")
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        print(f"  база не открывается: {exc}")
        return None
    try:
        rows = conn.execute(
            "SELECT f.doc_id, f.chunk_index, bm25(chunks_fts) AS score"
            " FROM chunks_fts f WHERE chunks_fts MATCH ?"
            " ORDER BY score LIMIT ?",
            (stemmed, limit),
        ).fetchall()
        sources = {
            str(row["doc_id"]): row
            for row in conn.execute(
                "SELECT doc_id, area, source_path, filename FROM document_sources"
            )
        }
    except sqlite3.Error as exc:
        print(f"  запрос к FTS не удался: {exc}")
        return None
    finally:
        conn.close()

    if not rows:
        print("  НЕТ совпадений: слова нет в морфологическом индексе этой базы.")
        return []
    for row in rows:
        source = sources.get(str(row["doc_id"]))
        where = (
            source["source_path"] or source["filename"]
            if source is not None
            else f"doc {str(row['doc_id'])[:12]}…"
        )
        area = source["area"] if source is not None else "?"
        print(f"  [{area}] {where} чанк {row['chunk_index']} bm25 {row['score']:.3f}")
    return rows


def _print_result(result: SearchResult) -> None:
    print(f"  морфология: {len(result.hits)}, семантика: {len(result.semantic_hits)}")
    for kind, group in (
        ("морфология", result.hits),
        ("семантика", result.semantic_hits),
    ):
        for hit in group[:TOP]:
            where = hit.source_path or hit.filename or hit.document_id[:12]
            print(f"    [{kind}] {where} чанк {hit.chunk_index} = {hit.score:.4f}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Почему слово не находится")
    parser.add_argument("word", help="запрос (одно слово или фраза)")
    parser.add_argument("--home", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    settings = Settings.default()
    if args.home:
        settings = Settings(home=args.home.expanduser())
    if args.host:
        settings.host = args.host
    if args.port is not None:
        settings.port = args.port

    db = settings.db_path
    print(f"Home: {settings.home}")
    print(f"БД:   {db} ({'есть' if db.is_file() else 'НЕТ'})")
    if not db.is_file():
        return 1

    rows = _fts_hits(db, args.word, args.limit)
    if rows is None:
        return 1

    config = args.config or _candidate_config(settings)
    print(f"Конфиг: {config if config else 'НЕ НАЙДЕН (областей и скоупов нет)'}")
    areas = None
    semantic = True
    config_min_score = None
    if config is not None:
        try:
            areas, watch = load_config(config)
            semantic = watch.semantic
            config_min_score = watch.min_score
        except Exception as exc:  # noqa: BLE001 — диагностика не падает
            print(f"  конфиг не прочитан: {exc}")

    print("\n2. Локальный поиск по этой базе:")
    _ingest, search, store, _recovery = build_service(
        settings, areas=areas, semantic=semantic, min_score=config_min_score
    )
    del _ingest
    try:
        result = search.search(SearchQuery(text=args.word, limit=args.limit))
        _print_result(result)
        found = bool(result.hits)
    finally:
        store.close()

    print(f"\n3. Сервер http://{settings.host}:{settings.port} (HTTP):")
    remote = search_over_http(settings, args.word, args.limit)
    if remote is None:
        print("  сервер не отвечает — сравнить не с чем.")
    else:
        _print_result(remote)

    print("\nИтог:")
    if not rows:
        print("  слова нет в FTS базы: проблема на приёме (индексация/очередь),")
        print("  а не в поиске. Смотри журнал и `persinder embed`.")
    elif not found:
        print("  слово есть в FTS, но локальный морфологический поиск его не отдал —")
        print("  это баг слоя поиска, присылай вывод.")
    elif remote is not None and not remote.hits:
        print("  локально находится, сервер — нет: сервер работает с другим")
        print("  состоянием (старый процесс или другой home). Перезапусти serve.")
    else:
        print("  слово находится и в базе, и поиском; если клиент его не видит —")
        print("  смотри, какой командой/URL он спрашивает и что делает с ответом.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
