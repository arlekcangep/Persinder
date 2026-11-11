"""Консольный интерфейс Persinder (``python -m persinder``).

Код разложен по классам-исполнителям, чтобы ``__main__.py`` оставался точкой
входа:

* :class:`~persinder.cli.parser.Cli` — разбор аргументов и выбор команды;
* :class:`~persinder.cli.serve.ServeRunner` — HTTP + watcher + эмбеддинги;
* :class:`~persinder.cli.watch.WatchRunner` — только watcher;
* :class:`~persinder.cli.embedding.EmbedRunner` — очередь эмбеддингов;
* :class:`~persinder.cli.models.ModelManager` — модели (install/use/remove).
"""

from .ann import run_ann_rebuild
from .config import candidate_config as _candidate_config
from .embedding import EmbedRunner, run_embed
from .embedding import embed_loop as _embed_loop
from .models import (
    ModelManager,
    run_model_command,
    run_model_install,
    run_model_list,
    run_model_remove,
    run_model_use,
)
from .parser import Cli, main
from .report import (
    append_embed_error as _append_embed_error,
)
from .report import (
    format_embed,
    format_recovery,
    print_stats,
)
from .report import (
    say as _say,
)
from .search import run_search, search_over_http
from .serve import ServeRunner, run_serve
from .service import build_service, models_for_source, search_scopes
from .watch import WatchRunner, run_watch

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
