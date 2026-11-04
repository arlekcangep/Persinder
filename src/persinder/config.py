"""Настройки и каталоги Persinder."""

from __future__ import annotations

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

    def ensure(self) -> Settings:
        """Создать каталоги, если их нет."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.text_dir.mkdir(parents=True, exist_ok=True)
        return self

    @classmethod
    def default(cls) -> Settings:
        return cls(data_dir=Path.home() / ".persinder")
