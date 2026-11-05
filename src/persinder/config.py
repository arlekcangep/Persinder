"""Настройки и каталоги Persinder."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Настройки приложения."""

    data_dir: Path
    chunk_size: int = 1200
    chunk_overlap: int = 200
    host: str = "127.0.0.1"
    port: int = 8756

    @property
    def db_path(self) -> Path:
        return self.data_dir / "persinder.db"

    @property
    def text_dir(self) -> Path:
        """Временный каталог для сырого текста до подсчёта эмбеддингов."""
        return self.data_dir / "pending_texts"

    @property
    def virtual_dir(self) -> Path:
        """Папка данных, которых нет на диске (web, вставленный текст).

        Сюда ingest кладёт их сайдкары (``<doc_id>.pdr``); сам текст живёт в
        ``text_dir``.
        """
        return self.data_dir / "virtual"

    @property
    def models_dir(self) -> Path:
        """Глобальный каталог моделей эмбеддингов (корень, не отдельная модель).

        Каждая модель — своя подпапка ``<models_dir>/<ключ>/`` (веса +
        токенизатор). По умолчанию ``~/.persinder/models`` — общий для всех
        профилей; переопределяется переменной окружения ``PERSINDER_MODEL_DIR``.
        """
        env = os.environ.get("PERSINDER_MODEL_DIR")
        if env:
            return Path(env)
        return Path.home() / ".persinder" / "models"

    @property
    def models_state_path(self) -> Path:
        """Файл активных моделей профиля (``embedding``/``search``, D24)."""
        return self.data_dir / "models.json"

    def ensure(self) -> Settings:
        """Создать каталоги, если их нет."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.text_dir.mkdir(parents=True, exist_ok=True)
        self.virtual_dir.mkdir(parents=True, exist_ok=True)
        return self

    @classmethod
    def default(cls) -> Settings:
        return cls(data_dir=Path.home() / ".persinder")
