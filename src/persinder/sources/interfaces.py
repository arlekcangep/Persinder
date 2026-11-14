"""Абстрактные интерфейсы экстракторов.

Экстракторы (``Extractor``) и контейнеры (``ContainerExtractor``) реализуют
эти порты; реестр (`ExtractorRegistry`) опрашивает экземпляры по этой схеме.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .classify import ClassMatch

if TYPE_CHECKING:
    from .extractors._base import Extracted


@runtime_checkable
class Extractor(Protocol):
    """Порт экстрактора: класс + подтипы + ответ на опрос реестра + извлечение."""

    name: str
    """Уникальное имя экстрактора (используется в БД и конфиге)."""

    format_class: str
    """Класс формата (``text``, ``structure-text``, ``pub-formats``, ``archive``)."""

    def suffixes(self) -> frozenset[str]:
        """Расширения (нижний регистр, с точкой), например ``.md`` — фолбэк."""
        ...

    def responds(self, extension: str, matches: tuple[ClassMatch, ...]) -> bool:
        """Отклик на опрос реестра: обработает ли этот файл."""
        ...

    def extract(self, path: Path) -> Extracted:
        """Извлечь текст и метаданные из файла (возвращает ``Extracted``)."""
        ...


@runtime_checkable
class EntryResolver(Protocol):
    """Отбор экстракторов для записей контейнера (по содержимому записи).

    Реализуется ``_AreaEntryResolver`` из ``extractors/registry.py``: он
    классифицирует байты записи и опрашивает реестр. Контейнеры получают
    резолвер в ``extract_all``, чтобы не знать о реестре напрямую.
    """

    def extractors_for(self, name: str, data: bytes) -> list[Extractor]:
        """Экстракторы для одной записи контейнера (по её содержимому)."""
        ...


@runtime_checkable
class ContainerExtractor(Protocol):
    """Экстрактор-контейнер: раскрывает файл в несколько частей.

    В отличие от ``Extractor`` (один файл → один результат), контейнер отдаёт
    список ``Extracted``. Экстракторы для частей он берёт из резолвера
    (``_AreaEntryResolver``), который классифицирует записи по байтам.
    """

    name: str
    """Уникальное имя контейнера (обычно ``"archive"``)."""

    format_class: str
    """Класс контейнера — ``"archive"``."""

    def suffixes(self) -> frozenset[str]:
        """Расширения файлов-контейнеров (в нижнем регистре, с точкой)."""
        ...

    def responds(self, extension: str, matches: tuple[ClassMatch, ...]) -> bool:
        """Отклик на опрос реестра: этот ли контейнер."""
        ...

    def extract_all(self, path: Path, resolver: EntryResolver) -> list[Extracted]:
        """Раскрыть контейнер в список частей."""
        ...
