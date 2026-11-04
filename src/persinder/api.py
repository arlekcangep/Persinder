"""HTTP-обёртка над сервисом приёма (стандартная библиотека, без зависимостей).

Слушает только локальный интерфейс и запускается по требованию приложением.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .models import Area, IngestRequest, IngestResult

MAX_BODY_BYTES = 32 * 1024 * 1024  # 32 МБ


class IngestHTTPServer(ThreadingHTTPServer):
    """Сервер с привязанным сервисом приёма."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: object) -> None:
        super().__init__(address, _Handler)
        self.service = service


class _Handler(BaseHTTPRequestHandler):
    server_version = "Persinder/0.1"

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (имя задано базовым классом)
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/ingest":
            self._send_json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json(400, {"error": "invalid body size"})
            return

        try:
            data = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid json"})
            return
        if not isinstance(data, dict):
            self._send_json(400, {"error": "json object expected"})
            return

        try:
            area = Area.parse(data.get("area", ""))
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return

        request = IngestRequest(
            filename=str(data.get("filename", "")),
            area=area,
            text=str(data.get("text", "")),
            title=data.get("title"),
        )
        result = self.server.service.ingest(request)

        code = 200 if result.status != IngestResult.REJECTED else 422
        self._send_json(
            code,
            {
                "status": result.status,
                "document_id": result.document_id,
                "chunks": result.chunks,
                "message": result.message,
            },
        )

    def log_message(self, *args: object) -> None:
        """Тихий лог: не засоряем вывод."""
        return


def make_server(service: object, host: str, port: int) -> IngestHTTPServer:
    """Создать HTTP-сервер (без запуска). ``port=0`` выберет свободный порт."""
    return IngestHTTPServer((host, port), service)


def serve(service: object, host: str, port: int) -> None:
    """Запустить сервер и работать до остановки."""
    server = make_server(service, host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
