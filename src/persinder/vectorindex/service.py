"""Модуль 3: векторный поиск — query → вектор → ANN (USearch) → top-k.

Векторы чанков под моделью материализует воркер ``embed`` в таблицу
``embeddings`` (сайдкар — синхронизируемый артефакт, таблица — локальное
производное, §8). Поиск: эмбеддинг запроса (префикс ``query:``, §12.1) через
активную поисковую модель → ANN-индекс USearch (mmap-файл на модель,
``data_dir/ann/<модель>.usearch``) → top-k по косинусу.

Индекс ведётся воркером ``embed`` (добавляет векторы при расчёте) и ``ann
rebuild`` (полная пересборка из таблицы). Если файла индекса нет (например,
база создана до внедрения USearch) — точный скан таблицы как фолбэк: тот же
результат, медленнее на больших объёмах. Протухшие ключи (документ удалён)
отсекаются при сопоставлении с таблицей, поэтому индекс безопасно «грязный».
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..embed.sidecar import unpack_f16_vectors
from ..interfaces import Embedder, MetaStore
from ..search.models import SearchHit
from .index import UsannIndex


class VectorSearch:
    """Семантический поиск: ANN USearch с фолбэком на точный скан (модуль 3)."""

    def __init__(
        self,
        store: MetaStore,
        embedder_factory: Callable[[str], Embedder],
        search_model: str,
        ann_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._embedder_factory = embedder_factory
        self._search_model = search_model
        self._ann_dir = Path(ann_dir) if ann_dir else None

    def search(self, text: str, limit: int = 20) -> list[SearchHit]:
        """Top-k семантических хитов по косинусу с вектором запроса."""
        if not text.strip() or limit <= 0:
            return []
        embedder = self._embedder_factory(self._search_model)
        dim = embedder.dim
        query_vector = embedder.embed_query([text])[0]
        hits = self._search_index(query_vector, dim, limit)
        if hits is not None:
            return hits
        return self._search_scan(query_vector, dim, limit)

    def _search_index(
        self, query_vector: list[float], dim: int, limit: int
    ) -> list[SearchHit] | None:
        """Поиск по ANN-индексу; ``None`` — индекса нет (фолбэк на скан)."""
        if self._ann_dir is None:
            return None
        path = self._ann_dir / f"{self._search_model}.usearch"
        if not path.exists():
            return None
        index = UsannIndex(path, dim)
        if index.size == 0:
            return None
        matches = index.search(query_vector, limit)
        rows_by_id = {
            row["id"]: row
            for row in self._store.embeddings_for_ids([key for key, _ in matches])
        }
        hits: list[SearchHit] = []
        for key, score in matches:
            row = rows_by_id.get(key)
            if row is None:
                continue  # протухший ключ: документ удалён, индекс ещё не почищен
            hits.append(
                SearchHit(
                    document_id=row["doc_id"],
                    chunk_index=row["chunk_index"],
                    score=score,
                )
            )
        return hits

    def _search_scan(
        self, query_vector: list[float], dim: int, limit: int
    ) -> list[SearchHit]:
        """Точный фолбэк: косинус по всем векторам модели из таблицы."""
        rows = self._store.embeddings_for_model(self._search_model)
        scored: list[tuple[float, str, int]] = []
        for row in rows:
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
