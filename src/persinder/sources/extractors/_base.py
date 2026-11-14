"""База экстракторов: результат извлечения и общая логика отклика.

``Extracted`` — результат извлечения одной единицы (файла или части
контейнера); ``BaseExtractor`` — общая схема отклика ``responds`` для
обычных экстракторов.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..classify import TEXT, ClassMatch


@dataclass(frozen=True)
class Extracted:
    """Результат извлечения одной единицы (файла или части контейнера).

    ``name`` — путь части внутри исходного файла (например записи ZIP:
    ``"dir/inner.md"``); пусто — сам исходный файл. Для нескольких
    экстракторов на одну запись к имени добавляется ``/<extractor>`` — это
    только человекочитаемое имя, **не** элемент пути в архиве.
    ``container_path`` — путь файла-контейнера на диске (``""`` — обычный
    файл); ``container_entries`` — цепочка записей внутри контейнера, однозначно
    задающая часть: ``("folder/second.zip", "folder/some.pdf")``. Вместе они
    дают обратный разбор ``source_path`` без эвристик по строке.
    ``extractor`` — имя экстрактора, который дал этот результат (пишется в БД).
    ``error`` — непустой при проблеме разбора; текст в этом случае пуст.
    """

    text: str
    tags: tuple[str, ...] = ()
    pages: int = 0
    error: str | None = None
    name: str = ""
    extractor: str = ""
    container_path: str = ""
    container_entries: tuple[str, ...] = ()


class BaseExtractor:
    """Общая логика отклика для обычных экстракторов.

    Отклик, если: (1) назван наш класс **и** подтип совпадает/не назван —
    точное попадание; (2) классификатор ничего не назвал **или** назвал лишь
    общий ``text`` (контент неразличим) — тогда расширение выбирает подтип
    внутри класса (например ``.md`` → markdown, ``.mht`` → mhtml). Уверенный
    чужой класс расширением не перекрывается (``.txt`` с телом %PDF- — это
    pdf, а не текст).
    """

    format_class: str = TEXT
    subtypes: frozenset[str] = frozenset()

    def suffixes(self) -> frozenset[str]:
        """Расширения (нижний регистр, с точкой) — фолбэк при неразличимом контенте."""
        raise NotImplementedError

    def responds(self, extension: str, matches: tuple[ClassMatch, ...]) -> bool:
        for match in matches:
            if match.format_class != self.format_class:
                continue
            if not match.subtype or match.subtype in self.subtypes:
                return True
        if not matches or _generic_text_only(matches):
            return extension in self.suffixes()
        return False


def _generic_text_only(matches: tuple[ClassMatch, ...]) -> bool:
    """Все совпадения — лишь общий «просто текст» (без подтипа)."""
    return bool(matches) and all(
        match.format_class == TEXT and match.subtype == "text" for match in matches
    )
