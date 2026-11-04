"""Сервис поиска.

Пока реализован морфологический слой (FTS5). Далее — семантика и тэги, порядок
внутри группы: точные совпадения → семантика → морфология → тэги (ADR D16).
"""

from __future__ import annotations

from ..interfaces import MetaStore, Stemmer
from ..morphology import analyze_to_string
from .models import SearchHit, SearchQuery, SearchResult


class SearchService:
    """Реализация сервиса поиска (см. ``interfaces.Search``)."""

    def __init__(self, store: MetaStore, stemmer: Stemmer) -> None:
        self._store = store
        self._stemmer = stemmer

    def search(self, query: SearchQuery) -> SearchResult:
        text = query.text.strip()
        if not text or query.limit <= 0:
            return SearchResult(query=query.text)

        stemmed = analyze_to_string(text, self._stemmer)
        if not stemmed.strip():
            return SearchResult(query=query.text)

        rows = self._store.search_morph(stemmed, limit=query.limit)
        hits = tuple(
            SearchHit(
                document_id=row["doc_id"],
                chunk_index=row["chunk_index"],
                score=row["score"],
            )
            for row in rows
        )
        return SearchResult(query=query.text, hits=hits)
