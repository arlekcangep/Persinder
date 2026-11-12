"""Асинхронный HTTP-сервер поиска на FastAPI + uvicorn.

Полная замена ``PersinderHTTPServer`` из ``http.py`` для путей поиска:
``/search``, ``/context``, ``/logs``, ``/health``. ``/ingest`` и статика
раздаются здесь же, чтобы весь сервер работал в одном event loop.

Сервис поиска ``SearchService.search`` остаётся синхронным по контракту
(порт ``Search``) - вызов оборачивается в ``asyncio.to_thread``, чтобы не
блокировать event loop. Всё, что работает с памятью, синхронно.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from uvicorn.config import Config as UvicornConfig
from uvicorn.server import Server

from ..interfaces import Ingest, MetaStore, Search, TextStore
from ..search import SearchQuery, SearchResult
from .index_log import IndexLog
from .models import IngestPayload, IngestResponse, SearchResponse
from .textresolver import SOURCE_MISSING, TextResolver

if TYPE_CHECKING:
    from ..sources.extract import ExtractorRegistry

#: Максимум размера одного фрагмента контекста (один большой текст режется
#: на чанки, чтобы не вешать ответ).
_CONTEXT_CHUNK_BYTES = 1024 * 1024

#: Статичные файлы: собранный веб-клиент лежит рядом с модулем (http.py).
_STATIC_DIR = Path(__file__).resolve().parent / "static"

_INDEX_CACHE = "no-cache"
_ASSET_CACHE = "public, max-age=31536000, immutable"

_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
}

MAX_BODY_BYTES = 32 * 1024 * 1024


class SyncSearchService:
    """Синхронный фасад над портом ``Search``."""

    def __init__(self, search: Search) -> None:
        self._search = search

    def search(self, query: SearchQuery) -> SearchResult:
        return self._search.search(query)

    @property
    def log(self) -> IndexLog | None:
        return None


def build_fastapi_app(
    ingest: Ingest,
    search: Search,
    log: IndexLog | None = None,
    stats: Callable[[], dict[str, object]] | None = None,
    *,
    store: MetaStore | None = None,
    textstore: TextStore | None = None,
    registry: ExtractorRegistry | None = None,
) -> FastAPI:
    """Собрать приложение FastAPI (без запуска).

    ``store``/``textstore``/``registry`` — для ``/context`` (вариант C):
    резолвер сначала берёт текст из textstore, при отсутствии — перечитывает
    оригинал по ``source_path`` через экстрактор из реестра.
    """
    search_svc = SyncSearchService(search)
    resolver = TextResolver(store, textstore, registry) if store else None

    _app = FastAPI(
        title="Persinder",
        description="Персональный индексатор: поисковый сервис",
    )
    _app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @_app.get("/health")
    async def health() -> dict[str, object]:
        """Состояние сервера + счётчики (модули, векторы, очередь)."""
        payload: dict[str, object] = {"status": "ok"}
        if stats is not None:
            try:
                payload.update(stats())
            except Exception as exc:  # noqa: BLE001 — /health важнее статистики
                payload["stats_error"] = f"{exc.__class__.__name__}: {exc}"
        return payload

    @_app.get("/search")
    async def search_api(
        q: str = Query(default="", description="текст запроса"),
        limit: int = Query(default=20, ge=1, le=1000, description="максимум хитов"),
        min_score: float | None = Query(
            default=None, description="порог косинуса (только семантика)"
        ),
        morph_limit: int | None = Query(
            default=None, ge=0, description="лимит морфологического слоя"
        ),
        semantic_limit: int | None = Query(
            default=None, ge=0, description="лимит семантического слоя"
        ),
    ) -> SearchResponse:
        """Поиск (шаг 1): морфология + семантика, без контекста."""
        try:
            morph = None if morph_limit is None else int(morph_limit)
            semantic = None if semantic_limit is None else int(semantic_limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid layer limit") from exc
        query = SearchQuery(
            text=q,
            limit=limit,
            min_score=min_score,
            morph_limit=morph,
            semantic_limit=semantic,
        )
        result = await asyncio.to_thread(search_svc.search, query)
        return SearchResponse.from_result(result)

    @_app.get("/context")
    async def context_api(
        doc_id: str = Query(..., description="id документа"),
        start: int = Query(..., ge=0, description="смещение начала в символах"),
        end: int = Query(..., ge=0, description="смещение конца в символах"),
        encoding: str = Query(default="utf-8"),
        offset: int = Query(default=0, ge=0, description="подмотка окна в символах"),
    ) -> StreamingResponse:
        """Контекст (шаг 2): асинхронный поток NDJSON-фрагментов.

        ``start``/``end`` — смещения в символах относительно начала документа;
        ``offset`` подматывается к обоим. Ответ чанкуется, если фрагмент
        больше ``CONTEXT_CHUNK_BYTES``, чтобы большой текст не вешал ответ.
        """
        return StreamingResponse(
            context_stream(resolver, doc_id, start, end, encoding, offset),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "Transfer-Encoding": "chunked"},
        )

    @_app.get("/context/batch")
    async def context_batch_api(
        hits: str = Query(..., description="JSON-список {doc_id,start,end}"),
        encoding: str = Query(default="utf-8"),
    ) -> StreamingResponse:
        """Контекст пачкой: список хитов из /search, один поток."""
        try:
            items = json.loads(hits)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="invalid hits json") from exc
        if not isinstance(items, list):
            raise HTTPException(status_code=400, detail="hits must be a list")
        return StreamingResponse(
            context_stream_batch(resolver, items, encoding),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "Transfer-Encoding": "chunked"},
        )

    @_app.get("/logs")
    async def logs_api(
        limit: int = Query(default=200, ge=1, le=1000),
        after: int | None = Query(default=None),
        kind: str | None = Query(default=None),
        from_: str | None = Query(default=None, alias="from"),
        to_: str | None = Query(default=None, alias="to"),
    ) -> dict[str, object]:
        """События индексации (как раньше, но асинхронно)."""
        try:
            limit = max(1, min(limit, 1000))
            since = IndexLog._parse_time_bound(from_) if from_ else None
            until = IndexLog._parse_time_bound(to_, end_of_day=True) if to_ else None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if kind and kind not in IndexLog.KINDS:
            raise HTTPException(status_code=400, detail="invalid kind")
        log = search_svc.log
        if log is None:
            return {"events": [], "last_id": 0}
        events = [
            ev.to_dict()
            for ev in log.entries(
                limit=limit,
                after_id=after,
                kind=kind or None,
                since=since,
                until=until,
            )
        ]
        return {"events": events, "last_id": log.last_id}

    @_app.post("/ingest")
    async def ingest_api(request: IngestPayload) -> IngestResponse:
        """Приём текста (как раньше, но асинхронно)."""
        return IngestResponse.from_result(ingest.ingest(request.to_request()))

    # Статика: веб-клиент лежит в package-данных модуля api/static.
    if _STATIC_DIR.is_dir():
        _app.mount(
            "/",
            StaticFiles(
                directory=str(_STATIC_DIR),
                html=True,
                check_dir=True,
            ),
            name="static",
        )

    return _app


async def context_stream(
    resolver: TextResolver | None,
    doc_id: str,
    start: int,
    end: int,
    encoding: str,
    offset: int = 0,
):
    """Один хит -> поток NDJSON-фрагментов (асинхронный генератор).

    Текст берёт ``TextResolver`` (textstore → перечитывание оригинала), сам
    режет по символам. Если резолвера нет (``store`` не передан) или документ
    недоступен — фрагмент с ошибкой ``source_missing``/``error``.
    """
    if resolver is None:
        yield _ndjson_error(doc_id, start, end, SOURCE_MISSING)
        return

    resolved, error = await asyncio.to_thread(
        resolver.resolve, doc_id, start, end, offset
    )
    if resolved is None:
        yield _ndjson_error(doc_id, start, end, error or SOURCE_MISSING)
        return

    cut = resolved.text
    base = start + offset
    if len(cut.encode("utf-8")) <= _CONTEXT_CHUNK_BYTES:
        yield _ndjson_fragment(doc_id, base, base + len(cut), cut, encoding)
    else:
        # Длинный срез режем на фрагменты: не больше бюджета байт на один,
        # позиции — символьные (по границам символов — режем по str, не байтам).
        pos = base
        for part in _split_chars_by_bytes(cut, _CONTEXT_CHUNK_BYTES):
            yield _ndjson_fragment(doc_id, pos, pos + len(part), part, encoding)
            pos += len(part)


async def context_stream_batch(
    resolver: TextResolver | None,
    items: list[dict[str, object]],
    encoding: str,
):
    """Пачка хитов: один NDJSON-поток, хиты строго по порядку.

    Порядок хитов важен для клиента, поэтому обрабатываем последовательно:
    для каждого хита отдаём его фрагменты (ошибку — если аргументы битые),
    затем следующий.
    """
    for item in items:
        doc_id = item.get("doc_id")
        start = item.get("start", 0)
        end = item.get("end", 0)
        if not (
            isinstance(doc_id, str)
            and isinstance(start, int)
            and isinstance(end, int)
        ):
            yield _ndjson_error(doc_id, start, end, "invalid args")
            continue
        async for b in context_stream(resolver, doc_id, start, end, encoding):
            yield b


def _split_chars_by_bytes(text: str, budget: int) -> list[str]:
    """Разбить текст на фрагменты не больше ``budget`` байт.

    Режем по ``str`` — границы всегда совпадают с символами; размер части
    меряется в байтах UTF-8, чтобы клиент получал фрагменты примерно одного
    веса, а не по числу символов.
    """
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for ch in text:
        ch_bytes = len(ch.encode("utf-8"))
        if current and size + ch_bytes > budget:
            parts.append("".join(current))
            current = []
            size = 0
        current.append(ch)
        size += ch_bytes
    if current:
        parts.append("".join(current))
    return parts


def _ndjson_fragment(
    doc_id: str, start: int, end: int, text: str, encoding: str
) -> bytes:
    """Один фрагмент ответа NDJSON.

    ``encoding`` пока не используется: перекодировка в запрошенную кодировку
    — багфикс следующей итерации (сейчас отдаём как было — UTF-8).
    """
    fragment = {
        "type": "fragment",
        "doc_id": doc_id,
        "start": start,
        "end": end,
        "text": text,
    }
    body = json.dumps(fragment, ensure_ascii=False).encode("utf-8")
    return body + b"\n"


def _ndjson_error(doc_id: str, start: int, end: int, error: str) -> bytes:
    """Ошибка в виде NDJSON-фрагмента."""
    fragment = {
        "type": "fragment",
        "doc_id": doc_id,
        "start": start,
        "end": end,
        "error": error,
    }
    return json.dumps(fragment, ensure_ascii=False).encode("utf-8") + b"\n"


class PersinderFastAPIServer:
    """Обёртка над FastAPI + uvicorn (запуск/остановка)."""

    def __init__(
        self,
        app: FastAPI,
        host: str,
        port: int,
    ) -> None:
        self.app = app
        self.host = host
        self.port = port
        self._server: Server | None = None

    def serve(self) -> None:
        """Запустить сервер и ждать до остановки."""
        config = UvicornConfig(
            self.app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._server = Server(config)
        try:
            self._server.run()
        except KeyboardInterrupt:
            pass
        finally:
            self._server.should_exit = True

    def close(self) -> None:
        if self._server is not None:
            self._server.should_exit = True


def make_fastapi_server(
    ingest: Ingest,
    search: Search,
    host: str,
    port: int,
    log: IndexLog | None = None,
    stats: Callable[[], dict[str, object]] | None = None,
    *,
    store: MetaStore | None = None,
    textstore: TextStore | None = None,
    registry: ExtractorRegistry | None = None,
) -> PersinderFastAPIServer:
    """Создать FastAPI-сервер (без запуска). ``port=0`` выберет свободный порт."""
    app = build_fastapi_app(
        ingest,
        search,
        log,
        stats,
        store=store,
        textstore=textstore,
        registry=registry,
    )
    return PersinderFastAPIServer(app, host, port)


def serve_fastapi(
    ingest: Ingest,
    search: Search,
    host: str,
    port: int,
    log: IndexLog | None = None,
    stats: Callable[[], dict[str, object]] | None = None,
    *,
    store: MetaStore | None = None,
    textstore: TextStore | None = None,
    registry: ExtractorRegistry | None = None,
) -> None:
    """Запустить FastAPI-сервер и ждать до остановки."""
    server = make_fastapi_server(
        ingest,
        search,
        host,
        port,
        log,
        stats,
        store=store,
        textstore=textstore,
        registry=registry,
    )
    try:
        server.serve()
    finally:
        server.close()
