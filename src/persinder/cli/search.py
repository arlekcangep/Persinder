"""Поиск из консоли: сначала через запущенный сервис, затем локально."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import cast

from ..api.textresolver import TextResolver
from ..config import Settings
from ..search import SearchHit, SearchQuery, SearchResult
from ..sources import AreaConfig, default_registry, load_config
from ..store import SqliteStore
from ..textstore import FsTextStore
from .config import candidate_config, known_models
from .service import build_service

SEARCH_HTTP_TIMEOUT = 10.0  # секунд; локальный сервер отвечает или отказывает сразу


def _json_str(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("string expected")
    return value


def _json_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("int expected")
    return value


def _json_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("number expected")
    return float(value)


def _json_opt_str(value: object) -> str:
    return "" if value is None else _json_str(value)


def _json_opt_int(value: object) -> int | None:
    return None if value is None else _json_int(value)


def _hits_from_json(raw: object) -> tuple[SearchHit, ...]:
    """Разобрать список хитов ответа ``GET /search``; ошибка — ``ValueError``."""
    if not isinstance(raw, list):
        raise ValueError("hits list expected")
    hits: list[SearchHit] = []
    for item in cast("list[object]", raw):
        if not isinstance(item, dict):
            raise ValueError("hit object expected")
        data = cast("dict[str, object]", item)
        hits.append(
            SearchHit(
                document_id=_json_str(data["document_id"]),
                chunk_index=_json_int(data["chunk_index"]),
                score=_json_float(data["score"]),
                chunk_start=_json_opt_int(data.get("chunk_start")),
                chunk_end=_json_opt_int(data.get("chunk_end")),
                area=_json_opt_str(data.get("area")),
                source_path=_json_opt_str(data.get("source_path")),
                filename=_json_opt_str(data.get("filename")),
            )
        )
    return tuple(hits)


def search_over_http(
    settings: Settings,
    text: str,
    limit: int,
    min_score: float | None = None,
    morph_limit: int | None = None,
    semantic_limit: int | None = None,
) -> SearchResult | None:
    """Поиск через запущенный ``serve``; ``None`` — сервер недоступен.

    Скоупы и модели берутся у сервера, поэтому CLI видит то же, что HTTP.
    Любая ошибка транспорта или формы ответа — повод для локального fallback.
    """
    params = {"q": text, "limit": str(limit)}
    if min_score is not None:
        params["min_score"] = str(min_score)
    if morph_limit is not None:
        params["morph_limit"] = str(morph_limit)
    if semantic_limit is not None:
        params["semantic_limit"] = str(semantic_limit)
    query = urllib.parse.urlencode(params)
    url = f"http://{settings.host}:{settings.port}/search?{query}"
    try:
        with urllib.request.urlopen(url, timeout=SEARCH_HTTP_TIMEOUT) as resp:
            raw: object = json.loads(resp.read().decode("utf-8"))
        if not isinstance(raw, dict):
            return None
        data = cast("dict[str, object]", raw)
        echo = data.get("query")
        return SearchResult(
            query=text if echo is None else _json_str(echo),
            hits=_hits_from_json(data.get("hits")),
            semantic_hits=_hits_from_json(data.get("semantic_hits")),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def local_search(
    settings: Settings,
    text: str,
    limit: int,
    config_path: Path | None = None,
    min_score: float | None = None,
    morph_limit: int | None = None,
    semantic_limit: int | None = None,
) -> SearchResult:
    """Локальный поиск в БД профиля (сервера нет или запрошен ``--local``).

    Если найден конфиг областей, семантика ищет по всем активным моделям и
    только в тех папках, где этими моделями индексировали; глобальное
    ``semantic: false`` выключает векторный слой совсем.
    """
    settings.ensure()
    areas: tuple[AreaConfig, ...] | None = None
    semantic = True
    config_min_score: float | None = None
    config_path = config_path or candidate_config(settings)
    if config_path is not None:
        registry = default_registry()
        areas, watch = load_config(
            config_path,
            known_formats=registry.known_names(),
            known_models=known_models(settings),
        )
        semantic = watch.semantic
        config_min_score = watch.min_score
    _ingest, search, store = build_service(
        settings,
        areas=areas,
        semantic=semantic,
        min_score=config_min_score,
    )
    try:
        return search.search(
            SearchQuery(
                text=text,
                limit=limit,
                min_score=min_score,
                morph_limit=morph_limit,
                semantic_limit=semantic_limit,
            )
        )
    finally:
        store.close()


def hit_source(hit: SearchHit) -> str:
    """Источник для строки результата: путь (или имя) и область."""
    label = hit.source_path or hit.filename or hit.document_id
    return f"{label} ({hit.area})" if hit.area else label


def _layers(result: SearchResult) -> tuple[tuple[str, tuple[SearchHit, ...]], ...]:
    """Слои выдачи в порядке печати: морфология, затем семантика."""
    return (
        ("морфология", result.hits),
        ("семантика", result.semantic_hits),
    )


def print_search_result(result: SearchResult) -> None:
    print(f"морфология: {len(result.hits)}, семантика: {len(result.semantic_hits)}")
    for kind, hits in _layers(result):
        for hit in hits:
            print(
                f"  [{kind}] {hit_source(hit)} чанк {hit.chunk_index} = {hit.score:.4f}"
            )


def _one_line(text: str) -> str:
    """Сниппет одной строкой: коллапс пробелов, обрезка."""
    compact = " ".join(text.split())
    return compact[:300]


def context_over_http(settings: Settings, hit: SearchHit) -> str | None:
    """Сниппет хита через ``GET /context`` (NDJSON); ``None`` — недоступен."""
    if hit.chunk_start is None or hit.chunk_end is None:
        return None
    params = {
        "doc_id": hit.document_id,
        "start": str(hit.chunk_start),
        "end": str(hit.chunk_end),
    }
    url = (
        f"http://{settings.host}:{settings.port}/context"
        f"?{urllib.parse.urlencode(params)}"
    )
    try:
        with urllib.request.urlopen(url, timeout=SEARCH_HTTP_TIMEOUT) as resp:
            text = ""
            for raw_line in resp:
                line = raw_line.decode("utf-8").strip()
                if not line:
                    continue
                frag = json.loads(line)
                if isinstance(frag, dict) and frag.get("type") == "fragment":
                    text += str(frag.get("text") or "")
                else:
                    return None
            return text
    except (OSError, ValueError, TypeError):
        return None


def _print_contexts_http(settings: Settings, result: SearchResult) -> None:
    """Печать сниппетов через ``/context`` по мере прихода."""
    for kind, hits in _layers(result):
        for hit in hits:
            text = context_over_http(settings, hit)
            if text:
                print(f"    [{kind}] {_one_line(text)}", flush=True)


def _print_contexts_local(settings: Settings, result: SearchResult) -> None:
    """Печать сниппетов локально: ``TextResolver`` (textstore → оригинал)."""
    store = SqliteStore(settings.db_path)
    try:
        resolver = TextResolver(
            store, FsTextStore(settings.text_dir), default_registry()
        )
        for kind, hits in _layers(result):
            for hit in hits:
                if hit.chunk_start is None or hit.chunk_end is None:
                    continue
                resolved, _error = resolver.resolve(
                    hit.document_id, hit.chunk_start, hit.chunk_end
                )
                if resolved is not None and resolved.text:
                    print(f"    [{kind}] {_one_line(resolved.text)}", flush=True)
    finally:
        store.close()


def run_search(
    home: Path | None,
    text: str,
    limit: int,
    config_path: Path | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
    local: bool = False,
    min_score: float | None = None,
    morph_limit: int | None = None,
    semantic_limit: int | None = None,
) -> int:
    """Поиск из консоли: сначала через запущенный сервер, затем локально.

    HTTP-first: сервер — источник истины по моделям и скоупам, поэтому CLI
    видит те же результаты, что ``GET /search``. Локальный поиск — fallback,
    когда сервер не отвечает, или принудительно (``--local``). Явный
    ``--config`` тоже включает локальный режим: он задаёт конфиг CLI, а не
    сервера. ``min_score`` — порог косинуса семантического слоя;
    ``morph_limit``/``semantic_limit`` — отдельные лимиты слоёв.
    """
    settings = Settings.default()
    if home:
        settings = replace(settings, home=home)
    if host:
        settings = replace(settings, host=host)
    if port is not None:
        settings = replace(settings, port=port)

    if not (local or config_path is not None):
        result = search_over_http(
            settings,
            text,
            limit,
            min_score,
            morph_limit,
            semantic_limit,
        )
        if result is not None:
            print_search_result(result)
            _print_contexts_http(settings, result)
            return 0
        print(
            f"ВНИМАНИЕ: сервер http://{settings.host}:{settings.port} не отвечает — "
            "локальный поиск; скоупы и модели могут отличаться от сервера.",
            flush=True,
        )

    result = local_search(
        settings,
        text,
        limit,
        config_path,
        min_score,
        morph_limit,
        semantic_limit,
    )
    print_search_result(result)
    _print_contexts_local(settings, result)
    return 0
