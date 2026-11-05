"""Сервис поиска.

Морфологический слой (FTS5) — всегда; векторный слой (``VectorSearch``, модуль 3)
— если передан. Пока результаты отдаются отдельными группами; порядок внутри
группы: точные совпадения → семантика → морфология → тэги (ADR D16) — единый
рейтинг будет следующим шагом.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..interfaces import MetaStore, Stemmer
from ..morphology import analyze_to_string
from .models import SearchHit, SearchQuery, SearchResult

if TYPE_CHECKING:
    from ..vectorindex import VectorSearch


class SearchService:
    """Реализация сервиса поиска (см. ``interfaces.Search``)."""

    def __init__(
        self,
        store: MetaStore,
        stemmer: Stemmer,
        vector_search: VectorSearch | None = None,
    ) -> None:
        self._store = store
        self._stemmer = stemmer
        self._vector_search = vector_search

    def search(self, query: SearchQuery) -> SearchResult:
        text = query.text.strip()
        if not text or query.limit <= 0:
            return SearchResult(query=query.text)

        semantic: tuple[SearchHit, ...] = ()
        if self._vector_search is not None:
            semantic = tuple(self._vector_search.search(text, query.limit))

        stemmed = analyze_to_string(text, self._stemmer)
        if not stemmed.strip():
            return SearchResult(query=query.text, semantic_hits=semantic)

        rows = self._store.search_morph(stemmed, limit=query.limit)
        hits = tuple(
            SearchHit(
                document_id=row["doc_id"],
                chunk_index=row["chunk_index"],
                score=row["score"],
            )
            for row in rows
        )
        return SearchResult(query=query.text, hits=hits, semantic_hits=semantic)
