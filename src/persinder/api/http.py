"""HTTP-обёртка над сервисами Persinder (стандартная библиотека, без зависимостей).

Слушает только локальный интерфейс и запускается по требованию приложением.
"""

from __future__ import annotations

import json
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from ..ingest import IngestResult
from ..interfaces import Ingest, Search
from ..search import SearchQuery
from .models import IngestPayload, IngestResponse, PayloadError, SearchResponse

MAX_BODY_BYTES = 32 * 1024 * 1024  # 32 МБ


class PersinderHTTPServer(ThreadingHTTPServer):
    """Сервер с привязанными сервисами приёма и поиска."""

    daemon_threads = True
    ingest: Ingest
    search: Search

    def __init__(
        self, address: tuple[str, int], ingest: Ingest, search: Search
    ) -> None:
        super().__init__(address, _Handler)
        self.ingest = ingest
        self.search = search


class _Handler(BaseHTTPRequestHandler):
    server_version = "Persinder/0.1"

    def _send_json(self, code: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _services(self) -> PersinderHTTPServer:
        return cast(PersinderHTTPServer, self.server)

    def do_GET(self) -> None:  # noqa: N802 (имя задано базовым классом)
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            self._send_json(200, {"status": "ok"})
        elif parsed.path == "/search":
            self._handle_search(parsed.query)
        else:
            self._send_json(404, {"error": "not found"})

    def _handle_search(self, raw_query: str) -> None:
        params = parse_qs(raw_query)
        text = (params.get("q") or [""])[0]
        raw_limit = (params.get("limit") or ["20"])[0]
        try:
            limit = int(raw_limit)
        except ValueError:
            self._send_json(400, {"error": "invalid limit"})
            return

        min_values = params.get("min_score")
        raw_min_score = min_values[0] if min_values else None
        min_score: float | None = None
        if raw_min_score is not None:
            try:
                min_score = float(raw_min_score)
            except ValueError:
                self._send_json(400, {"error": "invalid min_score"})
                return
            if not math.isfinite(min_score):
                self._send_json(400, {"error": "invalid min_score"})
                return

        result = self._services().search.search(
            SearchQuery(text=text, limit=limit, min_score=min_score)
        )
        self._send_json(200, SearchResponse.from_result(result).to_dict())

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/ingest":
            self._send_json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json(400, {"error": "invalid body size"})
            return

        try:
            raw = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid json"})
            return

        try:
            payload = IngestPayload.from_json(raw)
        except PayloadError as exc:
            self._send_json(400, {"error": str(exc)})
            return

        result = self._services().ingest.ingest(payload.to_request())

        response = IngestResponse.from_result(result)
        code = 200 if result.status != IngestResult.REJECTED else 422
        self._send_json(code, response.to_dict())

    def log_message(self, format: str, *args: Any) -> None:
        """Тихий лог: не засоряем вывод."""
        return


def make_server(
    ingest: Ingest, search: Search, host: str, port: int
) -> PersinderHTTPServer:
    """Создать HTTP-сервер (без запуска). ``port=0`` выберет свободный порт."""
    return PersinderHTTPServer((host, port), ingest, search)


def serve(ingest: Ingest, search: Search, host: str, port: int) -> None:
    """Запустить сервер и работать до остановки."""
    server = make_server(ingest, search, host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
