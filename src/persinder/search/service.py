"""Сервис поиска.

Морфологический слой (FTS5) — всегда; векторный слой (``VectorSearch``, модуль 3)
— если передан. Пока результаты отдаются отдельными группами; порядок внутри
группы: точные совпадения → семантика → морфология → тэги (ADR D16) — единый
рейтинг будет следующим шагом.

Текст документа сервис не читает: каждый хит несёт источник и символьные
границы чанка (``chunk_start``/``chunk_end``), а сам текст клиент запрашивает
через ``/context``.
"""

from __future__ import annotations

from dataclasses import replace
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
        default_min_score: float | None = None,
    ) -> None:
        self._store = store
        self._stemmer = stemmer
        self._vector_search = vector_search
        self._default_min_score = default_min_score

    def search(self, query: SearchQuery) -> SearchResult:
        text = query.text.strip()
        if not text:
            return SearchResult(query=query.text)

        semantic_limit = query.limit_for_semantic()
        morph_limit = query.limit_for_morph()

        semantic: tuple[SearchHit, ...] = ()
        if self._vector_search is not None and semantic_limit > 0:
            min_score = (
                query.min_score
                if query.min_score is not None
                else self._default_min_score
            )
            semantic = self._enrich(
                tuple(
                    self._vector_search.search(
                        text, semantic_limit, min_score=min_score
                    )
                )
            )

        if morph_limit <= 0:
            return SearchResult(query=query.text, semantic_hits=semantic)

        stemmed = analyze_to_string(text, self._stemmer)
        if not stemmed.strip():
            return SearchResult(query=query.text, semantic_hits=semantic)

        rows = self._store.search_morph(stemmed, limit=morph_limit)
        hits = self._enrich(
            tuple(
                SearchHit(
                    document_id=row["doc_id"],
                    chunk_index=row["chunk_index"],
                    score=row["score"],
                )
                for row in rows
            )
        )
        return SearchResult(query=query.text, hits=hits, semantic_hits=semantic)

    def _enrich(self, hits: tuple[SearchHit, ...]) -> tuple[SearchHit, ...]:
        """Добавить источник и границы чанка (текст не читаем).

        Источник (``area``/``source_path``/``filename``) — из
        ``document_sources``: если у документа несколько источников, берётся
        первый (реальный файл раньше виртуального). Границы чанка
        (``chunk_start``/``chunk_end``) — из ``chunks``: по ним клиент
        запрашивает ``/context``. ``context`` намеренно не заполняется —
        текст тянется только тем, кому он нужен.
        """
        if not hits:
            return hits

        sources = {
            str(row["doc_id"]): row
            for row in self._store.sources_for_docs(
                sorted({hit.document_id for hit in hits})
            )
        }
        spans: dict[tuple[str, int], tuple[int, int]] = {}
        for hit in hits:
            span = self._store.chunk_span(hit.document_id, hit.chunk_index)
            if span is not None:
                spans[(hit.document_id, hit.chunk_index)] = span
        enriched: list[SearchHit] = []
        for hit in hits:
            source = sources.get(hit.document_id)
            span = spans.get((hit.document_id, hit.chunk_index))
            enriched.append(
                replace(
                    hit,
                    area=source["area"] if source else "",
                    source_path=source["source_path"] if source else "",
                    filename=source["filename"] if source else "",
                    chunk_start=span[0] if span else None,
                    chunk_end=span[1] if span else None,
                )
            )
        return tuple(enriched)
