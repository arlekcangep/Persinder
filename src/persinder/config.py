"""Настройки и каталоги Persinder."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Настройки приложения."""

    home: Path
    chunk_size: int = 1200
    chunk_overlap: int = 200
    host: str = "127.0.0.1"
    port: int = 8756

    @property
    def db_path(self) -> Path:
        return self.home / "persinder.db"

    @property
    def text_dir(self) -> Path:
        """Временный каталог для сырого текста до подсчёта эмбеддингов."""
        return self.home / "pending_texts"

    @property
    def virtual_dir(self) -> Path:
        """Папка данных, которых нет на диске (web, вставленный текст).

        Сюда ingest кладёт их сайдкары (``<doc_id>.pdr``); сам текст живёт в
        ``text_dir``.
        """
        return self.home / "virtual"

    @property
    def models_dir(self) -> Path:
        """Каталог моделей эмбеддингов: ``<home>/models`` (внутри APP-каталога).

        Каждая модель — своя подпапка ``<models_dir>/<ключ>/`` (веса +
        токенизатор). Модели всегда лежат в подкаталоге ``home`` — отдельного
        глобального каталога/переменной нет.
        """
        return self.home / "models"

    @property
    def models_state_path(self) -> Path:
        """Файл активных моделей профиля (``embedding``/``search``, D24)."""
        return self.home / "models.json"

    @property
    def ann_dir(self) -> Path:
        """Каталог ANN-индексов USearch: ``<home>/ann/<модель>.usearch``.

        Индекс — локальное производное (пересобирается из таблицы ``embeddings``,
        ``persinder ann rebuild``), живёт вне синка, на mmap — без резидента в RAM.
        """
        return self.home / "ann"

    def ensure(self) -> Settings:
        """Создать каталоги, если их нет."""
        self.home.mkdir(parents=True, exist_ok=True)
        self.text_dir.mkdir(parents=True, exist_ok=True)
        self.virtual_dir.mkdir(parents=True, exist_ok=True)
        self.ann_dir.mkdir(parents=True, exist_ok=True)
        return self

    @classmethod
    def default(cls) -> Settings:
        return cls(home=Path.home() / ".persinder")
