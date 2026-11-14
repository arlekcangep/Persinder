"""Кэш сырого текста на диске (``pending_texts``).

Текст кладётся сюда при приёме (``ingest``) и живёт, пока существует оригинал
(D26): им пользуются воркер эмбеддингов и ``/context``. Отдельного временного
хранилища «до подсчёта эмбеддингов» нет: воркер читает этот же кэш, а если
файла нет — перечитывает оригинал.
Запись атомарная: сначала временный файл, затем ``rename``.
"""

from __future__ import annotations

import os
from pathlib import Path


class FsTextStore:
    """Файловое хранилище: ``<root>/<key>.txt``."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = str(key).strip()
        if not safe or "/" in safe or "\\" in safe or ".." in safe:
            raise ValueError(f"недопустимый ключ: {key!r}")
        return self._root / f"{safe}.txt"

    def put(self, key: str, text: str) -> str:
        path = self._path(key)
        # Повторный скан/переиндексация приносят тот же текст: не переписывать
        # файл зря (полный reindex иначе перезаписывал бы всю библиотеку).
        try:
            if path.read_text(encoding="utf-8") == text:
                return str(path)
        except (OSError, UnicodeError):
            pass
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        return str(path)

    def read(self, key: str) -> str:
        return self._path(key).read_text(encoding="utf-8")

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()
