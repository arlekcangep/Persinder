"""Транспортный адаптер HTTP над сервисами Persinder."""

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
    "PersinderHTTPServer",
    "make_server",
    "serve",
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
