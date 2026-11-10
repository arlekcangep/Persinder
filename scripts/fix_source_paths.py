#!/usr/bin/env python3
"""Одноразовая починка пустых ``document_sources.source_path`` (старая БД).

Зачем: в базах, созданных до появления колонки ``source_path``, миграция
добавляет её пустой. Пока путь пуст, ``enqueue_all`` (``model install``,
``--reindex``) ставит задачи с пустым путём, и эмбеддинги пишутся не в
``.persinder/<модель>/`` рядом с файлом, а в ``<home>/virtual/<модель>/`` —
со стороны кажется, что папки модели нет вовсе.

Пустой путь сам по себе — штатный режим «документа нет на диске» (вставленный
текст, web, часть архива без файла): такие источники скрипт **не трогает**.
Он заполняет путь только там, где файл (или контейнер архива) реально найден
под корнем области из конфига.

Что делает:

1. По ``--config`` и ``--home`` заполняет пустой ``source_path`` источникам,
   у которых файл найден на диске. ``filename`` — путь от корня области; для
   части архива первый компонент — сам архив (контейнер существует, записи
   внутри него).
2. Удаляет устаревшие задачи ``embedding_queue`` с пустым путём у документов,
   у которых нет виртуальных источников (иначе они снова уведут сайдкары в
   ``virtual/``).
3. С ``--clean-virtual`` удаляет виртуальные сайдкары документов, у которых
   появился реальный путь, и осиротевшие файлы удалённых документов. Сайдкар —
   кэш: он пересчитается при следующей индексации.

Скрипт идемпотентен: повторный запуск ничего не меняет. ``--dry-run``
показывает, что было бы сделано, и откатывает транзакцию.

Запуск (тот же python, что и у сервиса), при остановленном сервисе:

    .venv/bin/python scripts/fix_source_paths.py --config persinder.yaml --dry-run
    .venv/bin/python scripts/fix_source_paths.py --config persinder.yaml
    .venv/bin/python scripts/fix_source_paths.py --config persinder.yaml --clean-virtual

После починки перезапусти сервис (``./run.sh`` — переиндексация по умолчанию):
недостающие сайдкары появятся рядом с файлами.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

from persinder.config import Settings
from persinder.embed import is_installed, load, models_root
from persinder.sources import load_config


def _find_config(home: Path) -> Path | None:
    """Где искать конфиг, если ``--config`` не задан: папка запуска, затем home."""
    names = ("persinder.yaml", "persinder.yml")
    candidates = [
        *(Path.cwd() / name for name in names),
        *(home / name for name in names),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _roots_by_role(config: Path) -> dict[str, list[Path]]:
    """Корни областей из конфига, сгруппированные по роли (personal/foreign/...)."""
    areas, _watch = load_config(config)
    roots: dict[str, list[Path]] = {}
    for area in areas:
        roots.setdefault(area.role.value, []).append(area.path)
    return roots


def _resolve(filename: str, roots: list[Path]) -> Path | None:
    """Путь файла на диске по имени источника; ``None`` — файла нет (виртуальный)."""
    rel = Path(filename)
    if not rel.parts:
        return None
    if rel.is_absolute():
        return rel if rel.exists() else None
    # Обычный файл: полный путь от корня области существует.
    for root in roots:
        candidate = root / rel
        if candidate.exists():
            return candidate
    # Часть архива: на диске лежит контейнер (первый компонент), записи — внутри.
    container = rel.parts[0]
    for root in roots:
        if (root / container).exists():
            return root / rel
    return None


def _clean_virtual_targets(conn: sqlite3.Connection, virtual_dir: Path) -> list[Path]:
    """Виртуальные сайдкары, ставшие лишними (документ переехал к файлу/удалён)."""
    targets: list[Path] = []
    if not virtual_dir.is_dir():
        return targets
    for model_dir in sorted(path for path in virtual_dir.iterdir() if path.is_dir()):
        for sidecar in sorted(model_dir.glob("*.pdr")):
            doc_id = sidecar.stem
            has_real = conn.execute(
                "SELECT 1 FROM document_sources"
                " WHERE doc_id = ? AND source_path != '' LIMIT 1",
                (doc_id,),
            ).fetchone()
            alive = conn.execute(
                "SELECT 1 FROM documents WHERE id = ? LIMIT 1", (doc_id,)
            ).fetchone()
            if has_real is not None or alive is None:
                targets.append(sidecar)
    return targets


def _remove_empty_model_dirs(virtual_dir: Path) -> None:
    """Убрать опустевшие ``<virtual>/<модель>/`` после чистки сайдкаров."""
    if not virtual_dir.is_dir():
        return
    for model_dir in sorted(path for path in virtual_dir.iterdir() if path.is_dir()):
        if not any(model_dir.iterdir()):
            model_dir.rmdir()


def _iter_sources(conn: sqlite3.Connection) -> Iterator[sqlite3.Row]:
    yield from conn.execute(
        "SELECT doc_id, area, filename FROM document_sources"
        " WHERE source_path = '' ORDER BY area, filename"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Заполнить пустые source_path по конфигу областей (одноразово)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="YAML-конфиг областей (по умолчанию persinder.yaml рядом или в home)",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="каталог приложения (по умолчанию ~/.persinder)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="только показать изменения, ничего не записывать",
    )
    parser.add_argument(
        "--clean-virtual",
        action="store_true",
        help="удалить виртуальные сайдкары документов с реальным путём и осиротевшие",
    )
    args = parser.parse_args()

    home = (args.home or Settings.default().home).expanduser()
    settings = Settings(home=home)
    config = args.config or _find_config(home)
    if config is None:
        print("конфиг не найден: укажи --config persinder.yaml")
        return 1
    db_path = settings.db_path
    if not db_path.is_file():
        print(f"БД не найдена: {db_path}")
        return 1

    try:
        roots = _roots_by_role(config)
    except Exception as exc:  # noqa: BLE001 — конфиг бывает каким угодно
        print(f"ошибка конфига {config}: {exc}")
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    healed = 0
    left_virtual = 0
    stale = 0
    clean_targets: list[Path] = []
    conn.execute("BEGIN")
    try:
        before = int(
            conn.execute(
                "SELECT COUNT(*) FROM document_sources WHERE source_path = ''"
            ).fetchone()[0]
        )
        for row in _iter_sources(conn):
            path = _resolve(str(row["filename"]), roots.get(str(row["area"]), []))
            if path is None:
                left_virtual += 1
                continue
            print(f"  путь: {row['area']}/{row['filename']} -> {path}")
            conn.execute(
                "UPDATE document_sources SET source_path = ?"
                " WHERE doc_id = ? AND area = ? AND filename = ?",
                (str(path), row["doc_id"], row["area"], row["filename"]),
            )
            healed += 1

        # Задачи с пустым путём у документов, у которых не осталось ни одного
        # виртуального источника, — устаревшие: они увели бы сайдкар в virtual/.
        stale = int(
            conn.execute(
                "SELECT COUNT(*) FROM embedding_queue"
                " WHERE source_path = '' AND doc_id NOT IN ("
                "  SELECT doc_id FROM document_sources WHERE source_path = ''"
                " )"
            ).fetchone()[0]
        )
        conn.execute(
            "DELETE FROM embedding_queue"
            " WHERE source_path = '' AND doc_id NOT IN ("
            "  SELECT doc_id FROM document_sources WHERE source_path = ''"
            " )"
        )
        if args.clean_virtual:
            clean_targets = _clean_virtual_targets(conn, home / "virtual")
            for target in clean_targets:
                print(f"  виртуальный сайдкар: {target}")

        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

    if not args.dry_run:
        for target in clean_targets:
            target.unlink(missing_ok=True)
        _remove_empty_model_dirs(home / "virtual")

    print()
    print(f"База: {db_path}")
    print(f"Конфиг: {config}")
    print(f"Пустых путей до: {before}")
    print(f"  восстановлено: {healed}")
    print(f"  оставлено виртуальными (файла нет): {left_virtual}")
    print(f"Устаревших задач в очереди: {stale}")
    if args.clean_virtual:
        print(f"Виртуальных сайдкаров: {len(clean_targets)}")
    if args.dry_run:
        print("DRY-RUN: изменения не сохранены.")
    else:
        print("Готово. Перезапусти сервис (./run.sh), чтобы досчитать сайдкары.")

    state = load(settings.models_state_path)
    print(f"Активная модель эмбеддингов: {state.embedding}")
    if state.embedding != "placeholder-v1" and not is_installed(
        models_root(home), state.embedding
    ):
        print(
            f"ВНИМАНИЕ: она не установлена в {models_root(home)} — "
            "сайдкары не будут считаться.\n"
            f"  установи: persinder model install {state.embedding}"
        )
    print("Скрипт сам эмбеддинги не считает: папки создаёт сервис при запуске.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
