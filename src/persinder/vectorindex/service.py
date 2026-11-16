"""Модуль 3: векторный поиск — query → вектор → ANN (USearch) → top-k.

Векторы чанков под моделью материализует воркер ``embed`` в таблицу
``embeddings`` (сайдкар — синхронизируемый артефакт, таблица — локальное
производное, §8). Поиск: эмбеддинг запроса (префикс ``query:``, §12.1) через
активную поисковую модель → ANN-индекс USearch (mmap-файл на модель,
``home/ann/<модель>.usearch``) → top-k по косинусу.

Индекс приблизительный: граф HNSW теряет «краевые» векторы, поэтому до
``EXACT_SEARCH_MAX`` векторов тот же индекс опрашивается **точно**
(``UsannIndex.search`` сам выбирает режим по размеру). Иначе документ может
пропасть из выдачи целиком — не «низко в рейтинге», а не быть вовсе.

Моделей может быть несколько (D24 + конфиг областей): глобальная активная
модель плюс модели, выбранные для отдельных папок. По каждой модели поиск идёт
отдельно, результаты сливаются по ``(doc_id, chunk_index)`` с максимумом счёта.
Чтобы старые векторы не давали мусор, у модели можно задать ``ModelScope`` —
папки, где ею индексировали (``semantic: false`` области в скоуп не входят).

Индекс ведётся воркером ``embed`` (добавляет векторы при расчёте) и ``ann
rebuild`` (полная пересборка из таблицы). Если файла индекса нет (например,
база создана до внедрения USearch) — точный скан таблицы как фолбэк: тот же
результат, медленнее на больших объёмах. Протухшие ключи (документ удалён)
отсекаются при сопоставлении с таблицей, поэтому индекс безопасно «грязный».
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..embed.sidecar import unpack_f16_vectors
from ..interfaces import Embedder, MetaStore
from ..search.models import SearchHit
from .index import UsannIndex


@dataclass(frozen=True)
class ModelScope:
    """Где модель имеет право искать.

    ``paths`` — каталоги областей (префиксы ``source_path``), которые
    индексируются этой моделью; ``allow_virtual`` — разрешить документы без
    файла на диске (web/вставленный текст). Пустой скоуп — модель активна по
    конфигу, но искать ей негде.
    """

    paths: tuple[str, ...] = ()
    allow_virtual: bool = False

    def is_empty(self) -> bool:
        """Нет ни одной области и виртуальные документы запрещены."""
        return not self.paths and not self.allow_virtual


class VectorSearch:
    """Семантический поиск: ANN USearch с фолбэком на точный скан (модуль 3)."""

    def __init__(
        self,
        store: MetaStore,
        embedder_factory: Callable[[str], Embedder],
        search_model: str | Sequence[str],
        ann_dir: Path | None = None,
        scopes: Mapping[str, ModelScope] | None = None,
    ) -> None:
        self._store = store
        self._embedder_factory = embedder_factory
        # Одна модель (старое поведение) или несколько активных (глобальная +
        # модели папок из конфига).
        self._models: tuple[str, ...] = (
            (search_model,) if isinstance(search_model, str) else tuple(search_model)
        )
        self._ann_dir = Path(ann_dir) if ann_dir else None
        # None — скоупа нет (ищем по всем источникам, как раньше).
        self._scopes = dict(scopes) if scopes is not None else None

    def search(
        self, text: str, limit: int = 20, min_score: float | None = None
    ) -> list[SearchHit]:
        """Top-k семантических хитов: по каждой модели отдельно, затем слияние.

        ``min_score`` — порог косинуса: хиты ниже него не возвращаются (порог
        применяется к слитому результату, до обрезки по ``limit``).
        """
        if not text.strip() or limit <= 0 or not self._models:
            return []
        by_key: dict[tuple[str, int], SearchHit] = {}
        for model in self._models:
            for hit in self._model_hits(text, model, limit):
                key = (hit.document_id, hit.chunk_index)
                previous = by_key.get(key)
                if previous is None or hit.score > previous.score:
                    by_key[key] = hit
        hits = sorted(by_key.values(), key=lambda hit: hit.score, reverse=True)
        if min_score is not None:
            hits = [hit for hit in hits if hit.score >= min_score]
        return hits[:limit]

    def _model_hits(self, text: str, model: str, limit: int) -> list[SearchHit]:
        """Хиты одной модели (с учётом её скоупа)."""
        allowed = self._allowed_doc_ids(model)
        if allowed is not None and not allowed:
            return []  # модель есть в конфиге, но индексировать ею нечего
        embedder = self._embedder_factory(model)
        dim = embedder.dim
        query_vector = embedder.embed_query([text])[0]
        hits = self._search_index(model, query_vector, dim, limit, allowed)
        if hits is not None:
            return hits
        return self._search_scan(model, query_vector, dim, limit, allowed)

    def _allowed_doc_ids(self, model: str) -> set[str] | None:
        """Документы в скоупе модели; ``None`` — скоупа нет (все источники)."""
        if self._scopes is None:
            return None
        scope = self._scopes.get(model)
        if scope is None:
            return set()  # модели нет в конфиге — искать ею негде
        if scope.is_empty():
            return set()
        return self._store.doc_ids_in_paths(
            list(scope.paths), include_virtual=scope.allow_virtual
        )

    def _search_index(
        self,
        model: str,
        query_vector: list[float],
        dim: int,
        limit: int,
        allowed: set[str] | None,
    ) -> list[SearchHit] | None:
        """Поиск по ANN-индексу; ``None`` — индекса нет (фолбэк на скан).

        При скоупе индекс отдаёт с запасом (``over-fetch``), а лишние документы
        отфильтровываются: так top-k не «съедается» чужими папками. Если
        отфильтрованных мало — выборка расширяется до размера индекса.
        Режим (точный перебор / граф) выбирает ``UsannIndex.search`` по размеру
        индекса: приблизительный поиск теряет кандидатов, а не только их порядок.
        """
        if self._ann_dir is None:
            return None
        path = self._ann_dir / f"{model}.usearch"
        if not path.exists():
            return None
        index = UsannIndex(path, dim)
        if index.size == 0:
            return None
        if index.size < self._store.count_embeddings(model):
            # В файле индекса не все векторы модели (оборванный проход, сбой
            # добавления, восстановленная БД): поиск по нему молча терял бы
            # документы, поэтому идём точным сканом таблицы.
            return None
        fetch = limit if allowed is None else min(max(limit * 4, 32), index.size)
        while True:
            matches = index.search(query_vector, fetch)
            rows_by_id = {
                row["id"]: row
                for row in self._store.embeddings_for_ids([key for key, _ in matches])
            }
            hits: list[SearchHit] = []
            for key, score in matches:
                row = rows_by_id.get(key)
                if row is None:
                    continue  # протухший ключ: документ удалён, индекс не почищен
                if allowed is not None and str(row["doc_id"]) not in allowed:
                    continue  # документ вне скоупа модели (другая папка/модель)
                hits.append(
                    SearchHit(
                        document_id=row["doc_id"],
                        chunk_index=row["chunk_index"],
                        score=score,
                    )
                )
            if allowed is None or len(hits) >= limit or fetch >= index.size:
                if len(hits) < limit:
                    # Индекс отдал меньше запрошенного: часть векторов в него
                    # не попала (сбой добавления, оборванный проход, старые
                    # ключи). Молча терять документы нельзя — точный скан.
                    return None
                return hits[:limit]
            fetch = min(fetch * 4, index.size)

    def _search_scan(
        self,
        model: str,
        query_vector: list[float],
        dim: int,
        limit: int,
        allowed: set[str] | None,
    ) -> list[SearchHit]:
        """Точный фолбэк: косинус по всем векторам модели из таблицы."""
        rows = self._store.embeddings_for_model(model)
        scored: list[tuple[float, str, int]] = []
        for row in rows:
            if allowed is not None and str(row["doc_id"]) not in allowed:
                continue
            vector = unpack_f16_vectors(row["vector"], dim)[0]
            similarity = sum(a * b for a, b in zip(query_vector, vector, strict=True))
            scored.append((similarity, str(row["doc_id"]), int(row["chunk_index"])))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            SearchHit(document_id=doc_id, chunk_index=chunk, score=score)
            for score, doc_id, chunk in scored[:limit]
        ]


def rebuild_index(
    store: MetaStore,
    embedder_factory: Callable[[str], Embedder],
    ann_dir: Path,
    model: str,
) -> int:
    """Пересобрать ANN-индекс модели из таблицы ``embeddings``; вернуть число ключей.

    Вызывается из ``persinder ann rebuild`` и нужен после холодного старта
    устройства (таблица есть, индекса нет) или при рассинхронизации.
    """
    path = Path(ann_dir) / f"{model}.usearch"
    if path.exists():
        path.unlink()
    rows = store.embeddings_for_model(model)
    if not rows:
        return 0
    dim = embedder_factory(model).dim
    index = UsannIndex(path, dim)
    index.add(
        [row["id"] for row in rows],
        [list(unpack_f16_vectors(row["vector"], dim)[0]) for row in rows],
    )
    return len(rows)
