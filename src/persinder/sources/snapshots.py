"""Персистентность снапшотов сканера.

Снапшот областей хранится в JSON рядом с БД, чтобы watcher видел удаления
и правки между запусками (процесс запускается по требованию, D1).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

from .models import AreaConfig, AreaScan


class SnapshotStore:
    """Загрузка/сохранение снапшотов всех областей в один JSON-файл.

    В файле хранятся только ``rel_path -> [size, mtime_ns]``; конфиг области
    подставляется из текущего YAML при загрузке (он источник правды).
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self, areas: tuple[AreaConfig, ...]) -> dict[Path, AreaScan]:
        """Прочитать снапшоты; области берутся из конфига."""
        by_path = {area.path: area for area in areas}
        try:
            raw: object = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        top: dict[str, Any] = cast("dict[str, Any]", raw)
        data: dict[Path, AreaScan] = {}
        for area_key, files_obj in top.items():
            area = by_path.get(Path(area_key))
            if area is None or not isinstance(files_obj, dict):
                continue  # область исчезла из конфига — снапшот не нужен
            files_map: dict[str, Any] = {
                str(k): v for k, v in cast("dict[Any, Any]", files_obj).items()
            }
            files: dict[str, tuple[int, int]] = {}
            for rel, stat_obj in files_map.items():
                stat: Any = stat_obj
                if not isinstance(stat, list):
                    continue
                stat = cast("list[Any]", stat)
                if len(stat) != 2:
                    continue
                a, b = stat[0], stat[1]
                if (
                    isinstance(a, int)
                    and not isinstance(a, bool)
                    and isinstance(b, int)
                    and not isinstance(b, bool)
                ):
                    files[rel] = (a, b)
            data[area.path] = AreaScan(area=area, files=files)
        return data

    def save(self, snapshots: dict[Path, AreaScan]) -> None:
        payload = {
            str(path): {rel: list(stat) for rel, stat in snap.files.items()}
            for path, snap in snapshots.items()
        }
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(tmp, self._path)  # атомарно, переживает обрыв
