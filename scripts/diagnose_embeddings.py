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
from persinder.embed import PLACEHOLDER, installed_keys, load, models_root
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


def _sidecar_counts(root: Path) -> dict[str, int]:
    """Сайдкары под корнем: ``<любая папка>/.persinder/<модель>/*.pdr``.

    Считаем рекурсивно: у файлов в подпапках свои ``.persinder`` (рядом с
    файлом), поэтому подсчёт только по ``<root>/.persinder`` занижал цифры и
    «документы без сайдкара» выглядели как потерянные.
    """
    counts: dict[str, int] = {}
    for base in root.rglob(".persinder"):
        if not base.is_dir():
            continue
        for model_dir in base.iterdir():
            if model_dir.is_dir():
                counts[model_dir.name] = counts.get(model_dir.name, 0) + _count_pdr(
                    model_dir
                )
    return counts


def _find_docs(conn: sqlite3.Connection, needle: str) -> list[sqlite3.Row]:
    """Документы по подстроке пути/имени, иначе — по тексту чанка (со стемом)."""
    sql = (
        "SELECT DISTINCT d.id, s.area, s.source_path, s.filename, d.char_count"
        " FROM documents d JOIN document_sources s ON s.doc_id = d.id"
        " WHERE s.source_path LIKE ? OR s.filename LIKE ?"
    )
    rows = conn.execute(sql, (f"%{needle}%", f"%{needle}%")).fetchall()
    if rows:
        return rows
    from persinder.morphology import analyze_to_string, default_stemmer

    # В ``chunks_fts`` лежат уже стеммированные токены. Стеммим запрос тем же
    # анализатором, что и при индексации, и склеиваем токены через AND:
    # иначе фраза («серый волчок») ищется как один несуществующий токен.
    terms = analyze_to_string(needle, default_stemmer()).split()
    if not terms:
        return []
    match = " ".join(f'"{term}"*' for term in terms)
    try:
        ids = [
            str(row["doc_id"])
            for row in conn.execute(
                "SELECT DISTINCT doc_id FROM chunks_fts WHERE chunks_fts MATCH ?",
                (match,),
            )
        ]
    except sqlite3.OperationalError:
        return []
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    return conn.execute(
        "SELECT DISTINCT d.id, s.area, s.source_path, s.filename, d.char_count"
        " FROM documents d JOIN document_sources s ON s.doc_id = d.id"
        f" WHERE d.id IN ({marks})",
        ids,
    ).fetchall()


def _score_doc(
    conn: sqlite3.Connection,
    doc_id: str,
    model: str,
    query: str,
    models_dir: Path,
) -> None:
    """Лучший косинус документа и его позиция в корпусе — на реальной модели."""
    from persinder.embed import build_embedder, unpack_f16_vectors

    embedder = build_embedder(models_dir, model)
    query_vector = embedder.embed_query([query])[0]
    dim = embedder.dim
    lengths: dict[int, int] = {}
    for row in conn.execute(
        "SELECT chunk_index, char_start, char_end FROM chunks WHERE doc_id = ?",
        (doc_id,),
    ):
        lengths[int(row["chunk_index"])] = int(row["char_end"]) - int(row["char_start"])

    own: dict[int, float] = {}
    for row in conn.execute(
        "SELECT chunk_index, vector FROM embeddings WHERE model = ? AND doc_id = ?",
        (model, doc_id),
    ):
        vector = unpack_f16_vectors(bytes(row["vector"]), dim)[0]
        own[int(row["chunk_index"])] = sum(
            a * b for a, b in zip(query_vector, vector, strict=True)
        )
    if not own:
        print("    score: векторов под моделью поиска нет — считать нечего")
        return
    best_index = max(own, key=lambda index: own[index])
    best = own[best_index]

    above = 0
    for row in conn.execute(
        "SELECT vector FROM embeddings WHERE model = ? AND doc_id != ?",
        (model, doc_id),
    ):
        vector = unpack_f16_vectors(bytes(row["vector"]), dim)[0]
        score = sum(a * b for a, b in zip(query_vector, vector, strict=True))
        if score > best:
            above += 1

    print(f"    запрос {query!r}: лучший косинус {best:.4f}")
    print(
        f"      чанк {best_index}, длина {lengths.get(best_index, '?')} симв.;"
        f" чанков в документе: {len(own)}"
    )
    if lengths:
        print(
            f"      длины чанков: мин {min(lengths.values())},"
            f" макс {max(lengths.values())} симв."
        )
    print(
        f"      чанков корпуса с косинусом выше: {above}"
        f" → документ примерно на позиции {above + 1} в семантическом рейтинге"
    )
    position = above + 1
    chunk_max = max(lengths.values()) if lengths else 0
    if position <= 5:
        print(
            "      ВЫВОД: документ в топ-5 семантики — поиск обязан его"
            " показывать (если нет, смотри лимиты слоя и min_score)."
        )
    elif best < 0.85 and chunk_max > 600:
        print(
            "      ВЫВОД: фраза лежит в длинном чанке, а у e5 короткий запрос"
            " против 1000+ символов даёт ~0.82 и у релевантного, и у фона:"
            " документ в индексе, но его обгоняет шум. Это про ранжирование"
            " (cross-encoder / мелкие чанки), а не про потерю данных."
        )
    else:
        print(
            "      ВЫВОД: документ ниже топа при нормальном косинусе —"
            " проверь min_score и лимит семантического слоя."
        )


def _explain_doc(
    conn: sqlite3.Connection,
    needle: str,
    home: Path,
    settings: Settings,
    areas: tuple[AreaConfig, ...],
    models: ModelState,
    ann_dir: Path,
    query: str | None = None,
) -> None:
    """Разбор документа: векторы, сайдкар, скоуп, ANN и его место в рейтинге."""
    print()
    print(f"Документ по подстроке {needle!r} (путь/имя, иначе текст чанка):")
    rows = _find_docs(conn, needle)
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

        if query:
            _score_doc(conn, doc_id, search_model, query, settings.models_dir)

        if vectors.get(search_model, 0) < chunks:
            print(
                "    ВНИМАНИЕ: в таблице меньше векторов, чем чанков —"
                " сайдкар покрывает больше, чем лежит в БД."
            )
        elif scope is not None and not inside:
            print(
                "    ВНИМАНИЕ: документ вне скоупа модели — проверь path области"
                " в конфиге (та же раскладка путей, что в source_path)."
            )
        elif ann_size is not None and ann_size < total_vectors:
            print(
                f"    ВНИМАНИЕ: в ANN {ann_size} ключей при {total_vectors} векторах"
                " в таблице. `persinder ann rebuild` — сборка индекса из готовых"
                " векторов, без пересчёта эмбеддингов."
            )
        elif not query:
            print(
                "    Векторы, сайдкар, скоуп и ANN согласованы: документ в"
                ' индексе. Добавь --query "слово", чтобы увидеть его косинус'
                " и место в рейтинге."
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Диагностика сайдкаров и очереди")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--home", type=Path, default=None)
    parser.add_argument(
        "--doc",
        default=None,
        help="подстрока пути/имени файла или слова из текста: разобрать,"
        " почему документ не находится семантикой (только чтение)",
    )
    parser.add_argument(
        "--query",
        default=None,
        help="запрос для замера: показать лучший косинус документа и его место"
        " в рейтинге (нужна установленная модель)",
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
        print("Очередь эмбеддингов (pending) по документам:")
        try:
            queue_rows = conn.execute(
                "SELECT doc_id, model, COUNT(*) AS n, MAX(source_path) AS path"
                " FROM embedding_queue WHERE status = 'pending'"
                " GROUP BY doc_id, model ORDER BY n, doc_id LIMIT 20"
            ).fetchall()
            queue_total = _scalar(
                conn,
                "SELECT COUNT(*) FROM embedding_queue WHERE status = 'pending'",
            )
        except sqlite3.OperationalError as exc:
            print(f"  (нет таблицы очереди: {exc})")
            queue_rows = []
            queue_total = 0
        if not queue_rows:
            print("  пусто")
        else:
            print(
                f"  всего задач: {queue_total}, документов показано: {len(queue_rows)}"
            )
        for row in queue_rows:
            doc_id = str(row["doc_id"])
            model = str(row["model"])
            path = str(row["path"] or "")
            print()
            print(f"  {model}: {int(row['n'])} задач")
            print(f"    документ: {doc_id[:16]}…")
            print(f"    источник: {path or 'путь не записан (виртуальный)'}")
            # Воркер читает текст по doc_id; нет файла — задачи сиротские.
            text_file = settings.text_dir / f"{doc_id}.txt"
            if text_file.is_file():
                print(f"    текст: есть, {text_file.stat().st_size} Б")
            else:
                print("    текст: НЕТ — задачи сиротские, воркер их снимет")
            sidecar = (
                file_sidecar_path(path, model)
                if path
                else virtual_sidecar_path(settings.virtual_dir, doc_id, model)
            )
            print(f"    сайдкар: {'есть' if sidecar.is_file() else 'НЕТ'}")
            if model != PLACEHOLDER and model not in installed:
                print(
                    f"    ВНИМАНИЕ: модель {model} не установлена — задачи не"
                    " посчитаются, пока её не поставить"
                )
            elif model not in (models.embedding, models.search):
                print(f"    модель {model} не активна в профиле (embedding/search)")

        # Сырой текст — не очередь: после эмбеддинга он остаётся на диске как
        # источник сниппетов и досчёта. Путать эти две вещи — классическая
        # ошибка («в pending_texts 22 файла, значит 22 задачи»).
        texts = (
            sorted(settings.text_dir.glob("*.txt"))
            if settings.text_dir.is_dir()
            else []
        )
        text_bytes = sum(p.stat().st_size for p in texts)
        print()
        print(f"Сырые тексты ({settings.text_dir}):")
        print(
            f"  {len(texts)} файлов, {text_bytes} Б — это НЕ очередь: текст"
            " остаётся после эмбеддинга (сниппеты, досчёт), а задач в очереди:"
            f" {queue_total}"
        )

        model = models.search
        print()
        print(f"Документы без сайдкара {model} (источник → чанков/векторов):")
        try:
            doc_rows = conn.execute(
                "SELECT s.doc_id, s.area, s.filename, s.source_path,"
                " (SELECT COUNT(*) FROM chunks c WHERE c.doc_id = s.doc_id)"
                "   AS chunks,"
                " (SELECT COUNT(*) FROM embeddings e"
                "   WHERE e.doc_id = s.doc_id AND e.model = ?) AS vectors"
                " FROM document_sources s ORDER BY s.area, s.filename",
                (model,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            print(f"  (нет таблиц документов: {exc})")
            doc_rows = []
        missing_sidecars: list[sqlite3.Row] = []
        for row in doc_rows:
            doc_id = str(row["doc_id"])
            path = str(row["source_path"] or "")
            sidecar = (
                file_sidecar_path(path, model)
                if path
                else virtual_sidecar_path(settings.virtual_dir, doc_id, model)
            )
            if not sidecar.is_file():
                missing_sidecars.append(row)
        if not missing_sidecars:
            print(f"  нет: у всех источников ({len(doc_rows)}) сайдкар на месте")
        else:
            print(f"  {len(missing_sidecars)} из {len(doc_rows)}")
        for row in missing_sidecars:
            doc_id = str(row["doc_id"])
            has_text = (settings.text_dir / f"{doc_id}.txt").is_file()
            print(f"  [{row['area']}] {row['filename']}")
            print(f"    {row['source_path'] or 'путь не записан (виртуальный)'}")
            print(
                f"    чанков {int(row['chunks'])}, векторов {int(row['vectors'])},"
                f" текст {'есть' if has_text else 'НЕТ'}"
            )
            if not has_text:
                print("    → текст удалён: посчитать нельзя, нужна переиндексация")

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
        print("Сайдкары рядом с файлами (по всем подпапкам):")
        for roots in roots_by_role.values():
            for root in roots:
                counts = _sidecar_counts(root)
                if not counts:
                    print(f"  {root}: сайдкаров нет")
                    continue
                summary = ", ".join(
                    f"{model_name} — {n} pdr"
                    for model_name, n in sorted(counts.items())
                )
                print(f"  {root}: {summary}")

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
        if e5 not in installed and e5 != PLACEHOLDER:
            print(f"  модель {e5} НЕ установлена в {models_root(home)} —")
            print("  веса не скачаны, эмбеддинги этой моделью не считаются.")
        no_text = [
            row
            for row in missing_sidecars
            if not (settings.text_dir / f"{str(row['doc_id'])}.txt").is_file()
        ]
        if missing_sidecars:
            print(f"  документов без сайдкара {e5}: {len(missing_sidecars)} —")
            print("  в семантике их нет, пока они не посчитаны.")
            if no_text:
                print(
                    f"  у {len(no_text)} из них НЕТ сырого текста: считать нечего,"
                    " нужна переиндексация источника"
                )
                print("  (`persinder serve --reindex` перечитает файлы с диска).")
        elif queue_total:
            print(f"  в очереди {queue_total} задач — их разбирает воркер сервиса.")
            print(
                "  Если число не меняется: проверь модель (выше), наличие текстов"
                " и последние строки лога ошибок."
            )
        else:
            print(
                f"  всё посчитано: очередь пуста, сайдкары {e5} на месте у всех"
                " источников."
            )
            print(
                "  Файлы в pending_texts — не задачи: сырой текст остаётся на"
                " диске для сниппетов и досчёта. Это норма."
            )
        if empty_paths:
            print("  есть пустые source_path — запусти scripts/fix_source_paths.py")

        if args.doc:
            _explain_doc(
                conn,
                args.doc,
                home,
                settings,
                areas,
                models,
                settings.ann_dir,
                args.query,
            )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
