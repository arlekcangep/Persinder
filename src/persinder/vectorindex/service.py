"""Модуль 3: векторный поиск — query → вектор → SQLite embeddings → косинус.

Векторы чанков под моделью материализует воркер ``embed`` в таблицу
``embeddings`` (сайдкар — синхронизируемый артефакт, эта таблица — локальное
производное, §8). Поиск: эмбеддинг запроса (префикс ``query:``, §12.1) через
активную поисковую модель, косинус по всем векторам модели, top-k. Векторы
нормированы, поэтому косинус — это скалярное произведение.

В прототипе — полный скан (без ANN-индекса): для личной базы это доли секунды.
На больших объёмах — USearch (§8, ещё не реализован).
"""

from __future__ import annotations

from collections.abc import Callable

from ..embed.sidecar import unpack_f16_vectors
from ..interfaces import Embedder, MetaStore
from ..search.models import SearchHit


class VectorSearch:
    """Семантический поиск по векторам из SQLite (модуль 3)."""

    def __init__(
        self,
        store: MetaStore,
        embedder_factory: Callable[[str], Embedder],
        search_model: str,
    ) -> None:
        self._store = store
        self._embedder_factory = embedder_factory
        self._search_model = search_model

    def search(self, text: str, limit: int = 20) -> list[SearchHit]:
        """Top-k семантических хитов по косинусу с вектором запроса."""
        if not text.strip() or limit <= 0:
            return []
        embedder = self._embedder_factory(self._search_model)
        dim = embedder.dim
        query_vector = embedder.embed_query([text])[0]
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
