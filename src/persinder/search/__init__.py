"""Сервис поиска: три группы и порядок внутри группы."""

from .models import SearchHit, SearchQuery, SearchResult
from .service import SearchService

__all__ = ["SearchService", "SearchQuery", "SearchHit", "SearchResult"]
