"""Транспортный адаптер HTTP над сервисом приёма."""

from .http import IngestHTTPServer, make_server, serve
from .models import IngestPayload, IngestResponse, PayloadError

__all__ = [
    "IngestHTTPServer",
    "make_server",
    "serve",
    "IngestPayload",
    "IngestResponse",
    "PayloadError",
]
