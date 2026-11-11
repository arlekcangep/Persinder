"""Точка входа ``python -m persinder``.

Вся логика — в пакете :mod:`persinder.cli`; здесь только реэкспорт для
совместимости (тесты и скрипты импортируют имена отсюда) и запуск.
"""

from __future__ import annotations

from .cli import (  # noqa: F401 — реэкспорт публичных имён CLI
    Cli,
    EmbedRunner,
    ModelManager,
    ServeRunner,
    WatchRunner,
    _append_embed_error,
    _candidate_config,
    _embed_loop,
    _say,
    build_service,
    format_embed,
    format_recovery,
    main,
    models_for_source,
    print_stats,
    run_ann_rebuild,
    run_embed,
    run_model_command,
    run_model_install,
    run_model_list,
    run_model_remove,
    run_model_use,
    run_search,
    run_serve,
    run_watch,
    search_over_http,
    search_scopes,
)

__all__ = [
    "Cli",
    "EmbedRunner",
    "ModelManager",
    "ServeRunner",
    "WatchRunner",
    "_append_embed_error",
    "_candidate_config",
    "_embed_loop",
    "_say",
    "build_service",
    "format_embed",
    "format_recovery",
    "main",
    "models_for_source",
    "print_stats",
    "run_ann_rebuild",
    "run_embed",
    "run_model_command",
    "run_model_install",
    "run_model_list",
    "run_model_remove",
    "run_model_use",
    "run_search",
    "run_serve",
    "run_watch",
    "search_over_http",
    "search_scopes",
]


if __name__ == "__main__":
    raise SystemExit(main())
