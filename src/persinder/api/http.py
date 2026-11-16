"""HTTP-обёртка над сервисами Persinder (стандартная библиотека, без зависимостей).

Слушает только локальный интерфейс и запускается по требованию приложением.
"""

from __future__ import annotations

import json
import math
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from ..ingest import IngestResult
from ..interfaces import Ingest, Search
from ..search import SearchQuery
from .index_log import IndexLog
from .models import IngestPayload, IngestResponse, PayloadError, SearchResponse

MAX_BODY_BYTES = 32 * 1024 * 1024  # 32 МБ

# Собранный веб-клиент кладётся рядом с пакетом (см. web/README.md): человеку,
# который просто запускает `persinder serve`, Node.js и сборка не нужны.
STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_FILE = STATIC_DIR / "index.html"
ASSETS_DIR = STATIC_DIR / "assets"

INDEX_CACHE = "no-cache"
ASSET_CACHE = "public, max-age=31536000, immutable"

_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
}


class PersinderHTTPServer(ThreadingHTTPServer):
    """Сервер с привязанными сервисами приёма и поиска."""

    daemon_threads = True
    ingest: Ingest
    search: Search
    log: IndexLog | None

    def __init__(
        self,
        address: tuple[str, int],
        ingest: Ingest,
        search: Search,
        log: IndexLog | None = None,
    ) -> None:
        super().__init__(address, _Handler)
        self.ingest = ingest
        self.search = search
        self.log = log

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Клиент мог разорвать соединение (например, отменил запрос при
        новом вводе) — это штатно, traceback не печатаем; прочие ошибки
        показываем как раньше."""
        exc_type = sys.exc_info()[0]
        if exc_type is not None and issubclass(exc_type, ConnectionError):
            return
        super().handle_error(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    server_version = "Persinder/0.1"

    def _send_json(self, code: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            # Windows: ConnectionAbortedError (WinError 10053) — браузер
            # разорвал соединение, отвечать некому; это не ошибка сервера.
            return

    def _services(self) -> PersinderHTTPServer:
        return cast(PersinderHTTPServer, self.server)

    def do_GET(self) -> None:  # noqa: N802 (имя задано базовым классом)
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            self._send_json(200, {"status": "ok"})
        elif parsed.path == "/search":
            self._handle_search(parsed.query)
        elif parsed.path == "/logs":
            self._handle_logs(parsed.query)
        else:
            self._handle_static(parsed.path)

    def _handle_static(self, path: str) -> None:
        """Отдать собранный веб-клиент; неизвестные маршруты — на index.html (SPA)."""
        if path in ("", "/", "/index.html"):
            self._send_file(INDEX_FILE, INDEX_CACHE)
            return

        if path == "/assets" or path.startswith("/assets/"):
            candidate = (STATIC_DIR / path.lstrip("/")).resolve()
            if candidate.is_relative_to(ASSETS_DIR) and candidate.is_file():
                self._send_file(candidate, ASSET_CACHE)
            else:
                self._send_json(404, {"error": "not found"})
            return

        # Файлы с расширением (favicon.ico, robots.txt) не подменяем приложением.
        if Path(path).suffix:
            self._send_json(404, {"error": "not found"})
            return

        self._send_file(INDEX_FILE, INDEX_CACHE)

    def _send_file(self, path: Path, cache: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json(404, {"error": "not found"})
            return

        content_type = (
            _CONTENT_TYPES.get(path.suffix.lower())
            or mimetypes.guess_type(path.name)[0]
            or "application/octet-stream"
        )
        try:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:  # клиент отключился — см. _send_json
            return

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

        layer_limits: dict[str, int | None] = {
            "morph_limit": None,
            "semantic_limit": None,
        }
        for name in layer_limits:
            values = params.get(name)
            if values is None:
                continue
            try:
                layer_limits[name] = int(values[0])
            except ValueError:
                self._send_json(400, {"error": f"invalid {name}"})
                return

        result = self._services().search.search(
            SearchQuery(
                text=text,
                limit=limit,
                min_score=min_score,
                morph_limit=layer_limits["morph_limit"],
                semantic_limit=layer_limits["semantic_limit"],
            )
        )
        self._send_json(200, SearchResponse.from_result(result).to_dict())

    def _handle_logs(self, raw_query: str) -> None:
        """Последние события индексации: ``GET /logs?limit=200&after=N``."""
        params = parse_qs(raw_query)
        try:
            limit = int((params.get("limit") or ["200"])[0])
        except ValueError:
            self._send_json(400, {"error": "invalid limit"})
            return
        limit = max(1, min(limit, 1000))

        after: int | None = None
        raw_after = params.get("after")
        if raw_after:
            try:
                after = int(raw_after[0])
            except ValueError:
                self._send_json(400, {"error": "invalid after"})
                return

        log = self._services().log
        if log is None:
            self._send_json(200, {"events": [], "last_id": 0})
            return

        events = [ev.to_dict() for ev in log.entries(limit=limit, after_id=after)]
        self._send_json(200, {"events": events, "last_id": log.last_id})

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
    ingest: Ingest,
    search: Search,
    host: str,
    port: int,
    log: IndexLog | None = None,
) -> PersinderHTTPServer:
    """Создать HTTP-сервер (без запуска). ``port=0`` выберет свободный порт."""
    return PersinderHTTPServer((host, port), ingest, search, log)


def serve(
    ingest: Ingest,
    search: Search,
    host: str,
    port: int,
    log: IndexLog | None = None,
) -> None:
    """Запустить сервер и работать до остановки."""
    server = make_server(ingest, search, host, port, log)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
