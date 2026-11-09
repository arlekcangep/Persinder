#!/usr/bin/env python3
"""Диагностика эмбеддингов: почему нет папки ``.persinder/<модель>/``.

Скрипт **только читает** (БД открывается в режиме read-only) и печатает
факты: где home и БД, какие модели активны и установлены, сколько источников
с пустым путём, что лежит в очереди и таблице векторов, какие сайдкары уже
есть рядом с файлами и в ``virtual/``.

Запуск (тот же python, что и у сервиса), сервис можно не останавливать:

    .venv/Scripts/python.exe scripts/diagnose_embeddings.py --config persinder.yaml
    .venv/bin/python scripts/diagnose_embeddings.py --config persinder.yaml

Если ``--home`` не задан, берётся ``~/.persinder`` — тот же каталог, что у
сервиса по умолчанию.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from persinder.config import Settings
from persinder.embed import installed_keys, load, models_root
from persinder.sources import load_config


def _find_config(home: Path) -> Path | None:
    names = ("persinder.yaml", "persinder.yml")
    candidates = [
        *(Path.cwd() / name for name in names),
        *(home / name for name in names),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> int:
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.OperationalError as exc:
        print(f"    (запрос не выполнен: {exc})")
        return 0
    return int(row[0]) if row is not None and row[0] is not None else 0


def _resolve(filename: str, roots: list[Path]) -> Path | None:
    rel = Path(filename)
    if not rel.parts:
        return None
    if rel.is_absolute():
        return rel if rel.exists() else None
    for root in roots:
        if (root / rel).exists():
            return root / rel
    container = rel.parts[0]
    for root in roots:
        if (root / container).exists():
            return root / rel
    return None


def _count_pdr(folder: Path) -> int:
    if not folder.is_dir():
        return 0
    return sum(1 for _ in folder.rglob("*.pdr"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Диагностика сайдкаров и очереди")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--home", type=Path, default=None)
    args = parser.parse_args()

    home = (args.home or Settings.default().home).expanduser()
    settings = Settings(home=home)
    db_path = settings.db_path
    print(f"Home: {home}")
    print(f"БД:   {db_path} ({'есть' if db_path.is_file() else 'НЕТ'})")

    config = args.config or _find_config(home)
    print(f"Конфиг: {config if config else 'НЕ НАЙДЕН'}")

    roots_by_role: dict[str, list[Path]] = {}
    if config is not None:
        try:
            areas, _watch = load_config(config)
        except Exception as exc:  # noqa: BLE001
            print(f"  ошибка конфига: {exc}")
            areas = ()
        for area in areas:
            roots_by_role.setdefault(area.role.value, []).append(area.path)
            exists = "есть" if area.path.exists() else "НЕТ"
            print(f"  область {area.role.value}: {area.path} ({exists})")

    models = load(settings.models_state_path)
    print()
    print(f"Активные модели: embedding = {models.embedding}, search = {models.search}")
    installed = installed_keys(models_root(home))
    print(f"Установлены в {models_root(home)}: {installed or 'ничего'}")

    if not db_path.is_file():
        print("\nБД нет — сервис ещё не запускался в этом home.")
        return 1

    conn = _connect_ro(db_path)
    try:
        documents = _scalar(conn, "SELECT COUNT(*) FROM documents")
        sources = _scalar(conn, "SELECT COUNT(*) FROM document_sources")
        empty_paths = _scalar(
            conn, "SELECT COUNT(*) FROM document_sources WHERE source_path = ''"
        )
        print()
        print(f"Документов: {documents}, источников: {sources}")
        print(f"Источников с пустым source_path: {empty_paths}")

        if empty_paths:
            rows = conn.execute(
                "SELECT area, filename FROM document_sources"
                " WHERE source_path = '' ORDER BY area, filename LIMIT 15"
            ).fetchall()
            print("  примеры (файл на диске ищется по конфигу):")
            for row in rows:
                area = str(row["area"])
                found = _resolve(str(row["filename"]), roots_by_role.get(area, []))
                where = str(found) if found is not None else "НЕ НАЙДЕН (виртуальный)"
                print(f"    [{area}] {row['filename']} -> {where}")

        print()
        print("Очередь эмбеддингов (pending):")
        try:
            rows = conn.execute(
                "SELECT model, source_path = '' AS no_path, COUNT(*) AS n"
                " FROM embedding_queue WHERE status = 'pending'"
                " GROUP BY model, no_path ORDER BY model"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            print(f"  (нет таблицы очереди: {exc})")
            rows = []
        if not rows:
            print("  пусто")
        for row in rows:
            kind = "без пути (virtual)" if row["no_path"] else "с путём"
            print(f"  {row['model']}: {row['n']} задач, {kind}")

        print()
        print("Векторы в БД (таблица embeddings):")
        try:
            rows = conn.execute(
                "SELECT model, COUNT(*) AS n FROM embeddings GROUP BY model"
                " ORDER BY model"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            print(f"  (нет таблицы векторов: {exc})")
            rows = []
        if not rows:
            print("  пусто")
        for row in rows:
            print(f"  {row['model']}: {row['n']}")

        print()
        print("Сайдкары рядом с файлами:")
        for roots in roots_by_role.values():
            for root in roots:
                base = root / ".persinder"
                if not base.is_dir():
                    print(f"  {root}: папки .persinder нет")
                    continue
                model_dirs = sorted(p.name for p in base.iterdir() if p.is_dir())
                if not model_dirs:
                    print(f"  {root}: .persinder пуст")
                for name in model_dirs:
                    print(
                        f"  {root}: .persinder/{name} — {_count_pdr(base / name)} pdr"
                    )

        virtual = home / "virtual"
        print()
        print(f"Виртуальные сайдкары ({virtual}):")
        if not virtual.is_dir():
            print("  папки нет")
        else:
            model_dirs = sorted(p for p in virtual.iterdir() if p.is_dir())
            if not model_dirs:
                print("  пусто")
            for folder in model_dirs:
                print(f"  {folder.name}: {_count_pdr(folder)} pdr")

        print()
        print("Диагноз:")
        e5 = models.embedding
        if e5 not in installed and e5 != "placeholder-v1":
            print(f"  модель {e5} НЕ установлена в {models_root(home)} —")
            print("  веса не скачаны, эмбеддинги этой моделью не считаются.")
        if empty_paths and models.embedding != "placeholder-v1":
            print("  есть пустые source_path — запусти scripts/fix_source_paths.py")
        if not empty_paths and models.embedding != "placeholder-v1":
            print("  пустых путей нет: скрипт починки ничего не изменит.")
        print("  Если очередь пуста и векторов нет — эмбеддинги не считались:")
        print("  запусти ./run.sh (по умолчанию --reindex) и смотри его вывод.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
