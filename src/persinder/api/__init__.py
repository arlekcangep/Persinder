"""Транспортный адаптер HTTP над сервисами Persinder."""

# Новый FastAPI-сервер — шаг 1 замены ThreadingHTTPServer на FastAPI + uvicorn
from .fastapi_server import PersinderFastAPIServer, make_fastapi_server, serve_fastapi

# Old HTTP server (ThreadingHTTPServer) — оставляем для обратной совместимости
from .http import PersinderHTTPServer, make_server, serve
from .index_log import (
    KIND_EMBED,
    KIND_INDEX,
    KIND_SYSTEM,
    LOG_KINDS,
    IndexLog,
    LogEvent,
    parse_time_bound,
)
from .models import (
    IngestPayload,
    IngestResponse,
    PayloadError,
    SearchResponse,
)

__all__ = [
    # Старые экспорты (старый сервер)
    "PersinderHTTPServer",
    "make_server",
    "serve",
    # Новые экспорты (FastAPI сервер, шаг 1)
    "PersinderFastAPIServer",
    "make_fastapi_server",
    "serve_fastapi",
    "KIND_EMBED",
    "KIND_INDEX",
    "KIND_SYSTEM",
    "LOG_KINDS",
    "IndexLog",
    "LogEvent",
    "parse_time_bound",
    "IngestPayload",
    "IngestResponse",
    "SearchResponse",
    "PayloadError",
]
