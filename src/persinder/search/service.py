"""Сервис поиска.

Морфологический слой (FTS5) — всегда; векторный слой (``VectorSearch``, модуль 3)
— если передан. Пока результаты отдаются отдельными группами; порядок внутри
группы: точные совпадения → семантика → морфология → тэги (ADR D16) — единый
рейтинг будет следующим шагом.

Если передан ``TextStore``, каждый хит обогащается контекстом — текстом
документа вокруг чанка (несколько строк до и после, см. ``context_around``).
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from ..interfaces import MetaStore, Stemmer, TextStore
from ..morphology import analyze_to_string
from .context import (
    DEFAULT_CONTEXT_LINES,
    DEFAULT_CONTEXT_SENTENCES,
    context_around,
    hit_context,
)
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
        textstore: TextStore | None = None,
        context_lines: int = DEFAULT_CONTEXT_LINES,
        context_sentences: int = DEFAULT_CONTEXT_SENTENCES,
        default_min_score: float | None = None,
    ) -> None:
        if context_lines < 0:
            raise ValueError("число строк контекста не может быть отрицательным")
        if context_sentences < 0:
            raise ValueError("число предложений контекста не может быть отрицательным")
        self._store = store
        self._stemmer = stemmer
        self._vector_search = vector_search
        self._textstore = textstore
        self._context_lines = context_lines
        self._context_sentences = context_sentences
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
            ),
            stems=frozenset(stemmed.split()),
        )
        return SearchResult(query=query.text, hits=hits, semantic_hits=semantic)

    def _enrich(
        self,
        hits: tuple[SearchHit, ...],
        stems: frozenset[str] | None = None,
    ) -> tuple[SearchHit, ...]:
        """Добавить источник и контекст (если есть ``TextStore``).

        Источник (``area``/``source_path``/``filename``) — из
        ``document_sources``: если у документа несколько источников, берётся
        первый (реальный файл раньше виртуального). Морфологический слой
        (``stems`` задан): контекст от найденного слова — ``context_sentences``
        предложений до и после него. Семантический слой (``stems is None``):
        строки вокруг чанка — ``context_lines``.
        """
        if not hits:
            return hits

        sources = {
            str(row["doc_id"]): row
            for row in self._store.sources_for_docs(
                sorted({hit.document_id for hit in hits})
            )
        }
        enriched: list[SearchHit] = []
        for hit in hits:
            source = sources.get(hit.document_id)
            if source is None:
                enriched.append(hit)
            else:
                enriched.append(
                    replace(
                        hit,
                        area=source["area"],
                        source_path=source["source_path"],
                        filename=source["filename"],
                    )
                )

        if self._textstore is None:
            return tuple(enriched)

        textstore = self._textstore
        texts: dict[str, str | None] = {}
        with_context: list[SearchHit] = []
        for hit in enriched:
            doc_id = hit.document_id
            if doc_id not in texts:
                texts[doc_id] = self._read_text(textstore, doc_id)
            text = texts[doc_id]
            span = (
                None
                if text is None
                else self._store.chunk_span(doc_id, hit.chunk_index)
            )
            if text is None or span is None:
                with_context.append(hit)
                continue
            if stems is None:
                context = context_around(text, span[0], span[1], self._context_lines)
            else:
                context = hit_context(
                    text, span, stems, self._stemmer, self._context_sentences
                )
            with_context.append(replace(hit, context=context))
        return tuple(with_context)

    def _read_text(self, textstore: TextStore, doc_id: str) -> str | None:
        """Сырой текст документа; ``None`` — если его нет или он недоступен."""
        try:
            if not textstore.exists(doc_id):
                return None
            return textstore.read(doc_id)
        except OSError:
            return None
