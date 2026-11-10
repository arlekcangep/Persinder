#!/usr/bin/env python3
"""Диагностика эмбеддингов: почему нет папки ``.persinder/<модель>/``.

Скрипт **только читает** (БД открывается в режиме read-only) и печатает
факты: где home и БД, какие модели активны и установлены, сколько источников
с пустым путём, что лежит в очереди и таблице векторов, какие сайдкары уже
есть рядом с файлами и в ``virtual/``, состояние ANN-индексов (``ann/``,
открываются ли) и хвост ``embed_errors.log``.

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

from persinder.__main__ import search_scopes
from persinder.config import Settings
from persinder.embed import installed_keys, load, models_root
from persinder.embed.sidecar import (
    file_sidecar_path,
    read_sidecar,
    virtual_sidecar_path,
)
from persinder.embed.state import ModelState
from persinder.sources import load_config
from persinder.sources.models import AreaConfig
from persinder.vectorindex import UsannIndex


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


def _explain_doc(
    conn: sqlite3.Connection,
    needle: str,
    home: Path,
    settings: Settings,
    areas: tuple[AreaConfig, ...],
    models: ModelState,
    ann_dir: Path,
) -> None:
    """Почему документ не находится семантикой: векторы, сайдкар, скоуп, ANN."""
    print()
    print(f"Документ по подстроке {needle!r}:")
    rows = conn.execute(
        "SELECT d.id, s.area, s.source_path, s.filename, d.char_count"
        " FROM documents d JOIN document_sources s ON s.doc_id = d.id"
        " WHERE s.source_path LIKE ? OR s.filename LIKE ?",
        (f"%{needle}%", f"%{needle}%"),
    ).fetchall()
    if not rows:
        print("  не найден в БД: файл не проиндексирован (или другой home).")
        return

    scopes = search_scopes(areas, models)
    search_model = models.search
    total_vectors = _scalar(
        conn, "SELECT COUNT(*) FROM embeddings WHERE model = ?", (search_model,)
    )
    ann_path = ann_dir / f"{search_model}.usearch"
    ann_size: int | None = None
    if ann_path.is_file():
        try:
            ann_size = UsannIndex(ann_path, dim=384).size
        except Exception as exc:  # noqa: BLE001 — диагностика не падает
            print(f"  ANN не открывается: {exc.__class__.__name__}: {exc}")

    for row in rows:
        doc_id = str(row["id"])
        path = str(row["source_path"])
        where = path or f"{row['filename']} (виртуальный)"
        chunks = _scalar(
            conn, "SELECT COUNT(*) FROM chunks WHERE doc_id = ?", (doc_id,)
        )
        print()
        print(f"  [{row['area']}] {where}")
        print(f"    doc_id: {doc_id[:16]}…, чанков: {chunks}")
        vectors: dict[str, int] = {}
        for vrow in conn.execute(
            "SELECT model, COUNT(*) AS n FROM embeddings WHERE doc_id = ?"
            " GROUP BY model ORDER BY model",
            (doc_id,),
        ):
            vectors[str(vrow["model"])] = int(vrow["n"])
            print(f"    векторы: {vrow['model']} — {vrow['n']}")
        if not vectors:
            print("    векторы: НЕТ ни под одной моделью")

        for model in dict.fromkeys((models.search, models.embedding)):
            sidecar = (
                file_sidecar_path(path, model)
                if path
                else virtual_sidecar_path(home / "virtual", doc_id, model)
            )
            if not sidecar.is_file():
                print(f"    сайдкар {model}: НЕТ ({sidecar})")
                continue
            data = read_sidecar(sidecar)
            fp = data.fingerprint.model if data is not None else "?"
            print(
                f"    сайдкар {model}: есть, отпечаток {fp},"
                f" чанков {len(data.chunks) if data else '?'}"
            )

        scope = scopes.get(search_model)
        inside = False
        if scope is not None:
            inside = any(path.startswith(prefix) for prefix in scope.paths) or (
                not path and scope.allow_virtual
            )
            state = "входит" if inside else "ВНЕ"
            print(f"    скоуп модели {search_model}: {state}")

        if vectors.get(search_model, 0) < chunks:
            print(
                "    ПРИЧИНА: векторов под моделью поиска меньше, чем чанков:"
                " документ не эмбеддился этой моделью (смотри отпечаток сайдкара)."
                " Чинится точечно для документа, без переиндексации корпуса."
            )
        elif scope is not None and not inside:
            print(
                "    ПРИЧИНА: документ вне скоупа модели — проверь path области"
                " в конфиге (та же раскладка путей, что в source_path)."
            )
        elif ann_size is not None and ann_size < total_vectors:
            print(
                f"    ПРИЧИНА: в ANN {ann_size} ключей при {total_vectors} векторах"
                " в таблице. `persinder ann rebuild` — это сборка индекса из"
                " готовых векторов, без пересчёта эмбеддингов."
            )
        else:
            print(
                "    Векторы и скоуп в порядке: документ должен находиться"
                " семантикой (смотри его score при большом --semantic-limit)."
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Диагностика сайдкаров и очереди")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--home", type=Path, default=None)
    parser.add_argument(
        "--doc",
        default=None,
        help="подстрока пути/имени файла: разобрать, почему документ "
        "не находится семантикой (только чтение)",
    )
    args = parser.parse_args()

    home: Path = (args.home or Settings.default().home).expanduser()
    settings = Settings(home=home)
    db_path = settings.db_path
    print(f"Home: {home}")
    print(f"БД:   {db_path} ({'есть' if db_path.is_file() else 'НЕТ'})")

    config = args.config or _find_config(home)
    print(f"Конфиг: {config if config else 'НЕ НАЙДЕН'}")

    roots_by_role: dict[str, list[Path]] = {}
    areas: tuple[AreaConfig, ...] = ()
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
        vectors_by_model: dict[str, int] = {}
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
            vectors_by_model[str(row["model"])] = int(row["n"])
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
            virtual_model_dirs = sorted(p for p in virtual.iterdir() if p.is_dir())
            if not virtual_model_dirs:
                print("  пусто")
            for folder in virtual_model_dirs:
                print(f"  {folder.name}: {_count_pdr(folder)} pdr")

        print()
        print(f"ANN-индексы USearch ({settings.ann_dir}):")
        if not settings.ann_dir.is_dir():
            print("  папки нет")
        else:
            files = sorted(settings.ann_dir.iterdir())
            if not files:
                print("  пусто")
            for path in files:
                if not path.is_file():
                    print(f"  {path.name}: не файл")
                    continue
                size = path.stat().st_size
                print(f"  {path.name}: {size} байт", end="")
                try:
                    index = UsannIndex(path, dim=384)
                    print(f", ключей: {index.size}")
                except Exception as exc:  # noqa: BLE001 — диагностика не падает
                    print(f", НЕ ОТКРЫВАЕТСЯ: {exc.__class__.__name__}: {exc}")

        log_path = home / "embed_errors.log"
        print()
        print(f"Лог ошибок эмбеддингов ({log_path}):")
        if not log_path.is_file():
            print("  файла нет (ошибок не было или сервис ещё не перезапускался)")
        else:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if not lines:
                print("  пуст")
            for line in lines[-10:]:
                print(f"  {line}")

        print()
        print("Диагноз:")
        e5 = models.embedding
        e5_sidecars = any(
            (root / ".persinder" / e5).is_dir()
            for roots in roots_by_role.values()
            for root in roots
        )
        if e5 not in installed and e5 != "placeholder-v1":
            print(f"  модель {e5} НЕ установлена в {models_root(home)} —")
            print("  веса не скачаны, эмбеддинги этой моделью не считаются.")
        if vectors_by_model.get(e5, 0) and not e5_sidecars:
            print(
                f"  векторы {e5} в БД есть ({vectors_by_model.get(e5, 0)}), а"
                " сайдкаров нет:"
            )
            print("  документы не завершались на шаге после векторов (ANN/сайдкар).")
            print("  Обнови сервис, перезапусти его и пришли вывод и")
            print(f"  {log_path} — там будет настоящая причина.")
        if args.doc:
            _explain_doc(
                conn, args.doc, home, settings, areas, models, settings.ann_dir
            )
        if empty_paths and models.embedding != "placeholder-v1":
            print("  есть пустые source_path — запусти scripts/fix_source_paths.py")
        if not empty_paths and models.embedding != "placeholder-v1":
            print("  пустых путей нет: скрипт починки ничего не изменит.")
        print("  Если очередь пуста и векторов нет — эмбеддинги не считались:")
        print("  запусти `persinder embed` (доберёт очередь), полная")
        print("  переиндексация корпуса для этого не нужна.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
