"""``persinder ann rebuild``: пересборка ANN-индексов из таблицы ``embeddings``."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..config import Settings
from ..embed import build_embedder
from ..store import SqliteStore
from ..vectorindex import rebuild_index


def run_ann_rebuild(home: Path | None, model: str | None) -> int:
    """Пересобрать ANN-индексы из таблицы ``embeddings`` (§8).

    Нужно после холодного старта устройства (таблица есть, индекса нет) и при
    рассинхронизации. По умолчанию — все модели, у которых есть векторы.
    """
    settings = Settings.default()
    if home:
        settings = replace(settings, home=home)
    settings.ensure()
    store = SqliteStore(settings.db_path)
    try:
        keys = [model] if model else store.embedding_models()
        for key in keys:
            count = rebuild_index(
                store,
                lambda k: build_embedder(settings.models_dir, k),
                settings.ann_dir,
                key,
            )
            print(f"{key}: ANN-индекс пересобран, ключей {count}.")
        return 0
    finally:
        store.close()
