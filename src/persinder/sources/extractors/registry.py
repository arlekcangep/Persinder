"""Реестр экстракторов: опрос всех экстракторов с учётом конфига области.

``ExtractorRegistry`` классифицирует содержимое файла, опрашивает
экстракторы по схеме ``responds`` и запускает откликнувшихся. Резолвер
``_AreaEntryResolver`` выбирает экстракторы для записей контейнера по байтам.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from ..classify import (
    ARCHIVE,
    PUB_FORMATS,
    RESERVED,
    STRUCTURE_TEXT,
    TEXT,
    ClassifierSet,
    ClassMatch,
    default_classifiers,
)
from ..interfaces import ContainerExtractor, Extractor
from ..models import AreaConfig
from ._base import Extracted
from ._common import archive_suffix
from .archive import ArchiveExtractor
from .chm import ChmExtractor
from .epub import EpubExtractor
from .fb2 import Fb2Extractor
from .html import HtmlExtractor
from .markdown import MarkdownExtractor
from .mhtml import MhtmlExtractor
from .pdf import PdfExtractor
from .rtf import RtfExtractor
from .text import TextExtractor


class _AreaEntryResolver:
    """Отбор экстракторов для записей контейнера: классификация по байтам.

    Не «карта суффикс → экстрактор», а опрос через ту же схему, что и у
    реестра: содержимое записи (+ имя как хинт) → класс/подтип → отклики.
    """

    def __init__(
        self,
        registry: ExtractorRegistry,
        area: AreaConfig,
        classifiers: ClassifierSet,
    ) -> None:
        self._registry = registry
        self._area = area
        self._classifiers = classifiers

    def extractors_for(self, name: str, data: bytes) -> list[Extractor]:
        extension = archive_suffix(name)
        matches = self._classifiers.classify(data, extension)
        return self._registry.leaf_responders(extension, matches, self._area)


class ExtractorRegistry:
    """Опрос всех экстракторов с учётом классов конфига области.

    Экстракторы (``Extractor``) и контейнеры (``ContainerExtractor``)
    регистрируются явно. При обработке файла реестр (1) классифицирует
    содержимое (``classify``), (2) **опрашивает все экстракторы**, передавая
    расширение и совпадения классов, (3) запускает откликнувшихся по очереди.
    Белый список области (``AreaConfig.formats``) содержит либо **класс**
    (``structure-text`` — весь класс), либо **конкретный формат**
    (``markdown`` — только он; см. ``_is_enabled``).
    """

    def __init__(
        self,
        extractors: tuple[Extractor, ...],
        containers: tuple[ContainerExtractor, ...] = (),
        classifiers: ClassifierSet | None = None,
        unavailable: dict[str, str] | None = None,
    ) -> None:
        if not extractors and not containers:
            raise ValueError("реестр экстракторов пуст")
        self._extractors: tuple[Extractor, ...] = extractors
        self._containers: tuple[ContainerExtractor, ...] = containers
        # Суффиксы, которые **могли бы** обслуживаться, но экстрактор не создан
        # (не установлен пакет): ``.rtf`` → «не установлен striprtf…». Без этой
        # карты пропуск файла выглядел как «формат не обслуживается» — без
        # причины и без подсказки, что доставить.
        self._unavailable: dict[str, str] = dict(unavailable or {})
        self._names: set[str] = set()
        self._classes: set[str] = set()
        all_extractors: tuple[Extractor | ContainerExtractor, ...] = (
            *extractors,
            *containers,
        )
        for extractor in all_extractors:
            if extractor.name in self._names:
                raise ValueError(f"имя экстрактора {extractor.name} занято")
            self._names.add(extractor.name)
            self._classes.add(extractor.format_class)
        self._classifiers = classifiers or default_classifiers()

    def known_names(self) -> frozenset[str]:
        """Доступные в конфиге имена: классы + зарезервированные + форматы.

        Возвращается общее множество имён экстракторов, классов,
        зарезервированных классов и легаси-алиасов, чтобы ``load_config``
        валидировал и классы (``structure-text``), и конкретные форматы
        (``markdown``, ``pdf``).
        """
        return frozenset(self._names | self._classes | RESERVED | set(CLASS_ALIASES))

    def known_classes(self) -> frozenset[str]:
        """Классы этого реестра (без легаси-алиасов)."""
        return frozenset(self._classes | RESERVED)

    def extractor_for(self, name: str) -> Extractor | None:
        """Вернуть экстрактор по его имени (атрибут ``name`` класса).

        Регистр перебирает свои ``_extractors`` и возвращает первого,
        чье ``name`` совпадает. Если ничего не найдено — ``None``.
        """
        for ext in self._extractors:
            if ext.name == name:
                return ext
        return None

    # --- проверка поддержки (без чтения файла) ---

    def enabled_suffixes(self, area: AreaConfig) -> frozenset[str]:
        """Суффиксы, которые область реально обслуживает этим реестром."""
        suffixes: set[str] = set()
        all_extractors: tuple[Extractor | ContainerExtractor, ...] = (
            *self._extractors,
            *self._containers,
        )
        for extractor in all_extractors:
            if self._is_enabled(extractor, area):
                suffixes.update(extractor.suffixes())
        return frozenset(suffixes)

    def supports(self, path: Path, area: AreaConfig) -> bool:
        """Обслуживается ли файл сейчас — по расширению и ``formats`` области."""
        return path.suffix.lower() in self.enabled_suffixes(area)

    def explain_miss(self, path: Path, area: AreaConfig) -> str:
        """Почему файл не обслуживается: причина + что сделать.

        Порядок проверок:
        * экстрактор есть, но не создан (нет пакета) — сказать, какой пакет;
        * формат поддержан, но не разрешён в ``formats`` области — назвать имя;
        * иначе — просто «формат не обслуживается».
        """
        suffix = path.suffix.lower()
        if suffix in self._unavailable:
            return self._unavailable[suffix]
        all_extractors: tuple[Extractor | ContainerExtractor, ...] = (
            *self._extractors,
            *self._containers,
        )
        for extractor in all_extractors:
            if suffix in extractor.suffixes():
                names = sorted(
                    {
                        extractor.name,
                        extractor.format_class,
                        *(CLASS_ALIASES.get(extractor.name, ""),),
                    }
                    - {""}
                )
                return (
                    f"{suffix} не разрешён в formats области {area.path}"
                    f" — добавь {names[0]!r} (или класс {extractor.format_class!r})"
                )
        return "формат не обслуживается"

    # --- опрос ---

    def leaf_responders(
        self,
        extension: str,
        matches: tuple[ClassMatch, ...],
        area: AreaConfig,
    ) -> list[Extractor]:
        """Обычные экстракторы области, откликнувшиеся на (расширение + классы)."""
        return [
            extractor
            for extractor in self._extractors
            if self._is_enabled(extractor, area)
            and extractor.responds(extension, matches)
        ]

    def container_responder(
        self,
        extension: str,
        matches: tuple[ClassMatch, ...],
        area: AreaConfig,
    ) -> ContainerExtractor | None:
        """Контейнер области, откликнувшийся на (расширение + классы)."""
        for container in self._containers:
            if self._is_enabled(container, area) and container.responds(
                extension, matches
            ):
                return container
        return None

    def entry_resolver(self, area: AreaConfig) -> _AreaEntryResolver:
        """Резолвер экстракторов для записей контейнера (по содержимому записи)."""
        return _AreaEntryResolver(self, area, self._classifiers)

    @staticmethod
    def _is_enabled(
        extractor: Extractor | ContainerExtractor, area: AreaConfig
    ) -> bool:
        """Включён ли экстрактор в области.

        ``AreaConfig.formats`` может называть:
        - **класс** (``structure-text``) — тогда работает весь класс
          (md+html+mhtml), различаясь по подтипу/расширению;
        - **конкретный формат** (``markdown``, ``pdf``) — тогда из всего класса
          работает только он (``[markdown]`` не индексирует html).
        """
        names = frozenset(area.formats)
        return extractor.format_class in names or extractor.name in names

    # --- main вход: файл области → части ---

    def extract(self, path: Path, area: AreaConfig) -> list[Extracted] | None:
        """Извлечь файл: классификация по содержимому → опрос → запуск.

        Результат — части (обычный файл — одна часть, контейнер — по части на
        запись); ``None`` — ничего не откликнулось (класс не в конфиге либо
        формат не поддержан).
        """
        extension = path.suffix.lower()
        matches = self._classifiers.classify_path(path)
        container = self.container_responder(extension, matches, area)
        extractors = self.leaf_responders(extension, matches, area)

        all_responders = len(extractors) + (1 if container else 0)
        parts: list[Extracted] = []
        if container is not None:
            resolver = _AreaEntryResolver(self, area, self._classifiers)
            parts.extend(container.extract_all(path, resolver))
        for extractor in extractors:
            result = extractor.extract(path)
            if all_responders > 1:
                # Несколько экстракторов на один файл: имя части — экстрактор,
                # чтобы части не конфликтовали за один источник (см. _part_name).
                result = dataclasses.replace(result, name=result.name or extractor.name)
            parts.append(result)
        if not parts:
            return None
        return parts


# Легаси-алиасы: старые имена экстракторов в конфиге → классы.
CLASS_ALIASES: dict[str, str] = {
    "markdown": STRUCTURE_TEXT,
    "html": STRUCTURE_TEXT,
    "mhtml": STRUCTURE_TEXT,
    "text": TEXT,
    "pdf": PUB_FORMATS,
    "archive": ARCHIVE,
    "fb2": PUB_FORMATS,
    "epub": PUB_FORMATS,
}


def default_registry() -> ExtractorRegistry:
    """Стандартный набор: text + structure-text + pub-formats + archive.

    PDF добавляется, только если установлен extra ``pdf`` (pypdf), RTF — extra
    ``rtf`` (striprtf). FB2/EPUB/CHM — стандартная библиотека. Классы
    ``ocr``/``code``/``science`` зарезервированы в ``known_names``, но
    экстракторов у них нет — файлы таких классов просто не индексируются,
    пока не будут реализованы.
    """
    extractors: list[Extractor] = [
        MarkdownExtractor(),
        TextExtractor(),
        HtmlExtractor(),
        MhtmlExtractor(),
        Fb2Extractor(),
        EpubExtractor(),
        ChmExtractor(),
    ]
    unavailable: dict[str, str] = {}
    for factory, suffixes in ((PdfExtractor, (".pdf",)), (RtfExtractor, (".rtf",))):
        try:
            extractors.append(factory())
        except RuntimeError as exc:
            # Экстрактор не создан: формат есть в природе, но пакета нет.
            # Запоминаем причину, чтобы пропуск файла объяснял, что доставить.
            for suffix in suffixes:
                unavailable[suffix] = str(exc)
    containers: tuple[ContainerExtractor, ...] = (ArchiveExtractor(),)
    return ExtractorRegistry(tuple(extractors), containers, unavailable=unavailable)
